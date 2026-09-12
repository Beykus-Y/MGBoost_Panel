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
import asyncio

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
    return {
        "slot": slot, "child_intent_id": prepared["child_intent_id"], "child_uuid": child_uuid,
        "remote": remote, "username": prepared["child_username"],
    }


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


# --- Astra review round: real worker-entrypoint integration + grace/retry --

def test_worker_apply_entrypoint_reaches_applied_without_typeerror(db, monkeypatch):
    """Finding 1: _run_sync(func, *args) calls func(*args) positionally --
    apply_locked's `now` is keyword-only. A store-level call with now= as a
    keyword (as the rest of this file already does) can never catch a
    positional-call bug at the real src.stars._apply_ready_legacy_switches
    call site. Drive the REAL worker entrypoint instead."""
    import src.stars as stars_mod

    account_id, tg = _legacy_source(db, expiry=1000, username="lsw-worker-apply", tg=997301)
    invoice = db.stars_purchases.create_legacy_switch_invoice(
        telegram_id=tg, target_plan_code="BASIC", duration_days=30, ttl_seconds=3600, now=900,
    )
    assert db.stars_purchases.capture_paid(
        invoice["id"], charge_id="lsw-worker-charge", provider_charge_id=None,
        payer_telegram_id=tg, currency="XTR", amount=invoice["stars_price"], now=950,
    ) == "paid"
    db.stars_purchases.apply_paid_invoice(invoice["id"], now=950)
    switch = _switch_row(db, account_id)
    assert switch["state"] == "SCHEDULED"

    # Advance the worker's own clock past activation_at -- activation_at
    # itself is a frozen fact (confirmed_at is set) and cannot be mutated
    # directly, so monkeypatch time.time() inside the module under test
    # instead, exactly matching what a real elapsed wait would do.
    monkeypatch.setattr(stars_mod.time, "time", lambda: switch["activation_at"] + 10)

    asyncio.run(stars_mod._apply_ready_legacy_switches(None, db))

    fresh = _switch_row(db, account_id)
    assert fresh["state"] == "APPLIED"
    assert db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_entitlement_mutations WHERE account_id=? AND operation='LEGACY_COMMERCIAL_STARS_SWITCH'",
        (account_id,),
    ).fetchone()[0] == 1
    assert db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_legacy_stars_plan_switch_applications WHERE invoice_id=?", (invoice["id"],)
    ).fetchone()[0] == 1
    assert db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_stars_purchase_sync_jobs WHERE invoice_id=?", (invoice["id"],)
    ).fetchone()[0] == 1


def test_worker_confirm_entrypoint_reaches_scheduled_without_typeerror(db):
    """Finding 1's clarification: the confirm path (apply_paid_invoice ->
    confirm_locked) must also be exercised through the real worker tick
    function that calls it, not just the store method directly."""
    from src.stars import process_legacy_switch_invoice_row

    account_id, tg = _legacy_source(db, expiry=100000, username="lsw-worker-confirm", tg=997302)
    invoice = db.stars_purchases.create_legacy_switch_invoice(
        telegram_id=tg, target_plan_code="BASIC", duration_days=30, ttl_seconds=3600, now=1000,
    )
    assert db.stars_purchases.capture_paid(
        invoice["id"], charge_id="lsw-worker-confirm-charge", provider_charge_id=None,
        payer_telegram_id=tg, currency="XTR", amount=invoice["stars_price"], now=1050,
    ) == "paid"
    row = db.stars_purchases.pending_legacy_switch_invoices()
    assert len(row) == 1 and row[0]["id"] == invoice["id"]
    asyncio.run(process_legacy_switch_invoice_row(None, db, row[0]))
    assert _switch_row(db, account_id)["state"] == "SCHEDULED"


def test_grace_extends_live_access_continuously_to_activation_boundary(db):
    """Finding 6: paid_at=12:03 on an already-expired source -> activation
    at 13:00. Access must be continuous (current_expiry becomes 13:00, not
    left at the pre-grace expired value) and the new commercial term starts
    fully at 13:00, not before."""
    day0 = 0
    paid_at = day0 + 12 * 3600 + 3 * 60  # 12:03
    account_id, tg = _legacy_source(db, expiry=paid_at - 3600, username="lsw-grace", tg=997303)
    invoice = db.stars_purchases.create_legacy_switch_invoice(
        telegram_id=tg, target_plan_code="BASIC", duration_days=30, ttl_seconds=100000, now=paid_at - 10,
    )
    assert db.stars_purchases.capture_paid(
        invoice["id"], charge_id="lsw-grace-charge", provider_charge_id=None,
        payer_telegram_id=tg, currency="XTR", amount=invoice["stars_price"], now=paid_at,
    ) == "paid"
    db.stars_purchases.apply_paid_invoice(invoice["id"], now=paid_at)
    switch = _switch_row(db, account_id)
    expected_activation = day0 + 13 * 3600  # 13:00
    assert switch["activation_at"] == expected_activation

    sub = db._conn.execute(
        "SELECT current_expiry,status FROM mgboost_subscriptions WHERE account_id=? ORDER BY id DESC LIMIT 1",
        (account_id,),
    ).fetchone()
    # Access is continuous up to 13:00 -- the live legacy subscription's
    # current_expiry was moved forward at CONFIRM time, not left expired.
    assert sub["current_expiry"] == expected_activation
    assert sub["status"] == "ACTIVE"
    assert db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_entitlement_mutations WHERE account_id=? AND operation='LEGACY_COMMERCIAL_ALIGNMENT_GRACE'",
        (account_id,),
    ).fetchone()[0] == 1

    # The new commercial term only starts fully AT 13:00, not before: apply
    # before the boundary must still be refused.
    with pytest.raises(Exception):
        db.legacy_stars_plan_switch.apply_locked(switch["id"], now=expected_activation - 1)
    applied = db.legacy_stars_plan_switch.apply_locked(switch["id"], now=expected_activation)
    assert applied["state"] == "APPLIED"
    sub_after = db._conn.execute(
        "SELECT current_expiry FROM mgboost_subscriptions WHERE account_id=? ORDER BY id DESC LIMIT 1", (account_id,),
    ).fetchone()
    assert sub_after["current_expiry"] == expected_activation + 30 * 86400


