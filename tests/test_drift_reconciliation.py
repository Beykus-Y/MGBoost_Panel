"""PH3-08 v2: post-ACK stabilization, periodic drift audit, and durable
entitlement convergence sweep -- corrective pass for the production
Incident A root cause (Marzban's own background status scheduler rolling a
child back to `expired` seconds after a successful, verified
`child.user.state.sync` ACK) plus the wider class of "APPLIED is treated as
a permanent fact" and "an entitlement mutation's caller must remember to
trigger convergence" bugs found alongside it.
"""

import importlib
import os
import tempfile

import pytest

from src.broker_operations import BrokerOperations
from src import parent_sync

from tests.test_child_provisioning import PRIMARY, PRIMARY_LOGIN
from tests.test_child_lifecycle import _build_applied_child


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


def _observe_fn(remote):
    return lambda payload: BrokerOperations(remote).dispatch("child.user.state.observe", payload)


def _set_subscription(db, account_id, *, status, current_expiry):
    db._conn.execute(
        "UPDATE mgboost_subscriptions SET status=?,current_expiry=?,updated_at=updated_at "
        "WHERE account_id=?",
        (status, current_expiry, account_id),
    )
    db._conn.commit()


def _intent(db, child_intent_id):
    return dict(db._conn.execute(
        "SELECT desired_state,observed_state FROM mgboost_child_user_intents WHERE id=?",
        (child_intent_id,),
    ).fetchone())


def _op_for(db, child_intent_id):
    return dict(db._conn.execute(
        "SELECT * FROM mgboost_parent_sync_operations WHERE child_intent_id=? "
        "ORDER BY id DESC LIMIT 1",
        (child_intent_id,),
    ).fetchone())


# --- A1/A2: post-ACK rollback race + stabilization ---------------------------

def test_a1_post_ack_rollback_is_not_visible_until_due_then_gets_detected_and_repaired(db):
    fx = _build_applied_child(db, mapping="DRIFT_A1", tg=810001)
    _set_subscription(db, fx["account"]["account_id"], status="ACTIVE", current_expiry=50_000)
    parent_sync.run_account_sync_cycle(
        db, fx["account"]["account_id"], sync_fn=_sync_fn(fx["remote"]),
        worker_id="worker-one", now=100,
    )
    op = _op_for(db, fx["child_intent_id"])
    assert op["state"] == "APPLIED"
    assert op["verify_after"] == 100 + 20  # default stabilization grace

    # T+11s: an external Marzban scheduler races the successful ACK and
    # rolls the child back to expired -- exactly the production race.
    fx["remote"].users[fx["child_username"]]["status"] = "expired"

    # Before the grace period elapses, nothing has looked again yet -- the
    # local model is still (correctly, for now) unaware.
    still_quiet = parent_sync.run_drift_audit_cycle(
        db, observe_fn=_observe_fn(fx["remote"]), sync_fn=_sync_fn(fx["remote"]),
        worker_id="worker-one", now=105,
    )
    assert still_quiet == {"checked": 0, "verified": 0, "flagged": 0, "repaired": 0, "manual_review": 0}
    assert fx["remote"].users[fx["child_username"]]["status"] == "expired"

    # Once the stabilization grace elapses, the audit must catch and repair
    # the drift within the same tick.
    result = parent_sync.run_drift_audit_cycle(
        db, observe_fn=_observe_fn(fx["remote"]), sync_fn=_sync_fn(fx["remote"]),
        worker_id="worker-one", now=121,
    )
    assert result == {"checked": 1, "verified": 0, "flagged": 1, "repaired": 1, "manual_review": 0}
    assert fx["remote"].users[fx["child_username"]]["status"] == "active"
    assert fx["remote"].users[fx["child_username"]]["expire"] == 50_000
    assert fx["remote"].users[fx["child_username"]]["proxies"]["vless"]["id"] == fx["child_uuid"]
    assert _intent(db, fx["child_intent_id"]) == {"desired_state": "ACTIVE", "observed_state": "ACTIVE"}
    op_after = _op_for(db, fx["child_intent_id"])
    assert op_after["state"] == "APPLIED"
    assert op_after["attempts"] == 2  # original dispatch + the repair dispatch


