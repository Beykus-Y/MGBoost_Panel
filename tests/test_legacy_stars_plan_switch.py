"""DL-063: self-service LEGACY_PAID_COMPAT_V1_* -> commercial Stars switch.

Focused invariant coverage, modeled on test_legacy_commercial_transition.py
(minus device-retirement, which does not exist in this engine) and
test_stars_purchase.py's invoice fixtures. Locks down the specific bugs the
plan review rounds found: the worker-delay activation_at bug, the
cancel-vs-capture external race, device limit blocking, crash/replay
idempotency, and (below) the LEGACY -> WL TRANSITION_BASELINE/authoritative
lineage path -- the one piece of this engine's logic that has no other
exercise anywhere in the suite.
"""
import pytest

from src.broker_operations import BrokerOperations
from src.child_contract import source_contract_hash
from src.legacy_stars_plan_switch import (
    DeviceLimitExceeded, LegacyStarsPlanSwitchConflict, ceil_to_utc_hour,
)
from src.plan_catalog import seed_plan_catalog
from src.wl_topology import WL_NODE_IDS
from tests.test_legacy_paid_compat import db, _capability, _reviewed_account
from tests.test_child_provisioning import HWID_KEY
from tests.test_marzban_broker import FakeMarzban


def _legacy_source(db, *, expiry, username="lsw-user", tg=997001, approved_limit=3):
    """A LEGACY_PAID_COMPAT_V1_D{approved_limit} subscription, expiring at
    `expiry`, plus the commercial Stars catalog seeded so BASIC/WL etc. are
    purchasable targets."""
    from src.legacy_paid_compat import ensure_legacy_paid_compat_entitlement
    account, cap = _reviewed_account(
        db, username=username, tg=tg, legacy_expiry=expiry,
    )
    ensure_legacy_paid_compat_entitlement(
        db, capability=cap, account_id=account["account_id"],
        approved_extra_device_slots=approved_limit - 3,
        evidence={"owner_decision": "legacy stars switch test"},
        decision_ref="lsw-test", now=100,
    )
    seed_plan_catalog(db.plan_catalog, now=101)
    return account["account_id"], tg


def _add_child(db, account_id, *, suffix="one", now=300):
    """Real active child lineage for an account, through the same PH3-03
    ensure/ACK contract test_legacy_commercial_transition.py's own
    ``_add_child`` uses -- needed to exercise apply_locked's authoritative
    lineage check honestly rather than mocking it away."""
    alias = db._conn.execute(
        "SELECT id,legacy_username FROM mgboost_legacy_account_aliases WHERE account_id=? "
        "ORDER BY id LIMIT 1", (account_id,),
    ).fetchone()
    remote = FakeMarzban()
    username = alias["legacy_username"]
    if username != "alice":
        remote.users[username] = remote.users.pop("alice")
        remote.users[username]["username"] = username
    slot = db.device_slots.claim(account_id, f"lsw-hwid-{suffix}", HWID_KEY, now=now)
    prepared = db.child_provisioning.prepare_child_ensure(
        account_id=account_id, slot_generation_id=slot["generation_id"], source_alias_id=alias["id"],
        source_contract_hash=source_contract_hash(remote.users[username]), expire=0,
        idempotency_key=f"lsw-child-{suffix}----------------", now=now + 1,
    )
    claimed = db.child_provisioning.claim(prepared["operation_id"], worker_id="lsw-fixture", now=now + 2)
    created = BrokerOperations(remote).dispatch("child.user.ensure", claimed["payload"])
    child_uuid = created.pop("uuid")
    db.child_provisioning.acknowledge(
        prepared["operation_id"], worker_id="lsw-fixture", outcome=created["outcome"],
        child_uuid=child_uuid, remote_result=created, now=now + 3,
    )
    return {"slot": slot, "child_intent_id": prepared["child_intent_id"], "child_uuid": child_uuid}


def _switch_row(db, account_id):
    return db._conn.execute(
        "SELECT * FROM mgboost_legacy_stars_plan_switches WHERE account_id=? ORDER BY id DESC LIMIT 1",
        (account_id,),
    ).fetchone()


