"""Focused regressions from the account 11 parent-sync forensic review."""

import importlib
import os
import tempfile

import pytest

from src import parent_sync
from src.broker_operations import BrokerOperations
from src.child_contract import expiry_semantically_matches
from tests.test_child_lifecycle import _build_applied_child
from tests.test_child_provisioning import HWID_KEY, PRIMARY, PRIMARY_LOGIN


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


def _sync_fn(remote):
    return lambda payload: BrokerOperations(remote).dispatch("child.user.state.sync", payload)


def _set_subscription(db, account_id, *, status="ACTIVE", expiry=50_000):
    db._conn.execute(
        "UPDATE mgboost_subscriptions SET status=?,current_expiry=? WHERE account_id=?",
        (status, expiry, account_id),
    )
    db._conn.commit()


def _incomplete_current_child(db, fx, *, observed_state):
    slot = db.device_slots.claim(
        fx["account"]["account_id"], "forensic-poisoned-sibling-hwid", HWID_KEY, now=210,
    )
    prepared = db.child_provisioning.prepare_child_ensure(
        account_id=fx["account"]["account_id"], slot_generation_id=slot["generation_id"],
        source_alias_id=fx["alias_id"], source_contract_hash="a" * 64,
        expire=0, idempotency_key="forensic-poisoned-child", now=211,
    )
    db._conn.execute(
        "UPDATE mgboost_child_user_intents SET observed_state=? WHERE id=?",
        (observed_state, prepared["child_intent_id"]),
    )
    db._conn.commit()
    return prepared


def test_error_child_is_excluded_while_healthy_sibling_syncs(db):
    fx = _build_applied_child(db, mapping="FORENSIC_ELIGIBLE", tg=991001)
    poisoned = _incomplete_current_child(db, fx, observed_state="ERROR")
    _set_subscription(db, fx["account"]["account_id"])

    result = parent_sync.run_account_sync_cycle(
        db, fx["account"]["account_id"], sync_fn=_sync_fn(fx["remote"]),
        worker_id="forensic-worker", now=300,
    )

    assert result["prepared"] == 1
    assert fx["remote"].users[fx["child_username"]]["expire"] == 50_000
    assert db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_parent_sync_operations WHERE child_intent_id=?",
        (poisoned["child_intent_id"],),
    ).fetchone()[0] == 0
    assert db._conn.execute(
        "SELECT observed_state,uuid_verifier FROM mgboost_child_user_intents WHERE id=?",
        (poisoned["child_intent_id"],),
    ).fetchone()["observed_state"] == "ERROR"
    assert db._conn.execute(
        "SELECT uuid_verifier FROM mgboost_child_user_intents WHERE id=?",
        (poisoned["child_intent_id"],),
    ).fetchone()["uuid_verifier"] is None


def test_active_child_without_verifier_fails_closed(db):
    fx = _build_applied_child(db, mapping="FORENSIC_NO_VERIFIER", tg=991002)
    incomplete = _incomplete_current_child(db, fx, observed_state="ACTIVE")
    _set_subscription(db, fx["account"]["account_id"])

    db.parent_sync.refresh_desired_state(fx["account"]["account_id"], now=300)
    prepared = db.parent_sync.enqueue_current_children(fx["account"]["account_id"], now=300)

    assert [op["child_intent_id"] for op in prepared] == [fx["child_intent_id"]]
    assert incomplete["child_intent_id"] not in [op["child_intent_id"] for op in prepared]


@pytest.mark.parametrize("observed_state", ["ACTIVE", "DISABLED"])
def test_confirmed_active_or_disabled_child_remains_parent_sync_eligible(db, observed_state):
    fx = _build_applied_child(db, mapping=f"FORENSIC_HEALTHY_{observed_state}", tg=991003 if observed_state == "ACTIVE" else 991004)
    _set_subscription(db, fx["account"]["account_id"])
    db._conn.execute(
        "UPDATE mgboost_child_user_intents SET observed_state=? WHERE id=?",
        (observed_state, fx["child_intent_id"]),
    )
    db._conn.commit()

    db.parent_sync.refresh_desired_state(fx["account"]["account_id"], now=300)
    prepared = db.parent_sync.enqueue_current_children(fx["account"]["account_id"], now=300)

    assert [op["child_intent_id"] for op in prepared] == [fx["child_intent_id"]]


@pytest.mark.parametrize(
    "desired_expire,observed_expire,expected",
    [(0, None, True), (None, 0, True), (1_790_000_000, None, False), (1_790_000_000, 0, False)],
)
def test_expiry_semantics_normalize_only_unlimited(db, desired_expire, observed_expire, expected):
    assert expiry_semantically_matches("active", desired_expire, observed_expire) is expected
    assert expiry_semantically_matches("disabled", desired_expire, observed_expire) is True

    # Integration guard: the drift audit must share the exact helper
    # semantics used by state-sync and therefore never repair NULL versus 0.
    if desired_expire == 0 and observed_expire is None:
        fx = _build_applied_child(db, mapping="FORENSIC_UNLIMITED", tg=991006)
        _set_subscription(db, fx["account"]["account_id"], status="UNLIMITED", expiry=None)
        parent_sync.run_account_sync_cycle(
            db, fx["account"]["account_id"], sync_fn=_sync_fn(fx["remote"]),
            worker_id="forensic-worker", now=300,
        )
        fx["remote"].users[fx["child_username"]]["expire"] = None
        mutations_before = len([call for call in fx["remote"].calls if call[0] == "modify_user"])
        result = parent_sync.run_drift_audit_cycle(
            db,
            observe_fn=lambda payload: BrokerOperations(fx["remote"]).dispatch(
                "child.user.state.observe", payload,
            ),
            sync_fn=_sync_fn(fx["remote"]), worker_id="forensic-worker", now=321,
        )
        mutations_after = len([call for call in fx["remote"].calls if call[0] == "modify_user"])
        assert result == {"checked": 1, "verified": 1, "flagged": 0, "repaired": 0, "manual_review": 0}
        assert mutations_after == mutations_before


def test_invalid_child_verifier_is_typed_terminal_not_retry(db):
    fx = _build_applied_child(db, mapping="FORENSIC_TYPED_ERROR", tg=991005)
    _set_subscription(db, fx["account"]["account_id"])
    state = db.parent_sync.refresh_desired_state(fx["account"]["account_id"], now=300)
    op = db.parent_sync._prepare_locked(
        account_id=fx["account"]["account_id"], child_intent_id=fx["child_intent_id"],
        child_username=fx["child_username"], uuid_verifier=None,
        parent_revision=state["revision"], desired_status="active", desired_expire=50_000, now=300,
    )
    db._conn.commit()

    assert parent_sync.process_sync(
        db, op["operation_id"], worker_id="forensic-worker", sync_fn=_sync_fn(fx["remote"]), now=300,
    ) is None
    stored = db._conn.execute(
        "SELECT state,attempts,last_error_class FROM mgboost_parent_sync_operations WHERE id=?", (op["id"],),
    ).fetchone()
    assert tuple(stored) == ("ERROR", 1, "INVALID_CHILD_UUID_VERIFIER")
    assert db._conn.execute(
        "SELECT safe_error_class FROM mgboost_parent_sync_attempt_events WHERE sync_operation_id=? AND event_type='FAILED'",
        (op["id"],),
    ).fetchone()[0] == "INVALID_CHILD_UUID_VERIFIER"