def test_grace_exact_utc_hour_boundary_is_a_zero_length_noop_value(db):
    """Finding 6's exact-boundary case, mirroring
    test_legacy_commercial_transition.py's own (3600,1000,3600) case:
    source expiry already exactly on a UTC hour, confirmed at exactly that
    same hour -- activation_at equals the original expiry, grace amount is
    zero (current_expiry doesn't change value, but the mutation still runs
    and status is normalized to ACTIVE)."""
    account_id, tg = _legacy_source(db, expiry=3600, username="lsw-grace-boundary", tg=997304)
    invoice = db.stars_purchases.create_legacy_switch_invoice(
        telegram_id=tg, target_plan_code="BASIC", duration_days=30, ttl_seconds=100000, now=1000,
    )
    assert db.stars_purchases.capture_paid(
        invoice["id"], charge_id="lsw-grace-boundary-charge", provider_charge_id=None,
        payer_telegram_id=tg, currency="XTR", amount=invoice["stars_price"], now=3600,
    ) == "paid"
    db.stars_purchases.apply_paid_invoice(invoice["id"], now=3600)
    switch = _switch_row(db, account_id)
    assert switch["activation_at"] == 3600
    sub = db._conn.execute(
        "SELECT current_expiry,status FROM mgboost_subscriptions WHERE account_id=? ORDER BY id DESC LIMIT 1",
        (account_id,),
    ).fetchone()
    assert sub["current_expiry"] == 3600
    assert sub["status"] == "ACTIVE"


def test_retry_before_confirmation_anchor_never_produces_scheduled_with_null_activation(db, monkeypatch):
    """Finding 7: a MANUAL_REVIEW hit before confirmation ever set
    activation_at must not be blindly restored to SCHEDULED (ready_due()
    would never select a NULL activation_at, a permanent dead end). It must
    go back to PENDING_PAYMENT so the next confirm pass can actually
    retry."""
    import src.stars as stars_mod
    from src.stars import process_legacy_switch_invoice_row

    account_id, tg = _legacy_source(db, expiry=100000, username="lsw-retry-preconfirm", tg=997305, approved_limit=4)
    invoice = db.stars_purchases.create_legacy_switch_invoice(
        telegram_id=tg, target_plan_code="BASIC", duration_days=30, ttl_seconds=3600, now=1000,
    )
    assert db.stars_purchases.capture_paid(
        invoice["id"], charge_id="lsw-retry-preconfirm-charge", provider_charge_id=None,
        payer_telegram_id=tg, currency="XTR", amount=invoice["stars_price"], now=1050,
    ) == "paid"
    # Force device-limit-exceeded-at-confirm: claim devices up to the legacy
    # allowance (4), one more than BASIC's device_limit (3), AFTER the
    # invoice was created but before confirm runs.
    for i in range(4):
        db.device_slots.claim(account_id, f"lsw-retry-preconfirm-hwid-{i}", HWID_KEY, now=100)
    db.stars_purchases.apply_paid_invoice(invoice["id"], now=1050)
    switch = _switch_row(db, account_id)
    assert switch["state"] == "MANUAL_REVIEW"
    assert switch["confirmed_at"] is None and switch["activation_at"] is None

    cap = _capability(db)
    retried = db.legacy_stars_plan_switch.retry_manual_review(
        cap, switch["id"], reason="device count corrected after review", now=1100,
    )
    assert retried["state"] == "PENDING_PAYMENT"
    assert retried["activation_at"] is None

    # Free one device back down so the next confirm attempt actually
    # succeeds, then let a normal worker pass carry it through to APPLIED.
    generation = db._conn.execute(
        "SELECT slot_id,generation FROM mgboost_device_slot_generations WHERE account_id=? AND status='ACTIVE' ORDER BY id LIMIT 1",
        (account_id,),
    ).fetchone()
    db.device_slots.release(
        account_id, generation["slot_id"], generation["generation"],
        reason="test freed one device for retry", now=1101,
    )

    row = db.stars_purchases.pending_legacy_switch_invoices()
    assert len(row) == 1
    asyncio.run(process_legacy_switch_invoice_row(None, db, row[0]))
    scheduled = _switch_row(db, account_id)
    assert scheduled["state"] == "SCHEDULED"
    assert scheduled["activation_at"] is not None

    monkeypatch.setattr(stars_mod.time, "time", lambda: scheduled["activation_at"] + 10)
    asyncio.run(stars_mod._apply_ready_legacy_switches(None, db))
    assert _switch_row(db, account_id)["state"] == "APPLIED"


