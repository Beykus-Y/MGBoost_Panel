"""PH7-10 manual external-payment admin routes over the proven PH5-09/10
`ManualPaymentStore` backend. No payment logic lives here: price authority is
exclusively the server-side versioned fixed RUB catalog (DL-031/034-040),
every mutation goes through the store (which re-validates account/catalog/
amount/capability server-side), and child convergence after apply is driven
through the existing PH3-08 `run_account_sync_cycle` exactly like the
canonical PH5-05 Stars driver.
"""

from __future__ import annotations

import json
import time
from urllib.parse import parse_qs, urlsplit

from ..admin_read_models import _subscription_summary
from ..http_utils import error_response, json_response
from ..manual_payment import (
    ApplyRequiresManualReview,
    ManualPaymentConflict,
    ManualPaymentError,
)
from ..parent_sync import run_account_sync_cycle
from ..plan_catalog import GB_DECIMAL, RUB_CATALOG_VERSION
from ..security import require_admin_auth
from ..subscription_renewal import compute_new_expiry
from ..wl_package_catalog import PACKAGE_SPECS

from .admin_support import (
    account_or_404,
    bounded_int,
    bounded_str,
    int_or_404,
    read_json_body,
    require_primary_capability,
    service_marzban,
)

_MIN_IDEMPOTENCY_KEY = 16
_MAX_IDEMPOTENCY_KEY = 128

# The store signals lifecycle/immutability denials (DL-039: applied facts are
# immutable, pending-only edits, terminal cancel/review states) with its base
# ManualPaymentError; these map to 409, everything else is a 400.
_IMMUTABLE_MARKERS = (
    "can no longer be edited",
    "is immutable and cannot be cancelled",
    "record is already cancelled",
    "cancelled records are never applicable",
    "resolve the manual review before applying",
)


def _payment_http_status(exc: ManualPaymentError) -> int:
    message = str(exc)
    return 409 if any(marker in message for marker in _IMMUTABLE_MARKERS) else 400


def _capability(handler, db):
    return db.primary_admin_authority.authorize_session(handler._admin_session)


def _record_ref(record) -> dict:
    """Safe operator-facing projection of a manual payment record."""
    record = dict(record)
    return {
        "id": record["id"],
        "public_id": record.get("public_id"),
        "kind": record["kind"],
        "status": record["status"],
        "account_id": record["account_id"],
        "plan_code": record.get("plan_code_snapshot") if "plan_code_snapshot" in record else record.get("plan_code"),
        "duration_days": record.get("duration_days_snapshot") if "duration_days_snapshot" in record else record.get("duration_days"),
        "package_sku": record.get("package_sku_snapshot") if "package_sku_snapshot" in record else record.get("package_sku"),
        "amount_minor": record.get("expected_amount_minor", record.get("amount_minor")),
        "currency": record.get("currency"),
        "payment_method": record.get("payment_method"),
        "external_reference": record.get("external_reference"),
        "comment": record.get("comment") or None,
        "created_at": record.get("created_at"),
        "updated_at": record.get("updated_at"),
        "applied_at": record.get("applied_at") or None,
        "applied_operation": record.get("applied_operation") or None,
        "applied_expiry": record.get("applied_expiry") or None,
        "cancelled_at": record.get("cancelled_at") or None,
        "review_reason": record.get("review_reason") or None,
    }


def _sync_state(db, payment_record_id: int) -> dict | None:
    row = db._conn.execute(
        "SELECT payment_record_id,state,attempts,last_error_class,synced_at FROM "
        "mgboost_manual_payment_sync_jobs WHERE payment_record_id=?",
        (int(payment_record_id),),
    ).fetchone()
    return dict(row) if row else None


# --- catalog + preview -------------------------------------------------------

