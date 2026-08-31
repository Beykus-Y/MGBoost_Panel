"""Focused money/entitlement lifecycle for legacy->commercial transitions."""
import pytest

from src.broker_operations import BrokerOperations
from src.child_contract import source_contract_hash
from src.manual_payment import ManualPaymentError
from src.legacy_commercial_transition import (
    LegacyCommercialTransitionConflict, LegacyCommercialTransitionError,
)
from src.plan_catalog import RUB_PRICES, seed_plan_catalog
from tests.test_legacy_paid_compat import db, _capability, _reviewed_account
from tests.test_marzban_broker import FakeMarzban
from tests.test_child_provisioning import HWID_KEY


def _legacy(db, *, expiry=4500, username="lct-user", tg=998001,
            observed_device_count=0, approved_limit=3, unlimited=False):
    from src.legacy_paid_compat import ensure_legacy_paid_compat_entitlement
    account, cap = _reviewed_account(
        db, username=username, tg=tg, legacy_expiry=expiry,
        observed_device_count=observed_device_count,
    )
    ensure_legacy_paid_compat_entitlement(
        db, capability=cap, account_id=account["account_id"],
        approved_extra_device_slots=0 if unlimited else approved_limit - 3,
        device_limit_exempt=unlimited,
        evidence={"owner_decision": "legacy transition test"},
        decision_ref="legacy-transition-test", now=100,
    )
    seed_plan_catalog(db.plan_catalog, now=101)
    return account["account_id"], cap


def _payment(db, cap, account_id, *, plan="BASIC", days=30, tag="a"):
    return db.manual_payments.create_record(
        cap, account_id=account_id, plan_code=plan, duration_days=days,
        external_reference=f"lct-ref-{tag}",
        recorded_amount_minor=RUB_PRICES[(plan, days)],
        payment_method="bank_transfer", idempotency_key=f"lct-payment-key-{tag}------",
        now=200,
    )


def _add_child(db, account_id, *, suffix="one", now=300):
    alias = db._conn.execute(
        "SELECT id,legacy_username FROM mgboost_legacy_account_aliases WHERE account_id=? "
        "ORDER BY id LIMIT 1", (account_id,),
    ).fetchone()
    remote = FakeMarzban()
    username = alias["legacy_username"]
    if username != "alice":
        remote.users[username] = remote.users.pop("alice")
        remote.users[username]["username"] = username
    slot = db.device_slots.claim(account_id, f"lct-hwid-{suffix}", HWID_KEY, now=now)
    prepared = db.child_provisioning.prepare_child_ensure(
        account_id=account_id, slot_generation_id=slot["generation_id"], source_alias_id=alias["id"],
        source_contract_hash=source_contract_hash(remote.users[username]), expire=0,
        idempotency_key=f"lct-child-{suffix}----------------", now=now + 1,
    )
    claimed = db.child_provisioning.claim(prepared["operation_id"], worker_id="lct-fixture", now=now + 2)
    created = BrokerOperations(remote).dispatch("child.user.ensure", claimed["payload"])
    child_uuid = created.pop("uuid")
    db.child_provisioning.acknowledge(
        prepared["operation_id"], worker_id="lct-fixture", outcome=created["outcome"],
        child_uuid=child_uuid, remote_result=created, now=now + 3,
    )
    return {"slot": slot, "child_intent_id": prepared["child_intent_id"], "child_uuid": child_uuid}