def test_a2_grace_period_is_configurable_not_hardcoded_eleven_seconds(db):
    fx = _build_applied_child(db, mapping="DRIFT_A2", tg=810002)
    _set_subscription(db, fx["account"]["account_id"], status="ACTIVE", current_expiry=50_000)
    parent_sync.run_account_sync_cycle(
        db, fx["account"]["account_id"], sync_fn=_sync_fn(fx["remote"]),
        worker_id="worker-one", now=100, lease_seconds=5,
    )
    # Directly exercise acknowledge()'s grace parameter to prove it is not a
    # hardcoded 11-second constant anywhere in the stabilization path.
    op = _op_for(db, fx["child_intent_id"])
    assert op["verify_after"] - 100 == 20
    fx["remote"].users[fx["child_username"]]["status"] = "expired"
    _set_subscription(db, fx["account"]["account_id"], status="ACTIVE", current_expiry=60_000)
    # A brand-new revision reschedules through the normal path; acknowledge
    # is exercised again with an explicit non-default grace.
    db.parent_sync.refresh_desired_state(fx["account"]["account_id"], now=130)
    prepared = db.parent_sync.enqueue_current_children(fx["account"]["account_id"], now=130)
    claimed = db.parent_sync.claim(prepared[0]["operation_id"], worker_id="worker-two", now=130, lease_seconds=5)
    result = BrokerOperations(fx["remote"]).dispatch("child.user.state.sync", claimed["payload"])
    acked = db.parent_sync.acknowledge(
        prepared[0]["operation_id"], worker_id="worker-two", outcome=result["outcome"], now=130,
        observed_status=result.get("observed_status"), observed_expire=result.get("observed_expire"),
        stabilization_grace_seconds=5,
    )
    assert acked["verify_after"] - 130 == 5


def test_a3_bounded_repair_attempts_terminate_into_manual_review_not_infinite_loop(db):
    fx = _build_applied_child(db, mapping="DRIFT_A3", tg=810003)
    _set_subscription(db, fx["account"]["account_id"], status="ACTIVE", current_expiry=50_000)
    parent_sync.run_account_sync_cycle(
        db, fx["account"]["account_id"], sync_fn=_sync_fn(fx["remote"]),
        worker_id="worker-one", now=100,
    )
    now = 121
    for _ in range(10):
        fx["remote"].users[fx["child_username"]]["status"] = "expired"
        parent_sync.run_drift_audit_cycle(
            db, observe_fn=_observe_fn(fx["remote"]), sync_fn=_sync_fn(fx["remote"]),
            worker_id="worker-one", now=now, max_attempts=3, repair_retry_delay_seconds=0,
        )
        now += 25
    op = _op_for(db, fx["child_intent_id"])
    assert op["state"] == "ERROR"
    assert op["last_error_class"] == "STABILIZATION_RETRY_EXHAUSTED"
    # A terminated op is no longer scheduled for further checks -- no
    # infinite mutation loop against a remote that keeps rolling itself back.
    assert op["verify_after"] is None
    due = db.parent_sync.due_for_drift_audit(now=now + 10_000, limit=50)
    assert all(row["operation_id"] != op["operation_id"] for row in due)


# --- B1/B2/B3: periodic authoritative drift audit ----------------------------

