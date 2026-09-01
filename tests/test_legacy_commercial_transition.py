"""Focused money/entitlement lifecycle for legacy->commercial transitions."""
import pytest

from src.broker_operations import BrokerOperations
from src.child_contract import source_contract_hash
from src.manual_payment import ManualPaymentError
from src.legacy_commercial_transition import (
    LegacyCommercialTransitionConflict, LegacyCommercialTransitionError,
    LegacyCommercialTransitionLeaseLost,
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
    current = db.legacy_commercial_transitions.get(transition["id"])
    assert current["revision"] == transition["revision"] + 1
    assert db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_legacy_commercial_transition_events "
        "WHERE transition_id=? AND event_type='CANCELLED'", (transition["id"],),
    ).fetchone()[0] == 1


def test_generic_payment_cancel_has_same_single_transition_audit(db):
    account_id, cap = _legacy(db, expiry=4321, username="lct-generic-cancel", tg=997911)
    payment = _payment(db, cap, account_id, tag="generic-cancel")
    transition = db.legacy_commercial_transitions.create(
        cap, payment_record_id=payment["id"], reason="real paid transition", now=1000,
    )
    db.manual_payments.cancel_record(
        cap, payment["id"], reason="payment was not received", now=1001,
    )
    current = db.legacy_commercial_transitions.get(transition["id"])
    assert current["state"] == "CANCELLED"
    assert current["revision"] == transition["revision"] + 1
    assert db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_legacy_commercial_transition_events "
        "WHERE transition_id=? AND event_type='CANCELLED'", (transition["id"],),
    ).fetchone()[0] == 1
    with pytest.raises(ManualPaymentError):
        db.manual_payments.cancel_record(
            cap, payment["id"], reason="duplicate cancellation replay", now=1002,
        )


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


@pytest.mark.parametrize("column,temporary", [
    ("current_expiry", 9999),
    ("status", "DISABLED"),
    ("current_plan_version_id", None),
])
def test_source_aba_is_detected_by_post_confirmation_row_version(db, column, temporary):
    account_id, cap = _legacy(db, expiry=3600, username=f"lct-aba-{column}", tg=997930+len(column))
    payment = _payment(db, cap, account_id, tag=f"aba-{column}")
    transition = db.legacy_commercial_transitions.create(
        cap, payment_record_id=payment["id"], reason="real paid transition", now=1000,
    )
    transition = db.legacy_commercial_transitions.confirm_payment(cap, transition["id"], now=1000)
    original = db._conn.execute(
        f"SELECT {column} FROM mgboost_subscriptions WHERE id=?",
        (transition["source_subscription_id"],),
    ).fetchone()[0]
    if column == "current_plan_version_id":
        temporary = db._conn.execute(
            "SELECT id FROM mgboost_plan_versions WHERE plan_code='BASIC'",
        ).fetchone()[0]
    db._conn.execute(
        f"UPDATE mgboost_subscriptions SET {column}=?,row_version=row_version+1 WHERE id=?",
        (temporary, transition["source_subscription_id"]),
    )
    db._conn.execute(
        f"UPDATE mgboost_subscriptions SET {column}=?,row_version=row_version+1 WHERE id=?",
        (original, transition["source_subscription_id"]),
    )
    db._conn.commit()
    db.legacy_commercial_transitions.claim_due(worker_id="aba-worker", now=transition["activation_at"])
    with pytest.raises(LegacyCommercialTransitionConflict, match="diverged"):
        db.legacy_commercial_transitions.validate_due_source(transition["id"])


def test_replacement_subscription_with_same_values_is_rejected(db):
    account_id, cap = _legacy(db, expiry=3600, username="lct-replacement", tg=997949)
    payment = _payment(db, cap, account_id, tag="replacement")
    transition = db.legacy_commercial_transitions.create(
        cap, payment_record_id=payment["id"], reason="real paid transition", now=1000,
    )
    transition = db.legacy_commercial_transitions.confirm_payment(cap, transition["id"], now=1000)
    source = db._conn.execute(
        "SELECT * FROM mgboost_subscriptions WHERE id=?", (transition["source_subscription_id"],),
    ).fetchone()
    db._conn.execute(
        "UPDATE mgboost_subscriptions SET status='CANCELLED',row_version=row_version+1 WHERE id=?",
        (source["id"],),
    )
    db._conn.execute(
        "INSERT INTO mgboost_subscriptions "
        "(account_id,current_plan_version_id,status,started_at,current_expiry,created_at,updated_at,row_version) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (account_id, source["current_plan_version_id"], "ACTIVE", source["started_at"],
         source["current_expiry"], 2000, 2000, source["row_version"]),
    )
    db._conn.commit()
    db.legacy_commercial_transitions.claim_due(worker_id="replacement-worker", now=transition["activation_at"])
    with pytest.raises(LegacyCommercialTransitionConflict, match="diverged"):
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