def test_legacy_to_wl_baseline_is_actually_consumed_by_the_real_ledger(db):
    """Finding 2: TRANSITION_BASELINE rows were being written but the ledger
    (wl_usage_ledger.record_sample) only ever queried the manual-RUB
    baseline table -- a Stars-engine baseline sat PENDING forever and every
    post-boundary observation billed as ordinary delta, never forgiving the
    legacy/commercial crossing interval. Exercise the REAL ledger ingestion
    path, not just row existence."""
    account_id, tg = _legacy_source(db, expiry=1000, username="lsw-wl-ledger", tg=997306)
    child = _add_child(db, account_id, suffix="ledger", now=300)

    invoice = db.stars_purchases.create_legacy_switch_invoice(
        telegram_id=tg, target_plan_code="WL", duration_days=30, ttl_seconds=3600, now=900,
    )
    assert db.stars_purchases.capture_paid(
        invoice["id"], charge_id="lsw-wl-ledger-charge", provider_charge_id=None,
        payer_telegram_id=tg, currency="XTR", amount=invoice["stars_price"], now=950,
    ) == "paid"
    db.stars_purchases.apply_paid_invoice(invoice["id"], now=950)
    switch = _switch_row(db, account_id)
    applied = db.legacy_stars_plan_switch.apply_locked(switch["id"], now=switch["activation_at"])
    node_id = sorted(WL_NODE_IDS)[0]
    baseline_before = db._conn.execute(
        "SELECT state FROM mgboost_legacy_stars_plan_switch_wl_baselines WHERE switch_id=? AND node_id=?",
        (switch["id"], node_id),
    ).fetchone()
    assert baseline_before["state"] == "PENDING"

    # First post-boundary observation: must be forgiven (delta_bytes==0,
    # transition_baseline==True), never billed as ordinary usage.
    first = db.wl_usage_ledger.record_sample(
        account_id=account_id, child_intent_id=child["child_intent_id"], node_id=node_id,
        cursor_after=5_000_000_000, collector_id="w1", collected_at=switch["activation_at"] + 60,
    )
    assert first["delta_bytes"] == 0
    assert first["transition_baseline"] is True
    baseline_after = db._conn.execute(
        "SELECT state,consumed_at FROM mgboost_legacy_stars_plan_switch_wl_baselines WHERE switch_id=? AND node_id=?",
        (switch["id"], node_id),
    ).fetchone()
    assert baseline_after["state"] == "CONSUMED"
    assert baseline_after["consumed_at"] == switch["activation_at"] + 60
    assert db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_wl_usage_samples WHERE child_intent_id=? AND node_id=?",
        (child["child_intent_id"], node_id),
    ).fetchone()[0] == 0  # the forgiven interval never becomes a billed sample

    # Second observation: no double-forgiveness -- bills the real delta.
    second = db.wl_usage_ledger.record_sample(
        account_id=account_id, child_intent_id=child["child_intent_id"], node_id=node_id,
        cursor_after=5_000_001_234, collector_id="w1", collected_at=switch["activation_at"] + 3600,
    )
    assert second.get("transition_baseline") is not True
    assert second["delta_bytes"] == 1234
    assert db._conn.execute(
        "SELECT SUM(bytes_delta) FROM mgboost_wl_usage_samples WHERE child_intent_id=? AND node_id=?",
        (child["child_intent_id"], node_id),
    ).fetchone()[0] == 1234


def test_alignment_grace_reaches_real_children_via_durable_sync(db):
    """Finding 2 (second Astra pass): LEGACY_COMMERCIAL_ALIGNMENT_GRACE
    extends the LOCAL subscription's current_expiry, but that alone does
    nothing for the account's actual children until the existing durable
    parent/child-sync machinery carries it there. confirm_locked now calls
    ParentSyncStore.refresh_desired_state/enqueue_current_children right
    after its grace-extending commit (mirroring legacy_commercial_
    transition.py::confirm_payment's own post-commit block) -- this test
    proves the full path end to end through the REAL sync cycle, not just
    that a queue row exists: paid_at=12:03 on an already-expired source ->
    activation=13:00 -> local subscription reaches 13:00 -> a real child's
    remote/observed expire actually converges to 13:00 too, before the
    later commercial apply ever runs."""
    from src.parent_sync import run_account_sync_cycle

    day0 = 0
    paid_at = day0 + 12 * 3600 + 3 * 60  # 12:03
    activation_at = day0 + 13 * 3600  # 13:00
    account_id, tg = _legacy_source(db, expiry=paid_at - 3600, username="lsw-grace-sync", tg=997305)
    child = _add_child(db, account_id, suffix="grace-sync", now=50)

    invoice = db.stars_purchases.create_legacy_switch_invoice(
        telegram_id=tg, target_plan_code="BASIC", duration_days=30, ttl_seconds=100000, now=paid_at - 10,
    )
    assert db.stars_purchases.capture_paid(
        invoice["id"], charge_id="lsw-grace-sync-charge", provider_charge_id=None,
        payer_telegram_id=tg, currency="XTR", amount=invoice["stars_price"], now=paid_at,
    ) == "paid"
    # apply_paid_invoice -> confirm_locked: this is where the grace CAS and
    # the new post-commit ParentSyncStore calls both happen.
    db.stars_purchases.apply_paid_invoice(invoice["id"], now=paid_at)

    sub = db._conn.execute(
        "SELECT current_expiry FROM mgboost_subscriptions WHERE account_id=? ORDER BY id DESC LIMIT 1", (account_id,),
    ).fetchone()
    assert sub["current_expiry"] == activation_at

    # A durable sync op must already be queued from confirm_locked's own
    # post-commit enqueue -- not just "will be created whenever a cycle
    # happens to run next".
    queued = db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_parent_sync_operations WHERE account_id=? AND state='PENDING'",
        (account_id,),
    ).fetchone()[0]
    assert queued >= 1

    def sync_fn(payload):
        return BrokerOperations(child["remote"]).dispatch("child.user.state.sync", payload)

    run_account_sync_cycle(db, account_id, sync_fn=sync_fn, worker_id="grace-sync-test", now=paid_at + 5)

    remote_user = child["remote"].users[child["username"]]
    assert remote_user["expire"] == activation_at

    intent = db._conn.execute(
        "SELECT desired_state,observed_state FROM mgboost_child_user_intents WHERE id=?",
        (child["child_intent_id"],),
    ).fetchone()
    assert intent["desired_state"] == "ACTIVE"
    assert intent["observed_state"] == "ACTIVE"

    # Access was never broken in the process, and the real commercial term
    # still only starts at the boundary, not before.
    switch = _switch_row(db, account_id)
    with pytest.raises(Exception):
        db.legacy_stars_plan_switch.apply_locked(switch["id"], now=activation_at - 1)
    applied = db.legacy_stars_plan_switch.apply_locked(switch["id"], now=activation_at)
    assert applied["state"] == "APPLIED"


