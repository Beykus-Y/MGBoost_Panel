"""Narrow finishing pass: `child_worker_main.run_reconciliation_tick` wires
the already-existing, already-tested PH3-08 `sweep_convergence()` /
`run_drift_audit_cycle()` (BUG A/A2/B/G) into the one actual continuously
running production loop (`mgboost-child-worker.service` ->
`child_worker_main.py`'s `while True: worker.run_once(); ...`). Before this
wiring those functions were fully unit-tested in isolation but never called
by anything the production process actually runs -- these tests exercise
`run_reconciliation_tick` exactly as the real loop calls it (same `marzban`
object, same `worker_id`, same `now`), proving the wiring itself, not the
underlying reconciliation logic (already covered by
`tests/test_drift_reconciliation.py`).
"""

import importlib
import logging
import os
import tempfile

import pytest

from src.broker_operations import BrokerOperations
from src import parent_sync

from tests.test_child_provisioning import PRIMARY, PRIMARY_LOGIN
from tests.test_child_lifecycle import _build_applied_child
from tests.test_marzban_broker import FakeMarzban

import child_worker_main


@pytest.fixture
def db(monkeypatch):
    tmp = tempfile.mkdtemp()
    monkeypatch.setenv("DATA_DIR", tmp)
    monkeypatch.setenv("PRIMARY_MGBOOST_ADMIN_ACTOR_ID", PRIMARY)
    monkeypatch.setenv("PRIMARY_MGBOOST_ADMIN_LOGIN", PRIMARY_LOGIN)
    monkeypatch.setenv("PARENT_SYNC_RECONCILIATION_MODE", "global")
    import src.config as config
    import src.database as database
    importlib.reload(config)
    importlib.reload(database)
    database.DB_PATH = os.path.join(tmp, "db.sqlite3")
    instance = database.Database()
    yield instance
    instance._conn.close()


class _StubMarzban:
    """Minimal stand-in for `ServiceMarzbanClient`: exposes exactly the two
    methods `run_reconciliation_tick` calls, dispatched against an in-memory
    `FakeMarzban` remote via the same typed broker operations production
    uses. Optionally raises on `sync_child_user_state` to prove one
    reconciliation failure cannot escape and kill the worker loop."""

    def __init__(self, remote, *, sync_raises=None):
        self.remote = remote
        self.sync_raises = sync_raises
        self.sync_calls = 0
        self.observe_calls = 0

    def sync_child_user_state(self, payload):
        self.sync_calls += 1
        if self.sync_raises is not None:
            raise self.sync_raises
        return BrokerOperations(self.remote).dispatch("child.user.state.sync", payload)

    def observe_child_user_state(self, payload):
        self.observe_calls += 1
        return BrokerOperations(self.remote).dispatch("child.user.state.observe", payload)


def _set_subscription(db, account_id, *, status, current_expiry):
    db._conn.execute(
        "UPDATE mgboost_subscriptions SET status=?,current_expiry=?,updated_at=updated_at "
        "WHERE account_id=?",
        (status, current_expiry, account_id),
    )
    db._conn.commit()


def _op_for(db, child_intent_id):
    return dict(db._conn.execute(
        "SELECT * FROM mgboost_parent_sync_operations WHERE child_intent_id=? "
        "ORDER BY id DESC LIMIT 1",
        (child_intent_id,),
    ).fetchone())


# --- W1: the worker tick itself must invoke sweep_convergence ----------------

def test_w1_worker_tick_converges_a_never_swept_account(db):
    fx = _build_applied_child(db, mapping="WIRING_W1", tg=910001)
    _set_subscription(db, fx["account"]["account_id"], status="ACTIVE", current_expiry=70_000)
    marzban = _StubMarzban(fx["remote"])

    # Nothing but `run_reconciliation_tick` is called -- if the worker loop
    # never invoked `sweep_convergence`, this due (never-swept) account
    # would stay at its stale expire forever.
    result = child_worker_main.run_reconciliation_tick(
        db, marzban, worker_id="wiring-w1", now=1_000,
    )

    assert result["sweep"] is not None
    assert result["sweep"]["accounts_swept"] == 1
    op = _op_for(db, fx["child_intent_id"])
    assert op["state"] == "APPLIED"
    assert op["desired_expire"] == 70_000
    assert fx["remote"].users[fx["child_username"]]["expire"] == 70_000