def test_happy_path_far_expiry_activates_after_remaining_days_not_soon(db):
    """DL-062/DL-063 formula: activation_at anchors on the remaining paid
    legacy time, not "soon" -- a switch review round explicitly flagged a
    draft that got this backwards."""
    twenty_days = 20 * 86400
    account_id, tg = _legacy_source(db, expiry=1000 + twenty_days)
    invoice = db.stars_purchases.create_legacy_switch_invoice(
        telegram_id=tg, target_plan_code="BASIC", duration_days=30, ttl_seconds=3600, now=1000,
    )
    assert db.stars_purchases.capture_paid(
        invoice["id"], charge_id="lsw-charge-1", provider_charge_id=None,
        payer_telegram_id=tg, currency="XTR", amount=invoice["stars_price"], now=1050,
    ) == "paid"
    switch = _switch_row(db, account_id)
    assert switch["state"] == "PENDING_PAYMENT"
    result = db.stars_purchases.apply_paid_invoice(invoice["id"], now=1050)
    assert result["already_applied"] is False
    switch = _switch_row(db, account_id)
    assert switch["state"] == "SCHEDULED"
    # paid_at=1050, original_source_expiry=1000+20d -> activation should be
    # ~20 days out from paid_at, not "soon" (a fixed few-second/hour bug
    # would put it near 1050+3600).
    assert switch["activation_at"] - 1050 > 19 * 86400
    assert switch["activation_at"] == ceil_to_utc_hour(1000 + twenty_days)


def test_worker_delay_does_not_shift_activation_when_source_already_expired(db):
    """The bug this test locks down: activation_at must be derived from the
    durable stars_invoices.paid_at, never from confirm_locked's own `now`.
    paid_at=12:03 (i.e. an already-expired source at that moment), but the
    worker only gets around to calling apply_paid_invoice/confirm at 16:40 --
    activation_at must be ceil_to_utc_hour(paid_at)=13:00, not 17:00."""
    day0 = 0
    paid_at = day0 + 12 * 3600 + 3 * 60  # 12:03
    worker_now = day0 + 16 * 3600 + 40 * 60  # 16:40, simulated worker-tick delay
    account_id, tg = _legacy_source(db, expiry=paid_at - 3600)  # already expired before payment
    invoice = db.stars_purchases.create_legacy_switch_invoice(
        telegram_id=tg, target_plan_code="BASIC", duration_days=30, ttl_seconds=100000, now=paid_at - 10,
    )
    assert db.stars_purchases.capture_paid(
        invoice["id"], charge_id="lsw-charge-2", provider_charge_id=None,
        payer_telegram_id=tg, currency="XTR", amount=invoice["stars_price"], now=paid_at,
    ) == "paid"
    db.stars_purchases.apply_paid_invoice(invoice["id"], now=worker_now)
    switch = _switch_row(db, account_id)
    assert switch["state"] == "SCHEDULED"
    expected_activation = ceil_to_utc_hour(paid_at)
    assert switch["activation_at"] == expected_activation
    assert switch["activation_at"] == day0 + 13 * 3600  # 13:00, not 17:00
    assert switch["activation_at"] != ceil_to_utc_hour(worker_now)


def test_device_overage_blocks_invoice_creation_with_no_orphaned_rows(db):
    # Legacy source approved for 4 devices (PAID_BASELINE_LIMITS-valid);
    # BASIC's device_limit is 3 -- claim 4 real devices (within the legacy
    # allowance) so the target (BASIC) is genuinely over capacity, not just
    # artificially.
    account_id, tg = _legacy_source(db, expiry=100000, approved_limit=4)
    target = db._conn.execute(
        "SELECT id,device_limit FROM mgboost_plan_versions WHERE plan_code='BASIC' AND version=1"
    ).fetchone()
    assert int(target["device_limit"]) == 3
    for i in range(4):
        db.device_slots.claim(account_id, f"lsw-overage-hwid-{i}", HWID_KEY, now=100)
    with pytest.raises(DeviceLimitExceeded):
        db.stars_purchases.create_legacy_switch_invoice(
            telegram_id=tg, target_plan_code="BASIC", duration_days=30, ttl_seconds=3600, now=1000,
        )
    assert db._conn.execute("SELECT COUNT(*) FROM stars_invoices WHERE invoice_kind='LEGACY_PLAN_SWITCH'").fetchone()[0] == 0
    assert db._conn.execute("SELECT COUNT(*) FROM mgboost_legacy_stars_plan_switches").fetchone()[0] == 0


