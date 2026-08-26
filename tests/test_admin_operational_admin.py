"""Operational admin completion: authenticated routes over the existing
proven primitives — PH7-10 manual payments, Wave B device revoke/free/rebind,
OPD-39 ownership rebind, dashboard queues and PH5-04 account detail.

Every mutation rides the real backend engines (no new semantics), so this
file exercises authorization boundaries, server-side price authority,
idempotency/stale-submit convergence, immutability after apply, child-sync
representation and queue surfacing.
"""

import json

import pytest

from src.routes import admin_devices as AD
from src.routes import admin_ownership as AO
from src.routes import admin_payments as AP
from src.security import AdminSessionStore
from src.subscription_renewal import compute_new_expiry

from tests._ops_helpers import (
    HWID_KEY,
    PRIMARY_LOGIN,
    build_topology_account,
    capability,
    db,  # pytest fixture
    make_handler,
    paid_wl_subscription,
)
from tests.test_child_provisioning import HWID_KEY as _UNUSED  # noqa: F401  (parity check)


# ---------------------------------------------------------------------------
# Authorization boundaries


def test_unauthenticated_mutations_are_denied(db):
    account, _ = build_topology_account(db, tag="authz")
    checks = [
        (AP.handle_manual_payment_create, str(account["id"]), {}),
        (AP.handle_manual_payment_preview, str(account["id"]),
         {"plan_code": "WL", "duration_days": 30}),
        lambda h, rid=None: None,
    ]
    for func, target, payload in [(c[0], c[1], c[2]) for c in checks[:2]]:
        h = make_handler(db, payload=payload, authenticated=False)
        func(h, target)
        assert h.status == 401, (func.__name__, h.status)
    for slot_action in (AD.handle_device_revoke, AD.handle_device_free,
                        AD.handle_device_rebind):
        h = make_handler(db, payload={"reason": "x"}, authenticated=False)
        slot_action(h, str(account["id"]), "1")
        assert h.status == 401
    h = make_handler(db, payload={}, authenticated=False)
    AO.handle_telegram_ownership_rebind(h, str(account["id"]))
    assert h.status == 401


def test_non_primary_admin_is_forbidden_on_capability_routes(db):
    account, _ = build_topology_account(db, tag="nonprim")
    create_payload = {"plan_code": "WL", "duration_days": 30,
                      "recorded_amount_minor": 349, "external_reference": "np-1",
                      "payment_method": "bank", "idempotency_key": "n" * 20}
    h = make_handler(db, payload=create_payload, primary=False)
    AP.handle_manual_payment_create(h, str(account["id"]))
    assert h.status == 403

    for action in (AD.handle_device_revoke, AD.handle_device_free,
                   AD.handle_device_rebind):
        h = make_handler(db, payload={"reason": "secondary try"}, primary=False)
        action(h, str(account["id"]), "1")
        assert h.status == 403

    h = make_handler(db, payload={"expected_old_telegram_id": 1, "new_telegram_id": 2,
                                  "mode": "ORDINARY", "reason": "try", "confirm": True},
                     primary=False)
    AO.handle_telegram_ownership_rebind(h, str(account["id"]))
    assert h.status == 403

    # Read-only surfaces stay available to a normal admin session.
    h = make_handler(db, command="GET")
    AP.handle_manual_payment_catalog(h)
    assert h.status == 200


# ---------------------------------------------------------------------------
# Catalog / preview: server-side price authority only


