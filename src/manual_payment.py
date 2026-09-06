"""PH5-09/PH5-10 manual external-payment record and entitlement application.

Dormant library, matching the project's established
"build the durable contract before its consumer UI exists" discipline: no
admin route/UI/bot wiring is added here (the future admin manual-payment
mutation wave is a separate, explicitly-unauthorized step).

Scope follows `ROADMAP.md` PH5-09/PH5-10 and DL-029..040 exactly:

* The payer pays the primary MGBoost admin outside Telegram Stars (first
  rollout RUB-only) and the *primary* MGBoost admin records and applies the
  paid product to an existing parent account.  There is no reseller tenant,
  balance, wholesale/margin or reseller identity anywhere in this module.
* Account source stays whatever it already is; payment channel is
  ``EXTERNAL_PAYMENT``; the applying entitlement mutation source is
  ``MANUAL_PAYMENT`` (existing PH3-09 provenance vocabulary).
* The only price authority is the existing versioned fixed RUB catalog
  (PH5-01 plan prices / PH5-03 package prices).  Arbitrary prices, days or
  GB are structurally impossible: every record pins exact catalog/plan/
  duration/product rows at creation and re-validates those pinned rows --
  never current prices -- before anything applies.  A pinned, later-retired
  catalog version remains the contractual price for its in-flight record.
* A pending record may be corrected until it is applied; every correction
  appends a bounded before/after audit row.  An applied (or cancelled)
  record is immutable at the SQLite-trigger level; corrections to applied
  facts would require a compensating-operation contract that does not exist
  yet, so none is provided here (no hidden rewrite path).
* Application reuses -- never duplicates -- the established engines:
  PH5-02 ``apply_same_plan_purchase`` for same-plan renewal of the SAME
  parent account (DL-044 formula), PH5-03 ``grant_paid_package`` for
  packages, PH5-04 ``calculate`` as the independent proof that the result
  matches the committed state, and PH3-08's outbox
  (``run_account_sync_cycle``) for child-expiry convergence.  Package grants
  additionally produce their canonical ``mgboost_payment_records`` row via
  the existing PH3-09 writer because that is what the deployed grant engine
  requires; plan renewals intentionally follow the PH5-05 precedent where
  the evidence chain lives in this module's own tables.

Every idempotency/replay boundary is durable, not process-local: unique
``idempotency_key_hash``, unique ``external_reference``, the renewal/grant
engines' own mutation-keyed replay, a UNIQUE application link per record and
SQLite writer serialization via ``BEGIN IMMEDIATE``.  The RLock only orders
in-process callers; it is never the sole correctness boundary.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import time

from .entitlement_engine import calculate_effective_entitlement
from .subscription_renewal import (
    PlanMismatch, RenewalError, UnlimitedSubscriptionConflict, UnknownPlan,
)
from .wl_packages import (
    PackageEligibilityError, PackageIdempotencyConflict, PackagePaymentError,
    PackageSalesNotEnabledError, WLPackageError, assert_wl_package_sales_enabled,
)


CURRENCY = "RUB"
PAYMENT_CHANNEL = "EXTERNAL_PAYMENT"
MUTATION_SOURCE = "MANUAL_PAYMENT"

_RECORD_KEY_SCOPE = "manual-payment-v1\0"

RECORD_STATUSES = ("PENDING", "APPLIED", "CANCELLED", "MANUAL_REVIEW")


class ManualPaymentError(ValueError):
    pass


class ManualPaymentConflict(ManualPaymentError):
    pass


class ApplyRequiresManualReview(ManualPaymentError):
    pass


def _canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _key_hash(key: str) -> str:
    if not isinstance(key, str) or not 16 <= len(key) <= 512:
        raise ManualPaymentError("idempotency key length is invalid")
    return hashlib.sha256((_RECORD_KEY_SCOPE + key).encode("utf-8")).hexdigest()


def _request_hash(payload: dict) -> str:
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def _clean(value, *, field: str, max_len: int, required: bool = True, min_len: int = 1):
    if value is None:
        if required:
            raise ManualPaymentError(f"{field} is required")
        return None
    if not isinstance(value, str):
        raise ManualPaymentError(f"{field} must be a string")
    value = value.strip()
    if len(value) < min_len:
        raise ManualPaymentError(f"{field} is too short")
    if len(value) > max_len:
        raise ManualPaymentError(f"{field} is too long")
    return value


def _amount(value, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ManualPaymentError(f"{field} must be a positive integer")
    return int(value)


class ManualPaymentStore:
    def __init__(self, connection: sqlite3.Connection, lock, accounts, plan_catalog,
                 subscription_renewal, provenance, wl_packages, wl_package_catalog,
                 primary_admin_authority):
        self._conn = connection
        self._lock = lock
        self._accounts = accounts
        self._plan_catalog = plan_catalog
        self._subscription_renewal = subscription_renewal
        self._provenance = provenance
        self._wl_packages = wl_packages
        self._wl_package_catalog = wl_package_catalog
        self._authority = primary_admin_authority
        self._database = None

    def bind_database(self, db) -> None:
        self._database = db

    # --- create ---------------------------------------------------------------

    def create_record(
        self, capability, *, account_id: int, external_reference: str,
        recorded_amount_minor: int, payment_method: str,
        plan_code: str | None = None, duration_days: int | None = None,
        package_sku: str | None = None, comment: str | None = None,
        idempotency_key: str, now: int | None = None,
    ) -> dict:
        """Record one manually confirmed RUB payment as a PENDING lifecycle
        record with pinned catalog snapshots.  Retrying with the exact same
        arguments returns the existing record; any argument difference under
        the same idempotency key is a hard conflict."""
        timestamp = int(time.time()) if now is None else int(now)
        actor_ref = self._authority.require(capability)
        reference = _clean(external_reference, field="external_reference", max_len=512)
        method = _clean(payment_method, field="payment_method", max_len=64)
        note = _clean(comment, field="comment", max_len=1000, required=False)
        amount = _amount(recorded_amount_minor, field="recorded_amount_minor")
        explicit_product = sum(1 for v in (plan_code, package_sku) if v is not None)
        if explicit_product != 1:
            raise ManualPaymentError("exactly one target product kind must be specified")
        if package_sku is None:
            if not isinstance(plan_code, str) or not plan_code.strip():
                raise ManualPaymentError("plan_code must be a non-empty string")
            if isinstance(duration_days, bool) or not isinstance(duration_days, int) \
                    or duration_days <= 0:
                raise ManualPaymentError("duration_days must be a positive integer")
            kind = "PLAN_PRODUCT"
            request_payload = {
                "account_id": int(account_id), "kind": kind,
                "plan_code": plan_code.strip(), "duration_days": int(duration_days),
            }
        else:
            if duration_days is not None:
                raise ManualPaymentError("package records take no duration_days")
            try:
                assert_wl_package_sales_enabled()
            except PackageSalesNotEnabledError as exc:
                raise ManualPaymentError(str(exc)) from exc
            kind = "WL_PACKAGE"
            request_payload = {
                "account_id": int(account_id), "kind": kind,
                "package_sku": package_sku.strip(),
            }
        request_payload.update({
            "external_reference": reference, "recorded_amount_minor": amount,
            "payment_method": method, "comment": note,
        })
        key_hash = _key_hash(idempotency_key)
        request_hash = _request_hash(request_payload)
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                prior = self._conn.execute(
                    "SELECT * FROM mgboost_manual_payment_records WHERE idempotency_key_hash=?",
                    (key_hash,),
                ).fetchone()
                if prior:
                    if prior["request_hash"] != request_hash:
                        raise ManualPaymentConflict(
                            "idempotency key reused for another manual payment"
                        )
                    self._conn.commit()
                    return dict(prior)
                self._require_live_account_locked(int(account_id))
                duplicate = self._conn.execute(
                    "SELECT 1 FROM mgboost_manual_payment_records WHERE external_reference=?",
                    (reference,),
                ).fetchone()
                if duplicate:
                    raise ManualPaymentConflict(
                        "another manual payment record already uses this external reference"
                    )
                snapshots = (
                    self._resolve_plan_product_locked(plan_code.strip(), int(duration_days))
                    if kind == "PLAN_PRODUCT"
                    else self._resolve_package_product_locked(package_sku)
                )
                if snapshots["amount"] != amount:
                    raise ManualPaymentError(
                        "recorded amount disagrees with the fixed RUB catalog price"
                    )
                columns = {
                    "public_id": "mpay_" + secrets.token_urlsafe(18),
                    "kind": kind, "status": "PENDING",
                    "account_id": int(account_id),
                    "catalog_version_id": snapshots["catalog_version_id"],
                    "catalog_version_snapshot": snapshots["catalog_version"],
                    "expected_amount_minor": snapshots["amount"],
                    "recorded_amount_minor": amount,
                    "currency": CURRENCY, "payment_method": method,
                    "external_reference": reference, "comment": note,
                    "actor_type": "PRIMARY_ADMIN", "actor_ref": actor_ref,
                    "idempotency_key_hash": key_hash, "request_hash": request_hash,
                    "created_at": timestamp, "updated_at": timestamp,
                }
                if kind == "PLAN_PRODUCT":
                    columns.update({
                        "plan_version_id": snapshots["plan_version_id"],
                        "duration_id": snapshots["duration_id"],
                        "plan_price_id": snapshots["price_id"],
                        "plan_code_snapshot": snapshots["plan_code"],
                        "plan_version_snapshot": snapshots["plan_version"],
                        "duration_days_snapshot": snapshots["duration_days"],
                    })
                else:
                    columns.update({
                        "package_price_id": snapshots["price_id"],
                        "package_product_id": snapshots["product_id"],
                        "package_sku_snapshot": snapshots["sku"],
                        "package_product_version_snapshot": snapshots["product_version"],
                        "package_bytes_snapshot": snapshots["bytes"],
                    })
                placeholders = ",".join("?" for _ in columns)
                cursor = self._conn.execute(
                    f"INSERT INTO mgboost_manual_payment_records ({','.join(columns)}) "
                    f"VALUES ({placeholders})",
                    tuple(columns.values()),
                )
                created = self._conn.execute(
                    "SELECT * FROM mgboost_manual_payment_records WHERE id=?",
                    (cursor.lastrowid,),
                ).fetchone()
                self._conn.commit()
                return dict(created)
            except sqlite3.IntegrityError as exc:
                self._conn.rollback()
                raise ManualPaymentConflict(
                    "duplicate manual payment identity or reference"
                ) from exc
            except Exception:
                self._conn.rollback()
                raise

    # --- pending lifecycle ----------------------------------------------------

    def edit_pending_record(self, capability, payment_record_id: int, *, reason: str,
                            changes: dict, now: int | None = None) -> dict:
        """Correct a PENDING record before apply (DL-039).  Allowed fields:
        plan product (plan_code+duration_days), package_sku,
        recorded_amount_minor, payment_method, external_reference, comment.
        Every change re-pins and re-validates the full catalog snapshot and
        appends one bounded before/after audit row."""
        timestamp = int(time.time()) if now is None else int(now)
        actor_ref = self._authority.require(capability)
        audit_reason = _clean(reason, field="reason", max_len=1000, min_len=8)
        if not isinstance(changes, dict) or not changes:
            raise ManualPaymentError("at least one change is required")
        allowed = {
            "plan_code", "duration_days", "package_sku", "recorded_amount_minor",
            "payment_method", "external_reference", "comment",
        }
        unknown = set(changes) - allowed
        if unknown:
            raise ManualPaymentError(f"unsupported editable fields: {sorted(unknown)}")
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                row = self._require_record_locked(payment_record_id)
                self._reject_confirmed_transition_payment_locked(row["id"])
                if row["status"] != "PENDING":
                    raise ManualPaymentError(
                        f"a {row['status']} record can no longer be edited "
                        "(applied facts are immutable; reviewed records must be "
                        "resolved explicitly first)"
                    )
                swap_plan = ("plan_code" in changes) or ("duration_days" in changes)
                swap_package = "package_sku" in changes
                if swap_plan and swap_package:
                    raise ManualPaymentError("cannot switch between product kinds by editing")
                if row["kind"] == "WL_PACKAGE":
                    if swap_plan:
                        raise ManualPaymentError(
                            "a package record cannot become a plan product"
                        )
                elif swap_package:
                    raise ManualPaymentError(
                        "a plan record cannot become a package product"
                    )
                updates: dict = {}
                if swap_plan:
                    new_plan = changes.get("plan_code", row["plan_code_snapshot"])
                    new_days = changes.get("duration_days", row["duration_days_snapshot"])
                    snapshots = self._resolve_plan_product_locked(str(new_plan), int(new_days))
                    updates.update({
                        "plan_version_id": snapshots["plan_version_id"],
                        "duration_id": snapshots["duration_id"],
                        "plan_price_id": snapshots["price_id"],
                        "catalog_version_id": snapshots["catalog_version_id"],
                        "catalog_version_snapshot": snapshots["catalog_version"],
                        "plan_code_snapshot": snapshots["plan_code"],
                        "plan_version_snapshot": snapshots["plan_version"],
                        "duration_days_snapshot": snapshots["duration_days"],
                        "expected_amount_minor": snapshots["amount"],
                    })
                if swap_package:
                    snapshots = self._resolve_package_product_locked(changes["package_sku"])
                    updates.update({
                        "package_price_id": snapshots["price_id"],
                        "package_product_id": snapshots["product_id"],
                        "catalog_version_id": snapshots["catalog_version_id"],
                        "catalog_version_snapshot": snapshots["catalog_version"],
                        "package_sku_snapshot": snapshots["sku"],
                        "package_product_version_snapshot": snapshots["product_version"],
                        "package_bytes_snapshot": snapshots["bytes"],
                        "expected_amount_minor": snapshots["amount"],
                    })
                if "recorded_amount_minor" in changes:
                    updates["recorded_amount_minor"] = _amount(
                        changes["recorded_amount_minor"], field="recorded_amount_minor"
                    )
                if "payment_method" in changes:
                    updates["payment_method"] = _clean(
                        changes["payment_method"], field="payment_method", max_len=64
                    )
                if "external_reference" in changes:
                    new_reference = _clean(
                        changes["external_reference"], field="external_reference", max_len=512
                    )
                    clash = self._conn.execute(
                        "SELECT 1 FROM mgboost_manual_payment_records "
                        "WHERE external_reference=? AND id!=?",
                        (new_reference, int(payment_record_id)),
                    ).fetchone()
                    if clash:
                        raise ManualPaymentConflict(
                            "another manual payment record already uses this external reference"
                        )
                    updates["external_reference"] = new_reference
                if "comment" in changes:
                    updates["comment"] = _clean(
                        changes["comment"], field="comment", max_len=1000, required=False
                    )
                expected_after = updates.get("expected_amount_minor", row["expected_amount_minor"])
                recorded_after = updates.get(
                    "recorded_amount_minor", row["recorded_amount_minor"]
                )
                if recorded_after != expected_after:
                    raise ManualPaymentError(
                        "edited amount disagrees with the fixed RUB catalog price"
                    )
                assignments = ",".join(f"{name}=?" for name in updates)
                self._conn.execute(
                    f"UPDATE mgboost_manual_payment_records SET {assignments},updated_at=? "
                    "WHERE id=? AND status='PENDING'",
                    (*updates.values(), timestamp, int(payment_record_id)),
                )
                fresh = self._get_record_row(payment_record_id)
                after_view = self._editable_view_locked(fresh)
                if after_view == self._editable_view_locked(row):
                    raise ManualPaymentError("edit changes nothing")
                self._append_edit_locked(
                    payment_record_id=int(payment_record_id), account_id=row["account_id"],
                    edit_kind="FIELD_EDIT", reason=audit_reason,
                    before_json=_canonical(self._editable_view_locked(row)),
                    after_json=_canonical(after_view),
                    actor_ref=actor_ref, now=timestamp,
                )
                self._conn.commit()
                return dict(fresh)
            except sqlite3.IntegrityError as exc:
                self._conn.rollback()
                raise ManualPaymentConflict("duplicate manual payment identity") from exc
            except Exception:
                self._conn.rollback()
                raise

    def resolve_manual_review(self, capability, payment_record_id: int, *,
                              resolution_note: str, now: int | None = None) -> dict:
        """Explicitly return a MANUAL_REVIEW record to PENDING after operator
        resolution.  The review reason stays on the edits trail forever."""
        timestamp = int(time.time()) if now is None else int(now)
        actor_ref = self._authority.require(capability)
        note = _clean(resolution_note, field="resolution_note", max_len=1000, min_len=8)
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                row = self._require_record_locked(payment_record_id)
                self._reject_confirmed_transition_payment_locked(row["id"])
                if row["status"] != "MANUAL_REVIEW":
                    raise ManualPaymentError(
                        f"a {row['status']} record is not in manual review"
                    )
                before = {
                    "status": row["status"], "review_reason": row["review_reason"],
                    "review_at": row["review_at"],
                }
                self._conn.execute(
                    "UPDATE mgboost_manual_payment_records SET status='PENDING',"
                    "review_reason=NULL,review_at=NULL,updated_at=? "
                    "WHERE id=? AND status='MANUAL_REVIEW'",
                    (timestamp, int(payment_record_id)),
                )
                fresh = self._get_record_row(payment_record_id)
                self._append_edit_locked(
                    payment_record_id=int(payment_record_id), account_id=row["account_id"],
                    edit_kind="RESOLVE_REVIEW", reason=note,
                    before_json=_canonical(before),
                    after_json=_canonical({"status": "PENDING"}),
                    actor_ref=actor_ref, now=timestamp,
                )
                self._conn.commit()
                return dict(fresh)
            except Exception:
                self._conn.rollback()
                raise

    def cancel_record(self, capability, payment_record_id: int, *, reason: str,
                      now: int | None = None) -> dict:
        """Terminal cancellation of an unapplied record.  Applied records are
        historical money facts: they are never cancellable here."""
        timestamp = int(time.time()) if now is None else int(now)
        actor_ref = self._authority.require(capability)
        cancel_reason = _clean(reason, field="reason", max_len=1000, min_len=8)
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                row = self._require_record_locked(payment_record_id)
                self._reject_confirmed_transition_payment_locked(row["id"])
                if row["status"] == "APPLIED":
                    raise ManualPaymentError(
                        "an applied manual payment is immutable and cannot be cancelled"
                    )
                if row["status"] == "CANCELLED":
                    raise ManualPaymentError("record is already cancelled")
                self._conn.execute(
                    "UPDATE mgboost_manual_payment_records SET status='CANCELLED',"
                    "cancelled_at=?,cancel_reason=?,updated_at=? "
                    "WHERE id=? AND status!='APPLIED'",
                    (timestamp, cancel_reason, timestamp, int(payment_record_id)),
                )
                self._append_edit_locked(
                    payment_record_id=int(payment_record_id), account_id=row["account_id"],
                    edit_kind="CANCEL", reason=cancel_reason,
                    before_json=_canonical({"status": row["status"]}),
                    after_json=_canonical({"status": "CANCELLED"}),
                    actor_ref=actor_ref, now=timestamp,
                )
                fresh = self._get_record_row(payment_record_id)
                self._conn.commit()
                return dict(fresh)
            except Exception:
                self._conn.rollback()
                raise

    # --- apply (PH5-09 core, PH5-10 renewal path) -------------------------------

    def apply_record(self, capability, payment_record_id: int, *,
                     now: int | None = None) -> dict:
        """Apply one unapplied record exactly once through the established
        engines.  Unsolvable divergences land in MANUAL_REVIEW instead of
        being guessed; retries after any crash converge through the renewal/
        grant engines' own durable idempotency keys."""
        timestamp = int(time.time()) if now is None else int(now)
        actor_ref = self._authority.require(capability)
        with self._lock:
            row = dict(self._require_record_locked(payment_record_id))
            self._reject_confirmed_transition_payment_locked(row["id"])
            if row["status"] == "CANCELLED":
                raise ManualPaymentError("cancelled records are never applicable")
            if row["status"] == "MANUAL_REVIEW":
                raise ApplyRequiresManualReview(
                    "resolve the manual review before applying this record"
                )
            if row["kind"] == "PLAN_PRODUCT":
                self._validate_plan_snapshot_locked(row)
            else:
                self._validate_package_snapshot_locked(row)
        if row["kind"] == "PLAN_PRODUCT":
            return self._apply_plan_locked(row, actor_ref=actor_ref, now=timestamp)
        return self._apply_package_locked(row, actor_ref=actor_ref, now=timestamp)

    def _apply_plan_locked(self, row: dict, *, actor_ref: str, now: int) -> dict:
        # Keep the PH5-02 commit and its immediate PH5-04 proof inside one
        # process-local critical section.  SQLite already serializes writers
        # cross-process; the idempotency keys make every crash/replay converge
        # regardless of ordering.
        with self._lock:
            already_applied_before = row["status"] == "APPLIED"
            try:
                renewal = self._subscription_renewal.apply_same_plan_purchase(
                    account_id=row["account_id"], plan_code=row["plan_code_snapshot"],
                    duration_days=row["duration_days_snapshot"],
                    payment_channel=PAYMENT_CHANNEL, mutation_source=MUTATION_SOURCE,
                    actor_type="PRIMARY_ADMIN", actor_ref=actor_ref,
                    external_reference=row["external_reference"],
                    reason=f"manual external payment {row['public_id']}",
                    idempotency_key=f"ph5-09-manual-payment-v1:{row['id']:012d}",
                    now=now,
                )
            except (
                PlanMismatch, UnlimitedSubscriptionConflict, UnknownPlan, RenewalError,
            ) as exc:
                self._mark_review_locked(
                    row["id"], f"apply_state_mismatch:{type(exc).__name__}", now
                )
                raise ApplyRequiresManualReview(
                    "manual payment requires review against current subscription state"
                ) from exc
            already_applied = already_applied_before or renewal["already_applied"]
            entitlement = calculate_effective_entitlement(
                self._database, account_id=row["account_id"], now=now
            )
            # A replay may occur after another independently paid payment has
            # legitimately extended the same subscription further: the replayed
            # mutation's immutable expiry is then a lower historical point while
            # PH5-04 correctly reports the later current expiry.  A fresh apply
            # must match exactly; a replay must retain the paid plan and never
            # observe an expiry below its own committed entitlement.
            expiry_matches = (
                entitlement["subscription"]["effective_expiry"] == renewal["new_expiry"]
                if not already_applied
                else entitlement["subscription"]["effective_expiry"] >= renewal["new_expiry"]
            )
            if entitlement["plan"]["code"] != row["plan_code_snapshot"] or not expiry_matches:
                raise RuntimeError(
                    "PH5-04 entitlement result disagrees with applied manual renewal"
                )
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                current = self._get_record_row(row["id"])
                operation_row = self._conn.execute(
                    "SELECT operation FROM mgboost_entitlement_mutations WHERE id=? AND account_id=?",
                    (renewal["mutation_id"], row["account_id"]),
                ).fetchone()
                if operation_row is None:
                    raise RuntimeError("applied renewal mutation is missing")
                operation = operation_row["operation"]
                existing = self._conn.execute(
                    "SELECT * FROM mgboost_manual_payment_applications WHERE payment_record_id=?",
                    (row["id"],),
                ).fetchone()
                if current["status"] == "PENDING":
                    if existing is None:
                        self._conn.execute(
                            "INSERT INTO mgboost_manual_payment_applications "
                            "(payment_record_id,account_id,entitlement_mutation_id,"
                            "applied_operation,applied_expiry,related_grant_id,"
                            "entitlement_snapshot_json,created_at) VALUES (?,?,?,?,?,NULL,?,?)",
                            (row["id"], row["account_id"], renewal["mutation_id"],
                             operation, renewal["new_expiry"], _canonical(entitlement), now),
                        )
                    self._conn.execute(
                        "INSERT OR IGNORE INTO mgboost_manual_payment_sync_jobs "
                        "(payment_record_id,account_id,entitlement_mutation_id,created_at,updated_at) "
                        "VALUES (?,?,?,?,?)",
                        (row["id"], row["account_id"], renewal["mutation_id"], now, now),
                    )
                    updated = self._conn.execute(
                        "UPDATE mgboost_manual_payment_records SET status='APPLIED',applied_at=?,"
                        "entitlement_mutation_id=?,applied_operation=?,applied_expiry=?,updated_at=? "
                        "WHERE id=? AND status='PENDING'",
                        (now, renewal["mutation_id"], operation, renewal["new_expiry"],
                         now, row["id"]),
                    )
                    if updated.rowcount != 1:
                        raise RuntimeError("manual payment record status transition failed")
                else:
                    # A prior/concurrent attempt completed this exact apply.
                    # Applied records are immutable, so nothing may be
                    # rewritten -- only verified against the durable fact.
                    if (
                        current["status"] != "APPLIED"
                        or existing is None
                        or existing["entitlement_mutation_id"] != renewal["mutation_id"]
                        or existing["applied_expiry"] != renewal["new_expiry"]
                        or current["entitlement_mutation_id"] != renewal["mutation_id"]
                    ):
                        raise RuntimeError("durable manual payment application disagrees")
                self._conn.commit()
                final = dict(self._get_record_row(row["id"]))
                return {
                    **renewal,
                    "already_applied": already_applied,
                    "payment": self.public_view(final),
                    "entitlement": entitlement,
                }
            except Exception:
                self._conn.rollback()
                raise

    def _apply_package_locked(self, row: dict, *, actor_ref: str, now: int) -> dict:
        with self._lock:
            already_applied_before = row["status"] == "APPLIED"
            # The deployed PH5-03 grant engine demands its canonical
            # CONFIRMED EXTERNAL_PAYMENT row in mgboost_payment_records;
            # recording it is part of applying this record, keyed per record.
            payment = self._provenance.record_payment(
                row["account_id"], payment_channel=PAYMENT_CHANNEL,
                record_status="CONFIRMED", amount_minor=row["expected_amount_minor"],
                currency=CURRENCY, payment_method=row["payment_method"],
                external_reference=row["external_reference"],
                actor_type="PRIMARY_ADMIN", actor_ref=actor_ref,
                evidence={"manual_payment_record_id": int(row["id"]),
                          "kind": "WL_PACKAGE"},
                idempotency_key=f"ph5-09-manual-payment-paymentrow-v1:{row['id']:012d}",
                now=now,
            )
            try:
                grant = self._wl_packages.grant_paid_package(
                    account_id=row["account_id"], sku=row["package_sku_snapshot"],
                    price_channel="RUB", payment_id=payment["id"],
                    idempotency_key=f"ph5-09-manual-package-grant-v1:{row['id']:012d}",
                    catalog_version=row["catalog_version_snapshot"],
                    actor_type="PRIMARY_ADMIN", actor_ref=actor_ref, now=now,
                )
            except (
                PackageEligibilityError, PackagePaymentError, PackageIdempotencyConflict,
                WLPackageError,
            ) as exc:
                self._mark_review_locked(
                    row["id"], f"package_grant_conflict:{type(exc).__name__}", now
                )
                raise ApplyRequiresManualReview(
                    "manual package purchase conflicts with WL eligibility/payment state"
                ) from exc
            already_applied = already_applied_before or grant["already_applied"]
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                current = self._get_record_row(row["id"])
                existing = self._conn.execute(
                    "SELECT * FROM mgboost_manual_payment_applications WHERE payment_record_id=?",
                    (row["id"],),
                ).fetchone()
                if current["status"] == "PENDING":
                    if existing is None:
                        grant_snapshot = {
                            "grant_id": int(grant["id"]), "sku": row["package_sku_snapshot"],
                            "granted_bytes": int(row["package_bytes_snapshot"]),
                            "catalog_version": row["catalog_version_snapshot"],
                            "payment_id": int(payment["id"]),
                        }
                        self._conn.execute(
                            "INSERT INTO mgboost_manual_payment_applications "
                            "(payment_record_id,account_id,entitlement_mutation_id,"
                            "applied_operation,applied_expiry,related_grant_id,"
                            "entitlement_snapshot_json,created_at) "
                            "VALUES (?,?,?,'PACKAGE_GRANT',NULL,?,?,?)",
                            (row["id"], row["account_id"], grant["grant_mutation_id"],
                             int(grant["id"]), _canonical(grant_snapshot), now),
                        )
                    updated = self._conn.execute(
                        "UPDATE mgboost_manual_payment_records SET status='APPLIED',applied_at=?,"
                        "entitlement_mutation_id=?,applied_operation='PACKAGE_GRANT',"
                        "applied_expiry=NULL,updated_at=? "
                        "WHERE id=? AND status='PENDING'",
                        (now, grant["grant_mutation_id"], now, row["id"]),
                    )
                    if updated.rowcount != 1:
                        raise RuntimeError("manual payment record status transition failed")
                else:
                    # A prior/concurrent attempt completed this exact apply;
                    # applied records are immutable, so only verify.
                    if (
                        current["status"] != "APPLIED"
                        or existing is None
                        or existing["entitlement_mutation_id"] != grant["grant_mutation_id"]
                        or existing["related_grant_id"] != int(grant["id"])
                        or current["entitlement_mutation_id"] != grant["grant_mutation_id"]
                    ):
                        raise RuntimeError("durable manual payment application disagrees")
                self._conn.commit()
                final = dict(self._get_record_row(row["id"]))
                return {
                    **{k: grant[k] for k in ("id", "status", "granted_bytes")},
                    "already_applied": already_applied,
                    "grant": grant,
                    "payment": self.public_view(final),
                }
            except Exception:
                self._conn.rollback()
                raise

    # --- reads / sync hand-off -------------------------------------------------

    def get_record(self, payment_record_id: int) -> dict | None:
        with self._lock:
            row = self._get_record_row(payment_record_id)
        return dict(row) if row else None

    def find_by_public_id(self, public_id: str) -> dict | None:
        if not isinstance(public_id, str) or not public_id.startswith("mpay_"):
            raise ManualPaymentError("invalid manual payment public id")
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM mgboost_manual_payment_records WHERE public_id=?",
                (public_id,),
            ).fetchone()
        return dict(row) if row else None

    def list_records(self, *, status: str | None = None, account_id: int | None = None,
                     limit: int = 200) -> list[dict]:
        if status is not None and status not in RECORD_STATUSES:
            raise ManualPaymentError("invalid status filter")
        limit = _amount(limit, field="limit")
        if limit > 500:
            raise ManualPaymentError("limit is out of bounds")
        sql = "SELECT * FROM mgboost_manual_payment_records WHERE 1=1"
        params: list = []
        if status is not None:
            sql += " AND status=?"
            params.append(status)
        if account_id is not None:
            sql += " AND account_id=?"
            params.append(int(account_id))
        sql += " ORDER BY created_at DESC,id DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def pending_apply_records(self) -> list[dict]:
        return self.list_records(status="PENDING")

    def edit_history(self, payment_record_id: int) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM mgboost_manual_payment_edits WHERE payment_record_id=? "
                "ORDER BY created_at ASC,id ASC",
                (int(payment_record_id),),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_application(self, payment_record_id: int) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM mgboost_manual_payment_applications WHERE payment_record_id=?",
                (int(payment_record_id),),
            ).fetchone()
        return dict(row) if row else None

    def pending_sync_jobs(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM mgboost_manual_payment_sync_jobs WHERE state='PENDING' "
                "ORDER BY payment_record_id"
            ).fetchall()
        return [dict(row) for row in rows]

    def record_sync_result(self, payment_record_id: int, *, state: str,
                           error_class: str | None = None,
                           now: int | None = None) -> None:
        """Mirror of the canonical PH5-05 hand-off bookkeeping: only terminal
        aggregate convergence may mark SYNCED; backoff/partial keeps the job
        recoverable."""
        if state not in {"PENDING", "SYNCED", "MANUAL_REVIEW"}:
            raise ValueError("invalid manual payment child sync state")
        timestamp = int(time.time()) if now is None else int(now)
        with self._lock:
            self._conn.execute(
                "UPDATE mgboost_manual_payment_sync_jobs SET state=?,attempts=attempts+1,"
                "last_error_class=?,last_attempt_at=?,synced_at=?,updated_at=? "
                "WHERE payment_record_id=? AND state='PENDING'",
                (state, error_class, timestamp, timestamp if state == "SYNCED" else None,
                 timestamp, int(payment_record_id)),
            )
            self._conn.commit()

    # --- presentation ----------------------------------------------------------

    def public_view(self, row: dict) -> dict:
        """Bounded projection for future output surfaces.  No UUID, HWID,
        subscription bearer or admin credential ever passes through this
        module; nothing needs stripping today, but the boundary lives in one
        place."""
        return {
            "public_id": row["public_id"], "kind": row["kind"], "status": row["status"],
            "account_id": int(row["account_id"]),
            "currency": row["currency"],
            "expected_amount_minor": int(row["expected_amount_minor"]),
            "recorded_amount_minor": int(row["recorded_amount_minor"]),
            "payment_method": row["payment_method"],
            "external_reference": row["external_reference"],
            "created_at": int(row["created_at"]),
            "applied_expiry": row["applied_expiry"],
        }

    # --- internal helpers -------------------------------------------------------

    def _get_record_row(self, payment_record_id: int):
        return self._conn.execute(
            "SELECT * FROM mgboost_manual_payment_records WHERE id=?",
            (int(payment_record_id),),
        ).fetchone()

    def _require_record_locked(self, payment_record_id: int) -> sqlite3.Row:
        row = self._get_record_row(payment_record_id)
        if row is None:
            raise ManualPaymentError("manual payment record not found")
        return row

    def _reject_confirmed_transition_payment_locked(self, payment_record_id: int) -> None:
        bound = self._conn.execute(
            "SELECT 1 FROM mgboost_legacy_commercial_transitions "
            "WHERE payment_record_id=? AND payment_confirmed_at IS NOT NULL "
            "AND state NOT IN ('APPLIED','CANCELLED')",
            (int(payment_record_id),),
        ).fetchone()
        if bound:
            raise ManualPaymentError(
                "confirmed legacy transition payment is orchestrator-locked"
            )

    def _require_live_account_locked(self, account_id: int) -> None:
        row = self._conn.execute(
            "SELECT id,status FROM mgboost_accounts WHERE id=?", (int(account_id),),
        ).fetchone()
        if row is None or row["status"] == "CLOSED":
            raise ManualPaymentError("target parent account does not exist or is closed")

    @staticmethod
    def _editable_view_locked(row) -> dict:
        view = {
            "recorded_amount_minor": int(row["recorded_amount_minor"]),
            "payment_method": row["payment_method"],
            "external_reference": row["external_reference"],
            "comment": row["comment"],
        }
        if row["kind"] == "PLAN_PRODUCT":
            view.update({
                "plan_code": row["plan_code_snapshot"],
                "duration_days": row["duration_days_snapshot"],
            })
        else:
            view["package_sku"] = row["package_sku_snapshot"]
        return view

    def _append_edit_locked(self, *, payment_record_id, account_id, edit_kind, reason,
                            before_json, after_json, actor_ref, now) -> None:
        self._conn.execute(
            "INSERT INTO mgboost_manual_payment_edits "
            "(payment_record_id,account_id,edit_kind,reason,before_json,after_json,"
            "actor_type,actor_ref,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (int(payment_record_id), int(account_id), edit_kind, reason,
             before_json, after_json, "PRIMARY_ADMIN", actor_ref, now),
        )

    def _resolve_plan_product_locked(self, plan_code, duration_days) -> dict:
        if not isinstance(plan_code, str) or not plan_code.strip():
            raise ManualPaymentError("plan_code must be a non-empty string")
        if isinstance(duration_days, bool) or not isinstance(duration_days, int) \
                or duration_days <= 0:
            raise ManualPaymentError("duration_days must be a positive integer")
        plan = self._conn.execute(
            "SELECT * FROM mgboost_plan_versions WHERE plan_code=? AND version=1",
            (plan_code.strip(),),
        ).fetchone()
        if not plan or plan["plan_kind"] != "COMMERCIAL" or not plan["billing_required"]:
            raise ManualPaymentError("unknown or non-billable plan")
        duration = self._conn.execute(
            "SELECT * FROM mgboost_plan_durations WHERE plan_version_id=? AND duration_days=? "
            "ORDER BY duration_version DESC LIMIT 1",
            (plan["id"], int(duration_days)),
        ).fetchone()
        catalog = self._conn.execute(
            "SELECT * FROM mgboost_price_catalog_versions WHERE channel='RUB' AND status='ACTIVE'"
        ).fetchone()
        if not duration or not catalog:
            raise ManualPaymentError("requested plan product has no active RUB catalog entry")
        price = self._conn.execute(
            "SELECT * FROM mgboost_plan_prices WHERE catalog_version_id=? AND plan_version_id=? "
            "AND duration_id=?",
            (catalog["id"], plan["id"], duration["id"]),
        ).fetchone()
        if not price:
            raise ManualPaymentError("requested plan product is unavailable on the RUB channel")
        return {
            "plan_version_id": plan["id"], "duration_id": duration["id"],
            "catalog_version_id": catalog["id"], "price_id": price["id"],
            "plan_code": plan["plan_code"], "plan_version": plan["version"],
            "duration_days": int(duration["duration_days"]),
            "catalog_version": catalog["catalog_version"], "amount": int(price["amount"]),
        }

    def _validate_plan_snapshot_locked(self, row) -> None:
        product = self._conn.execute(
            "SELECT pv.plan_code,pv.version,pd.duration_days,cv.catalog_version,cv.channel,"
            "pp.amount FROM mgboost_plan_prices pp "
            "JOIN mgboost_plan_versions pv ON pv.id=pp.plan_version_id "
            "JOIN mgboost_plan_durations pd ON pd.id=pp.duration_id "
            "JOIN mgboost_price_catalog_versions cv ON cv.id=pp.catalog_version_id "
            "WHERE pp.id=? AND pp.plan_version_id=? AND pp.duration_id=? AND pp.catalog_version_id=?",
            (row["plan_price_id"], row["plan_version_id"], row["duration_id"],
             row["catalog_version_id"]),
        ).fetchone()
        if not product or product["channel"] != "RUB":
            raise ManualPaymentError(
                "pinned product references are missing or have the wrong currency channel"
            )
        expected = (
            row["plan_code_snapshot"], row["plan_version_snapshot"],
            row["duration_days_snapshot"], row["catalog_version_snapshot"],
            row["expected_amount_minor"],
        )
        actual = (
            product["plan_code"], product["version"], product["duration_days"],
            product["catalog_version"], product["amount"],
        )
        if expected != actual:
            raise ManualPaymentError(
                "immutable product snapshot disagrees with its pinned RUB catalog"
            )

    def _resolve_package_product_locked(self, sku) -> dict:
        if not isinstance(sku, str) or not sku.strip():
            raise ManualPaymentError("package sku must be a non-empty string")
        price = self._wl_package_catalog.active_price(sku.strip(), "RUB")
        if price is None:
            raise ManualPaymentError("requested package is unavailable on the RUB channel")
        return {
            "catalog_version_id": int(price["catalog_version_id"]),
            "catalog_version": price["catalog_version"],
            "price_id": int(price["id"]), "product_id": int(price["package_product_id"]),
            "sku": price["sku"], "product_version": int(price["product_version"]),
            "bytes": int(price["bytes"]), "amount": int(price["amount"]),
        }

    def _validate_package_snapshot_locked(self, row) -> None:
        product = self._conn.execute(
            "SELECT p.sku,p.version,p.bytes,cv.catalog_version,cv.channel,pp.amount "
            "FROM mgboost_wl_package_prices pp "
            "JOIN mgboost_wl_package_products p ON p.id=pp.package_product_id "
            "JOIN mgboost_price_catalog_versions cv ON cv.id=pp.catalog_version_id "
            "WHERE pp.id=? AND pp.package_product_id=? AND pp.catalog_version_id=?",
            (row["package_price_id"], row["package_product_id"], row["catalog_version_id"]),
        ).fetchone()
        if not product or product["channel"] != "RUB":
            raise ManualPaymentError(
                "pinned package references are missing or have the wrong currency channel"
            )
        if (
            product["sku"] != row["package_sku_snapshot"]
            or product["version"] != row["package_product_version_snapshot"]
            or product["bytes"] != row["package_bytes_snapshot"]
            or product["catalog_version"] != row["catalog_version_snapshot"]
            or product["amount"] != row["expected_amount_minor"]
        ):
            raise ManualPaymentError(
                "immutable package snapshot disagrees with its pinned RUB catalog"
            )

    def _mark_review_locked(self, payment_record_id: int, reason: str, now: int) -> None:
        self._conn.execute(
            "UPDATE mgboost_manual_payment_records SET status='MANUAL_REVIEW',"
            "review_reason=?,review_at=?,updated_at=? WHERE id=? AND status='PENDING'",
            (reason[:200], now, now, int(payment_record_id)),
        )
        self._conn.commit()