def test_v1_schema_upgrades_to_v2_without_checksum_mismatch_data_preserved(monkeypatch, tmp_path):
    """Finding 1 (second Astra pass): a database that already ran exactly
    what commit 0832fe3 shipped (dl063_legacy_stars_plan_switch_v1, with the
    original narrower state/event CHECKs, no REFUNDED) must upgrade cleanly
    through the current migration chain (which now also runs
    dl063_legacy_stars_plan_switch_v2) -- no RuntimeError/checksum mismatch,
    and every row inserted under v1 survives byte-for-byte. Simulated by
    skipping v2's own apply call for the FIRST Database() construction (so
    that DB is left in the exact 0832fe3-shipped state), inserting a
    v1-legal row, closing, then constructing a second, completely normal
    Database() against the same file -- this is the real upgrade path an
    already-deployed database would go through."""
    import importlib
    import os

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PRIMARY_MGBOOST_ADMIN_ACTOR_ID", "owner:primary-admin-stable-id")
    monkeypatch.setenv("PRIMARY_MGBOOST_ADMIN_LOGIN", "authenticated-primary-login")
    import src.config as config
    import src.database as database
    importlib.reload(config)
    importlib.reload(database)
    database.DB_PATH = os.path.join(str(tmp_path), "db.sqlite3")

    def _noop_v2(connection, *, now=None):
        return False

    monkeypatch.setattr(database, "apply_legacy_stars_plan_switch_schema_v2", _noop_v2)
    v1_db = database.Database()
    from src.plan_catalog import seed_plan_catalog
    seed_plan_catalog(v1_db.plan_catalog, now=1)
    account_id, tg = _legacy_source(v1_db, expiry=100000, username="lsw-v1-upgrade", tg=997900)
    invoice = v1_db.stars_purchases.create_legacy_switch_invoice(
        telegram_id=tg, target_plan_code="BASIC", duration_days=30, ttl_seconds=3600, now=1000,
    )
    switch_before = _switch_row(v1_db, account_id)
    assert switch_before["state"] == "PENDING_PAYMENT"
    v2_row = v1_db._conn.execute(
        "SELECT 1 FROM mgboost_schema_migrations WHERE migration_id='dl063_legacy_stars_plan_switch_v2'"
    ).fetchone()
    assert v2_row is None  # confirmed: this DB is in the exact 0832fe3-shipped state
    v1_db._conn.close()

    monkeypatch.undo()  # restore the real apply_legacy_stars_plan_switch_schema_v2
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PRIMARY_MGBOOST_ADMIN_ACTOR_ID", "owner:primary-admin-stable-id")
    monkeypatch.setenv("PRIMARY_MGBOOST_ADMIN_LOGIN", "authenticated-primary-login")
    importlib.reload(config)
    importlib.reload(database)
    database.DB_PATH = os.path.join(str(tmp_path), "db.sqlite3")

    upgraded_db = database.Database()  # must not raise a checksum-mismatch RuntimeError
    try:
        preserved = upgraded_db._conn.execute(
            "SELECT id,public_id,state,invoice_id FROM mgboost_legacy_stars_plan_switches WHERE id=?",
            (switch_before["id"],),
        ).fetchone()
        assert preserved is not None
        assert preserved["public_id"] == switch_before["public_id"]
        assert preserved["state"] == "PENDING_PAYMENT"
        assert preserved["invoice_id"] == switch_before["invoice_id"]

        # Free the live-UNIQUE slot (this is itself a v1-legal transition)
        # before proving v2's widened CHECK/state-machine is active.
        upgraded_db.legacy_stars_plan_switch.cancel_unpaid_locked(switch_before["id"], now=1500)

        # v2's widened CHECK/state-machine is actually active on this
        # upgraded DB (not just on a from-scratch one): a fresh switch can
        # now legally reach REFUNDED, which the original 0832fe3 CHECK
        # would have rejected outright.
        invoice2 = upgraded_db.stars_purchases.create_legacy_switch_invoice(
            telegram_id=tg, target_plan_code="WL", duration_days=30, ttl_seconds=3600, now=2000,
        )
        switch2 = _switch_row(upgraded_db, account_id)
        assert switch2["id"] != switch_before["id"]
        upgraded_db._conn.execute(
            "UPDATE mgboost_legacy_stars_plan_switches SET state='REFUNDED' WHERE id=?",
            (switch2["id"],),
        )
        upgraded_db._conn.commit()
        assert upgraded_db._conn.execute(
            "SELECT state FROM mgboost_legacy_stars_plan_switches WHERE id=?", (switch2["id"],)
        ).fetchone()["state"] == "REFUNDED"
    finally:
        upgraded_db._conn.close()