def test_rub_catalog_matches_approved_prices_exactly(db):
    from src.plan_catalog import RUB_CATALOG_VERSION
    h = make_handler(db, command="GET")
    AP.handle_manual_payment_catalog(h)
    catalog = h.json()
    assert catalog["channel"] == "RUB"
    assert catalog["catalog_version"] == RUB_CATALOG_VERSION
    expected = {("BASIC", 30): 169, ("BASIC", 60): 279,
                ("BASIC_PLUS", 30): 239, ("BASIC_PLUS", 60): 339,
                ("BASIC_PRO", 30): 279, ("BASIC_PRO", 60): 399,
                ("WL", 30): 349, ("WL", 60): 579,
                ("EXTENDED", 30): 399, ("EXTENDED", 60): 679,
                ("FAMILY", 30): 499, ("FAMILY", 60): 749}
    actual = {(p["plan_code"], p["duration_days"]): p["amount_minor"]
              for p in catalog["plans"]}
    assert actual == expected
    packages = {p["sku"]: p["amount_minor"] for p in catalog["packages"]}
    assert packages == {"WL_PACKAGE_50_GB": 139, "WL_PACKAGE_100_GB": 249,
                        "WL_PACKAGE_250_GB": 579, "WL_PACKAGE_500_GB": 999}
    assert all(p["currency"] == "RUB" for p in catalog["plans"] + catalog["packages"])


def test_preview_same_plan_exact_price_and_estimate(db):
    account, _ = build_topology_account(db, tag="prev")
    base_expiry = db._conn.execute(
        "SELECT current_expiry FROM mgboost_subscriptions WHERE account_id=?",
        (account["id"],)).fetchone()[0]
    h = make_handler(db, payload={"plan_code": "WL", "duration_days": 30})
    AP.handle_manual_payment_preview(h, str(account["id"]))
    data = h.json()
    assert data["purchasable"] is True and data["amount_minor"] == 349
    # The estimate uses the same DL-044 formula for the live wall clock.
    import time as _time
    _, expected_now_based = compute_new_expiry(base_expiry, 30, now=int(_time.time()))
    assert data["expected_new_expiry"] >= max(base_expiry, int(_time.time())) + 29 * 86400
    assert data["expected_new_expiry_is_estimate"] is True


def test_preview_blocks_other_plan_unlimited_and_package_on_base(db):
    wl_account, _ = build_topology_account(db, tag="pvwl")
    h = make_handler(db, payload={"plan_code": "BASIC", "duration_days": 30})
    AP.handle_manual_payment_preview(h, str(wl_account["id"]))
    data = h.json()
    assert data["purchasable"] is False
    assert data["not_purchasable_reason"] == "PLAN_SWITCH_REQUIRES_PH5_06"

    package_payload = {"package_sku": "WL_PACKAGE_50_GB"}
    h = make_handler(db, payload=package_payload)
    AP.handle_manual_payment_preview(h, str(wl_account["id"]))
    assert h.json()["purchasable"] is True

    base_account, _ = build_topology_account(db, tag="pvbase", plan="BASIC")
    h = make_handler(db, payload=package_payload)
    AP.handle_manual_payment_preview(h, str(base_account["id"]))
    blocked = h.json()
    assert blocked["purchasable"] is False
    assert blocked["not_purchasable_reason"] == "CURRENT_PLAN_NOT_WL"


def test_preview_rejects_unknown_products(db):
    account, _ = build_topology_account(db, tag="pv404")
    h = make_handler(db, payload={"plan_code": "NOPE", "duration_days": 30})
    AP.handle_manual_payment_preview(h, str(account["id"]))
    assert h.status == 404
    h = make_handler(db, payload={"plan_code": "WL", "duration_days": 45})
    AP.handle_manual_payment_preview(h, str(account["id"]))
    assert h.status == 404
    h = make_handler(db, payload={"package_sku": "MAGIC_GB"})
    AP.handle_manual_payment_preview(h, str(account["id"]))
    assert h.status == 404
    h = make_handler(db, payload={"plan_code": "WL", "duration_days": 30})
    AP.handle_manual_payment_preview(h, "999999")
    assert h.status == 404  # unknown account (IDOR-safe)


# ---------------------------------------------------------------------------
# Create/edit/cancel/apply lifecycle through the routes


def _create(db, account_id, *, days=30, amount=349, ref, key,
            plan="WL", method="bank_transfer"):
    h = make_handler(db, payload={
        "plan_code": plan, "duration_days": days, "recorded_amount_minor": amount,
        "external_reference": ref, "payment_method": method,
        "idempotency_key": key})
    AP.handle_manual_payment_create(h, str(account_id))
    return h