def handle_manual_payment_catalog(handler):
    """Server-provided products only; the UI can never inject price/plan."""
    if not require_admin_auth(handler):
        return
    db = handler.server.db
    plans = []
    products = db.plan_catalog.active_catalog("RUB")
    for product in products:
        quota_bytes = product["wl_quota_bytes"] or 0
        period_days = product["wl_period_days"] or 30
        plans.append({
            "plan_code": product["plan_code"],
            "display_name": product["display_name"],
            "duration_days": product["duration_days"],
            "amount_minor": product["amount"],
            "currency": "RUB",
            "device_limit_mode": product["device_limit_mode"],
            "device_limit": product["device_limit"],
            "wl_mode": product["wl_mode"],
            "wl_quota_gb": round(quota_bytes / GB_DECIMAL),
            "period_days": period_days,
            "catalog_version": product["catalog_version"],
        })
    packages = []
    for sku, display_name, bytes_count in PACKAGE_SPECS:
        price_row = db.wl_package_catalog.active_price(sku, "RUB")
        if not price_row:
            continue
        packages.append({
            "sku": sku,
            "display_name": display_name,
            "bytes": price_row["bytes"],
            "amount_minor": price_row["amount"],
            "currency": "RUB",
            "catalog_version": price_row["catalog_version"],
        })
    json_response(handler, 200, {
        "channel": "RUB",
        "catalog_version": RUB_CATALOG_VERSION,
        "plans": sorted(plans, key=lambda item: (item["display_name"], item["duration_days"])),
        "packages": packages,
    })


def handle_manual_payment_preview(handler, account_id):
    if not require_admin_auth(handler):
        return
    db = handler.server.db
    account = account_or_404(handler, db, account_id)
    if account is None:
        return
    data = read_json_body(handler)
    if data is None:
        return
    plan_code, plan_error = bounded_str(data, "plan_code", max_len=64, required=False)
    package_sku, package_error = bounded_str(data, "package_sku", max_len=64, required=False)
    duration_days, duration_error = bounded_int(
        data, "duration_days", minimum=1, maximum=3655, required=False)
    for message in (plan_error, package_error, duration_error):
        if message:
            error_response(handler, 400, message)
            return
    if bool(plan_code or duration_days) == bool(package_sku):
        error_response(handler, 400, "Provide either plan_code+duration_days or package_sku")
        return

    subscription = _subscription_summary(db, int(account_id), now=int(time.time()))
    response = {
        "account_id": int(account_id),
        "current_plan_code": subscription and subscription.get("plan_code"),
        "current_display_name": subscription and subscription.get("display_name"),
        "current_expiry": subscription and subscription.get("current_expiry"),
    }
    if package_sku:
        price_row = db.wl_package_catalog.active_price(package_sku, "RUB")
        if not price_row:
            error_response(handler, 404, "Unknown package SKU for the RUB catalog")
            return
        try:
            entitlement = db.entitlements.calculate(account_id=int(account_id))
            eligible = bool(entitlement.get("wl", {}).get("package_eligible"))
            real_mode = entitlement.get("wl", {}).get("real_plan_mode")
        except Exception:
            eligible, real_mode = False, None
        response.update({
            "product_kind": "WL_PACKAGE",
            "package_sku": package_sku,
            "display_name": f"+{round(price_row['bytes'] / GB_DECIMAL)} GB",
            "package_bytes": price_row["bytes"],
            "amount_minor": price_row["amount"],
            "currency": "RUB",
            "catalog_version": price_row["catalog_version"],
            "purchasable": eligible,
            "not_purchasable_reason": None if eligible else (
                "CURRENT_PLAN_NOT_WL" if real_mode != "LIMITED" else "PACKAGE_INELIGIBLE"
            ),
        })
        json_response(handler, 200, response)
        return

    # PLAN_PRODUCT preview: same-plan renewal only until PH5-06 exists.
    if not subscription or not subscription.get("plan_code"):
        response.update({
            "product_kind": "PLAN_PRODUCT", "purchasable": False,
            "not_purchasable_reason": "NO_ACTIVE_SUBSCRIPTION",
        })
        json_response(handler, 200, response)
        return
    catalog_product = None
    for product in db.plan_catalog.active_catalog("RUB"):
        if product["plan_code"] == plan_code \
                and product["duration_days"] == int(duration_days or 0):
            catalog_product = product
            break
    if catalog_product is None:
        error_response(handler, 404, "Unknown plan/duration for the RUB catalog")
        return
    same_plan = catalog_product["plan_code"] == subscription["plan_code"]
    unlimited_admin_grant = subscription.get("status") == "UNLIMITED"
    purchasable = same_plan and not unlimited_admin_grant
    reason = None
    if not same_plan:
        reason = "PLAN_SWITCH_REQUIRES_PH5_06"
    elif unlimited_admin_grant:
        reason = "ADMIN_GRANTED_UNLIMITED_NOT_EXTENDABLE"
    _, expected_new_expiry = compute_new_expiry(
        subscription.get("current_expiry"), catalog_product["duration_days"], now=int(time.time()),
    )
    response.update({
        "product_kind": "PLAN_PRODUCT",
        "plan_code": catalog_product["plan_code"],
        "display_name": catalog_product["display_name"],
        "duration_days": catalog_product["duration_days"],
        "amount_minor": catalog_product["amount"],
        "currency": "RUB",
        "catalog_version": catalog_product["catalog_version"],
        "purchasable": purchasable,
        "not_purchasable_reason": reason,
        # The store/engines own the authoritative anchor at apply time; this
        # is a presentation estimate via the same DL-044 formula.
        "expected_new_expiry_is_estimate": True,
        "expected_new_expiry": expected_new_expiry,
        "wl_effect": {
            "mode": catalog_product["wl_mode"],
            "quota_gb_per_period": round((catalog_product["wl_quota_bytes"] or 0) / GB_DECIMAL),
            "period_days": catalog_product["wl_period_days"] or 30,
        },
    })
    json_response(handler, 200, response)