def test_pre_v2_scheduled_switch_without_grace_applies_after_upgrade(monkeypatch, tmp_path):
    """Astra review round 3, finding 1: a SCHEDULED switch confirmed by the
    PRE-grace confirm_locked (any switch confirmed before this session's
    LEGACY_COMMERCIAL_ALIGNMENT_GRACE fix landed, i.e. still shaped like
    commit 0832fe3) never had its live subscription's current_expiry moved
    to aligned_source_expiry -- only the fixed confirm_locked does that.
    apply_locked's CAS check now requires that match, so an untouched
    pre-fix row would be rejected into MANUAL_REVIEW on its very next apply
    attempt, silently stranding an already-paid switch. The v2 migration's
    one-shot backfill must retroactively perform the missing grace CAS so
    the row keeps applying by its ORIGINAL semantics after upgrade.

    Reproduced by: constructing a DB with v2 skipped (0832fe3 shape),
    confirming a switch (today's code always grace-extends correctly), then
    manually reverting the subscription + switch rows to exactly the shape
    the OLD pre-fix code would have left behind, closing, and reopening
    normally -- the real upgrade path an already-deployed pre-fix database
    would go through."""
    import importlib
    import os

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PRIMARY_MGBOOST_ADMIN_ACTOR_ID", "owner:primary-admin-stable-id")
    monkeypatch.setenv("PRIMARY_MGBOOST_ADMIN_LOGIN", "authenticated-primary-login")
    import src.config as config
    import src.database as database
    importlib.reload(config)
    importlib.reload(database)
    database.DB_PATH = os.path.join(str(tmp_path), "db.sqlite3")

    def _noop_v2(connection, *, now=None):
        return False

    monkeypatch.setattr(database, "apply_legacy_stars_plan_switch_schema_v2", _noop_v2)
    v1_db = database.Database()
    from src.plan_catalog import seed_plan_catalog
    seed_plan_catalog(v1_db.plan_catalog, now=1)

    # Expiry deliberately NOT on an hour boundary: 12:03 -> activation 13:00.
    day0 = 0
    paid_at = day0 + 12 * 3600 + 3 * 60
    activation_at = day0 + 13 * 3600
    account_id, tg = _legacy_source(v1_db, expiry=paid_at - 3600, username="lsw-pre-v2-grace", tg=997901)
    invoice = v1_db.stars_purchases.create_legacy_switch_invoice(
        telegram_id=tg, target_plan_code="BASIC", duration_days=30, ttl_seconds=100000, now=paid_at - 10,
    )
    assert v1_db.stars_purchases.capture_paid(
        invoice["id"], charge_id="lsw-pre-v2-grace-charge", provider_charge_id=None,
        payer_telegram_id=tg, currency="XTR", amount=invoice["stars_price"], now=paid_at,
    ) == "paid"
    # Today's code always grace-extends correctly here.
    v1_db.stars_purchases.apply_paid_invoice(invoice["id"], now=paid_at)
    switch = _switch_row(v1_db, account_id)
    assert switch["state"] == "SCHEDULED"
    assert switch["aligned_source_expiry"] == activation_at
    sub_graced = v1_db._conn.execute(
        "SELECT current_expiry,status,row_version FROM mgboost_subscriptions WHERE id=?",
        (switch["source_subscription_id"],),
    ).fetchone()
    assert sub_graced["current_expiry"] == activation_at  # confirmed: grace really happened here

    # Manually revert to exactly what the OLD pre-fix confirm_locked would
    # have left behind: subscription still at its pre-grace expiry/status/
    # row_version, and the switch's own bookkeeping pointing at that
    # pre-grace row_version (the old code never incremented it for grace).
    v1_db._conn.execute(
        "UPDATE mgboost_subscriptions SET current_expiry=?,status=?,row_version=? WHERE id=?",
        (switch["original_source_expiry"], switch["source_subscription_status"],
         sub_graced["row_version"] - 1, switch["source_subscription_id"]),
    )
    v1_db._conn.execute(
        "UPDATE mgboost_legacy_stars_plan_switches SET source_post_confirmation_row_version=? WHERE id=?",
        (sub_graced["row_version"] - 1, switch["id"]),
    )
    v1_db._conn.commit()
    reverted_sub = v1_db._conn.execute(
        "SELECT current_expiry FROM mgboost_subscriptions WHERE id=?", (switch["source_subscription_id"],),
    ).fetchone()
    assert reverted_sub["current_expiry"] == switch["original_source_expiry"]  # confirmed: now un-graced
    v2_row = v1_db._conn.execute(
        "SELECT 1 FROM mgboost_schema_migrations WHERE migration_id='dl063_legacy_stars_plan_switch_v2'"
    ).fetchone()
    assert v2_row is None  # confirmed: this DB is in the exact pre-v2 (0832fe3) shape
    v1_db._conn.close()

    monkeypatch.undo()
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PRIMARY_MGBOOST_ADMIN_ACTOR_ID", "owner:primary-admin-stable-id")
    monkeypatch.setenv("PRIMARY_MGBOOST_ADMIN_LOGIN", "authenticated-primary-login")
    importlib.reload(config)
    importlib.reload(database)
    database.DB_PATH = os.path.join(str(tmp_path), "db.sqlite3")

    upgraded_db = database.Database()  # runs the real v2 migration, including the backfill
    try:
        backfilled_sub = upgraded_db._conn.execute(
            "SELECT current_expiry,status FROM mgboost_subscriptions WHERE id=?",
            (switch["source_subscription_id"],),
        ).fetchone()
        assert backfilled_sub["current_expiry"] == activation_at  # grace CAS retroactively applied
        assert backfilled_sub["status"] == "ACTIVE"

        # The due apply must now actually succeed, not route to MANUAL_REVIEW.
        applied = upgraded_db.legacy_stars_plan_switch.apply_locked(switch["id"], now=activation_at)
        assert applied["state"] == "APPLIED"
        sub_after = upgraded_db._conn.execute(
            "SELECT current_plan_version_id,current_expiry FROM mgboost_subscriptions WHERE account_id=? "
            "ORDER BY id DESC LIMIT 1", (account_id,),
        ).fetchone()
        assert sub_after["current_expiry"] == activation_at + 30 * 86400
        plan_row = upgraded_db._conn.execute(
            "SELECT plan_code FROM mgboost_plan_versions WHERE id=?", (sub_after["current_plan_version_id"],)
        ).fetchone()
        assert plan_row["plan_code"] == "BASIC"
    finally:
        upgraded_db._conn.close()