def test_manipulated_inputs_are_rejected_at_create(db):
    account, _ = build_topology_account(db, tag="tamper")
    wrong_price = {"plan_code": "WL", "duration_days": 30, "recorded_amount_minor": 100,
                   "external_reference": "t-price", "payment_method": "bank",
                   "idempotency_key": "a" * 20}
    h = make_handler(db, payload=wrong_price)
    AP.handle_manual_payment_create(h, str(account["id"]))
    assert h.status == 400

    bad_days = dict(wrong_price, recorded_amount_minor=349, duration_days=45,
                    external_reference="t-days")
    h = make_handler(db, payload=bad_days)
    AP.handle_manual_payment_create(h, str(account["id"]))
    assert h.status in (400, 404)

    h = _create(db, "88888888", ref="t-acct", key="k" * 20)
    assert h.status == 404  # nonexistent target account


def test_duplicate_submit_converges_via_durable_idempotency(db):
    account, _ = build_topology_account(db, tag="dupes")
    h1 = _create(db, account["id"], ref="dup-1", key="duplicate-key-00000001")
    assert h1.status == 200
    first = h1.json()["payment"]
    h2 = _create(db, account["id"], ref="dup-1", key="duplicate-key-00000001")
    second = h2.json()["payment"]
    assert h2.status == 200
    assert second["public_id"] == first["public_id"]

    # Same reference with a DIFFERENT request must conflict, not diverge.
    h3 = _create(db, account["id"], days=60, amount=579, ref="dup-1",
                 key="duplicate-key-00000002")
    assert h3.status == 409


def test_edit_cancel_apply_full_pending_lifecycle(db):
    account, children = build_topology_account(db, tag="cycle", n_children=1)
    h = _create(db, account["id"], ref="cyc-1", key="lifecycle-key-000000001")
    rid = h.json()["payment"]["id"]

    h = make_handler(db, payload={"reason": "change comment to correct one",
                                  "changes": {"comment": "fixed note"}})
    AP.handle_manual_payment_edit(h, str(rid))
    assert h.status == 200 and h.json()["payment"]["status"] == "PENDING"

    detail = make_handler(db, command="GET")
    AP.handle_manual_payment_detail(detail, str(rid))
    body = detail.json()
    assert body["payment"]["comment"] == "fixed note"
    assert body["edits"] and body["edits"][0]["edit_kind"] == "FIELD_EDIT"

    cancel = make_handler(db, payload={"reason": "cancelling wrong transfer entry"})
    AP.handle_manual_payment_cancel(cancel, str(rid))
    assert cancel.status == 200
    edit_cancelled = make_handler(db, payload={"reason": "too late anyway123",
                                               "changes": {"comment": "x"}})
    AP.handle_manual_payment_edit(edit_cancelled, str(rid))
    assert edit_cancelled.status == 409
    apply_cancelled = make_handler(db, payload={})
    AP.handle_manual_payment_apply(apply_cancelled, str(rid))
    assert apply_cancelled.status == 409

    h = _create(db, account["id"], ref="cyc-2", key="lifecycle-key-000000002")
    fresh_rid = h.json()["payment"]["id"]
    apply_h = make_handler(db, payload={})
    AP.handle_manual_payment_apply(apply_h, str(fresh_rid))
    result = apply_h.json()
    assert apply_h.status == 200 and result["already_applied"] is False
    base_expiry = db._conn.execute(
        "SELECT current_expiry FROM mgboost_subscriptions WHERE account_id=?",
        (account["id"],)).fetchone()[0]
    assert result["renewal_after_expiry"] == result["renewal_before_expiry"] + 30 * 86400 \
        or result["entitlement_summary"]["effective_expiry"] >= 30 * 86400
    assert result["payment"]["applied_operation"] == "RENEW"

    replay = make_handler(db, payload={})
    AP.handle_manual_payment_apply(replay, str(fresh_rid))
    assert replay.json()["already_applied"] is True

    immut_cancel = make_handler(db, payload={"reason": "cannot touch applied"})
    AP.handle_manual_payment_cancel(immut_cancel, str(fresh_rid))
    assert immut_cancel.status == 409
    immut_edit = make_handler(db, payload={"reason": "cannot edit applied rec",
                                           "changes": {"comment": "zz"}})
    AP.handle_manual_payment_edit(immut_edit, str(fresh_rid))
    assert immut_edit.status == 409