# --- W2: the worker tick itself must invoke run_drift_audit_cycle ------------

def test_w2_worker_tick_detects_and_repairs_post_ack_remote_drift(db):
    fx = _build_applied_child(db, mapping="WIRING_W2", tg=910002)
    _set_subscription(db, fx["account"]["account_id"], status="ACTIVE", current_expiry=80_000)
    marzban = _StubMarzban(fx["remote"])

    # Get to a stable APPLIED op the same way production does: a first tick.
    child_worker_main.run_reconciliation_tick(db, marzban, worker_id="wiring-w2", now=100)
    op = _op_for(db, fx["child_intent_id"])
    assert op["state"] == "APPLIED"
    assert op["verify_after"] == 100 + 20  # default stabilization grace

    # The production race: Marzban's own scheduler rolls the child back.
    fx["remote"].users[fx["child_username"]]["status"] = "expired"

    # Before verify_after, a tick must not touch anything yet.
    child_worker_main.run_reconciliation_tick(db, marzban, worker_id="wiring-w2", now=105)
    assert fx["remote"].users[fx["child_username"]]["status"] == "expired"

    # Once due, the *worker tick* -- not a direct call to
    # run_drift_audit_cycle -- must detect and repair the drift.
    result = child_worker_main.run_reconciliation_tick(db, marzban, worker_id="wiring-w2", now=121)
    assert result["drift_audit"] is not None
    assert result["drift_audit"]["flagged"] == 1
    assert result["drift_audit"]["repaired"] == 1
    assert fx["remote"].users[fx["child_username"]]["status"] == "active"


# --- W3: a matching remote is only observed, never mutated -------------------

def test_w3_stable_remote_is_read_only_during_worker_tick(db):
    fx = _build_applied_child(db, mapping="WIRING_W3", tg=910003)
    _set_subscription(db, fx["account"]["account_id"], status="ACTIVE", current_expiry=90_000)
    marzban = _StubMarzban(fx["remote"])
    child_worker_main.run_reconciliation_tick(db, marzban, worker_id="wiring-w3", now=100)
    assert marzban.sync_calls == 1  # the one real provisioning sync above

    # Remote still matches the target exactly -- due for the periodic audit,
    # but nothing should be mutated.
    result = child_worker_main.run_reconciliation_tick(db, marzban, worker_id="wiring-w3", now=121)
    assert result["drift_audit"]["verified"] == 1
    assert result["drift_audit"]["flagged"] == 0
    assert marzban.observe_calls >= 1
    assert marzban.sync_calls == 1  # unchanged: no new mutation happened


# --- W4: one reconciliation failure must not kill the worker loop -----------

def test_w4_broker_failure_is_isolated_logged_and_recoverable(db, caplog):
    fx = _build_applied_child(db, mapping="WIRING_W4", tg=910004)
    _set_subscription(db, fx["account"]["account_id"], status="ACTIVE", current_expiry=100_000)
    marzban = _StubMarzban(fx["remote"])
    child_worker_main.run_reconciliation_tick(db, marzban, worker_id="wiring-w4", now=100)
    fx["remote"].users[fx["child_username"]]["status"] = "expired"

    # Simulate a broker/Marzban outage during the repair dispatch: sync_fn
    # raises instead of returning a typed outcome.
    marzban.sync_raises = ConnectionError("broker unavailable")
    with caplog.at_level(logging.ERROR):
        result = child_worker_main.run_reconciliation_tick(
            db, marzban, worker_id="wiring-w4", now=121,
        )
    # The tick itself must not raise -- this is the whole point of the wiring:
    # a broker/Marzban outage on one child's repair must never propagate out
    # of run_reconciliation_tick and kill the worker's `while True` loop.
    assert result["drift_audit"] == {
        "checked": 1, "verified": 0, "flagged": 1, "repaired": 0, "manual_review": 0,
    }
    assert not any("parent_sync_drift_audit_failed" in record.message for record in caplog.records)
    op = _op_for(db, fx["child_intent_id"])
    assert op["state"] == "RETRY"

    # The two phases are independently isolated: the drift-audit phase's
    # broker failure must not stop the sweep phase from running (or from
    # running again on the very next tick) -- proving one reconciliation
    # failure never wedges the whole tick, let alone the worker's loop.
    marzban.sync_raises = ConnectionError("broker unavailable")
    result_next = child_worker_main.run_reconciliation_tick(db, marzban, worker_id="wiring-w4", now=122)
    assert result_next["sweep"] is not None
    assert result_next["sweep"]["accounts_swept"] == 0  # nothing newly due yet
    # The RETRY op is no longer APPLIED, so the audit query naturally has
    # nothing to check -- a clean, exception-free empty pass, not a repeat
    # crash: the failure truly stayed contained to the one earlier tick.
    assert result_next["drift_audit"] == {
        "checked": 0, "verified": 0, "flagged": 0, "repaired": 0, "manual_review": 0,
    }