# --- mutations ---------------------------------------------------------------

def handle_manual_payment_create(handler, account_id):
    if not require_admin_auth(handler):
        return
    db = handler.server.db
    account = account_or_404(handler, db, account_id)
    if account is None:
        return
    actor = require_primary_capability(handler, db)
    if actor is None:
        return
    data = read_json_body(handler)
    if data is None:
        return
    external_reference, ref_error = bounded_str(
        data, "external_reference", min_len=1, max_len=200)
    payment_method, method_error = bounded_str(data, "payment_method", max_len=100)
    comment, comment_error = bounded_str(data, "comment", max_len=500, required=False)
    idempotency_key, key_error = bounded_str(
        data, "idempotency_key", min_len=_MIN_IDEMPOTENCY_KEY, max_len=_MAX_IDEMPOTENCY_KEY)
    amount, amount_error = bounded_int(
        data, "recorded_amount_minor", minimum=1, maximum=10_000_000)
    plan_code, plan_error = bounded_str(data, "plan_code", max_len=64, required=False)
    package_sku, package_error = bounded_str(data, "package_sku", max_len=64, required=False)
    duration_days, duration_error = bounded_int(
        data, "duration_days", minimum=1, maximum=3655, required=False)
    for message in (ref_error, method_error, comment_error, key_error, amount_error,
                    plan_error, package_error, duration_error):
        if message:
            error_response(handler, 400, message)
            return
    if bool(plan_code or duration_days) == bool(package_sku):
        error_response(handler, 400, "Provide either plan_code+duration_days or package_sku")
        return
    kwargs = dict(
        account_id=int(account_id),
        external_reference=external_reference,
        recorded_amount_minor=int(amount),
        payment_method=payment_method,
        idempotency_key=idempotency_key,
    )
    if comment is not None:
        kwargs["comment"] = comment
    if package_sku is not None:
        kwargs["package_sku"] = package_sku
    else:
        kwargs.update(plan_code=plan_code, duration_days=int(duration_days))
    try:
        record = db.manual_payments.create_record(_capability(handler, db), **kwargs)
    except ManualPaymentConflict as exc:
        error_response(handler, 409, str(exc))
        return
    except ManualPaymentError as exc:
        error_response(handler, _payment_http_status(exc), str(exc))
        return
    fresh = db.manual_payments.get_record(record["id"])
    json_response(handler, 200, {"payment": _record_ref(fresh or record)})