def test_pre_v2_scheduled_switch_with_drift_is_never_touched_by_backfill(monkeypatch, tmp_path):
    """Astra review round 3 follow-up (P1): the v2 backfill must be a
    fail-closed CAS, not a repair tool. If the live subscription no longer
    matches EXACTLY the pre-grace snapshot the switch recorded at
    confirmation (source_subscription_id/original_source_expiry/
    source_subscription_status/source_post_confirmation_row_version) --
    e.g. a legitimate renewal or admin change touched it between the old
    confirmation and this migration running -- the migration must leave the
    subscription and the switch's own bookkeeping completely untouched, and
    must not fabricate an alignment-grace mutation on top of drifted data.
    The subsequent due apply must then correctly find the same divergence
    apply_locked already detects for any other post-confirmation drift, and
    route to MANUAL_REVIEW -- never silently apply."""
    import importlib
    import os

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PRIMARY_MGBOOST_ADMIN_ACTOR_ID", "owner:primary-admin-stable-id")
    monkeypatch.setenv("PRIMARY_MGBOOST_ADMIN_LOGIN", "authenticated-primary-login")
    import src.config as config
    import src.database as database
    importlib.reload(config)
    importlib.reload(database)
    database.DB_PATH = os.path.join(str(tmp_path), "db.sqlite3")

    def _noop_v2(connection, *, now=None):
        return False

    monkeypatch.setattr(database, "apply_legacy_stars_plan_switch_schema_v2", _noop_v2)
    v1_db = database.Database()
    from src.plan_catalog import seed_plan_catalog
    seed_plan_catalog(v1_db.plan_catalog, now=1)

    day0 = 0
    paid_at = day0 + 12 * 3600 + 3 * 60  # 12:03
    activation_at = day0 + 13 * 3600  # 13:00
    account_id, tg = _legacy_source(v1_db, expiry=paid_at - 3600, username="lsw-pre-v2-drift", tg=997902)
    invoice = v1_db.stars_purchases.create_legacy_switch_invoice(
        telegram_id=tg, target_plan_code="BASIC", duration_days=30, ttl_seconds=100000, now=paid_at - 10,
    )
    assert v1_db.stars_purchases.capture_paid(
        invoice["id"], charge_id="lsw-pre-v2-drift-charge", provider_charge_id=None,
        payer_telegram_id=tg, currency="XTR", amount=invoice["stars_price"], now=paid_at,
    ) == "paid"
    v1_db.stars_purchases.apply_paid_invoice(invoice["id"], now=paid_at)  # today's code: grace really happens
    switch = _switch_row(v1_db, account_id)
    assert switch["state"] == "SCHEDULED"
    sub_graced = v1_db._conn.execute(
        "SELECT current_expiry,status,row_version FROM mgboost_subscriptions WHERE id=?",
        (switch["source_subscription_id"],),
    ).fetchone()

    # Revert to the exact pre-fix shape (same as the no-drift test)...
    v1_db._conn.execute(
        "UPDATE mgboost_subscriptions SET current_expiry=?,status=?,row_version=? WHERE id=?",
        (switch["original_source_expiry"], switch["source_subscription_status"],
         sub_graced["row_version"] - 1, switch["source_subscription_id"]),
    )
    v1_db._conn.execute(
        "UPDATE mgboost_legacy_stars_plan_switches SET source_post_confirmation_row_version=? WHERE id=?",
        (sub_graced["row_version"] - 1, switch["id"]),
    )
    # ...then apply a LEGITIMATE change on top, as if an admin/renewal
    # touched the subscription after the old confirmation but before this
    # upgrade ever runs: a real extra day of paid time and a bumped
    # row_version, deliberately different from what the switch's own
    # snapshot expects.
    drifted_expiry = switch["original_source_expiry"] + 86400
    v1_db._conn.execute(
        "UPDATE mgboost_subscriptions SET current_expiry=?,row_version=row_version+1 WHERE id=?",
        (drifted_expiry, switch["source_subscription_id"]),
    )
    v1_db._conn.commit()
    drifted_sub = v1_db._conn.execute(
        "SELECT current_expiry,row_version FROM mgboost_subscriptions WHERE id=?",
        (switch["source_subscription_id"],),
    ).fetchone()
    mutations_before = v1_db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_entitlement_mutations WHERE account_id=?", (account_id,)
    ).fetchone()[0]
    v1_db._conn.close()

    monkeypatch.undo()
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PRIMARY_MGBOOST_ADMIN_ACTOR_ID", "owner:primary-admin-stable-id")
    monkeypatch.setenv("PRIMARY_MGBOOST_ADMIN_LOGIN", "authenticated-primary-login")
    importlib.reload(config)
    importlib.reload(database)
    database.DB_PATH = os.path.join(str(tmp_path), "db.sqlite3")

    upgraded_db = database.Database()  # runs the real v2 migration + backfill
    try:
        # Fail-closed: the drifted subscription is completely untouched --
        # not silently overwritten, not masked, not "fixed".
        sub_after = upgraded_db._conn.execute(
            "SELECT current_expiry,status,row_version FROM mgboost_subscriptions WHERE id=?",
            (switch["source_subscription_id"],),
        ).fetchone()
        assert sub_after["current_expiry"] == drifted_expiry
        assert sub_after["current_expiry"] != switch["aligned_source_expiry"]
        assert sub_after["row_version"] == drifted_sub["row_version"]

        # No fabricated grace mutation was created on top of drifted data.
        mutations_after = upgraded_db._conn.execute(
            "SELECT COUNT(*) FROM mgboost_entitlement_mutations WHERE account_id=?", (account_id,)
        ).fetchone()[0]
        assert mutations_after == mutations_before

        # The switch's own bookkeeping is untouched too.
        switch_after = upgraded_db._conn.execute(
            "SELECT source_post_confirmation_row_version FROM mgboost_legacy_stars_plan_switches WHERE id=?",
            (switch["id"],),
        ).fetchone()
        assert switch_after["source_post_confirmation_row_version"] == sub_graced["row_version"] - 1

        # The subsequent due apply correctly finds the same divergence
        # apply_locked already detects for any other post-confirmation
        # drift, and routes to MANUAL_REVIEW -- never a silent apply.
        with pytest.raises(LegacyStarsPlanSwitchConflict):
            upgraded_db.legacy_stars_plan_switch.apply_locked(switch["id"], now=activation_at)
        assert _switch_row(upgraded_db, account_id)["state"] == "MANUAL_REVIEW"
    finally:
        upgraded_db._conn.close()