@pytest.mark.parametrize("stage,trigger_sql", [
    ("subscription", "BEFORE UPDATE ON mgboost_subscriptions WHEN NEW.current_plan_version_id!=OLD.current_plan_version_id"),
    ("mutation", "BEFORE INSERT ON mgboost_entitlement_mutations WHEN NEW.operation='LEGACY_COMMERCIAL_TRANSITION'"),
    ("term", "BEFORE INSERT ON mgboost_subscription_terms"),
    ("period", "BEFORE INSERT ON mgboost_wl_periods"),
    ("baseline", "BEFORE INSERT ON mgboost_wl_transition_baselines"),
    ("application", "BEFORE INSERT ON mgboost_manual_payment_applications"),
    ("payment", "BEFORE UPDATE ON mgboost_manual_payment_records WHEN NEW.status='APPLIED'"),
    ("sync", "BEFORE INSERT ON mgboost_manual_payment_sync_jobs"),
    ("transition", "BEFORE UPDATE ON mgboost_legacy_commercial_transitions WHEN NEW.state='APPLIED'"),
    ("event", "BEFORE INSERT ON mgboost_legacy_commercial_transition_events WHEN NEW.event_type='APPLIED'"),
])
def test_atomic_apply_failure_matrix_rolls_back_logical_stages(db, stage, trigger_sql):
    account_id, cap = _legacy(
        db, expiry=3600, username=f"lct-atomic-{stage}", tg=998100+len(stage),
    )
    _add_child(db, account_id, suffix=f"atomic-{stage}")
    payment = _payment(db, cap, account_id, plan="WL", tag=f"atomic-{stage}")
    transition = db.legacy_commercial_transitions.create(
        cap, payment_record_id=payment["id"], reason="real paid transition", now=1000,
    )
    transition = db.legacy_commercial_transitions.confirm_payment(cap, transition["id"], now=1000)
    db.legacy_commercial_transitions.claim_due(worker_id="atomic-matrix-worker", now=transition["activation_at"])
    db.legacy_commercial_transitions.assess_capacity(transition["id"], now=transition["activation_at"])
    before_sub = tuple(db._conn.execute(
        "SELECT current_plan_version_id,status,current_expiry,row_version FROM mgboost_subscriptions WHERE id=?",
        (transition["source_subscription_id"],),
    ).fetchone())
    before_counts = {
        table: db._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in (
            "mgboost_entitlement_mutations", "mgboost_subscription_terms", "mgboost_wl_periods",
            "mgboost_wl_transition_baselines", "mgboost_manual_payment_applications",
            "mgboost_manual_payment_sync_jobs",
        )
    }
    db._conn.execute(
        f"CREATE TEMP TRIGGER inject_{stage} {trigger_sql} "
        "BEGIN SELECT RAISE(ABORT,'injected logical stage failure'); END"
    )
    with pytest.raises(Exception, match="injected logical stage failure"):
        db.legacy_commercial_transitions.apply_ready(transition["id"], now=transition["activation_at"])
    assert tuple(db._conn.execute(
        "SELECT current_plan_version_id,status,current_expiry,row_version FROM mgboost_subscriptions WHERE id=?",
        (transition["source_subscription_id"],),
    ).fetchone()) == before_sub
    assert db.manual_payments.get_record(payment["id"])["status"] == "PENDING"
    assert db.legacy_commercial_transitions.get(transition["id"])["state"] == "READY_TO_APPLY"
    for table, count in before_counts.items():
        assert db._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == count


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