def handle_manual_payments_list(handler):
    if not require_admin_auth(handler):
        return
    params = parse_qs(urlsplit(getattr(handler, "path", "")).query)
    status = params.get("status", [None])[0]
    account_raw = params.get("account_id", [None])[0]
    account_id = None
    if account_raw is not None:
        if not str(account_raw).isdigit():
            error_response(handler, 400, "Invalid account filter")
            return
        account_id = int(account_raw)
    db = handler.server.db
    records = db.manual_payments.list_records(status=status, account_id=account_id, limit=200)
    items = []
    labels: dict[int, str] = {}
    conn = db._conn
    for record in records:
        item = _record_ref(record)
        target = item["account_id"]
        if target not in labels:
            alias = conn.execute(
                "SELECT legacy_username FROM mgboost_legacy_account_aliases WHERE account_id=? "
                "ORDER BY CASE alias_role WHEN 'PRIMARY' THEN 0 ELSE 1 END,id LIMIT 1",
                (target,),
            ).fetchone()
            public = conn.execute(
                "SELECT public_id FROM mgboost_accounts WHERE id=?", (target,)
            ).fetchone()
            labels[target] = ((alias and alias["legacy_username"])
                              or (public and public["public_id"]) or f"#{target}")
        item["account_label"] = labels[target]
        sync = _sync_state(db, item["id"])
        item["sync_state"] = sync and sync["state"]
        items.append(item)
    json_response(handler, 200, {"payments": items})


def handle_manual_payment_detail(handler, payment_record_id):
    if not require_admin_auth(handler):
        return
    db = handler.server.db
    rid = int_or_404(handler, payment_record_id, what="Manual payment")
    if rid is None:
        return
    record = db.manual_payments.get_record(rid)
    if record is None:
        error_response(handler, 404, "Manual payment not found")
        return
    application = db.manual_payments.get_application(rid)
    renewal_before = renewal_after = None
    if application:
        mrow = db._conn.execute(
            "SELECT before_json,after_json FROM mgboost_entitlement_mutations "
            "WHERE id=? AND account_id=?",
            (application["entitlement_mutation_id"], record["account_id"]),
        ).fetchone()
        if mrow:
            try:
                before = json.loads(mrow["before_json"])
                after = json.loads(mrow["after_json"])
                renewal_before = before.get("current_expiry")
                renewal_after = after.get("new_expiry")
            except (TypeError, ValueError):
                pass
    json_response(handler, 200, {
        "payment": _record_ref(record),
        "edits": [
            {
                "edit_kind": row.get("edit_kind"), "reason": row.get("reason"),
                "actor_ref": row.get("actor_ref"), "created_at": row.get("created_at"),
            }
            for row in db.manual_payments.edit_history(rid)
        ],
        "application": dict(application) if application else None,
        "renewal_before_expiry": renewal_before,
        "renewal_after_expiry": renewal_after,
        "sync_state": _sync_state(db, rid),
    })