@pytest.mark.parametrize("expiry,confirmed,activation", [
    (4320, 1000, 7200), (3600, 1000, 3600), (500, 4217, 7200),
])
def test_confirmation_aligns_and_freezes_paid_term(db, expiry, confirmed, activation):
    account_id, cap = _legacy(db, expiry=expiry, username=f"lct-{expiry}-{confirmed}", tg=998000+expiry+confirmed)
    payment = _payment(db, cap, account_id, tag=f"{expiry}-{confirmed}")
    transition = db.legacy_commercial_transitions.create(
        cap, payment_record_id=payment["id"], reason="real external payment", now=900,
    )
    confirmed_row = db.legacy_commercial_transitions.confirm_payment(
        cap, transition["id"], now=confirmed,
    )
    assert confirmed_row["activation_at"] == activation
    assert confirmed_row["aligned_source_expiry"] == activation
    assert confirmed_row["target_expiry"] == activation + 30*86400
    source = db._conn.execute("SELECT current_plan_version_id,current_expiry FROM mgboost_subscriptions WHERE account_id=?", (account_id,)).fetchone()
    assert source["current_expiry"] == activation
    with pytest.raises(ManualPaymentError):
        db.manual_payments.apply_record(cap, payment["id"], now=confirmed+1)
    with pytest.raises(ManualPaymentError):
        db.manual_payments.cancel_record(cap, payment["id"], reason="cancel forbidden", now=confirmed+1)


def test_confirmed_payment_restores_expired_legacy_only_until_alignment_boundary(db):
    account_id, cap = _legacy(db, expiry=500, username="lct-expired-restore", tg=998888)
    db._conn.execute("UPDATE mgboost_subscriptions SET status='EXPIRED' WHERE account_id=?", (account_id,))
    db._conn.commit()
    payment = _payment(db, cap, account_id, tag="expired-restore")
    transition = db.legacy_commercial_transitions.create(
        cap, payment_record_id=payment["id"], reason="real paid transition", now=4200,
    )
    transition = db.legacy_commercial_transitions.confirm_payment(cap, transition["id"], now=4217)
    source = db._conn.execute(
        "SELECT status,current_expiry FROM mgboost_subscriptions WHERE account_id=?", (account_id,),
    ).fetchone()
    assert transition["activation_at"] == 7200
    assert tuple(source) == ("ACTIVE", 7200)


