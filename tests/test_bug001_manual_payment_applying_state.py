"""BUG-001 fix regression coverage: durable APPLYING freeze for manual payments.

See `BUGS.md` BUG-001 and `src/manual_payment_schema_v2.py` for the full root
cause / fix rationale. Before this fix, `mgboost_manual_payment_records`
only distinguished PENDING/APPLIED/CANCELLED/MANUAL_REVIEW; applying spans
at least two independently committing transactions (the canonical
entitlement/renewal or package-grant mutation, then this store's own
bookkeeping), and a crash in between left the record durably PENDING while
the entitlement had already, irreversibly, been granted -- `cancel_record`/
`edit_pending_record` only checked for APPLIED, so the record could be
cancelled or edited as though nothing had happened.

The fix adds a durable `APPLYING` freeze, committed *before* any entitlement
mutation is attempted (`ManualPaymentStore.apply_record`). This module is
narrowly scoped to BUG-001 only -- it does not touch BUG-002 (already
reclassified/fixed), BUG-003, BUG-004 (already fixed), BUG-005, promo
chronology, WL package enforcement, account/device architecture, the Stars
flow, admin UI, or any other roadmap phase.
"""

from __future__ import annotations

import importlib
import os
import sqlite3
import tempfile

import pytest

import src.manual_payment as manual_payment_module
from src.manual_payment import ApplyRequiresManualReview, ManualPaymentError
from src.plan_catalog import RUB_PRICES
from src.security import AdminSessionStore

PRIMARY = "owner:mgboost-primary:v1"
PRIMARY_LOGIN = "authenticated-primary-login"


@pytest.fixture
def db(monkeypatch):
    tmp = tempfile.mkdtemp(prefix="bug001-test-")
    monkeypatch.setenv("DATA_DIR", tmp)
    monkeypatch.setenv("PRIMARY_MGBOOST_ADMIN_ACTOR_ID", PRIMARY)
    monkeypatch.setenv("PRIMARY_MGBOOST_ADMIN_LOGIN", PRIMARY_LOGIN)
    import src.config as config
    import src.database as database
    importlib.reload(config)
    importlib.reload(database)
    database.DB_PATH = os.path.join(tmp, "db.sqlite3")
    instance = database.Database()
    from src.plan_catalog import seed_plan_catalog
    seed_plan_catalog(instance.plan_catalog, now=1)
    yield instance
    instance._conn.close()


def _capability(db):
    _raw, session = AdminSessionStore().create(PRIMARY_LOGIN, "test-server-jwt")
    return db.primary_admin_authority.authorize_session(session)


def _account(db):
    return db.accounts.create_account("DIRECT", now=1)


def _record(db, capability, account_id, *, plan="WL", days=30, ref="bug001-ref-000001", key=None, now=100):
    key = key or f"bug001-key-{ref}"
    return db.manual_payments.create_record(
        capability, account_id=account_id, plan_code=plan, duration_days=days,
        external_reference=ref, recorded_amount_minor=RUB_PRICES[(plan, days)],
        payment_method="bank_transfer", idempotency_key=key, now=now,
    )


def _status(db, record_id) -> str:
    return db.manual_payments.get_record(record_id)["status"]


# --- 1. normal apply: PENDING -> APPLYING -> APPLIED, exactly once --------

def test_normal_apply_transitions_through_applying_exactly_once(db):
    cap = _capability(db)
    account = _account(db)
    record = _record(db, cap, account["id"])
    assert record["status"] == "PENDING"
    result = db.manual_payments.apply_record(cap, record["id"], now=200)
    assert result["already_applied"] is False
    assert _status(db, record["id"]) == "APPLIED"
    term_count = db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_subscription_terms WHERE account_id=?", (account["id"],),
    ).fetchone()[0]
    assert term_count == 1
    mutation_count = db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_entitlement_mutations WHERE account_id=?", (account["id"],),
    ).fetchone()[0]
    assert mutation_count == 1


# --- 2. crash BEFORE the durable freeze: cancel is still legitimately -----
#        allowed after restart, nothing was ever attempted.