@pytest.mark.parametrize("shape", [
    "revoked", "missing", "observed_unknown", "observed_error", "observed_not_created",
])
def test_limited_apply_rejects_unproven_surviving_lineage(db, shape):
    """apply_ready's surviving-lineage gate is an explicit allowlist
    (_AUTHORITATIVE_CHILD_STATES = ACTIVE/DISABLED only) -- every other
    state either CHECK constraint currently permits must still be rejected,
    covering both desired_state (REVOKED) and every non-allowlisted
    observed_state (UNKNOWN, ERROR, NOT_CREATED; REVOKED covered by the
    'revoked' shape) plus a slot with no child at all."""
    account_id, cap = _legacy(db, expiry=3600, username=f"lct-survivor-{shape}", tg=996700+len(shape))
    if shape == "missing":
        db.device_slots.claim(account_id, "missing-child-hwid", HWID_KEY, now=300)
    else:
        child = _add_child(db, account_id, suffix=shape)
        observed = {
            "revoked": "REVOKED", "observed_unknown": "UNKNOWN",
            "observed_error": "ERROR", "observed_not_created": "NOT_CREATED",
        }[shape]
        desired = "REVOKED" if shape == "revoked" else "ACTIVE"
        db._conn.execute(
            "UPDATE mgboost_child_user_intents SET observed_state=?,desired_state=? WHERE id=?",
            (observed, desired, child["child_intent_id"]),
        )
    db._conn.commit()
    payment = _payment(db, cap, account_id, plan="WL", tag=f"survivor-{shape}")
    transition = db.legacy_commercial_transitions.create(
        cap, payment_record_id=payment["id"], reason="real paid transition", now=1000,
    )
    transition = db.legacy_commercial_transitions.confirm_payment(cap, transition["id"], now=1000)
    db.legacy_commercial_transitions.claim_due(worker_id="survivor-worker", now=transition["activation_at"])
    db.legacy_commercial_transitions.assess_capacity(transition["id"], now=transition["activation_at"])
    with pytest.raises(LegacyCommercialTransitionConflict, match="surviving child lineage"):
        db.legacy_commercial_transitions.apply_ready(transition["id"], now=transition["activation_at"])
    assert db.manual_payments.get_record(payment["id"])["status"] == "PENDING"
    assert db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_wl_transition_baselines WHERE transition_id=?",
        (transition["id"],),
    ).fetchone()[0] == 0


@pytest.mark.parametrize("state", ["ACTIVE", "DISABLED"])
def test_limited_apply_accepts_active_and_disabled_surviving_lineage(db, state):
    """DISABLED is the normal, reversible, already-confirmed-remote state of
    an existing current child (ParentSyncStore.acknowledge() writes it for
    both an administratively paused slot and a subscription that has simply
    expired) -- it must count as authoritative lineage exactly like ACTIVE."""
    account_id, cap = _legacy(db, expiry=3600, username=f"lct-ok-{state.lower()}", tg=996800+len(state))
    child = _add_child(db, account_id, suffix=state.lower())
    db._conn.execute(
        "UPDATE mgboost_child_user_intents SET observed_state=?,desired_state=? WHERE id=?",
        (state, state, child["child_intent_id"]),
    )
    db._conn.commit()
    payment = _payment(db, cap, account_id, plan="WL", tag=f"ok-{state}")
    transition = db.legacy_commercial_transitions.create(
        cap, payment_record_id=payment["id"], reason="real paid transition", now=1000,
    )
    transition = db.legacy_commercial_transitions.confirm_payment(cap, transition["id"], now=1000)
    db.legacy_commercial_transitions.claim_due(worker_id=f"ok-worker-{state}", now=transition["activation_at"])
    db.legacy_commercial_transitions.assess_capacity(transition["id"], now=transition["activation_at"])
    applied = db.legacy_commercial_transitions.apply_ready(transition["id"], now=transition["activation_at"])
    assert applied["state"] == "APPLIED"
    assert db.manual_payments.get_record(payment["id"])["status"] == "APPLIED"


def test_authoritative_child_state_allowlist_matches_known_schema_enum(db):
    """Guard against silently widening either CHECK constraint without a
    conscious decision about apply_ready's allowlist: if a future migration
    adds a new desired_state/observed_state value, this assertion breaks
    loudly instead of that new value defaulting to trusted."""
    import re
    from src.legacy_commercial_transition import _AUTHORITATIVE_CHILD_STATES
    assert set(_AUTHORITATIVE_CHILD_STATES) == {"ACTIVE", "DISABLED"}
    schema = db._conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='mgboost_child_user_intents'",
    ).fetchone()["sql"]

    def _enum(column):
        match = re.search(rf"{column}\b[^(]*IN\s*\(([^)]*)\)", schema)
        return {value.strip().strip("'") for value in match.group(1).split(",")}

    assert _enum("desired_state") == {"ACTIVE", "DISABLED", "REVOKED"}
    assert _enum("observed_state") == {
        "NOT_CREATED", "ACTIVE", "DISABLED", "REVOKED", "UNKNOWN", "ERROR",
    }


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


