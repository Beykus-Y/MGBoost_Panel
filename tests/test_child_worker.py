import json
import threading

import pytest

from src.broker_operations import BrokerOperations
from src.child_contract import source_contract_hash
from src.child_worker import ChildProvisioningWorker
from tests.test_child_provisioning import _account, db  # noqa: F401
from tests.test_marzban_broker import FakeMarzban


class Clock:
    def __init__(self, value=1_000):
        self.value = value

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


class Service:
    def __init__(self, remote):
        self.remote = remote
        self.operations = BrokerOperations(remote)
        self.observe_override = None

    def observe_child_user(self, request):
        if self.observe_override is not None:
            result = self.observe_override
            return result() if callable(result) else result
        return self.operations.dispatch("child.user.observe", request)

    def ensure_child_user(self, request):
        return self.operations.dispatch("child.user.ensure", request)


def _prepared(db, *, key="worker-operation"):
    key = "child-worker-test-" + key
    account, alias_id, slot = _account(db)
    remote = FakeMarzban()
    prepared = db.child_provisioning.prepare_child_ensure(
        account_id=account["account_id"],
        slot_generation_id=slot["generation_id"],
        source_alias_id=alias_id,
        source_contract_hash=source_contract_hash(remote.users["alice"]),
        expire=0,
        idempotency_key=key,
        now=900,
    )
    return prepared, remote


def _worker(db, prepared, remote, clock, **kwargs):
    return ChildProvisioningWorker(
        db, Service(remote), worker_id=kwargs.pop("worker_id", "worker-one"),
        allowed_operation_ids=[prepared["operation_id"]], mode=kwargs.pop("mode", "active"),
        clock=clock, retry_base_seconds=1, retry_cap_seconds=4,
        reconcile_interval_seconds=5, lease_seconds=5, **kwargs,
    )


def _child_count(remote):
    return len([name for name in remote.users if name.startswith("mgc_")])


def test_schema_is_additive_idempotent_and_empty_without_allowlisted_operation(db):
    from src.child_workflow_schema import apply_child_workflow_schema
    assert apply_child_workflow_schema(db._conn, now=1_000) is False
    assert db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_child_workflow_state"
    ).fetchone()[0] == 0


@pytest.mark.parametrize("crash_stage", [
    "after_claim_before_remote", "after_remote_create_before_ack", "after_local_ack",
])
def test_crash_boundaries_converge_to_exactly_one_remote_child(db, crash_stage):
    prepared, remote = _prepared(db, key="crash-" + crash_stage)
    clock = Clock()
    fired = {"value": False}

    def crash(stage, _operation):
        if stage == crash_stage and not fired["value"]:
            fired["value"] = True
            raise RuntimeError("TEST_CRASH_" + stage)

    worker = _worker(db, prepared, remote, clock)
    worker.crash_hook = crash
    with pytest.raises(RuntimeError, match="TEST_CRASH"):
        worker.run_once()
    clock.advance(6)
    restarted = _worker(db, prepared, remote, clock, worker_id="worker-restarted")
    result = restarted.run_once()
    row = db._conn.execute(
        "SELECT state FROM mgboost_outbox WHERE operation_id=?",
        (prepared["operation_id"],),
    ).fetchone()
    assert row[0] == "APPLIED"
    assert result["manual_review"] == 0
    assert _child_count(remote) == 1


def test_duplicate_delivery_and_two_workers_create_exactly_one(db):
    prepared, remote = _prepared(db, key="two-workers")
    clock = Clock()
    workers = [
        _worker(db, prepared, remote, clock, worker_id="worker-a"),
        _worker(db, prepared, remote, clock, worker_id="worker-b"),
    ]
    results = []
    threads = [threading.Thread(target=lambda w=w: results.append(w.run_once())) for w in workers]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert _child_count(remote) == 1
    assert db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_outbox WHERE operation_id=?",
        (prepared["operation_id"],),
    ).fetchone()[0] == 1
    clock.advance(6)
    workers[0].run_once()
    assert _child_count(remote) == 1


def test_two_database_connections_cannot_claim_one_logical_mutation_twice(db):
    prepared, remote = _prepared(db, key="two-db-connections")
    from src.database import Database
    second_db = Database()
    clock = Clock()
    claimed = threading.Event()
    release = threading.Event()
    results = []

    def pause_after_claim(stage, _operation):
        if stage == "after_claim_before_remote":
            claimed.set()
            release.wait(timeout=3)

    first = _worker(db, prepared, remote, clock, worker_id="process-a")
    first.crash_hook = pause_after_claim
    second = _worker(second_db, prepared, remote, clock, worker_id="process-b")
    thread = threading.Thread(target=lambda: results.append(first.run_once()))
    thread.start()
    assert claimed.wait(timeout=3)
    second_result = second.run_once()
    release.set()
    thread.join(timeout=3)
    try:
        assert second_result["skipped"] == 1
        assert results[0]["provisioned"] == 1
        assert _child_count(remote) == 1
    finally:
        second_db._conn.close()