def test_crash_before_freeze_leaves_record_cancellable(db, monkeypatch):
    cap = _capability(db)
    account = _account(db)
    record = _record(db, cap, account["id"])

    def exploding_reject_check(*args, **kwargs):
        raise RuntimeError("simulated crash before the freeze transition commits")

    monkeypatch.setattr(
        db.manual_payments, "_reject_confirmed_transition_payment_locked", exploding_reject_check,
    )
    with pytest.raises(RuntimeError, match="before the freeze"):
        db.manual_payments.apply_record(cap, record["id"], now=200)
    monkeypatch.undo()

    # Nothing committed -- the record is exactly as it was created.
    assert _status(db, record["id"]) == "PENDING"
    cancelled = db.manual_payments.cancel_record(cap, record["id"], reason="never applied", now=201)
    assert cancelled["status"] == "CANCELLED"
    mutation_count = db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_entitlement_mutations WHERE account_id=?", (account["id"],),
    ).fetchone()[0]
    assert mutation_count == 0


# --- 3. crash AFTER freeze, BEFORE the entitlement mutation is attempted --

def test_crash_after_freeze_before_entitlement_mutation_retries_cleanly(db, monkeypatch):
    cap = _capability(db)
    account = _account(db)
    record = _record(db, cap, account["id"])

    def exploding_renewal(*args, **kwargs):
        raise RuntimeError("simulated crash before apply_same_plan_purchase ever ran")

    monkeypatch.setattr(db.subscription_renewal, "apply_same_plan_purchase", exploding_renewal)
    with pytest.raises(RuntimeError, match="ever ran"):
        db.manual_payments.apply_record(cap, record["id"], now=200)
    monkeypatch.undo()

    # Frozen durably, exactly as the fix intends -- and no entitlement
    # mutation was ever attempted, so cancel/edit are both refused anyway
    # (unknown outcome => fail closed), but a retry proceeds cleanly.
    assert _status(db, record["id"]) == "APPLYING"
    mutation_count_before = db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_entitlement_mutations WHERE account_id=?", (account["id"],),
    ).fetchone()[0]
    assert mutation_count_before == 0
    with pytest.raises(ManualPaymentError, match="currently applying"):
        db.manual_payments.cancel_record(cap, record["id"], reason="must be refused", now=201)

    result = db.manual_payments.apply_record(cap, record["id"], now=210)
    assert result["already_applied"] is False
    assert _status(db, record["id"]) == "APPLIED"
    mutation_count_after = db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_entitlement_mutations WHERE account_id=?", (account["id"],),
    ).fetchone()[0]
    assert mutation_count_after == 1


# --- 4. crash AFTER entitlement COMMIT, BEFORE payment bookkeeping --------
#        (the original confirmed BUG-001 scenario) -- also covered with a
#        restart in tests/test_manual_payment_ph509.py; this variant adds a
#        real Database close/reopen and the WL_PACKAGE-kind path is not
#        duplicated here since it shares the exact same `apply_record`
#        freeze code path (kind-independent).