def _selected_retirement_fixture(db, suffix):
    account_id, cap = _legacy(
        db, expiry=3600, username=f"lct-retire-{suffix}", tg=996910+len(suffix),
        observed_device_count=4, approved_limit=6,
    )
    children = [_add_child(db, account_id, suffix=f"{suffix}-{index}", now=300+index*20)
                for index in range(4)]
    payment = _payment(db, cap, account_id, plan="BASIC", tag=f"retire-{suffix}")
    transition = db.legacy_commercial_transitions.create(
        cap, payment_record_id=payment["id"], reason="real paid transition", now=1000,
    )
    transition = db.legacy_commercial_transitions.confirm_payment(cap, transition["id"], now=1000)
    transition = db.legacy_commercial_transitions.record_selection(
        cap, transition["id"], generation_ids=[children[0]["slot"]["generation_id"]],
        reason="operator selected exact excess", now=1100,
    )
    claimed = db.legacy_commercial_transitions.claim_due(
        worker_id=f"retire-worker-{suffix}", now=transition["activation_at"], lease_seconds=60,
    )[0]
    return account_id, cap, payment, transition, claimed, children


def test_revoke_applied_crash_recovers_free_and_transition_apply(db):
    from src.child_lifecycle import process_free, process_revoke
    account_id, _cap, payment, transition, claimed, children = _selected_retirement_fixture(db, "revoke-crash")
    worker = "retire-worker-revoke-crash"
    selected = db.legacy_commercial_transitions.validate_retirement_topology(
        transition["id"], worker_id=worker, expected_revision=claimed["revision"],
        now=transition["activation_at"],
    )[0]
    revoke = db.child_lifecycle.prepare_revoke(
        account_id=account_id, old_child_intent_id=selected["selected_child_id"],
        reason="legacy commercial selected retirement",
        idempotency_key=f"legacy-transition-revoke:{transition['id']}:{selected['slot_generation_id']}",
        now=transition["activation_at"],
    )
    process_revoke(
        db, revoke["operation_id"], worker_id=worker,
        revoke_fn=lambda _payload: {"outcome": "REVOKED"}, now=transition["activation_at"],
    )
    # Simulated crash: FREE was never prepared. Recovery must prove the exact
    # selection/revoke binding and continue rather than classify it as stale.
    recovered = db.legacy_commercial_transitions.validate_retirement_topology(
        transition["id"], worker_id=worker, expected_revision=claimed["revision"],
        now=transition["activation_at"],
    )[0]
    assert recovered["revoke_state"] == "APPLIED"
    free = db.child_lifecycle.prepare_free(
        account_id=account_id, old_child_intent_id=recovered["selected_child_id"],
        reason="legacy commercial selected retirement",
        idempotency_key=f"legacy-transition-free:{transition['id']}:{recovered['slot_generation_id']}",
        now=transition["activation_at"],
    )
    process_free(db, free["operation_id"], worker_id=worker,
                 now=transition["activation_at"], strict_generation=True)
    db.legacy_commercial_transitions.assess_capacity(
        transition["id"], now=transition["activation_at"], worker_id=worker,
        expected_revision=claimed["revision"],
    )
    applied = db.legacy_commercial_transitions.apply_ready(
        transition["id"], now=transition["activation_at"],
    )
    assert applied["state"] == "APPLIED"
    assert db.manual_payments.get_record(payment["id"])["status"] == "APPLIED"


def test_free_release_crash_replay_is_proven(db):
    from src.child_lifecycle import process_free, process_revoke
    account_id, _cap, _payment_row, transition, _claimed, children = _selected_retirement_fixture(db, "free-crash")
    child = children[0]
    reason = "legacy commercial selected retirement"
    revoke = db.child_lifecycle.prepare_revoke(
        account_id=account_id, old_child_intent_id=child["child_intent_id"], reason=reason,
        idempotency_key=f"legacy-transition-revoke:{transition['id']}:{child['slot']['generation_id']}",
        now=transition["activation_at"],
    )
    process_revoke(db, revoke["operation_id"], worker_id="free-crash-worker",
                   revoke_fn=lambda _payload: {"outcome": "REVOKED"}, now=transition["activation_at"])
    free = db.child_lifecycle.prepare_free(
        account_id=account_id, old_child_intent_id=child["child_intent_id"], reason=reason,
        idempotency_key=f"legacy-transition-free:{transition['id']}:{child['slot']['generation_id']}",
        now=transition["activation_at"],
    )
    claimed_free = db.child_lifecycle.claim(
        free["operation_id"], worker_id="free-crash-worker", now=transition["activation_at"],
    )
    db.child_lifecycle.apply_free(
        free["operation_id"], worker_id="free-crash-worker", now=transition["activation_at"],
    )
    generation = db._conn.execute(
        "SELECT generation FROM mgboost_device_slot_generations WHERE id=?",
        (claimed_free["old_slot_generation_id"],),
    ).fetchone()[0]
    db.device_slots.release(account_id, claimed_free["slot_id"], generation,
                            reason=reason, now=transition["activation_at"])
    recovered = process_free(
        db, free["operation_id"], worker_id="free-crash-recovery",
        now=transition["activation_at"]+31, strict_generation=True,
    )
    assert recovered["state"] == "APPLIED"