def test_cancel_before_payment_frees_unique_slot_for_a_second_attempt(db):
    account_id, tg = _legacy_source(db, expiry=100000)
    invoice = db.stars_purchases.create_legacy_switch_invoice(
        telegram_id=tg, target_plan_code="BASIC", duration_days=30, ttl_seconds=3600, now=1000,
    )
    switch = _switch_row(db, account_id)
    assert switch["state"] == "PENDING_PAYMENT"
    db.legacy_stars_plan_switch.cancel_unpaid_locked(switch["id"], now=1001)
    assert _switch_row(db, account_id)["state"] == "CANCELLED"
    # UNIQUE partial index only excludes APPLIED/CANCELLED -- a second
    # attempt must now succeed.
    invoice2 = db.stars_purchases.create_legacy_switch_invoice(
        telegram_id=tg, target_plan_code="WL", duration_days=30, ttl_seconds=3600, now=1002,
    )
    assert invoice2["id"] != invoice["id"]


def test_second_switch_while_one_is_live_is_rejected(db):
    account_id, tg = _legacy_source(db, expiry=100000)
    db.stars_purchases.create_legacy_switch_invoice(
        telegram_id=tg, target_plan_code="BASIC", duration_days=30, ttl_seconds=3600, now=1000,
    )
    with pytest.raises(Exception):
        db.stars_purchases.create_legacy_switch_invoice(
            telegram_id=tg, target_plan_code="WL", duration_days=30, ttl_seconds=3600, now=1001,
        )


def test_cancel_after_payment_evidence_is_impossible(db):
    account_id, tg = _legacy_source(db, expiry=100000)
    invoice = db.stars_purchases.create_legacy_switch_invoice(
        telegram_id=tg, target_plan_code="BASIC", duration_days=30, ttl_seconds=3600, now=1000,
    )
    assert db.stars_purchases.capture_paid(
        invoice["id"], charge_id="lsw-charge-3", provider_charge_id=None,
        payer_telegram_id=tg, currency="XTR", amount=invoice["stars_price"], now=1050,
    ) == "paid"
    switch = _switch_row(db, account_id)
    with pytest.raises(LegacyStarsPlanSwitchConflict):
        db.legacy_stars_plan_switch.cancel_unpaid_locked(switch["id"], now=1060)


def test_cancel_commits_before_late_telegram_payment_routes_to_manual_review(db):
    """The literal external-race boundary: cancel_unpaid_locked commits
    first (switch CANCELLED, invoice still 'created'), THEN Telegram's
    successful_payment callback arrives for the same invoice. Money is
    never silently dropped (paid_at is still recorded) and never silently
    applied to a cancelled switch."""
    account_id, tg = _legacy_source(db, expiry=100000)
    invoice = db.stars_purchases.create_legacy_switch_invoice(
        telegram_id=tg, target_plan_code="BASIC", duration_days=30, ttl_seconds=3600, now=1000,
    )
    switch = _switch_row(db, account_id)
    db.legacy_stars_plan_switch.cancel_unpaid_locked(switch["id"], now=1010)
    assert _switch_row(db, account_id)["state"] == "CANCELLED"

    outcome = db.stars_purchases.capture_paid(
        invoice["id"], charge_id="lsw-charge-late", provider_charge_id=None,
        payer_telegram_id=tg, currency="XTR", amount=invoice["stars_price"], now=1020,
    )
    assert outcome == "manual_review"
    row = db.get_invoice(invoice["id"])
    assert row["status"] == "manual_review"
    assert row["paid_at"] == 1020  # payment evidence recorded, not dropped
    assert row["manual_review_reason"] == "legacy_switch_cancelled_before_capture"
    assert _switch_row(db, account_id)["state"] == "CANCELLED"  # never reopened/applied