def handle_manual_payment_edit(handler, payment_record_id):
    if not require_admin_auth(handler):
        return
    db = handler.server.db
    rid = int_or_404(handler, payment_record_id, what="Manual payment")
    if rid is None:
        return
    capability = require_primary_capability(handler, db)
    if capability is None:
        return
    data = read_json_body(handler)
    if data is None:
        return
    reason, reason_error = bounded_str(data, "reason", min_len=8, max_len=1000)
    if reason_error:
        error_response(handler, 400, reason_error)
        return
    changes = data.get("changes")
    allowed_fields = {"plan_code", "duration_days", "package_sku", "recorded_amount_minor",
                      "payment_method", "external_reference", "comment"}
    if not isinstance(changes, dict) or not changes:
        error_response(handler, 400, "changes must be a non-empty object")
        return
    if set(changes) - allowed_fields:
        error_response(handler, 400, "changes contain non-editable fields")
        return
    if len(json.dumps(changes)) > 4096:
        error_response(handler, 413, "changes payload too large")
        return
    try:
        record = db.manual_payments.edit_pending_record(capability, rid,
                                                        reason=reason, changes=changes)
    except ManualPaymentConflict as exc:
        error_response(handler, 409, str(exc))
        return
    except ManualPaymentError as exc:
        error_response(handler, _payment_http_status(exc), str(exc))
        return
    fresh = db.manual_payments.get_record(record["id"])
    json_response(handler, 200, {"payment": _record_ref(fresh or record)})


def handle_manual_payment_cancel(handler, payment_record_id):
    if not require_admin_auth(handler):
        return
    db = handler.server.db
    rid = int_or_404(handler, payment_record_id, what="Manual payment")
    if rid is None:
        return
    capability = require_primary_capability(handler, db)
    if capability is None:
        return
    data = read_json_body(handler)
    if data is None:
        return
    reason, reason_error = bounded_str(data, "reason", min_len=8, max_len=1000)
    if reason_error:
        error_response(handler, 400, reason_error)
        return
    try:
        record = db.manual_payments.cancel_record(capability, rid, reason=reason)
    except ManualPaymentConflict as exc:
        error_response(handler, 409, str(exc))
        return
    except ManualPaymentError as exc:
        error_response(handler, _payment_http_status(exc), str(exc))
        return
    fresh = db.manual_payments.get_record(record["id"])
    json_response(handler, 200, {"payment": _record_ref(fresh or record)})


def handle_manual_payment_resolve_review(handler, payment_record_id):
    if not require_admin_auth(handler):
        return
    db = handler.server.db
    rid = int_or_404(handler, payment_record_id, what="Manual payment")
    if rid is None:
        return
    capability = require_primary_capability(handler, db)
    if capability is None:
        return
    data = read_json_body(handler)
    if data is None:
        return
    note, note_error = bounded_str(data, "resolution_note", min_len=3, max_len=300)
    if note_error:
        error_response(handler, 400, note_error)
        return
    try:
        record = db.manual_payments.resolve_manual_review(capability, rid,
                                                          resolution_note=note)
    except ManualPaymentConflict as exc:
        error_response(handler, 409, str(exc))
        return
    except ManualPaymentError as exc:
        error_response(handler, _payment_http_status(exc), str(exc))
        return
    fresh = db.manual_payments.get_record(record["id"])
    json_response(handler, 200, {"payment": _record_ref(fresh or record)})


# --- apply + child-sync driver -----------------------------------------------

def _drive_child_sync_once(db, job: dict) -> str:
    """Mirror src/stars.py::_sync_canonical_purchase_children's state mapping,
    scoped to one applied record's durable hand-off job. Only PH3-08's
    terminal aggregate state proves convergence; everything else stays
    recoverably PENDING."""
    timestamp = int(time.time())
    try:
        result = run_account_sync_cycle(
            db, job["account_id"], sync_fn=service_marzban().sync_child_user_state,
            worker_id="admin-manual-ph5-09", now=timestamp,
        )
        aggregate = result.get("aggregate_state")
        errored = result.get("errored")
        state = (
            "SYNCED" if aggregate == "IN_SYNC" else
            "MANUAL_REVIEW" if aggregate == "MANUAL_REVIEW" or errored else
            "PENDING"
        )
        error_class = None
    except Exception as exc:
        state, error_class = "PENDING", type(exc).__name__
    db.manual_payments.record_sync_result(job["payment_record_id"], state=state,
                                          error_class=error_class)
    return state