def test_free_release_crash_recovery_rejects_real_rebind(db):
    from src.child_lifecycle import process_free, process_revoke
    from src.device_slots import StaleSlotGeneration
    account_id, _cap, _payment_row, transition, _claimed, children = _selected_retirement_fixture(db, "free-rebind")
    child = children[0]
    reason = "legacy commercial selected retirement"
    revoke = db.child_lifecycle.prepare_revoke(
        account_id=account_id, old_child_intent_id=child["child_intent_id"], reason=reason,
        idempotency_key=f"legacy-transition-revoke:{transition['id']}:{child['slot']['generation_id']}",
        now=transition["activation_at"],
    )
    process_revoke(db, revoke["operation_id"], worker_id="free-rebind-worker",
                   revoke_fn=lambda _payload: {"outcome": "REVOKED"}, now=transition["activation_at"])
    free = db.child_lifecycle.prepare_free(
        account_id=account_id, old_child_intent_id=child["child_intent_id"], reason=reason,
        idempotency_key=f"legacy-transition-free:{transition['id']}:{child['slot']['generation_id']}",
        now=transition["activation_at"],
    )
    claimed_free = db.child_lifecycle.claim(
        free["operation_id"], worker_id="free-rebind-worker", now=transition["activation_at"],
    )
    db.child_lifecycle.apply_free(
        free["operation_id"], worker_id="free-rebind-worker", now=transition["activation_at"],
    )
    generation = db._conn.execute(
        "SELECT generation FROM mgboost_device_slot_generations WHERE id=?",
        (claimed_free["old_slot_generation_id"],),
    ).fetchone()[0]
    db.device_slots.release(account_id, claimed_free["slot_id"], generation,
                            reason=reason, now=transition["activation_at"])
    rebound = db.device_slots.claim(
        account_id, "free-rebind-new-hwid", HWID_KEY, now=transition["activation_at"]-1,
    )
    with pytest.raises(StaleSlotGeneration):
        process_free(db, free["operation_id"], worker_id="free-rebind-recovery",
                     now=transition["activation_at"]+31, strict_generation=True)
    assert rebound["generation_id"] != child["slot"]["generation_id"]


def test_pre_revoke_topology_shrink_and_growth_fail_before_mutation(db):
    from src.child_lifecycle import process_free, process_revoke
    for suffix, grow in (("shrink", False), ("grow", True)):
        account_id, _cap, _payment_row, transition, claimed, children = _selected_retirement_fixture(db, suffix)
        worker = f"retire-worker-{suffix}"
        if grow:
            _add_child(db, account_id, suffix=f"{suffix}-extra", now=1200)
        else:
            victim = children[-1]
            revoke = db.child_lifecycle.prepare_revoke(
                account_id=account_id, old_child_intent_id=victim["child_intent_id"],
                reason="independent device removal", idempotency_key=f"independent-revoke-{suffix}---",
                now=1200,
            )
            process_revoke(db, revoke["operation_id"], worker_id=f"independent-{suffix}",
                           revoke_fn=lambda _payload: {"outcome": "REVOKED"}, now=1200)
            free = db.child_lifecycle.prepare_free(
                account_id=account_id, old_child_intent_id=victim["child_intent_id"],
                reason="independent device removal", idempotency_key=f"independent-free-{suffix}---",
                now=1201,
            )
            process_free(db, free["operation_id"], worker_id=f"independent-{suffix}", now=1201)
        before = db._conn.execute(
            "SELECT COUNT(*) FROM mgboost_child_lifecycle_operations WHERE account_id=? "
            "AND operation_kind='REVOKE' AND old_child_intent_id=?",
            (account_id, children[0]["child_intent_id"]),
        ).fetchone()[0]
        with pytest.raises(LegacyCommercialTransitionConflict, match="capacity excess"):
            db.legacy_commercial_transitions.validate_retirement_topology(
                transition["id"], worker_id=worker, expected_revision=claimed["revision"],
                now=transition["activation_at"],
            )
        after = db._conn.execute(
            "SELECT COUNT(*) FROM mgboost_child_lifecycle_operations WHERE account_id=? "
            "AND operation_kind='REVOKE' AND old_child_intent_id=?",
            (account_id, children[0]["child_intent_id"]),
        ).fetchone()[0]
        assert after == before == 0


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