def test_reverse_order_capture_wins_then_cancel_is_refused(db):
    account_id, tg = _legacy_source(db, expiry=100000)
    invoice = db.stars_purchases.create_legacy_switch_invoice(
        telegram_id=tg, target_plan_code="BASIC", duration_days=30, ttl_seconds=3600, now=1000,
    )
    switch = _switch_row(db, account_id)
    assert db.stars_purchases.capture_paid(
        invoice["id"], charge_id="lsw-charge-early", provider_charge_id=None,
        payer_telegram_id=tg, currency="XTR", amount=invoice["stars_price"], now=1010,
    ) == "paid"
    with pytest.raises(LegacyStarsPlanSwitchConflict):
        db.legacy_stars_plan_switch.cancel_unpaid_locked(switch["id"], now=1020)
    db.stars_purchases.apply_paid_invoice(invoice["id"], now=1010)
    assert _switch_row(db, account_id)["state"] == "SCHEDULED"


def test_apply_locked_crash_replay_is_idempotent(db):
    account_id, tg = _legacy_source(db, expiry=1000)
    invoice = db.stars_purchases.create_legacy_switch_invoice(
        telegram_id=tg, target_plan_code="BASIC", duration_days=30, ttl_seconds=3600, now=900,
    )
    assert db.stars_purchases.capture_paid(
        invoice["id"], charge_id="lsw-charge-4", provider_charge_id=None,
        payer_telegram_id=tg, currency="XTR", amount=invoice["stars_price"], now=950,
    ) == "paid"
    db.stars_purchases.apply_paid_invoice(invoice["id"], now=950)
    switch = _switch_row(db, account_id)
    activation_at = switch["activation_at"]

    first = db.legacy_stars_plan_switch.apply_locked(switch["id"], now=activation_at)
    assert first["state"] == "APPLIED"
    sub_after_first = db._conn.execute(
        "SELECT row_version,current_plan_version_id,current_expiry FROM mgboost_subscriptions WHERE account_id=? ORDER BY id DESC LIMIT 1",
        (account_id,),
    ).fetchone()

    second = db.legacy_stars_plan_switch.apply_locked(switch["id"], now=activation_at + 10)
    assert second["mutation_id"] == first["mutation_id"]
    sub_after_second = db._conn.execute(
        "SELECT row_version,current_plan_version_id,current_expiry FROM mgboost_subscriptions WHERE account_id=? ORDER BY id DESC LIMIT 1",
        (account_id,),
    ).fetchone()
    assert dict(sub_after_first) == dict(sub_after_second)  # no second CAS mutation

    plan_row = db._conn.execute(
        "SELECT plan_code FROM mgboost_plan_versions WHERE id=?", (sub_after_first["current_plan_version_id"],)
    ).fetchone()
    assert plan_row["plan_code"] == "BASIC"
    assert sub_after_first["current_expiry"] == switch["target_expiry"]


def test_apply_not_ready_before_activation_at(db):
    account_id, tg = _legacy_source(db, expiry=100000)
    invoice = db.stars_purchases.create_legacy_switch_invoice(
        telegram_id=tg, target_plan_code="BASIC", duration_days=30, ttl_seconds=3600, now=1000,
    )
    assert db.stars_purchases.capture_paid(
        invoice["id"], charge_id="lsw-charge-5", provider_charge_id=None,
        payer_telegram_id=tg, currency="XTR", amount=invoice["stars_price"], now=1050,
    ) == "paid"
    db.stars_purchases.apply_paid_invoice(invoice["id"], now=1050)
    switch = _switch_row(db, account_id)
    assert db.legacy_stars_plan_switch.ready_due(now=1050) == []
    with pytest.raises(LegacyStarsPlanSwitchConflict):
        db.legacy_stars_plan_switch.apply_locked(switch["id"], now=1050)
    due = db.legacy_stars_plan_switch.ready_due(now=switch["activation_at"])
    assert len(due) == 1 and due[0]["id"] == switch["id"]