def test_backfill_alignment_grace_is_idempotent_on_repeated_invocation(db):
    """Calling the backfill function itself twice against the same
    connection/state must never double-apply the grace CAS, double-bump
    row_version, or insert a second LEGACY_COMMERCIAL_ALIGNMENT_GRACE
    mutation -- the exact-match predicate against the pre-grace snapshot
    naturally excludes an already-graced row on the second pass (its
    current_expiry no longer equals original_source_expiry)."""
    from src.legacy_stars_plan_switch_schema_v2 import _backfill_missing_alignment_grace

    day0 = 0
    paid_at = day0 + 12 * 3600 + 3 * 60
    account_id, tg = _legacy_source(db, expiry=paid_at - 3600, username="lsw-backfill-idem", tg=997903)
    invoice = db.stars_purchases.create_legacy_switch_invoice(
        telegram_id=tg, target_plan_code="BASIC", duration_days=30, ttl_seconds=100000, now=paid_at - 10,
    )
    assert db.stars_purchases.capture_paid(
        invoice["id"], charge_id="lsw-backfill-idem-charge", provider_charge_id=None,
        payer_telegram_id=tg, currency="XTR", amount=invoice["stars_price"], now=paid_at,
    ) == "paid"
    db.stars_purchases.apply_paid_invoice(invoice["id"], now=paid_at)  # real grace already happened here
    switch = _switch_row(db, account_id)
    sub_first = dict(db._conn.execute(
        "SELECT current_expiry,status,row_version FROM mgboost_subscriptions WHERE id=?",
        (switch["source_subscription_id"],),
    ).fetchone())
    mutations_before = db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_entitlement_mutations WHERE account_id=? AND operation='LEGACY_COMMERCIAL_ALIGNMENT_GRACE'",
        (account_id,),
    ).fetchone()[0]
    assert mutations_before == 1  # confirm_locked's own grace mutation

    # Run the backfill directly, twice, against a row that is ALREADY
    # correctly graced -- both calls must be complete no-ops.
    _backfill_missing_alignment_grace(db._conn, int(paid_at) + 100)
    _backfill_missing_alignment_grace(db._conn, int(paid_at) + 200)
    db._conn.commit()

    sub_after = dict(db._conn.execute(
        "SELECT current_expiry,status,row_version FROM mgboost_subscriptions WHERE id=?",
        (switch["source_subscription_id"],),
    ).fetchone())
    assert sub_after == sub_first
    mutations_after = db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_entitlement_mutations WHERE account_id=? AND operation='LEGACY_COMMERCIAL_ALIGNMENT_GRACE'",
        (account_id,),
    ).fetchone()[0]
    assert mutations_after == 1  # still exactly one -- no double grace/mutation