def test_crash_after_entitlement_commit_survives_restart_without_contradiction(db, monkeypatch):
    cap = _capability(db)
    account = _account(db)
    record = _record(db, cap, account["id"], plan="BASIC", days=30, ref="bug001-restart-1")
    original = manual_payment_module.calculate_effective_entitlement

    def crashing_proof(*args, **kwargs):
        raise RuntimeError("simulated crash after the canonical renewal committed")

    monkeypatch.setattr(manual_payment_module, "calculate_effective_entitlement", crashing_proof)
    with pytest.raises(RuntimeError, match="crash after the canonical renewal"):
        db.manual_payments.apply_record(cap, record["id"], now=200)
    monkeypatch.undo()
    db._conn.close()

    import src.database as database
    reopened = database.Database()
    try:
        cap2 = _capability(reopened)
        mid = reopened.manual_payments.get_record(record["id"])
        assert mid["status"] == "APPLYING"
        # The contradiction BUG-001 reported: cancel must never turn this
        # into CANCELLED while the subscription is already ACTIVE/renewed.
        with pytest.raises(ManualPaymentError, match="currently applying"):
            reopened.manual_payments.cancel_record(cap2, record["id"], reason="must be refused", now=201)
        with pytest.raises(ManualPaymentError, match="can no longer be edited"):
            reopened.manual_payments.edit_pending_record(
                cap2, record["id"], reason="must be refused too",
                changes={"comment": "no"}, now=201,
            )
        subscription = reopened._conn.execute(
            "SELECT status,current_expiry FROM mgboost_subscriptions WHERE account_id=?",
            (account["id"],),
        ).fetchone()
        assert subscription["status"] == "ACTIVE"
        assert subscription["current_expiry"] == 200 + 30 * 86400

        recovered = reopened.manual_payments.apply_record(cap2, record["id"], now=210)
        assert recovered["already_applied"] is True
        assert reopened.manual_payments.get_record(record["id"])["status"] == "APPLIED"
        term_count = reopened._conn.execute(
            "SELECT COUNT(*) FROM mgboost_subscription_terms WHERE account_id=?", (account["id"],),
        ).fetchone()[0]
        assert term_count == 1
    finally:
        reopened._conn.close()


# --- 6. two independent SQLite connections: a held write lock blocks ------
#        rather than races or corrupts.

def test_second_connection_holding_a_write_lock_blocks_the_freeze(db):
    import src.database as database
    cap = _capability(db)
    account = _account(db)
    record = _record(db, cap, account["id"])  # fixture setup before the lock is taken

    second_conn = sqlite3.connect(database.DB_PATH, timeout=0.3)
    second_conn.execute("BEGIN IMMEDIATE")
    second_conn.execute(
        "UPDATE mgboost_manual_payment_records SET updated_at=updated_at WHERE id=?",
        (record["id"],),
    )
    try:
        with pytest.raises(sqlite3.OperationalError):
            db.manual_payments.apply_record(cap, record["id"], now=200)
    finally:
        second_conn.rollback()
        second_conn.close()
    # The blocked freeze attempt must not have partially applied anything.
    assert _status(db, record["id"]) == "PENDING"


# --- apply vs cancel: deterministic race outcome, whichever legitimately --
#     commits first wins, and the loser gets a clean, correct error.

def test_cancel_winning_the_race_is_reported_cleanly_not_as_an_error(db):
    cap = _capability(db)
    account = _account(db)
    record = _record(db, cap, account["id"])
    # Cancel commits first (a fully legitimate, real race outcome).
    db.manual_payments.cancel_record(cap, record["id"], reason="cancel wins the race", now=150)
    # Apply arrives second and must recognise the real current state instead
    # of raising a generic internal "freeze failed" error.
    with pytest.raises(ManualPaymentError, match="cancelled records are never applicable"):
        db.manual_payments.apply_record(cap, record["id"], now=200)
    assert _status(db, record["id"]) == "CANCELLED"


def test_apply_winning_the_race_makes_cancel_fail_closed(db):
    cap = _capability(db)
    account = _account(db)
    record = _record(db, cap, account["id"])
    result = db.manual_payments.apply_record(cap, record["id"], now=200)
    assert result["already_applied"] is False
    with pytest.raises(ManualPaymentError, match="applied manual payment is immutable"):
        db.manual_payments.cancel_record(cap, record["id"], reason="too late", now=201)


# --- 7. apply vs edit race -------------------------------------------------

def test_edit_after_apply_has_frozen_the_record_is_refused(db):
    cap = _capability(db)
    account = _account(db)
    record = _record(db, cap, account["id"])
    db.manual_payments.apply_record(cap, record["id"], now=200)
    with pytest.raises(ManualPaymentError, match="can no longer be edited"):
        db.manual_payments.edit_pending_record(
            cap, record["id"], reason="too late to edit",
            changes={"comment": "attempted"}, now=201,
        )