def test_non_legacy_source_cannot_use_this_engine(db):
    from src.plan_catalog import seed_plan_catalog
    account = db.accounts.create_account("DIRECT", now=1)
    db.accounts.link_telegram_owner(account["id"], 997999, provenance="MIGRATION", actor="test", now=1)
    seed_plan_catalog(db.plan_catalog, now=1)
    # Give it an ordinary BASIC subscription via a normal Stars purchase.
    invoice = db.stars_purchases.create_invoice(
        telegram_id=997999, plan_code="BASIC", duration_days=30, ttl_seconds=3600, now=10,
    )
    db.stars_purchases.capture_paid(
        invoice["id"], charge_id="ordinary-1", provider_charge_id=None,
        payer_telegram_id=997999, currency="XTR", amount=invoice["stars_price"], now=11,
    )
    db.stars_purchases.apply_paid_invoice(invoice["id"], now=11)
    with pytest.raises(Exception):
        db.stars_purchases.create_legacy_switch_invoice(
            telegram_id=997999, target_plan_code="WL", duration_days=30, ttl_seconds=3600, now=20,
        )


def test_ordinary_stars_plan_lock_is_unaffected(db):
    """Regression: the DL-063 addition must not weaken _assert_purchase_
    plan_locked for any ordinary (non-legacy-switch) purchase path."""
    from src.stars_purchase import PlanChangeRequired
    account = db.accounts.create_account("DIRECT", now=1)
    db.accounts.link_telegram_owner(account["id"], 997998, provenance="MIGRATION", actor="test", now=1)
    seed_plan_catalog(db.plan_catalog, now=1)
    invoice = db.stars_purchases.create_invoice(
        telegram_id=997998, plan_code="BASIC", duration_days=30, ttl_seconds=3600, now=10,
    )
    db.stars_purchases.capture_paid(
        invoice["id"], charge_id="ordinary-2", provider_charge_id=None,
        payer_telegram_id=997998, currency="XTR", amount=invoice["stars_price"], now=11,
    )
    db.stars_purchases.apply_paid_invoice(invoice["id"], now=11)
    with pytest.raises(PlanChangeRequired):
        db.stars_purchases.create_invoice(
            telegram_id=997998, plan_code="WL", duration_days=30, ttl_seconds=3600, now=20,
        )