def handle_manual_payment_apply(handler, payment_record_id):
    if not require_admin_auth(handler):
        return
    db = handler.server.db
    rid = int_or_404(handler, payment_record_id, what="Manual payment")
    if rid is None:
        return
    capability = require_primary_capability(handler, db)
    if capability is None:
        return
    # An empty-but-present body keeps the CSRF/content-type convention without
    # letting the client send any authoritative field.
    data = read_json_body(handler, max_bytes=1024)
    if data is None:
        return
    try:
        result = db.manual_payments.apply_record(capability, rid)
    except ApplyRequiresManualReview as exc:
        error_response(handler, 409, str(exc))
        return
    except ManualPaymentConflict as exc:
        error_response(handler, 409, str(exc))
        return
    except ManualPaymentError as exc:
        error_response(handler, _payment_http_status(exc), str(exc))
        return
    fresh = db.manual_payments.get_record(rid)
    grant = result.get("grant")
    payload = {
        "payment": _record_ref(fresh or result.get("payment", {})),
        "already_applied": bool(result.get("already_applied")),
        "mutation_id": result.get("mutation_id"),
    }
    entitlement = result.get("entitlement")
    if isinstance(entitlement, dict):
        subscription_result = entitlement.get("subscription", {})
        payload["entitlement_summary"] = {
            "effective_status": subscription_result.get("effective_status"),
            "effective_expiry": subscription_result.get("effective_expiry"),
            "plan_code": entitlement.get("plan", {}).get("code"),
        }
    if isinstance(grant, dict):
        payload["grant"] = {
            "bucket_id": grant.get("id"), "granted_bytes": result.get("granted_bytes"),
            "already_applied": grant.get("already_applied"),
        }
    renewal_after = None
    if payload["payment"].get("kind") == "PLAN_PRODUCT":
        sync_jobs = [job for job in db.manual_payments.pending_sync_jobs()
                     if job["payment_record_id"] == rid]
        if sync_jobs:
            payload["sync_state"] = _drive_child_sync_once(db, sync_jobs[0])
        elif payload["already_applied"]:
            payload["sync_state"] = (_sync_state(db, rid) or {}).get("state")
        application = db.manual_payments.get_application(rid)
        if application:
            mrow = db._conn.execute(
                "SELECT before_json,after_json FROM mgboost_entitlement_mutations "
                "WHERE id=? AND account_id=?", (application["entitlement_mutation_id"],
                                                fresh["account_id"]),
            ).fetchone()
            if mrow:
                try:
                    renewal_before = json.loads(mrow["before_json"]).get("current_expiry")
                    renewal_after = json.loads(mrow["after_json"]).get("new_expiry")
                except (TypeError, ValueError):
                    pass
        payload["renewal_before_expiry"] = renewal_before
        payload["renewal_after_expiry"] = renewal_after or payload["payment"].get("applied_expiry")
    json_response(handler, 200, payload)


def handle_manual_payment_sync(handler, payment_record_id):
    """Operator-driven retry for a PENDING/MANUAL_REVIEW child-sync job."""
    if not require_admin_auth(handler):
        return
    db = handler.server.db
    rid = int_or_404(handler, payment_record_id, what="Manual payment")
    if rid is None:
        return
    # Drives remote convergence, so it needs the primary-admin boundary too.
    if require_primary_capability(handler, db) is None:
        return
    record = db.manual_payments.get_record(rid)
    if record is None:
        error_response(handler, 404, "Manual payment not found")
        return
    jobs = [job for job in db.manual_payments.pending_sync_jobs()
            if job["payment_record_id"] == rid]
    current = _sync_state(db, rid)
    if not jobs:
        json_response(handler, 200, {
            "sync_state": current and current.get("state"),
            "driven": False, "message": "no pending child-sync job for this record",
        })
        return
    state = _drive_child_sync_once(db, jobs[0])
    json_response(handler, 200, {"sync_state": state, "driven": True})
