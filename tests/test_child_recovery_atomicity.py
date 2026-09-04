"""Corrective/hardening pass: `repair_child_ensure`'s local completion
mutation and its mandatory immutable audit row must commit as ONE durable
transaction. Before this pass, `recovery_acknowledge()` committed the CAS
mutation and `_audit()` committed the ledger row as two separate
transactions -- a crash or write failure between them could leave the child
repaired with no durable evidence of who/why. This module proves the fixed
invariant: NO SUCCESSFUL RECOVERY WITHOUT DURABLE ACTOR+REASON+AUDIT
EVIDENCE, by failing the audit INSERT at the real sqlite transaction
boundary (not by mocking `_audit`/`_insert_audit_row_locked` away) and
checking that the whole transaction rolled back.
"""

import pytest

from src.child_recovery import ChildRecoveryError, repair_child_ensure

from tests.test_p0_legacy_wl_provisioning_hotfix import (  # noqa: F401
    _mutation_count,
    _outbox,
    _repaired_scenario,
    db,
)

REASON = "atomicity regression: audit write must not be separable from recovery mutation"


_FAIL_TRIGGER = "_test_fail_child_recovery_repair_audit_insert"


def _fail_audit_insert_at_transaction_boundary(db_instance):
    """Installs a real sqlite trigger that aborts the audit ledger INSERT
    for a CHILD_RECOVERY_REPAIR row -- a failure injected at the actual
    database engine/transaction boundary (not a mock of `_audit`/
    `_insert_audit_row_locked`/any Python helper). The already-open
    ``BEGIN IMMEDIATE`` recovery transaction sees a genuine sqlite error
    from the engine, exactly like a real disk-full/constraint failure
    would."""
    db_instance._conn.execute(
        f"CREATE TEMP TRIGGER {_FAIL_TRIGGER} "
        "BEFORE INSERT ON mgboost_entitlement_mutations "
        "WHEN NEW.operation='CHILD_RECOVERY_REPAIR' "
        "BEGIN SELECT RAISE(ABORT, 'simulated durable audit write failure'); END"
    )


def _remove_audit_insert_failure_trigger(db_instance):
    db_instance._conn.execute(f"DROP TRIGGER IF EXISTS {_FAIL_TRIGGER}")


def test_recovery_audit_write_failure_rolls_back_the_whole_repair(db):
    account, cap, remote, observe_fn, _ensure_fn, op_id, _slot3 = _repaired_scenario(
        db, mapping="P0_ATOMIC_FAIL", tg=930100,
    )
    before_outbox = _outbox(db, op_id)
    before_intent = db._conn.execute(
        "SELECT * FROM mgboost_child_user_intents WHERE id=?",
        (before_outbox["child_intent_id"],),
    ).fetchone()
    assert before_outbox["state"] == "ERROR"
    assert before_intent["observed_state"] == "ERROR"
    assert before_intent["uuid_verifier"] is None

    _fail_audit_insert_at_transaction_boundary(db)
    try:
        with pytest.raises(ChildRecoveryError):
            repair_child_ensure(
                db, operation_id=op_id, capability=cap, reason=REASON,
                idempotency_key="p0-atomic-fail-repair-key-1", observe_fn=observe_fn, now=900,
            )
    finally:
        _remove_audit_insert_failure_trigger(db)

    # 1. The recovery mutation must NOT be considered REPAIRED / applied.
    after_outbox = _outbox(db, op_id)
    after_intent = db._conn.execute(
        "SELECT * FROM mgboost_child_user_intents WHERE id=?",
        (after_outbox["child_intent_id"],),
    ).fetchone()
    assert after_outbox["state"] == "ERROR", "outbox must stay ERROR, never APPLIED"
    assert after_intent["observed_state"] == "ERROR", "child intent must stay ERROR"
    assert after_intent["uuid_verifier"] is None, (
        "uuid_verifier must never be persisted when the mandatory audit row failed"
    )
    assert after_outbox["attempts"] == before_outbox["attempts"]

    # 2. No immutable success audit row exists for this repair attempt.
    assert _mutation_count(db, "CHILD_RECOVERY_REPAIR") == 0

    # 3. The connection is usable afterwards -- rollback actually completed
    #    and did not leave a dangling transaction/savepoint behind.
    db._conn.execute("SELECT 1").fetchone()

    # 4. A subsequent, unpatched repair attempt still succeeds cleanly --
    #    proving the earlier failure left no partial/poisoned state.
    result = repair_child_ensure(
        db, operation_id=op_id, capability=cap, reason=REASON,
        idempotency_key="p0-atomic-fail-repair-key-2", observe_fn=observe_fn, now=901,
    )
    assert result["status"] == "REPAIRED"
    assert _mutation_count(db, "CHILD_RECOVERY_REPAIR") == 1
    final_outbox = _outbox(db, op_id)
    assert final_outbox["state"] == "APPLIED"


def test_recovery_success_commits_mutation_and_audit_together(db):
    """Happy-path counterpart: when the audit write does NOT fail, the
    mutation and the audit row appear together -- there is no state where
    only one of the two durably exists."""
    account, cap, remote, observe_fn, _ensure_fn, op_id, _slot3 = _repaired_scenario(
        db, mapping="P0_ATOMIC_OK", tg=930101,
    )

    result = repair_child_ensure(
        db, operation_id=op_id, capability=cap, reason=REASON,
        idempotency_key="p0-atomic-ok-repair-key-1", observe_fn=observe_fn, now=900,
    )

    assert result["status"] == "REPAIRED"
    outbox = _outbox(db, op_id)
    intent = db._conn.execute(
        "SELECT * FROM mgboost_child_user_intents WHERE id=?", (outbox["child_intent_id"],),
    ).fetchone()
    assert outbox["state"] == "APPLIED"
    assert intent["observed_state"] == "ACTIVE"
    assert intent["uuid_verifier"] is not None
    # Mutation and audit are both durable, in the same commit.
    assert _mutation_count(db, "CHILD_RECOVERY_REPAIR") == 1
    audit = db._conn.execute(
        "SELECT * FROM mgboost_entitlement_mutations WHERE operation='CHILD_RECOVERY_REPAIR'",
    ).fetchone()
    assert audit["actor_ref"] and audit["reason"] == REASON
    assert result["mutation_id"] == audit["id"]