def test_b1_periodic_audit_no_ops_when_remote_still_matches(db):
    fx = _build_applied_child(db, mapping="DRIFT_B1", tg=810004)
    _set_subscription(db, fx["account"]["account_id"], status="ACTIVE", current_expiry=50_000)
    parent_sync.run_account_sync_cycle(
        db, fx["account"]["account_id"], sync_fn=_sync_fn(fx["remote"]),
        worker_id="worker-one", now=100,
    )
    calls_before = len(fx["remote"].calls)
    result = parent_sync.run_drift_audit_cycle(
        db, observe_fn=_observe_fn(fx["remote"]), sync_fn=_sync_fn(fx["remote"]),
        worker_id="worker-one", now=121, audit_interval_seconds=300,
    )
    assert result["verified"] == 1
    assert result["flagged"] == 0
    # Verification uses the read-only observe op; no modify_user call at all.
    assert not any(call[0] == "modify_user" for call in fx["remote"].calls[calls_before:])
    op = _op_for(db, fx["child_intent_id"])
    assert op["state"] == "APPLIED"
    assert op["verify_after"] == 121 + 300
    assert op["stabilized_at"] == 121

    # A second audit tick before the (now longer) interval elapses is a
    # true no-op: nothing due, nothing checked.
    quiet = parent_sync.run_drift_audit_cycle(
        db, observe_fn=_observe_fn(fx["remote"]), sync_fn=_sync_fn(fx["remote"]),
        worker_id="worker-one", now=150,
    )
    assert quiet["checked"] == 0


def test_b2_concurrent_audit_ticks_never_duplicate_repair_work(db):
    fx = _build_applied_child(db, mapping="DRIFT_B2", tg=810005)
    _set_subscription(db, fx["account"]["account_id"], status="ACTIVE", current_expiry=50_000)
    parent_sync.run_account_sync_cycle(
        db, fx["account"]["account_id"], sync_fn=_sync_fn(fx["remote"]),
        worker_id="worker-one", now=100,
    )
    fx["remote"].users[fx["child_username"]]["status"] = "expired"
    # Two consecutive ticks "racing" the same due window: the first flags +
    # repairs the op back to APPLIED; by the time the second tick runs, the
    # op's verify_after is either freshly scheduled (if repaired) or absent
    # (if it terminated) -- either way there is exactly one operation row
    # for this child, never a second parallel repair operation created.
    parent_sync.run_drift_audit_cycle(
        db, observe_fn=_observe_fn(fx["remote"]), sync_fn=_sync_fn(fx["remote"]),
        worker_id="worker-a", now=121,
    )
    second = parent_sync.run_drift_audit_cycle(
        db, observe_fn=_observe_fn(fx["remote"]), sync_fn=_sync_fn(fx["remote"]),
        worker_id="worker-b", now=121,
    )
    assert second == {"checked": 0, "verified": 0, "flagged": 0, "repaired": 0, "manual_review": 0}
    rows = db._conn.execute(
        "SELECT COUNT(*) AS n FROM mgboost_parent_sync_operations WHERE child_intent_id=?",
        (fx["child_intent_id"],),
    ).fetchone()
    assert rows["n"] == 1


def test_b3_revoked_generation_is_never_resurrected_by_the_periodic_audit(db):
    fx = _build_applied_child(db, mapping="DRIFT_B3", tg=810006)
    _set_subscription(db, fx["account"]["account_id"], status="ACTIVE", current_expiry=50_000)
    parent_sync.run_account_sync_cycle(
        db, fx["account"]["account_id"], sync_fn=_sync_fn(fx["remote"]),
        worker_id="worker-one", now=100,
    )
    op_before = _op_for(db, fx["child_intent_id"])
    assert op_before["verify_after"] is not None
    db._conn.execute(
        "UPDATE mgboost_child_user_intents SET desired_state='REVOKED',observed_state='REVOKED' WHERE id=?",
        (fx["child_intent_id"],),
    )
    db._conn.commit()
    fx["remote"].users[fx["child_username"]]["status"] = "expired"
    due = db.parent_sync.due_for_drift_audit(now=100_000, limit=50)
    assert all(row["operation_id"] != op_before["operation_id"] for row in due)
    calls_before = len(fx["remote"].calls)
    parent_sync.run_drift_audit_cycle(
        db, observe_fn=_observe_fn(fx["remote"]), sync_fn=_sync_fn(fx["remote"]),
        worker_id="worker-one", now=100_000,
    )
    assert len(fx["remote"].calls) == calls_before  # never touched