@pytest.mark.parametrize("target_plan", ["WL", "EXTENDED", "FAMILY"])
def test_legacy_to_wl_family_applies_transition_baseline_for_surviving_children(db, target_plan):
    """The one piece of this engine's logic with no other test anywhere:
    LEGACY UNLIMITED -> commercial LIMITED requires the authoritative
    surviving-child-lineage check to pass, WL periods to be scheduled, and a
    TRANSITION_BASELINE row per surviving child x WL node to be created in
    the SAME transaction as the entitlement mutation -- so that the first
    post-boundary usage observation for each of those pairs is the one that
    forgives the legacy/commercial crossing interval, never double-counting
    old legacy traffic against the new commercial WL quota."""
    account_id, tg = _legacy_source(db, expiry=1000, username=f"lsw-wl-{target_plan.lower()}", tg=997100 + hash(target_plan) % 100)
    child = _add_child(db, account_id, suffix=target_plan.lower(), now=300)

    invoice = db.stars_purchases.create_legacy_switch_invoice(
        telegram_id=tg, target_plan_code=target_plan, duration_days=30, ttl_seconds=3600, now=900,
    )
    assert db.stars_purchases.capture_paid(
        invoice["id"], charge_id=f"lsw-wl-charge-{target_plan}", provider_charge_id=None,
        payer_telegram_id=tg, currency="XTR", amount=invoice["stars_price"], now=950,
    ) == "paid"
    db.stars_purchases.apply_paid_invoice(invoice["id"], now=950)
    switch = _switch_row(db, account_id)
    assert switch["state"] == "SCHEDULED"

    applied = db.legacy_stars_plan_switch.apply_locked(switch["id"], now=switch["activation_at"])
    assert applied["state"] == "APPLIED"
    assert len(applied["wl_period_ids"]) == 1  # 30-day purchase / 30-day WL period = exactly one window

    sub = db._conn.execute(
        "SELECT s.current_plan_version_id,s.current_expiry,p.plan_code,p.wl_mode FROM mgboost_subscriptions s "
        "JOIN mgboost_plan_versions p ON p.id=s.current_plan_version_id WHERE s.account_id=? ORDER BY s.id DESC LIMIT 1",
        (account_id,),
    ).fetchone()
    assert sub["plan_code"] == target_plan
    assert sub["wl_mode"] == "LIMITED"
    assert sub["current_expiry"] == switch["target_expiry"]

    period = db._conn.execute(
        "SELECT id,status,quota_mode FROM mgboost_wl_periods WHERE account_id=? ORDER BY id DESC LIMIT 1", (account_id,)
    ).fetchone()
    assert period["id"] == applied["wl_period_ids"][0]
    assert period["quota_mode"] == "LIMITED"

    baselines = db._conn.execute(
        "SELECT child_intent_id,node_id,wl_period_id,state FROM mgboost_legacy_stars_plan_switch_wl_baselines "
        "WHERE switch_id=? ORDER BY node_id", (switch["id"],),
    ).fetchall()
    assert len(baselines) == len(WL_NODE_IDS)
    assert {b["child_intent_id"] for b in baselines} == {child["child_intent_id"]}
    assert {b["node_id"] for b in baselines} == set(WL_NODE_IDS)
    assert all(b["state"] == "PENDING" for b in baselines)
    assert all(b["wl_period_id"] == period["id"] for b in baselines)


def test_legacy_to_wl_blocked_when_surviving_child_lineage_is_not_authoritative(db):
    """A child whose desired/observed state has drifted out of the
    authoritative allowlist (ACTIVE/DISABLED) must route the whole apply to
    MANUAL_REVIEW -- never a partial WL-period/baseline write, and never a
    silent apply that could double-count legacy traffic."""
    account_id, tg = _legacy_source(db, expiry=1000, username="lsw-wl-driftchild", tg=997199)
    child = _add_child(db, account_id, suffix="drift", now=300)
    db._conn.execute(
        "UPDATE mgboost_child_user_intents SET observed_state='REVOKED' WHERE id=?",
        (child["child_intent_id"],),
    )
    db._conn.commit()

    invoice = db.stars_purchases.create_legacy_switch_invoice(
        telegram_id=tg, target_plan_code="WL", duration_days=30, ttl_seconds=3600, now=900,
    )
    db.stars_purchases.capture_paid(
        invoice["id"], charge_id="lsw-wl-drift-charge", provider_charge_id=None,
        payer_telegram_id=tg, currency="XTR", amount=invoice["stars_price"], now=950,
    )
    db.stars_purchases.apply_paid_invoice(invoice["id"], now=950)
    switch = _switch_row(db, account_id)

    with pytest.raises(LegacyStarsPlanSwitchConflict):
        db.legacy_stars_plan_switch.apply_locked(switch["id"], now=switch["activation_at"])
    fresh = _switch_row(db, account_id)
    assert fresh["state"] == "MANUAL_REVIEW"
    assert db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_wl_periods WHERE account_id=?", (account_id,)
    ).fetchone()[0] == 0
    assert db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_legacy_stars_plan_switch_wl_baselines WHERE switch_id=?", (switch["id"],)
    ).fetchone()[0] == 0
    # The subscription must still be on the legacy plan -- no partial apply.
    sub = db._conn.execute(
        "SELECT p.plan_code FROM mgboost_subscriptions s JOIN mgboost_plan_versions p ON p.id=s.current_plan_version_id "
        "WHERE s.account_id=? ORDER BY s.id DESC LIMIT 1", (account_id,),
    ).fetchone()
    assert sub["plan_code"].startswith("LEGACY_PAID_COMPAT_V1_")