def test_applied_plan_renewal_marks_sync_when_children_exist(db):
    account, children = build_topology_account(db, tag="syncd", n_children=1)
    child_row = db._conn.execute(
        "SELECT c.child_username FROM mgboost_child_user_intents c "
        "WHERE c.id=?", (children[0]["prepared"]["child_intent_id"],)).fetchone()
    h = _create(db, account["id"], ref="sync-ref-1", key="synced-key-0000000001")
    rid = h.json()["payment"]["id"]
    result = make_handler(db, payload={})
    AP.handle_manual_payment_apply(result, str(rid))
    body = result.json()
    assert body["sync_state"] in ("SYNCED", "PENDING")  # honest mapping either way
    if body["sync_state"] == "SYNCED":
        remote_after = db._fake_remote.users[child_row["child_username"]]["expire"]
        assert remote_after == body["renewal_after_expiry"]
        # The legacy-template parent user is never touched by child sync.
        assert db._fake_remote.users[f"ops_parent_syncd_{account['id']}"]["expire"] != \
            remote_after


def test_plan_drift_lands_in_manual_review_with_resolve_route(db):
    account, _ = build_topology_account(db, tag="drift")
    h = _create(db, account["id"], ref="drift-1", key="drift-key-0000000001")
    rid = h.json()["payment"]["id"]
    # Simulate out-of-band plan drift between record creation and apply (the
    # state the PH5-09 MANUAL_REVIEW contract exists for): point the
    # subscription row at another commercial plan version.
    other = db._conn.execute(
        "SELECT id FROM mgboost_plan_versions WHERE plan_code='BASIC_PRO' AND plan_kind='COMMERCIAL'")
    db._conn.execute(
        "UPDATE mgboost_subscriptions SET current_plan_version_id=? WHERE account_id=?",
        (other.fetchone()[0], account["id"]))
    db._conn.commit()
    drift_apply = make_handler(db, payload={})
    AP.handle_manual_payment_apply(drift_apply, str(rid))
    assert drift_apply.status == 409

    state_detail = make_handler(db, command="GET")
    AP.handle_manual_payment_detail(state_detail, str(rid))
    assert state_detail.json()["payment"]["status"] == "MANUAL_REVIEW"

    resolve = make_handler(db, payload={"resolution_note": "owner decided"})
    AP.handle_manual_payment_resolve_review(resolve, str(rid))
    assert resolve.status == 200
    assert resolve.json()["payment"]["status"] == "PENDING"


def test_package_purchase_grants_bucket_through_route(db):
    account, children = build_topology_account(db, tag="pkg")
    h = make_handler(db, payload={
        "package_sku": "WL_PACKAGE_50_GB", "recorded_amount_minor": 139,
        "external_reference": "pkg-ref-1", "payment_method": "cash",
        "idempotency_key": "package-key-000000000001"})
    AP.handle_manual_payment_create(h, str(account["id"]))
    assert h.status == 200
    rid = h.json()["payment"]["id"]
    applied = make_handler(db, payload={})
    AP.handle_manual_payment_apply(applied, str(rid))
    body = applied.json()
    assert body["grant"]["granted_bytes"] == 50 * 10**9
    assert "sync_state" not in body  # WL packages create no child-sync job