def test_reconcile_only_never_provisions_pending(db):
    prepared, remote = _prepared(db, key="reconcile-only")
    result = _worker(db, prepared, remote, Clock(), mode="reconcile_only").run_once()
    assert result["skipped"] == 1
    assert _child_count(remote) == 0
    assert db._conn.execute(
        "SELECT state FROM mgboost_outbox WHERE operation_id=?",
        (prepared["operation_id"],),
    ).fetchone()[0] == "PENDING"


def test_broker_or_marzban_outage_retries_then_recovers(db):
    prepared, remote = _prepared(db, key="outage-recovery")
    clock = Clock()
    remote.outage = True
    worker = _worker(db, prepared, remote, clock)
    first = worker.run_once()
    assert first["retried"] == 1 and _child_count(remote) == 0
    remote.outage = False
    clock.advance(2)
    restarted = _worker(db, prepared, remote, clock, worker_id="worker-after-outage")
    second = restarted.run_once()
    assert second["provisioned"] == 1
    assert _child_count(remote) == 1


def test_retry_exhaustion_is_terminal_manual_review(db):
    prepared, remote = _prepared(db, key="retry-exhaustion")
    clock = Clock()
    remote.outage = True
    worker = _worker(db, prepared, remote, clock, max_attempts=2)
    assert worker.run_once()["retried"] == 1
    clock.advance(2)
    assert worker.run_once()["manual_review"] == 1
    outbox = db._conn.execute(
        "SELECT state FROM mgboost_outbox WHERE operation_id=?",
        (prepared["operation_id"],),
    ).fetchone()[0]
    workflow = db._conn.execute(
        "SELECT reconcile_state FROM mgboost_child_workflow_state"
    ).fetchone()[0]
    assert (outbox, workflow, _child_count(remote)) == ("ERROR", "MANUAL_REVIEW", 0)


def test_remote_existing_is_reconciled_without_create(db):
    prepared, remote = _prepared(db, key="remote-existing")
    payload = json.loads(prepared["payload_json"])
    BrokerOperations(remote).dispatch("child.user.ensure", payload)
    creates_before = len([call for call in remote.calls if call[0] == "create_user"])
    result = _worker(db, prepared, remote, Clock()).run_once()
    creates_after = len([call for call in remote.calls if call[0] == "create_user"])
    assert result["provisioned"] == 1
    assert creates_before == creates_after == 1
    assert _child_count(remote) == 1


def test_applied_remote_missing_does_not_recreate(db):
    prepared, remote = _prepared(db, key="remote-missing")
    clock = Clock()
    worker = _worker(db, prepared, remote, clock)
    assert worker.run_once()["provisioned"] == 1
    child_name = json.loads(prepared["payload_json"])["child_username"]
    remote.users.pop(child_name)
    clock.advance(6)
    result = worker.run_once()
    assert result["retried"] == 1
    assert _child_count(remote) == 0
    assert not any(call[0] == "create_user" for call in remote.calls[-3:])
    assert db._conn.execute(
        "SELECT reconcile_state FROM mgboost_child_workflow_state"
    ).fetchone()[0] == "REMOTE_MISSING"


def test_applied_contract_mismatch_enters_manual_review_without_mutation(db):
    prepared, remote = _prepared(db, key="applied-mismatch")
    clock = Clock()
    worker = _worker(db, prepared, remote, clock)
    assert worker.run_once()["provisioned"] == 1
    child_name = json.loads(prepared["payload_json"])["child_username"]
    remote.users[child_name]["expire"] = 7
    mutations_before = len([
        call for call in remote.calls if call[0] in {"create_user", "modify_user", "delete_user"}
    ])
    clock.advance(6)
    assert worker.run_once()["manual_review"] == 1
    mutations_after = len([
        call for call in remote.calls if call[0] in {"create_user", "modify_user", "delete_user"}
    ])
    assert mutations_after == mutations_before
    assert db._conn.execute(
        "SELECT reconcile_state FROM mgboost_child_workflow_state"
    ).fetchone()[0] == "MANUAL_REVIEW"


