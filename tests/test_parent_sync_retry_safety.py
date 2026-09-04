"""Regression coverage for caught parent-sync dispatch exceptions."""

import importlib
import os
import tempfile

import pytest

from src import parent_sync
from src.broker_operations import BrokerOperations
from tests.test_child_lifecycle import _build_applied_child
from tests.test_child_provisioning import PRIMARY, PRIMARY_LOGIN


@pytest.fixture
def db(monkeypatch):
    tmp = tempfile.mkdtemp()
    monkeypatch.setenv("DATA_DIR", tmp)
    monkeypatch.setenv("PRIMARY_MGBOOST_ADMIN_ACTOR_ID", PRIMARY)
    monkeypatch.setenv("PRIMARY_MGBOOST_ADMIN_LOGIN", PRIMARY_LOGIN)
    import src.config as config
    import src.database as database
    importlib.reload(config)
    importlib.reload(database)
    database.DB_PATH = os.path.join(tmp, "db.sqlite3")
    instance = database.Database()
    yield instance
    instance._conn.close()


def _set_subscription(db, account_id, expiry):
    db._conn.execute(
        "UPDATE mgboost_subscriptions SET status='ACTIVE',current_expiry=? WHERE account_id=?",
        (expiry, account_id),
    )
    db._conn.commit()


def _operation(db, account_id):
    return dict(db._conn.execute(
        "SELECT * FROM mgboost_parent_sync_operations WHERE account_id=? ORDER BY id DESC LIMIT 1",
        (account_id,),
    ).fetchone())


def test_r1_sync_exception_retries_with_safe_class_and_backoff(db):
    fx = _build_applied_child(db, mapping="RETRY_R1", tg=920001)
    account_id = fx["account"]["account_id"]
    _set_subscription(db, account_id, 70_000)

    result = parent_sync.run_account_sync_cycle(
        db, account_id, sync_fn=lambda _payload: (_ for _ in ()).throw(ConnectionError("broker down")),
        worker_id="retry-r1", now=100, max_attempts=3, retry_base_seconds=5,
    )

    op = _operation(db, account_id)
    assert result["errored"] == 0
    assert op["state"] == "RETRY"
    assert op["attempts"] == 1
    assert op["last_error_class"] == "BROKER_OR_MARZBAN_UNAVAILABLE"
    assert op["next_attempt_at"] == 105
    assert op["lease_owner"] is None


def test_r2_sync_exception_exhaustion_is_terminal(db):
    fx = _build_applied_child(db, mapping="RETRY_R2", tg=920002)
    account_id = fx["account"]["account_id"]
    _set_subscription(db, account_id, 70_000)
    failing = lambda _payload: (_ for _ in ()).throw(ConnectionError("broker down"))

    for now in (100, 105, 115):
        parent_sync.run_account_sync_cycle(
            db, account_id, sync_fn=failing, worker_id="retry-r2", now=now,
            max_attempts=3, retry_base_seconds=5,
        )
    op = _operation(db, account_id)
    assert op["state"] == "ERROR"
    assert op["attempts"] == 3
    assert op["last_error_class"] == "PARENT_SYNC_RETRY_EXHAUSTED"
    assert db.parent_sync.claim(op["operation_id"], worker_id="retry-r2", now=99_999) is None
    events = db._conn.execute(
        "SELECT event_type,safe_error_class FROM mgboost_parent_sync_attempt_events "
        "WHERE sync_operation_id=? AND event_type='FAILED' ORDER BY id",
        (op["id"],),
    ).fetchall()
    assert [row["safe_error_class"] for row in events] == [
        "BROKER_OR_MARZBAN_UNAVAILABLE", "BROKER_OR_MARZBAN_UNAVAILABLE",
        "PARENT_SYNC_RETRY_EXHAUSTED",
    ]


def test_r3_failing_account_does_not_starve_remaining_sweep_batch(db):
    bad = _build_applied_child(db, mapping="RETRY_R3_A", tg=920003, alias="retry-bad")
    healthy = _build_applied_child(db, mapping="RETRY_R3_B", tg=920004, alias="retry-healthy")
    _set_subscription(db, bad["account"]["account_id"], 71_000)
    _set_subscription(db, healthy["account"]["account_id"], 72_000)

    def sync(payload):
        if payload["child_username"] == bad["child_username"]:
            raise ConnectionError("broker down")
        return BrokerOperations(healthy["remote"]).dispatch("child.user.state.sync", payload)

    result = parent_sync.sweep_convergence(db, sync_fn=sync, worker_id="retry-r3", now=100)
    assert result["accounts_swept"] == 2
    assert _operation(db, bad["account"]["account_id"])["state"] == "RETRY"
    assert _operation(db, healthy["account"]["account_id"])["state"] == "APPLIED"
    assert {row["account_id"] for row in result["results"]} == {
        bad["account"]["account_id"], healthy["account"]["account_id"],
    }