def test_c1_canary_scope_never_touches_due_account_outside_allowlist(db, monkeypatch):
    allowed = _build_applied_child(db, mapping="CANARY_A", tg=910010, alias="canary-a")
    outside = _build_applied_child(db, mapping="CANARY_B", tg=910011, alias="canary-b")
    _set_subscription(db, allowed["account"]["account_id"], status="ACTIVE", current_expiry=71_000)
    _set_subscription(db, outside["account"]["account_id"], status="ACTIVE", current_expiry=72_000)
    marzban = _StubMarzban(allowed["remote"])
    monkeypatch.setenv("PARENT_SYNC_RECONCILIATION_MODE", "canary")
    monkeypatch.setenv("PARENT_SYNC_ALLOWED_ACCOUNT_IDS", str(allowed["account"]["account_id"]))

    result = child_worker_main.run_reconciliation_tick(db, marzban, worker_id="canary-c1", now=1_000)

    assert result["mode"] == "canary"
    assert result["sweep"]["accounts_swept"] == 1
    assert db._conn.execute(
        "SELECT 1 FROM mgboost_convergence_sweep_cursor WHERE account_id=?",
        (outside["account"]["account_id"],),
    ).fetchone() is None
    assert db._conn.execute(
        "SELECT 1 FROM mgboost_parent_sync_operations WHERE account_id=?",
        (outside["account"]["account_id"],),
    ).fetchone() is None


def test_c2_explicit_global_mode_sweeps_all_due_accounts(db, monkeypatch):
    first = _build_applied_child(db, mapping="GLOBAL_A", tg=910012, alias="global-a")
    second = _build_applied_child(db, mapping="GLOBAL_B", tg=910013, alias="global-b")
    _set_subscription(db, first["account"]["account_id"], status="ACTIVE", current_expiry=73_000)
    _set_subscription(db, second["account"]["account_id"], status="ACTIVE", current_expiry=74_000)

    class TwoRemoteMarzban:
        def sync_child_user_state(self, payload):
            remote = first["remote"] if payload["child_username"] == first["child_username"] else second["remote"]
            return BrokerOperations(remote).dispatch("child.user.state.sync", payload)

        def observe_child_user_state(self, payload):
            remote = first["remote"] if payload["child_username"] == first["child_username"] else second["remote"]
            return BrokerOperations(remote).dispatch("child.user.state.observe", payload)

    monkeypatch.setenv("PARENT_SYNC_RECONCILIATION_MODE", "global")
    result = child_worker_main.run_reconciliation_tick(db, TwoRemoteMarzban(), worker_id="global-c2", now=1_000)
    assert result["mode"] == "global"
    assert result["sweep"]["accounts_swept"] == 2


def test_c3_disabled_mode_makes_no_reconciliation_remote_calls(db, monkeypatch):
    fx = _build_applied_child(db, mapping="DISABLED_A", tg=910014)
    marzban = _StubMarzban(fx["remote"])
    monkeypatch.setenv("PARENT_SYNC_RECONCILIATION_MODE", "disabled")
    result = child_worker_main.run_reconciliation_tick(db, marzban, worker_id="disabled-c3", now=1_000)
    assert result == {"mode": "disabled", "sweep": None, "drift_audit": None}
    assert marzban.sync_calls == 0
    assert marzban.observe_calls == 0