def test_worker_tick_activation_boundary_does_not_race_expiry_enforcement(db):
    """Incident 2026-09-01: activation_at is always aligned to the source
    subscription's own expiry, so at the moment a transition activates, that
    subscription is *also* simultaneously expiring. The worker's crash-
    recovery pre-pass used to run generic expiry-driven parent-sync for every
    not-yet-applied transition unconditionally -- including one becoming due
    this very tick -- which disabled its children (observed_state DISABLED)
    microseconds before apply_ready's surviving-lineage check ran, losing the
    race every single time and landing a routine, uncontested renewal in
    MANUAL_REVIEW. One real customer got stuck exactly like this in prod."""
    from src.legacy_commercial_transition_worker import run_worker_tick

    account_id, cap = _legacy(db, expiry=3600, username="lct-race", tg=996800)
    child = _add_child(db, account_id, suffix="race")
    payment = _payment(db, cap, account_id, plan="WL", days=60, tag="race")
    transition = db.legacy_commercial_transitions.create(
        cap, payment_record_id=payment["id"], reason="real paid transition", now=1000,
    )
    transition = db.legacy_commercial_transitions.confirm_payment(cap, transition["id"], now=1000)
    # The race only exists when activation coincides with the source
    # expiry -- confirm the fixture actually reproduces that boundary.
    assert transition["activation_at"] == 3600

    def sync_fn(payload):
        return {"outcome": "SYNCED"}

    result = run_worker_tick(
        db, sync_fn=sync_fn, revoke_fn=lambda _payload: {"outcome": "REVOKED"},
        now=transition["activation_at"], clock=lambda: transition["activation_at"],
        worker_prefix="race-test-worker",
    )
    assert result["manual_review"] == 0
    assert result["errors"] == []
    assert result["applied"] == 1

    final = db.legacy_commercial_transitions.get(transition["id"])
    assert final["state"] == "APPLIED"
    current = db._conn.execute(
        "SELECT p.plan_code,s.current_expiry FROM mgboost_subscriptions s "
        "JOIN mgboost_plan_versions p ON p.id=s.current_plan_version_id WHERE s.account_id=?",
        (account_id,),
    ).fetchone()
    assert current["plan_code"] == "WL"
    assert current["current_expiry"] == transition["target_expiry"]
    child_row = db._conn.execute(
        "SELECT desired_state,observed_state FROM mgboost_child_user_intents WHERE id=?",
        (child["child_intent_id"],),
    ).fetchone()
    assert child_row["desired_state"] == "ACTIVE"
    assert child_row["observed_state"] == "ACTIVE"