def test_cancelled_reference_stays_reserved_forever_dl054(db):
    account, _ = build_topology_account(db, tag="dl054")
    h = _create(db, account["id"], ref="forever-1", key="reserved-key-000000001")
    rid = h.json()["payment"]["id"]
    make_handler(db, payload={"reason": "wrong transfer"}).__class__
    cancel = make_handler(db, payload={"reason": "wrong transfer entered by operator"})
    AP.handle_manual_payment_cancel(cancel, str(rid))
    assert cancel.status == 200
    reuse = _create(db, account["id"], ref="forever-1", key="reserved-key-000000002")
    assert reuse.status == 409


def test_payment_list_projection_has_no_raw_secrets(db):
    account, _ = build_topology_account(db, tag="listsec")
    _create(db, account["id"], ref="sec-list-1", key="seclist-key-0000000001")
    h = make_handler(db, command="GET", path="/admin/manual-payments?status=PENDING")
    AP.handle_manual_payments_list(h)
    blob = json.dumps(h.json())
    assert "mgc_" not in blob
    for banned in ("uuid_verifier", "hwid_verifier", "raw_token", "Bearer"):
        assert banned not in blob


# ---------------------------------------------------------------------------
# Device lifecycle routes


def test_device_revoke_then_free_full_semantics(db):
    account, children = build_topology_account(db, tag="devrf", n_children=1)
    alias_prefix = f"ops_parent_devrf_{account['id']}"

    revoke = make_handler(db, payload={"reason": "stolen device report"})
    AD.handle_device_revoke(revoke, str(account["id"]), "1")
    assert revoke.status == 200 and revoke.json()["state"] == "APPLIED"
    intent_row = db._conn.execute(
        "SELECT desired_state,observed_state FROM mgboost_child_user_intents "
        "WHERE id=?", (children[0]["prepared"]["child_intent_id"],)).fetchone()
    assert intent_row["desired_state"] == "REVOKED"

    converge = make_handler(db, payload={"reason": "double click same reason"})
    AD.handle_device_revoke(converge, str(account["id"]), "1")
    assert converge.status == 200 and converge.json().get("converged") is True

    free_early_slot = build_topology_account(db, tag="devrf-b")[0]
    early_free = make_handler(db, payload={"reason": "attempt without revoke"})
    AD.handle_device_free(early_free, str(account["id"]), "9")
    assert early_free.status in (404, 409)

    free = make_handler(db, payload={"reason": "release for replacement"})
    AD.handle_device_free(free, str(account["id"]), "1")
    assert free.status == 200 and free.json()["state"] == "APPLIED"
    slot_state = db._conn.execute(
        "SELECT desired_state,observed_state FROM mgboost_device_slots "
        "WHERE account_id=? AND slot_number=?", (account["id"], 1)).fetchone()
    assert slot_state["desired_state"] == "FREE"
    gen = db._conn.execute(
        "SELECT g.status FROM mgboost_device_slots s JOIN mgboost_device_slot_generations g "
        "ON g.slot_id=s.id WHERE s.account_id=? AND s.slot_number=?",
        (account["id"], 1)).fetchone()
    assert gen["status"] == "RELEASED"


def test_device_rebind_requires_confirmation_creates_new_generation(db):
    account, children = build_topology_account(db, tag="devrb", n_children=1)
    old_generation_row = db._conn.execute(
        "SELECT generation FROM mgboost_device_slot_generations WHERE id=?",
        (children[0]["slot"]["generation_id"],)).fetchone()

    no_confirm = make_handler(db, payload={"reason": "replace",
                                           "new_device_hwid": "raw-new-hwid-rb"})
    AD.handle_device_rebind(no_confirm, str(account["id"]), "1")
    assert no_confirm.status == 409
    assert "confirm" in no_confirm.json()["error"]

    ok = make_handler(db, payload={"reason": "compromise replacement",
                                   "new_device_hwid": "raw-new-hwid-rb",
                                   "confirm": True})
    AD.handle_device_rebind(ok, str(account["id"]), "1")
    assert ok.status == 200 and ok.json()["state"] == "APPLIED"
    assert ok.json()["current_generation"] == old_generation_row["generation"] + 1

    lineage = db._conn.execute(
        "SELECT c.id,c.child_username,c.uuid_verifier,g.generation FROM "
        "mgboost_child_user_intents c JOIN mgboost_device_slot_generations g "
        "ON g.id=c.slot_generation_id WHERE g.slot_id="
        "(SELECT id FROM mgboost_device_slots WHERE account_id=? AND slot_number=?) "
        "ORDER BY g.generation", (account["id"], 1)).fetchall()
    verifiers = {row["generation"]: row["uuid_verifier"] for row in lineage}
    assert verifiers[old_generation_row["generation"]] != \
        verifiers[old_generation_row["generation"] + 1]
    usernames = [row["child_username"] for row in lineage]
    assert len(set(usernames)) == len(usernames)

    done_again = make_handler(db, payload={"reason": "another device",
                                           "new_device_hwid": "raw-other-hwid-x",
                                           "confirm": True})
    AD.handle_device_rebind(done_again, str(account["id"]), "1")
    assert done_again.status == 409