def test_edit_before_apply_is_reflected_by_the_subsequent_apply(db):
    cap = _capability(db)
    account = _account(db)
    record = _record(db, cap, account["id"], plan="WL", days=30)
    edited = db.manual_payments.edit_pending_record(
        cap, record["id"], reason="correct the plan before applying",
        changes={
            "plan_code": "BASIC", "duration_days": 30,
            "recorded_amount_minor": RUB_PRICES[("BASIC", 30)],
        },
        now=150,
    )
    assert edited["plan_code_snapshot"] == "BASIC"
    result = db.manual_payments.apply_record(cap, record["id"], now=200)
    assert result["already_applied"] is False
    entitlement = result["entitlement"]
    assert entitlement["plan"]["code"] == "BASIC"


# --- 8. repeated apply/retry: exactly-once entitlement effect --------------

def test_repeated_retries_never_apply_the_entitlement_more_than_once(db):
    cap = _capability(db)
    account = _account(db)
    record = _record(db, cap, account["id"])
    results = [db.manual_payments.apply_record(cap, record["id"], now=200 + i) for i in range(5)]
    assert [r["already_applied"] for r in results] == [False, True, True, True, True]
    assert len({r["new_expiry"] for r in results}) == 1
    mutation_count = db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_entitlement_mutations WHERE account_id=?", (account["id"],),
    ).fetchone()[0]
    assert mutation_count == 1
    application_count = db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_manual_payment_applications WHERE payment_record_id=?",
        (record["id"],),
    ).fetchone()[0]
    assert application_count == 1


# --- 9. a genuine failure BEFORE any entitlement mutation must not leave --
#        the payment stuck "applied" or unresolvable -- MANUAL_REVIEW, and
#        still safely cancellable, since nothing was ever committed.

def test_validation_failure_before_any_mutation_lands_in_reviewable_not_stuck_state(db):
    cap = _capability(db)
    account = _account(db)
    # Buy WL first for real, then create a manual record for a DIFFERENT
    # plan -- apply_same_plan_purchase raises PlanMismatch before any commit.
    db.subscription_renewal.apply_same_plan_purchase(
        account_id=account["id"], plan_code="WL", duration_days=30,
        payment_channel="TELEGRAM_STARS", mutation_source="DIRECT_PURCHASE",
        actor_type="TELEGRAM", actor_ref="900999",
        idempotency_key="bug001-fixture-wl-30", now=10,
    )
    record = _record(db, cap, account["id"], plan="BASIC", days=30, ref="bug001-mismatch-1")
    with pytest.raises(ApplyRequiresManualReview):
        db.manual_payments.apply_record(cap, record["id"], now=200)
    assert _status(db, record["id"]) == "MANUAL_REVIEW"
    mutation_count = db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_entitlement_mutations WHERE account_id=? "
        "AND mutation_source='MANUAL_PAYMENT'", (account["id"],),
    ).fetchone()[0]
    assert mutation_count == 0
    # Nothing was ever committed for this record -- it is safe to cancel it
    # outright rather than forcing a resolve-then-cancel detour.
    cancelled = db.manual_payments.cancel_record(cap, record["id"], reason="mismatched plan", now=201)
    assert cancelled["status"] == "CANCELLED"


# --- 10. malformed/duplicate requests must not perturb the state machine --

def test_apply_on_unknown_record_id_changes_nothing(db):
    cap = _capability(db)
    with pytest.raises(ManualPaymentError, match="not found"):
        db.manual_payments.apply_record(cap, 999999, now=200)
    assert db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_manual_payment_records"
    ).fetchone()[0] == 0


def test_double_cancel_is_refused_not_silently_repeated(db):
    cap = _capability(db)
    account = _account(db)
    record = _record(db, cap, account["id"])
    db.manual_payments.cancel_record(cap, record["id"], reason="first cancel", now=150)
    with pytest.raises(ManualPaymentError, match="already cancelled"):
        db.manual_payments.cancel_record(cap, record["id"], reason="second cancel", now=151)
    edits = db.manual_payments.edit_history(record["id"])
    assert len([e for e in edits if e["edit_kind"] == "CANCEL"]) == 1