def test_manual_review_recovers_after_expiry_race_disabled_children_via_retry(db):
    """The account_id=3 incident recovery path, end to end: a transition
    already stuck in MANUAL_REVIEW with its children left DISABLED by the
    (now-fixed) race must still be recoverable through the one sanctioned
    channel -- retry_manual_review() plus the next worker tick -- now that
    apply_ready's surviving-lineage gate accepts DISABLED as authoritative.
    Also proves the recovery is exactly-once: one entitlement mutation, one
    payment application, the correct (not doubled) target expiry, and a
    second retry/tick afterward changes nothing further."""
    from src.legacy_commercial_transition_worker import run_worker_tick
    from src.parent_sync import run_account_sync_cycle

    account_id, cap = _legacy(db, expiry=3600, username="lct-recovery", tg=996810)
    child = _add_child(db, account_id, suffix="recovery")
    payment = _payment(db, cap, account_id, plan="WL", days=60, tag="recovery")
    transition = db.legacy_commercial_transitions.create(
        cap, payment_record_id=payment["id"], reason="real paid transition", now=1000,
    )
    transition = db.legacy_commercial_transitions.confirm_payment(cap, transition["id"], now=1000)
    assert transition["activation_at"] == 3600

    def sync_fn(payload):
        return {"outcome": "SYNCED"}

    # Reproduce the incident's exact starting shape directly (independent of
    # the worker's ordering fix, which only stops this from happening going
    # forward): the generic expiry-driven sync that used to race apply, then
    # the resulting MANUAL_REVIEW the real apply attempt landed in.
    run_account_sync_cycle(db, account_id, sync_fn=sync_fn, worker_id="incident-repro", now=3600)
    disabled = db._conn.execute(
        "SELECT desired_state,observed_state FROM mgboost_child_user_intents WHERE id=?",
        (child["child_intent_id"],),
    ).fetchone()
    assert disabled["desired_state"] == "DISABLED"
    assert disabled["observed_state"] == "DISABLED"
    db.legacy_commercial_transitions.manual_review(
        transition["id"], reason="LegacyCommercialTransitionConflict", now=3600,
    )
    assert db.legacy_commercial_transitions.get(transition["id"])["state"] == "MANUAL_REVIEW"
    assert db.manual_payments.get_record(payment["id"])["status"] == "PENDING"

    retried = db.legacy_commercial_transitions.retry_manual_review(
        cap, transition["id"], reason="incident recovery after code fix", now=3700,
    )
    assert retried["state"] == "SCHEDULED"

    result = run_worker_tick(
        db, sync_fn=sync_fn, revoke_fn=lambda _payload: {"outcome": "REVOKED"},
        now=3700, clock=lambda: 3700, worker_prefix="recovery-test",
    )
    assert result["manual_review"] == 0
    assert result["errors"] == []
    assert result["applied"] == 1

    final = db.legacy_commercial_transitions.get(transition["id"])
    assert final["state"] == "APPLIED"
    assert db.manual_payments.get_record(payment["id"])["status"] == "APPLIED"
    applications = db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_manual_payment_applications WHERE payment_record_id=?",
        (payment["id"],),
    ).fetchone()[0]
    assert applications == 1
    mutations = db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_entitlement_mutations WHERE subscription_id=? "
        "AND operation='LEGACY_COMMERCIAL_TRANSITION'", (transition["source_subscription_id"] or 0,),
    ).fetchone()[0]
    assert mutations == 1

    current = db._conn.execute(
        "SELECT p.plan_code,s.current_expiry FROM mgboost_subscriptions s "
        "JOIN mgboost_plan_versions p ON p.id=s.current_plan_version_id WHERE s.account_id=?",
        (account_id,),
    ).fetchone()
    assert current["plan_code"] == "WL"
    assert current["current_expiry"] == transition["target_expiry"]
    # 60 days anchored to the original activation boundary (3600) -- not
    # doubled and not re-anchored to the later retry/recovery tick (3700).
    assert transition["target_expiry"] == 3600 + 60 * 86400

    recovered = db._conn.execute(
        "SELECT desired_state,observed_state FROM mgboost_child_user_intents WHERE id=?",
        (child["child_intent_id"],),
    ).fetchone()
    assert recovered["desired_state"] == "ACTIVE"
    assert recovered["observed_state"] == "ACTIVE"

    # A second retry is rejected (only MANUAL_REVIEW transitions are
    # retryable) -- no way to re-enter the apply path and double-charge.
    with pytest.raises(LegacyCommercialTransitionConflict, match="not in manual review"):
        db.legacy_commercial_transitions.retry_manual_review(
            cap, transition["id"], reason="bogus second retry", now=3800,
        )
    # A second tick is a pure no-op for this already-APPLIED transition.
    second_tick = run_worker_tick(
        db, sync_fn=sync_fn, revoke_fn=lambda _payload: {"outcome": "REVOKED"},
        now=3800, clock=lambda: 3800, worker_prefix="recovery-test-2",
    )
    assert second_tick == {"assessed": 0, "applied": 0, "retired": 0, "manual_review": 0, "errors": []}
    assert db.manual_payments.get_record(payment["id"])["status"] == "APPLIED"
    assert db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_manual_payment_applications WHERE payment_record_id=?",
        (payment["id"],),
    ).fetchone()[0] == 1