def test_applied_outage_is_not_false_success_and_recovers(db):
    prepared, remote = _prepared(db, key="applied-outage")
    clock = Clock()
    worker = _worker(db, prepared, remote, clock)
    assert worker.run_once()["provisioned"] == 1
    remote.outage = True
    clock.advance(6)
    result = worker.run_once()
    assert result["retried"] == 1
    assert result["metrics"]["desired_observed_divergence_count"] == 1
    assert db._conn.execute(
        "SELECT reconcile_state FROM mgboost_child_workflow_state"
    ).fetchone()[0] == "UNAVAILABLE"
    remote.outage = False
    clock.advance(2)
    restarted = _worker(db, prepared, remote, clock, worker_id="restarted-after-broker")
    assert restarted.run_once()["reconciled"] == 1
    assert tuple(db._conn.execute(
        "SELECT reconcile_state,failure_count FROM mgboost_child_workflow_state"
    ).fetchone()) == ("IN_SYNC", 0)
    assert db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_child_reconciliation_events WHERE event_type='RECOVERED'"
    ).fetchone()[0] == 1


@pytest.mark.parametrize("presence,expected", [
    ({"presence": "MISMATCH", "mismatch_code": "REMOTE_CONTRACT_MISMATCH"}, "REMOTE_CONTRACT_MISMATCH"),
    ({"presence": "AMBIGUOUS"}, "REMOTE_AMBIGUOUS"),
])
def test_remote_mismatch_or_ambiguity_requires_manual_review(db, presence, expected):
    prepared, remote = _prepared(db, key="mismatch-" + expected)
    clock = Clock()
    service = Service(remote)
    service.observe_override = presence
    worker = ChildProvisioningWorker(
        db, service, worker_id="worker", allowed_operation_ids=[prepared["operation_id"]],
        mode="active", clock=clock,
    )
    assert worker.run_once()["manual_review"] == 1
    row = db._conn.execute(
        "SELECT reconcile_state,manual_review_reason FROM mgboost_child_workflow_state"
    ).fetchone()
    assert tuple(row) == ("MANUAL_REVIEW", expected)
    assert _child_count(remote) == 0


def test_stale_reconciliation_lease_is_recovered(db):
    prepared, remote = _prepared(db, key="stale-reconcile")
    clock = Clock()
    worker = _worker(db, prepared, remote, clock)
    assert worker.run_once()["provisioned"] == 1
    clock.advance(6)
    db._conn.execute(
        "UPDATE mgboost_child_workflow_state SET lease_owner='dead-worker',"
        "lease_expires_at=?,next_check_at=?", (clock.value - 1, clock.value)
    )
    db._conn.commit()
    assert _worker(db, prepared, remote, clock, worker_id="recovery-worker").run_once()["reconciled"] == 1
    assert db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_child_reconciliation_events "
        "WHERE event_type='STALE_LEASE_RECOVERED'"
    ).fetchone()[0] == 1


def test_payload_digest_corruption_fails_before_remote_call(db):
    prepared, remote = _prepared(db, key="corrupt-payload")
    db._conn.execute("DROP TRIGGER trg_mgboost_outbox_identity_immutable")
    db._conn.execute(
        "UPDATE mgboost_outbox SET request_hash=? WHERE operation_id=?",
        ("f" * 64, prepared["operation_id"]),
    )
    db._conn.commit()
    result = _worker(db, prepared, remote, Clock()).run_once()
    assert result["manual_review"] == 1
    assert remote.calls == []


def test_old_generation_never_reactivates_or_calls_remote(db):
    prepared, remote = _prepared(db, key="old-generation")
    operation = db.child_workflow.get_operation(prepared["operation_id"])
    db.device_slots.release(
        operation["account_id"], operation["slot_id"], operation["generation"], now=950,
        reason="test generation retirement",
    )
    result = _worker(db, prepared, remote, Clock()).run_once()
    assert result["manual_review"] == 1
    assert remote.calls == []


def test_privacy_safe_metrics_and_persistence_have_no_raw_uuid(db):
    prepared, remote = _prepared(db, key="privacy-metrics")
    raw_uuid = None
    worker = _worker(db, prepared, remote, Clock())
    result = worker.run_once()
    child_name = json.loads(prepared["payload_json"])["child_username"]
    raw_uuid = remote.users[child_name]["proxies"]["vless"]["id"]
    assert set(result["metrics"]) >= {
        "pending_outbox_count", "oldest_pending_age_seconds", "retry_count",
        "reconciliation_errors", "remote_mismatch_count",
        "broker_marzban_failure_events", "stale_worker_lease_events",
        "manual_review_count", "desired_observed_divergence_count",
    }
    dump = "\n".join(
        str(value) for table in (
            "mgboost_child_workflow_state", "mgboost_child_reconciliation_events",
            "mgboost_child_user_intents", "mgboost_outbox",
        ) for row in db._conn.execute(f"SELECT * FROM {table}") for value in row
    )
    assert raw_uuid not in dump