# --- G: durable entitlement convergence sweep --------------------------------

def test_g_sweep_converges_a_mutation_whose_caller_never_triggered_sync(db):
    """Simulates ADMIN_GRANT-shaped behaviour: only the subscription row is
    mutated directly (exactly what `apply_same_plan_purchase` does) with
    *zero* call to refresh_desired_state/enqueue_current_children/
    run_account_sync_cycle -- proving the durable periodic sweep converges
    the child anyway, independent of whether any particular caller
    remembered to."""
    fx = _build_applied_child(db, mapping="DRIFT_G1", tg=810007)
    account_id = fx["account"]["account_id"]
    _set_subscription(db, account_id, status="EXPIRED", current_expiry=1_000)
    parent_sync.run_account_sync_cycle(
        db, account_id, sync_fn=_sync_fn(fx["remote"]), worker_id="worker-one", now=100,
    )
    assert fx["remote"].users[fx["child_username"]]["status"] == "disabled"

    # The "ADMIN_GRANT" itself: only the subscription row changes, no sync
    # call of any kind -- this is the crash-window / caller-forgot scenario.
    _set_subscription(db, account_id, status="ACTIVE", current_expiry=90_000)

    due = db.parent_sync.due_convergence_accounts(now=200, limit=50)
    assert account_id in due

    result = parent_sync.sweep_convergence(
        db, sync_fn=_sync_fn(fx["remote"]), worker_id="sweep-worker", now=200,
    )
    assert result["accounts_swept"] >= 1
    assert any(row["account_id"] == account_id for row in result["results"])
    remote_user = fx["remote"].users[fx["child_username"]]
    assert remote_user["status"] == "active"
    assert remote_user["expire"] == 90_000
    assert remote_user["proxies"]["vless"]["id"] == fx["child_uuid"]
    assert _intent(db, fx["child_intent_id"]) == {"desired_state": "ACTIVE", "observed_state": "ACTIVE"}


def test_g5_restart_recovery_a_fresh_cursor_sweeps_every_account_immediately(db):
    """No prior sweep has ever run for this process (no cursor row at all,
    exactly the state after a fresh restart) -- the very first sweep tick
    must still find and converge every account with a subscription."""
    fx1 = _build_applied_child(db, mapping="DRIFT_G5_A", tg=810008, alias="alice")
    fx2 = _build_applied_child(db, mapping="DRIFT_G5_B", tg=810009, alias="second-source")
    _set_subscription(db, fx1["account"]["account_id"], status="ACTIVE", current_expiry=70_000)
    _set_subscription(db, fx2["account"]["account_id"], status="ACTIVE", current_expiry=80_000)
    result = parent_sync.sweep_convergence(
        db, sync_fn=lambda payload: (
            BrokerOperations(fx1["remote"]).dispatch("child.user.state.sync", payload)
            if payload["child_username"] == fx1["child_username"]
            else BrokerOperations(fx2["remote"]).dispatch("child.user.state.sync", payload)
        ),
        worker_id="restart-worker", now=1_000,
    )
    assert result["accounts_swept"] == 2
    assert fx1["remote"].users[fx1["child_username"]]["status"] == "active"
    assert fx1["remote"].users[fx1["child_username"]]["expire"] == 70_000
    assert fx2["remote"].users[fx2["child_username"]]["status"] == "active"
    assert fx2["remote"].users[fx2["child_username"]]["expire"] == 80_000
    # A second immediate sweep is a true no-op: the due-cursor pushed both
    # accounts out past `now`.
    quiet = parent_sync.sweep_convergence(
        db, sync_fn=lambda payload: (_ for _ in ()).throw(AssertionError("should not be called")),
        worker_id="restart-worker", now=1_000,
    )
    assert quiet["accounts_swept"] == 0