@pytest.mark.parametrize("plan", ["BASIC","BASIC_PLUS","BASIC_PRO","WL","EXTENDED","FAMILY"])
@pytest.mark.parametrize("days", [30,60])
def test_atomic_apply_all_targets_and_durations(db, plan, days):
    account_id, cap = _legacy(db, expiry=3600, username=f"lct-{plan}-{days}", tg=997000+days+len(plan))
    payment = _payment(db, cap, account_id, plan=plan, days=days, tag=f"{plan}-{days}")
    transition = db.legacy_commercial_transitions.create(cap, payment_record_id=payment["id"], reason="real paid transition", now=1000)
    transition = db.legacy_commercial_transitions.confirm_payment(cap, transition["id"], now=1000)
    claimed = db.legacy_commercial_transitions.claim_due(
        worker_id="transition-test-worker", now=transition["activation_at"],
    )
    assert [row["id"] for row in claimed] == [transition["id"]]
    ready = db.legacy_commercial_transitions.assess_capacity(transition["id"], now=transition["activation_at"])
    assert ready["state"] == "READY_TO_APPLY"
    applied = db.legacy_commercial_transitions.apply_ready(transition["id"], now=transition["activation_at"])
    assert applied["state"] == "APPLIED"
    pay = db.manual_payments.get_record(payment["id"])
    assert pay["status"] == "APPLIED"
    current = db._conn.execute("SELECT p.plan_code,s.current_expiry FROM mgboost_subscriptions s JOIN mgboost_plan_versions p ON p.id=s.current_plan_version_id WHERE s.account_id=?", (account_id,)).fetchone()
    assert current["plan_code"] == plan
    assert current["current_expiry"] == transition["target_expiry"]
    baseline_count = db._conn.execute("SELECT COUNT(*) FROM mgboost_wl_transition_baselines WHERE transition_id=?", (transition["id"],)).fetchone()[0]
    assert baseline_count == 0  # zero-device fixture; no fabricated lineage
    assert len(applied["wl_period_ids"]) == ((days//30) if plan in {"WL","EXTENDED","FAMILY"} else 0)


@pytest.mark.parametrize("approved_limit,unlimited,expected", [
    (3, False, "LEGACY_PAID_COMPAT_V1_D3"),
    (4, False, "LEGACY_PAID_COMPAT_V1_D4"),
    (6, False, "LEGACY_PAID_COMPAT_V1_D6"),
    (3, True, "LEGACY_PAID_COMPAT_V1_UNLIMITED"),
])
def test_all_legacy_compat_source_variants_are_eligible(db, approved_limit, unlimited, expected):
    account_id, cap = _legacy(
        db, expiry=3600, username=f"lct-source-{expected.lower()}",
        tg=995000+approved_limit+(100 if unlimited else 0), approved_limit=approved_limit,
        unlimited=unlimited,
    )
    source = db._conn.execute(
        "SELECT p.plan_code FROM mgboost_subscriptions s JOIN mgboost_plan_versions p "
        "ON p.id=s.current_plan_version_id WHERE s.account_id=?", (account_id,),
    ).fetchone()[0]
    assert source == expected
    payment = _payment(db, cap, account_id, tag=f"source-{expected}")
    transition = db.legacy_commercial_transitions.create(
        cap, payment_record_id=payment["id"], reason="real paid transition", now=1000,
    )
    assert transition["state"] == "PENDING_PAYMENT"


def test_confirm_replay_is_one_grace_and_all_generic_payment_mutations_are_locked(db):
    account_id, cap = _legacy(db, expiry=4321, username="lct-guards", tg=997900)
    payment = _payment(db, cap, account_id, tag="guards")
    transition = db.legacy_commercial_transitions.create(
        cap, payment_record_id=payment["id"], reason="real paid transition", now=1000,
    )
    first = db.legacy_commercial_transitions.confirm_payment(cap, transition["id"], now=1100)
    replay = db.legacy_commercial_transitions.confirm_payment(cap, transition["id"], now=1200)
    assert replay["activation_at"] == first["activation_at"]
    assert db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_entitlement_mutations WHERE account_id=? "
        "AND operation='LEGACY_COMMERCIAL_ALIGNMENT_GRACE'", (account_id,),
    ).fetchone()[0] == 1
    with pytest.raises(ManualPaymentError):
        db.manual_payments.edit_pending_record(
            cap, payment["id"], reason="forbidden edit", changes={"comment": "changed"}, now=1201,
        )
    with pytest.raises(ManualPaymentError):
        db.manual_payments.resolve_manual_review(
            cap, payment["id"], resolution_note="forbidden resolution", now=1201,
        )


def test_pre_confirmation_edit_and_cancel_are_allowed(db):
    account_id, cap = _legacy(db, expiry=4321, username="lct-before", tg=997901)
    payment = _payment(db, cap, account_id, tag="before")
    transition = db.legacy_commercial_transitions.create(
        cap, payment_record_id=payment["id"], reason="real paid transition", now=1000,
    )
    edited = db.manual_payments.edit_pending_record(
        cap, payment["id"], reason="correct payment evidence",
        changes={"comment": "verified receipt"}, now=1001,
    )
    assert edited["comment"] == "verified receipt"
    cancelled = db.legacy_commercial_transitions.cancel(
        cap, transition["id"], reason="payment was not received", now=1002,
    )
    assert cancelled["state"] == "CANCELLED"
    assert db.manual_payments.get_record(payment["id"])["status"] == "CANCELLED"


@pytest.mark.parametrize("column,value", [
    ("current_expiry", 9999),
    ("current_plan_version_id", -1),
])
def test_source_divergence_is_detected_before_device_mutation(db, column, value):
    account_id, cap = _legacy(db, expiry=3600, username=f"lct-diverge-{column}", tg=997902+len(column))
    payment = _payment(db, cap, account_id, tag=f"diverge-{column}")
    transition = db.legacy_commercial_transitions.create(cap, payment_record_id=payment["id"], reason="real paid transition", now=1000)
    transition = db.legacy_commercial_transitions.confirm_payment(cap, transition["id"], now=1000)
    if column == "current_plan_version_id":
        value = db._conn.execute("SELECT id FROM mgboost_plan_versions WHERE plan_code='BASIC'").fetchone()[0]
    db._conn.execute(f"UPDATE mgboost_subscriptions SET {column}=? WHERE account_id=?", (value, account_id))
    db._conn.commit()
    db.legacy_commercial_transitions.claim_due(worker_id="divergence-worker", now=transition["activation_at"])
    with pytest.raises(LegacyCommercialTransitionConflict):
        db.legacy_commercial_transitions.validate_due_source(transition["id"])


def test_atomic_apply_rolls_back_every_local_fact_on_injected_failure(db):
    account_id, cap = _legacy(db, expiry=3600, username="lct-atomic", tg=997950)
    payment = _payment(db, cap, account_id, tag="atomic")
    transition = db.legacy_commercial_transitions.create(cap, payment_record_id=payment["id"], reason="real paid transition", now=1000)
    transition = db.legacy_commercial_transitions.confirm_payment(cap, transition["id"], now=1000)
    db.legacy_commercial_transitions.claim_due(worker_id="atomic-worker", now=transition["activation_at"])
    db.legacy_commercial_transitions.assess_capacity(transition["id"], now=transition["activation_at"])
    before = db._conn.execute("SELECT current_plan_version_id,current_expiry FROM mgboost_subscriptions WHERE account_id=?", (account_id,)).fetchone()
    db._conn.execute(
        "CREATE TEMP TRIGGER inject_transition_failure BEFORE INSERT ON mgboost_manual_payment_applications "
        "BEGIN SELECT RAISE(ABORT,'injected failure'); END"
    )
    with pytest.raises(Exception, match="injected failure"):
        db.legacy_commercial_transitions.apply_ready(transition["id"], now=transition["activation_at"])
    after = db._conn.execute("SELECT current_plan_version_id,current_expiry FROM mgboost_subscriptions WHERE account_id=?", (account_id,)).fetchone()
    assert tuple(after) == tuple(before)
    assert db.legacy_commercial_transitions.get(transition["id"])["state"] == "READY_TO_APPLY"
    assert db.manual_payments.get_record(payment["id"])["status"] == "PENDING"


@pytest.mark.parametrize("plan,limited", [
    ("BASIC", False), ("BASIC_PLUS", False), ("BASIC_PRO", False),
    ("WL", True), ("EXTENDED", True), ("FAMILY", True),
])
def test_transition_baseline_created_only_for_surviving_limited_lineages(db, plan, limited):
    account_id, cap = _legacy(db, expiry=3600, username=f"lct-child-{plan.lower()}", tg=996000+len(plan))
    child = _add_child(db, account_id, suffix=plan.lower())
    identity_before = db._conn.execute(
        "SELECT uuid_verifier,uuid_masked FROM mgboost_child_user_intents WHERE id=?", (child["child_intent_id"],),
    ).fetchone()
    telegram_before = db._conn.execute(
        "SELECT telegram_id,role,provenance FROM mgboost_telegram_identities WHERE account_id=?", (account_id,),
    ).fetchall()
    accounts_before = db._conn.execute("SELECT COUNT(*) FROM mgboost_accounts").fetchone()[0]
    credentials_before = db._conn.execute(
        "SELECT id,token_hash,generation,status FROM mgboost_subscription_credentials WHERE account_id=?", (account_id,),
    ).fetchall()
    payment = _payment(db, cap, account_id, plan=plan, tag=f"child-{plan}")
    transition = db.legacy_commercial_transitions.create(cap, payment_record_id=payment["id"], reason="real paid transition", now=1000)
    transition = db.legacy_commercial_transitions.confirm_payment(cap, transition["id"], now=1000)
    db.legacy_commercial_transitions.claim_due(worker_id=f"plan-worker-{plan}", now=transition["activation_at"])
    db.legacy_commercial_transitions.validate_due_source(transition["id"])
    db.legacy_commercial_transitions.assess_capacity(transition["id"], now=transition["activation_at"])
    db.legacy_commercial_transitions.apply_ready(transition["id"], now=transition["activation_at"])
    rows = db._conn.execute(
        "SELECT child_intent_id,node_id FROM mgboost_wl_transition_baselines WHERE transition_id=? ORDER BY node_id",
        (transition["id"],),
    ).fetchall()
    assert [tuple(row) for row in rows] == ([(child["child_intent_id"], 4), (child["child_intent_id"], 7)] if limited else [])
    identity_after = db._conn.execute(
        "SELECT uuid_verifier,uuid_masked FROM mgboost_child_user_intents WHERE id=?", (child["child_intent_id"],),
    ).fetchone()
    assert tuple(identity_after) == tuple(identity_before)
    assert [tuple(row) for row in db._conn.execute(
        "SELECT telegram_id,role,provenance FROM mgboost_telegram_identities WHERE account_id=?", (account_id,),
    ).fetchall()] == [tuple(row) for row in telegram_before]
    assert db._conn.execute("SELECT COUNT(*) FROM mgboost_accounts").fetchone()[0] == accounts_before
    assert [tuple(row) for row in db._conn.execute(
        "SELECT id,token_hash,generation,status FROM mgboost_subscription_credentials WHERE account_id=?", (account_id,),
    ).fetchall()] == [tuple(row) for row in credentials_before]


def test_capacity_conflict_requires_explicit_generation_and_never_retires_early(db):
    account_id, cap = _legacy(
        db, expiry=3600, username="lct-selection", tg=996900,
        observed_device_count=4, approved_limit=4,
    )
    first = _add_child(db, account_id, suffix="selection-one")
    second = _add_child(db, account_id, suffix="selection-two", now=400)
    third = _add_child(db, account_id, suffix="selection-three", now=500)
    fourth = _add_child(db, account_id, suffix="selection-four", now=600)
    payment = _payment(db, cap, account_id, plan="BASIC", tag="selection")
    transition = db.legacy_commercial_transitions.create(cap, payment_record_id=payment["id"], reason="real paid transition", now=1000)
    transition = db.legacy_commercial_transitions.confirm_payment(cap, transition["id"], now=1000)
    assert transition["state"] == "SELECTION_REQUIRED"
    with pytest.raises(LegacyCommercialTransitionConflict):
        db.legacy_commercial_transitions.record_selection(
            cap, transition["id"], generation_ids=[first["slot"]["generation_id"], second["slot"]["generation_id"]],
            reason="no automatic choice", now=1100,
        )
    selected = db.legacy_commercial_transitions.record_selection(
        cap, transition["id"], generation_ids=[second["slot"]["generation_id"]],
        reason="operator selected excess device", now=1101,
    )
    assert selected["state"] == "SELECTION_RECORDED"
    states = db._conn.execute(
        "SELECT status FROM mgboost_device_slot_generations WHERE account_id=? ORDER BY id", (account_id,),
    ).fetchall()
    assert [row["status"] for row in states] == ["ACTIVE", "ACTIVE", "ACTIVE", "ACTIVE"]


def test_worker_units_are_single_30_second_hardened_driver():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    service = (root / "mgboost-legacy-commercial-transition.service").read_text()
    timer = (root / "mgboost-legacy-commercial-transition.timer").read_text()
    assert "Type=oneshot" in service
    assert "EnvironmentFile=/opt/MGBoost_Panel/.env" in service
    assert "NoNewPrivileges=true" in service and "ProtectSystem=strict" in service
    assert "ReadWritePaths=/opt/MGBoost_Panel/data" in service
    assert "OnUnitActiveSec=30s" in timer and "RandomizedDelaySec=0" in timer
    assert timer.count("Unit=mgboost-legacy-commercial-transition.service") == 1


def test_transition_migration_is_idempotent_and_foreign_keys_are_clean(db):
    from src.legacy_commercial_transition_schema import (
        MIGRATION_ID, SCHEMA_CHECKSUM, apply_legacy_commercial_transition_schema,
    )
    assert apply_legacy_commercial_transition_schema(db._conn, now=9999) is False
    marker = db._conn.execute(
        "SELECT schema_checksum FROM mgboost_schema_migrations WHERE migration_id=?", (MIGRATION_ID,),
    ).fetchone()
    assert marker[0] == SCHEMA_CHECKSUM
    assert db._conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_commercial_to_commercial_remains_outside_this_engine(db):
    account_id, cap = _legacy(db, expiry=3600, username="lct-no-switch", tg=994900)
    first_payment = _payment(db, cap, account_id, plan="BASIC", tag="no-switch-first")
    transition = db.legacy_commercial_transitions.create(cap, payment_record_id=first_payment["id"], reason="real paid transition", now=1000)
    transition = db.legacy_commercial_transitions.confirm_payment(cap, transition["id"], now=1000)
    db.legacy_commercial_transitions.claim_due(worker_id="no-switch-worker", now=transition["activation_at"])
    db.legacy_commercial_transitions.assess_capacity(transition["id"], now=transition["activation_at"])
    db.legacy_commercial_transitions.apply_ready(transition["id"], now=transition["activation_at"])
    second_payment = _payment(db, cap, account_id, plan="BASIC", tag="no-switch-second")
    with pytest.raises(LegacyCommercialTransitionError, match="only active LEGACY_PAID_COMPAT"):
        db.legacy_commercial_transitions.create(
            cap, payment_record_id=second_payment["id"], reason="commercial switching forbidden", now=transition["activation_at"]+1,
        )


def test_worker_claim_is_exclusive_and_recoverable_after_lease_expiry(db):
    account_id, cap = _legacy(db, expiry=3600, username="lct-lease", tg=994901)
    payment = _payment(db, cap, account_id, tag="lease")
    transition = db.legacy_commercial_transitions.create(cap, payment_record_id=payment["id"], reason="real paid transition", now=1000)
    transition = db.legacy_commercial_transitions.confirm_payment(cap, transition["id"], now=1000)
    first = db.legacy_commercial_transitions.claim_due(
        worker_id="worker-first", now=transition["activation_at"], lease_seconds=30,
    )
    concurrent = db.legacy_commercial_transitions.claim_due(
        worker_id="worker-second", now=transition["activation_at"], lease_seconds=30,
    )
    recovered = db.legacy_commercial_transitions.claim_due(
        worker_id="worker-second", now=transition["activation_at"]+31, lease_seconds=30,
    )
    assert [row["id"] for row in first] == [transition["id"]]
    assert concurrent == []
    assert [row["id"] for row in recovered] == [transition["id"]]
    with pytest.raises(LegacyCommercialTransitionConflict):
        db.legacy_commercial_transitions.assess_capacity(
            transition["id"], now=transition["activation_at"]+31, worker_id="worker-first",
        )


def test_manual_review_requires_explicit_audited_retry_and_never_regrants_grace(db):
    account_id, cap = _legacy(db, expiry=3600, username="lct-review", tg=994902)
    payment = _payment(db, cap, account_id, tag="review")
    transition = db.legacy_commercial_transitions.create(cap, payment_record_id=payment["id"], reason="real paid transition", now=1000)
    transition = db.legacy_commercial_transitions.confirm_payment(cap, transition["id"], now=1000)
    db.legacy_commercial_transitions.claim_due(worker_id="review-worker", now=transition["activation_at"])
    db.legacy_commercial_transitions.manual_review(transition["id"], reason="RemoteStateMismatch", now=transition["activation_at"])
    with pytest.raises(LegacyCommercialTransitionError):
        db.legacy_commercial_transitions.retry_manual_review(cap, transition["id"], reason="short", now=transition["activation_at"]+1)
    retried = db.legacy_commercial_transitions.retry_manual_review(
        cap, transition["id"], reason="remote state was authoritatively verified", now=transition["activation_at"]+2,
    )
    assert retried["state"] == "SCHEDULED"
    assert db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_entitlement_mutations WHERE account_id=? "
        "AND operation='LEGACY_COMMERCIAL_ALIGNMENT_GRACE'", (account_id,),
    ).fetchone()[0] == 1
    assert db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_legacy_commercial_transition_events "
        "WHERE transition_id=? AND event_type='MANUAL_REVIEW_RETRY'", (transition["id"],),
    ).fetchone()[0] == 1