def test_pre_v2_exact_hour_boundary_backfill_is_idempotent_on_repeated_call(monkeypatch, tmp_path):
    """Astra review round 3, follow-up P2: a zero-length grace
    (original_source_expiry == aligned_source_expiry, e.g. a source
    expiring exactly on a UTC hour) leaves current_expiry numerically
    unchanged, so a plain before/after value comparison can't tell "never
    backfilled" apart from "already backfilled, called again" once
    row_version bookkeeping has moved in lockstep -- the exact bug this
    test locks down: without the idempotency-key guard, a second backfill
    call would re-bump row_version and insert a second
    LEGACY_COMMERCIAL_ALIGNMENT_GRACE mutation (7->8->9, 0->1->2) even
    though nothing about the subscription ever visibly changed.

    Reproduced the same way as the non-zero-length backfill test: a pre-v2
    (0832fe3-shaped) SCHEDULED switch whose source expiry sits exactly on
    an hour boundary, manually reverted to the pre-grace shape, then
    upgraded twice in a row (the second time calling the backfill function
    directly against the already-upgraded connection, since the outer
    migration's own one-shot checksum gate would otherwise mask a
    non-idempotent inner implementation)."""
    import importlib
    import os

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PRIMARY_MGBOOST_ADMIN_ACTOR_ID", "owner:primary-admin-stable-id")
    monkeypatch.setenv("PRIMARY_MGBOOST_ADMIN_LOGIN", "authenticated-primary-login")
    import src.config as config
    import src.database as database
    importlib.reload(config)
    importlib.reload(database)
    database.DB_PATH = os.path.join(str(tmp_path), "db.sqlite3")

    def _noop_v2(connection, *, now=None):
        return False

    monkeypatch.setattr(database, "apply_legacy_stars_plan_switch_schema_v2", _noop_v2)
    v1_db = database.Database()
    from src.plan_catalog import seed_plan_catalog
    seed_plan_catalog(v1_db.plan_catalog, now=1)

    # Exact UTC-hour boundary: expiry=3600, confirmed at 3600 -> activation
    # is also exactly 3600 (ceil_to_utc_hour(3600)==3600) -- zero-length grace.
    account_id, tg = _legacy_source(v1_db, expiry=3600, username="lsw-pre-v2-exact-hour", tg=997904)
    invoice = v1_db.stars_purchases.create_legacy_switch_invoice(
        telegram_id=tg, target_plan_code="BASIC", duration_days=30, ttl_seconds=100000, now=1000,
    )
    assert v1_db.stars_purchases.capture_paid(
        invoice["id"], charge_id="lsw-pre-v2-exact-hour-charge", provider_charge_id=None,
        payer_telegram_id=tg, currency="XTR", amount=invoice["stars_price"], now=3600,
    ) == "paid"
    v1_db.stars_purchases.apply_paid_invoice(invoice["id"], now=3600)  # today's code: real (zero-length) grace
    switch = _switch_row(v1_db, account_id)
    assert switch["state"] == "SCHEDULED"
    assert switch["aligned_source_expiry"] == switch["original_source_expiry"] == 3600
    sub_graced = v1_db._conn.execute(
        "SELECT current_expiry,status,row_version FROM mgboost_subscriptions WHERE id=?",
        (switch["source_subscription_id"],),
    ).fetchone()
    assert sub_graced["current_expiry"] == 3600
    # mgboost_entitlement_mutations is append-only (no-delete trigger) --
    # today's confirm_locked already inserted one real grace mutation
    # before this revert; the pre-v2 shape being simulated is otherwise
    # identical to what old code would have left, so mutation-count
    # assertions below are checked as deltas from this baseline rather than
    # absolute counts.
    mutations_baseline = v1_db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_entitlement_mutations WHERE account_id=? AND operation='LEGACY_COMMERCIAL_ALIGNMENT_GRACE'",
        (account_id,),
    ).fetchone()[0]

    # Revert to exactly the pre-fix shape (same trick as the non-zero test).
    v1_db._conn.execute(
        "UPDATE mgboost_subscriptions SET current_expiry=?,status=?,row_version=? WHERE id=?",
        (switch["original_source_expiry"], switch["source_subscription_status"],
         sub_graced["row_version"] - 1, switch["source_subscription_id"]),
    )
    v1_db._conn.execute(
        "UPDATE mgboost_legacy_stars_plan_switches SET source_post_confirmation_row_version=? WHERE id=?",
        (sub_graced["row_version"] - 1, switch["id"]),
    )
    v1_db._conn.commit()
    v1_db._conn.close()

    monkeypatch.undo()
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PRIMARY_MGBOOST_ADMIN_ACTOR_ID", "owner:primary-admin-stable-id")
    monkeypatch.setenv("PRIMARY_MGBOOST_ADMIN_LOGIN", "authenticated-primary-login")
    importlib.reload(config)
    importlib.reload(database)
    database.DB_PATH = os.path.join(str(tmp_path), "db.sqlite3")

    upgraded_db = database.Database()  # runs the real v2 migration + backfill, once
    try:
        sub_after_first = dict(upgraded_db._conn.execute(
            "SELECT current_expiry,status,row_version FROM mgboost_subscriptions WHERE id=?",
            (switch["source_subscription_id"],),
        ).fetchone())
        assert sub_after_first["current_expiry"] == 3600
        assert sub_after_first["status"] == "ACTIVE"
        assert sub_after_first["row_version"] == sub_graced["row_version"]  # exactly one bump from the reverted value
        mutations_after_first = upgraded_db._conn.execute(
            "SELECT COUNT(*) FROM mgboost_entitlement_mutations WHERE account_id=? AND operation='LEGACY_COMMERCIAL_ALIGNMENT_GRACE'",
            (account_id,),
        ).fetchone()[0]
        assert mutations_after_first == mutations_baseline + 1  # backfill's own grace applied exactly once, zero-length or not

        # Call the backfill function directly again, against the SAME
        # already-upgraded connection -- must be a true no-op.
        from src.legacy_stars_plan_switch_schema_v2 import _backfill_missing_alignment_grace
        _backfill_missing_alignment_grace(upgraded_db._conn, 3700)
        upgraded_db._conn.commit()

        sub_after_second = dict(upgraded_db._conn.execute(
            "SELECT current_expiry,status,row_version FROM mgboost_subscriptions WHERE id=?",
            (switch["source_subscription_id"],),
        ).fetchone())
        assert sub_after_second == sub_after_first  # row_version did NOT move again
        mutations_after_second = upgraded_db._conn.execute(
            "SELECT COUNT(*) FROM mgboost_entitlement_mutations WHERE account_id=? AND operation='LEGACY_COMMERCIAL_ALIGNMENT_GRACE'",
            (account_id,),
        ).fetchone()[0]
        assert mutations_after_second == mutations_after_first  # no further increase -- no 0->1->2

        # The due apply still succeeds normally afterward.
        applied = upgraded_db.legacy_stars_plan_switch.apply_locked(switch["id"], now=3600)
        assert applied["state"] == "APPLIED"
    finally:
        upgraded_db._conn.close()