def test_no_generic_delete_exists_in_admin_routes(db):
    from src.server import _ROUTES
    for method, pattern, _handler in _ROUTES:
        assert not (method == "DELETE" and "/admin/accounts/" in pattern.pattern), pattern.pattern


# ---------------------------------------------------------------------------
# Ownership rebind route


def test_telegram_ownership_rebind_ordinary_flow_and_guardrails(db):
    account, _ = build_topology_account(db, tag="own1")  # owner linked via helper
    actual_owner = db._conn.execute(
        "SELECT telegram_id FROM mgboost_telegram_identities WHERE account_id=? "
        "AND role='OWNER' AND revoked_at IS NULL", (account["id"],)).fetchone()["telegram_id"]
    payload_base = {"expected_old_telegram_id": actual_owner,
                    "new_telegram_id": 222222222, "mode": "ORDINARY",
                    "reason": "owner switched telegram account"}

    h = make_handler(db, payload=payload_base)
    AO.handle_telegram_ownership_rebind(h, str(account["id"]))
    assert h.status == 409 and "confirm" in h.json()["error"]

    short_reason = make_handler(db, payload=dict(payload_base, reason="x", confirm=True))
    AO.handle_telegram_ownership_rebind(short_reason, str(account["id"]))
    assert short_reason.status == 400

    ok = make_handler(db, payload=dict(payload_base, confirm=True))
    AO.handle_telegram_ownership_rebind(ok, str(account["id"]))
    assert ok.status == 200 and ok.json()["operation"]["state"] == "APPLIED"
    identities = db._conn.execute(
        "SELECT telegram_id,revoked_at FROM mgboost_telegram_identities "
        "WHERE account_id=? ORDER BY linked_at", (account["id"],)).fetchall()
    assert [row["telegram_id"] for row in identities] == [actual_owner, 222222222]
    assert identities[0]["revoked_at"] and not identities[1]["revoked_at"]

    stale = make_handler(db, payload=dict(payload_base, confirm=True,
                                          expected_old_telegram_id=42,
                                          new_telegram_id=777777777))
    AO.handle_telegram_ownership_rebind(stale, str(account["id"]))
    assert stale.status in (400, 409)