def test_administratively_paused_child_keeps_pause_across_transition_apply(db):
    """A slot an operator paused (PH7-05 Disable) before the transition ever
    activated must stay paused after apply -- the transition renewing the
    account's entitlement must never resurrect it. Exercises the same
    surviving-lineage allowlist (the paused child is DISABLED at both the
    child-intent and slot level, just like the expiry-race case) plus
    enqueue_current_children's pre-existing per-slot-pause override, which
    the recovery fix relies on to keep the two cases visually identical at
    the child_user_intents level but distinct at the slot level."""
    from src.legacy_commercial_transition_worker import run_worker_tick
    from src.parent_sync import run_account_sync_cycle

    account_id, cap = _legacy(db, expiry=3600, username="lct-paused", tg=996820, approved_limit=3)
    active_child = _add_child(db, account_id, suffix="active-survivor", now=300)
    paused_child = _add_child(db, account_id, suffix="paused-survivor", now=310)
    paused_slot_number = paused_child["slot"]["slot_number"]

    def sync_fn(payload):
        return {"outcome": "SYNCED"}

    pause_result = db.device_slot_admin.set_paused(
        cap, account_id=account_id, slot_number=paused_slot_number, paused=True,
        reason="operator pause before renewal", idempotency_key="lct-pause-key-000000001", now=500,
    )
    assert pause_result["converged"] is False
    # Converge the pause to Marzban before the transition boundary, mirroring
    # a real operator pause that has already reached the remote side.
    run_account_sync_cycle(db, account_id, sync_fn=sync_fn, worker_id="pause-converge", now=500)
    paused_before = db._conn.execute(
        "SELECT desired_state,observed_state FROM mgboost_child_user_intents WHERE id=?",
        (paused_child["child_intent_id"],),
    ).fetchone()
    assert paused_before["desired_state"] == "DISABLED"
    assert paused_before["observed_state"] == "DISABLED"

    payment = _payment(db, cap, account_id, plan="WL", days=60, tag="paused-case")
    transition = db.legacy_commercial_transitions.create(
        cap, payment_record_id=payment["id"], reason="real paid transition", now=1000,
    )
    transition = db.legacy_commercial_transitions.confirm_payment(cap, transition["id"], now=1000)
    assert transition["activation_at"] == 3600

    result = run_worker_tick(
        db, sync_fn=sync_fn, revoke_fn=lambda _payload: {"outcome": "REVOKED"},
        now=3600, clock=lambda: 3600, worker_prefix="pause-test",
    )
    assert result["manual_review"] == 0
    assert result["errors"] == []
    assert result["applied"] == 1
    assert db.legacy_commercial_transitions.get(transition["id"])["state"] == "APPLIED"

    active_after = db._conn.execute(
        "SELECT desired_state,observed_state FROM mgboost_child_user_intents WHERE id=?",
        (active_child["child_intent_id"],),
    ).fetchone()
    assert active_after["desired_state"] == "ACTIVE"
    assert active_after["observed_state"] == "ACTIVE"

    paused_after = db._conn.execute(
        "SELECT desired_state,observed_state FROM mgboost_child_user_intents WHERE id=?",
        (paused_child["child_intent_id"],),
    ).fetchone()
    assert paused_after["desired_state"] == "DISABLED"
    assert paused_after["observed_state"] == "DISABLED"
    slot_after = db._conn.execute(
        "SELECT desired_state FROM mgboost_device_slots WHERE account_id=? AND slot_number=?",
        (account_id, paused_slot_number),
    ).fetchone()
    assert slot_after["desired_state"] == "DISABLED"


def test_transition_migration_is_idempotent_and_foreign_keys_are_clean(db):
    from src.legacy_commercial_transition_schema import (
        MIGRATION_ID, SCHEMA_CHECKSUM, apply_legacy_commercial_transition_schema,
    )
    assert apply_legacy_commercial_transition_schema(db._conn, now=9999) is False
    marker = db._conn.execute(
        "SELECT schema_checksum FROM mgboost_schema_migrations WHERE migration_id=?", (MIGRATION_ID,),
    ).fetchone()
    assert marker[0] == SCHEMA_CHECKSUM
    from src.legacy_commercial_transition_schema_v2 import (
        MIGRATION_ID as V2_MIGRATION_ID, SCHEMA_CHECKSUM as V2_SCHEMA_CHECKSUM,
        apply_legacy_commercial_transition_schema_v2,
    )
    assert apply_legacy_commercial_transition_schema_v2(db._conn, now=10000) is False
    assert db._conn.execute(
        "SELECT schema_checksum FROM mgboost_schema_migrations WHERE migration_id=?",
        (V2_MIGRATION_ID,),
    ).fetchone()[0] == V2_SCHEMA_CHECKSUM
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


def test_stolen_lease_fences_old_worker_before_next_destructive_stage(db):
    account_id, _cap, _payment_row, transition, claimed, children = _selected_retirement_fixture(db, "lease-steal")
    old_worker = "retire-worker-lease-steal"
    recovered = db.legacy_commercial_transitions.claim_due(
        worker_id="new-distinct-worker", now=transition["activation_at"]+61, lease_seconds=60,
    )[0]
    assert recovered["revision"] != claimed["revision"]
    with pytest.raises(LegacyCommercialTransitionLeaseLost):
        db.legacy_commercial_transitions.assert_lease(
            transition["id"], worker_id=old_worker, expected_revision=claimed["revision"],
            now=transition["activation_at"]+61,
        )
    assert db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_child_lifecycle_operations WHERE old_child_intent_id=?",
        (children[0]["child_intent_id"],),
    ).fetchone()[0] == 0


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