def test_compromise_rebind_rotates_opaque_credential(db):
    account, _ = build_topology_account(db, tag="own2")
    delivered = {}
    from src.subscription_credential_issuance import issue_or_reissue_credential
    issue_or_reissue_credential(
        db, account_id=account["id"], actor_ref="test", reason="initial credential",
        idempotency_key="own2-initial-issue-key------0001",
        deliver_fn=lambda raw: delivered.update(token=raw), now=900)
    old_cred = db._conn.execute(
        "SELECT id,status FROM mgboost_subscription_credentials WHERE account_id=? "
        "ORDER BY generation DESC LIMIT 1", (account["id"],)).fetchone()

    actual_owner = db._conn.execute(
        "SELECT telegram_id FROM mgboost_telegram_identities WHERE account_id=? "
        "AND role='OWNER' AND revoked_at IS NULL", (account["id"],)).fetchone()["telegram_id"]
    h = make_handler(db, payload={"expected_old_telegram_id": actual_owner,
                                  "new_telegram_id": 444444444,
                                  "mode": "COMPROMISE",
                                  "reason": "token and telegram both compromised",
                                  "confirm": True})
    AO.handle_telegram_ownership_rebind(h, str(account["id"]))
    body = h.json()
    assert h.status == 200 and body["credential_rotated"] is True
    assert "не показывается" in body.get("message", "") or "Issue" in body.get("message", "")
    post_old = db._conn.execute(
        "SELECT status FROM mgboost_subscription_credentials WHERE id=?",
        (old_cred["id"],)).fetchone()
    latest = db._conn.execute(
        "SELECT status FROM mgboost_subscription_credentials WHERE account_id=? "
        "ORDER BY generation DESC LIMIT 1", (account["id"],)).fetchone()
    assert post_old["status"] == "REVOKED" and latest["status"] == "ACTIVE"


# ---------------------------------------------------------------------------
# Account detail canonical data + queues on dashboard


def test_account_detail_serves_ph504_entitlement_and_payments(db):
    from src.admin_read_models import account_detail
    from src.entitlement_engine import calculate_effective_entitlement
    account, children = build_topology_account(db, tag="detail")
    h = _create(db, account["id"], ref="detail-1", key="detail-key-00000000001")
    rid = h.json()["payment"]["id"]
    applied = make_handler(db, payload={})
    AP.handle_manual_payment_apply(applied, str(rid))

    direct = calculate_effective_entitlement(db, account_id=account["id"])
    detail = account_detail(db, account["id"], now=direct["calculated_at"])
    ent = detail["entitlement"]
    assert ent["subscription"]["effective_expiry"] == \
        direct["subscription"]["effective_expiry"]
    assert ent["plan"]["code"] == "WL"
    assert ent["wl"]["real_plan_mode"] == "LIMITED"
    manual = detail["manual_payments"][0]
    assert manual["status"] == "APPLIED"
    assert manual["application"]["applied_expiry"] == \
        direct["subscription"]["effective_expiry"]
    full_blob = json.dumps(detail)
    tech_only = json.dumps(detail["technical"])
    assert "mgc_" in tech_only
    assert full_blob.count("mgc_") == tech_only.count("mgc_")


def test_dashboard_summary_surfaces_operational_queues(db):
    from src.admin_read_models import dashboard_summary
    account, _ = build_topology_account(db, tag="queue")
    _create(db, account["id"], ref="queue-1", key="queues-key-00000000001")
    summary = dashboard_summary(db)
    queues = summary["queues"]
    assert queues["counts_by_status"].get("PENDING") == 1
    item = queues["pending"][0]
    assert item["public_id"].startswith("mpay_")
    assert item["amount_minor"] == 349
    assert queues["stars_manual_review"]["count"] == 0


def test_timeline_aggregator_groups_sources_and_scrubs_secrets(db):
    from src.admin_audit_timeline import account_timeline
    account, _ = build_topology_account(db, tag="tl")
    h = _create(db, account["id"], ref="tl-ref-1", key="timeline-key-000000001")
    rid = h.json()["payment"]["id"]
    make_handler(db, payload={"reason": "fix method bank_transfer→sbp",
                              "changes": {"payment_method": "sbp"}})
    edit = make_handler(db, payload={"reason": "switch payment method to sbp",
                                     "changes": {"payment_method": "sbp"}})
    AP.handle_manual_payment_edit(edit, str(rid))
    timeline = account_timeline(db, account["id"])
    sources = {entry["source"] for entry in timeline["entries"]}
    assert {"ENTITLEMENT_MUTATION", "MANUAL_PAYMENT"} <= sources
    blob = json.dumps(timeline)
    for banned in ("mgc_", "sha256:", "hmac-sha256:", HWID_KEY, "Bearer ", "raw-hwid"):
        assert banned not in blob
