"""PH6-07 -- production scheduler/reconciliation/drift/backlog around the
EXISTING PH6-06 enforcement engine.

Every scenario proves one named property from the slice brief: continuous
convergence WITHOUT a second enforcement engine (the orchestration wraps
`run_wl_enforcement_cycle`), post-terminal remote drift detection through
fresh observation with exact-allowlist-only classification, repair strictly
through the EXISTING epoch/op machinery (exactly-once by observation),
fail-closed behavior on ambiguous/unverifiable/unknown-topology findings,
structural no-op for UNLIMITED/STANDARD/non-signal accounts, single-entry
overlap safety, and an operator-grade identifier-free backlog read model.
"""

import fcntl
import json
import os
import uuid as uuid_module

import pytest

from src.child_contract import credential_verifier
from src.wl_enforcement import run_wl_enforcement_cycle
from src.wl_reconciliation import (
    backlog_snapshot,
    run_wl_reconciliation_cycle,
    scan_terminal_drift,
)

from tests.test_wl_enforcement import (
    NOW,
    NON_WL_A,
    NON_WL_B,
    WL_A,
    WL_B,
    WlBackedClient,
    _burn_quota,
    _enforce_fixture,
    _full_snapshot,
    _inbounds_of,
    _modify_count,
    _ok_observer,
    _seed_limited_period,
)

WORKER = "recon-worker"


@pytest.fixture
def db(monkeypatch):
    import importlib
    import os
    import tempfile

    tmp = tempfile.mkdtemp()
    from tests.test_child_provisioning import PRIMARY, PRIMARY_LOGIN
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


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------

def _disabled_fixture(db, mapping, n_children=1, observer=None):
    """Account converged to DISABLED by the real PH6-06 engine."""
    fx = _enforce_fixture(db, mapping=mapping, n_children=n_children)
    period = _seed_limited_period(db, account_id=fx["account"]["account_id"], now=NOW)
    _burn_quota(
        db, account_id=fx["account"]["account_id"],
        child_intent_id=fx["children"][0]["fx_child"], period_id=period,
        total_bytes=2_000_000_000,
    )
    summary = run_wl_enforcement_cycle(
        db=db, service_marzban=fx["client"], worker_id="fix-disable",
        now=NOW, topology_observer=observer or _ok_observer(),
    )
    assert summary["accounts_disabled"] == 1
    state = db.wl_enforcement.get_state(fx["account"]["account_id"])
    assert state["state"] == "DISABLED" and state["last_direction"] == "EXCLUDED"
    return fx, period


def _active_fixture(db, mapping):
    """Account converged to ACTIVE (LIMITED, not exceeded) by the engine,
    via the honest disable -> new-period -> restore path (an INCLUDED epoch
    always proves itself against a prior frozen baseline in PH6-06)."""
    fx = _enforce_fixture(db, mapping=mapping)
    account_id = fx["account"]["account_id"]
    period = _seed_limited_period(db, account_id=account_id, now=NOW)
    _burn_quota(db, account_id=account_id,
                child_intent_id=fx["children"][0]["fx_child"], period_id=period,
                total_bytes=2_000_000_000)
    run_wl_enforcement_cycle(
        db=db, service_marzban=fx["client"], worker_id="fix-disable",
        now=NOW, topology_observer=_ok_observer(),
    )
    assert db.wl_enforcement.get_state(account_id)["state"] == "DISABLED"
    db._conn.execute("UPDATE mgboost_wl_periods SET status='CLOSED' WHERE id=?", (period,))
    active_period = _seed_limited_period(db, account_id=account_id, now=NOW + 10,
                                         quota_bytes=5_000_000_000, starts_at=NOW + 10)
    run_wl_enforcement_cycle(
        db=db, service_marzban=fx["client"], worker_id="fix-restore",
        now=NOW + 20, topology_observer=_ok_observer(),
    )
    state = db.wl_enforcement.get_state(account_id)
    assert state["state"] == "ACTIVE" and state["last_direction"] == "INCLUDED"
    return fx, active_period


def _recon(db, fx, *, worker=WORKER, now=NOW, trigger="SCHEDULED", observer=None,
           lock_file=None):
    return run_wl_reconciliation_cycle(
        db=db, service_marzban=fx["client"], worker_id=worker, now=now,
        trigger=trigger, topology_observer=observer or _ok_observer(),
        lock_file=lock_file,
    )


def _drift_rows(db):
    return [dict(r) for r in db._conn.execute(
        "SELECT * FROM mgboost_wl_reconciliation_drift ORDER BY id"
    ).fetchall()]


def _cycle_rows(db):
    return [dict(r) for r in db._conn.execute(
        "SELECT * FROM mgboost_wl_reconciliation_cycles ORDER BY id"
    ).fetchall()]


@pytest.fixture
def approved_topology_expansion(monkeypatch):
    """Simulate the operator-approved versioned baseline update: a NEW exact
    WL inbound tag is added to the PH0-05 baseline (version bumped). Every
    module holding the allowlist by value is patched consistently."""
    import src.wl_enforcement as we
    import src.wl_enforcement_contract as wec
    import src.wl_topology as wt
    import src.wl_topology_guard as wtg
    new_tag = "wl-tcp-new-branch"
    expanded = frozenset(set(wt.WL_INBOUND_TAGS) | {new_tag})
    new_version = "2026-09-01-v2"
    monkeypatch.setattr(wt, "WL_INBOUND_TAGS", expanded)
    monkeypatch.setattr(wt, "WL_TOPOLOGY_VERSION", new_version)
    monkeypatch.setattr(wtg, "WL_TOPOLOGY_VERSION", new_version)
    monkeypatch.setattr(we, "WL_INBOUND_TAGS", expanded)
    monkeypatch.setattr(wec, "WL_INBOUND_TAGS", expanded)

    def _observer_with(new_tag_present=True):
        base = _ok_observer()
        if not new_tag_present:
            return base
        tags, nodes = base()
        return lambda: (frozenset(set(tags) | {new_tag}), nodes)

    return {"new_tag": new_tag, "observer": _observer_with}


# --------------------------------------------------------------------------
# Steady state: converged accounts are periodically reread, zero writes
# --------------------------------------------------------------------------

def test_converged_disabled_account_reread_periodically_with_zero_writes(db):
    fx, _period = _disabled_fixture(db, "RECON_STEADY")
    child = fx["children"][0]
    baseline_mutations = _modify_count(fx["remote"], child["username"])
    epochs_before = db.wl_enforcement.get_state(fx["account"]["account_id"])["epoch"]

    for i in range(3):
        summary = _recon(db, fx, now=NOW + 100 * i)
        assert summary["outcome"] == "OK"
        assert summary["drift"]["detected"] == 0

    assert _modify_count(fx["remote"], child["username"]) == baseline_mutations
    state = db.wl_enforcement.get_state(fx["account"]["account_id"])
    assert state["state"] == "DISABLED" and state["epoch"] == epochs_before
    assert _drift_rows(db) == []


# --------------------------------------------------------------------------
# Post-terminal drift: DISABLED child with WL manually re-added
# --------------------------------------------------------------------------

def test_disabled_child_wl_manually_readded_is_detected_and_repaired(db):
    fx, _period = _disabled_fixture(db, "RECON_WL_BACK")
    child = fx["children"][0]
    baseline_mutations = _modify_count(fx["remote"], child["username"])
    fx["remote"].users[child["username"]]["inbounds"]["vless"] = sorted(
        set(_inbounds_of(fx["remote"], child["username"])) | {WL_A}
    )

    summary = _recon(db, fx)

    assert summary["drift"]["detected"] == 1
    assert summary["drift"]["repaired"] == 1
    rows = _drift_rows(db)
    assert len(rows) == 1
    assert rows[0]["drift_class"] == "WL_PRESENT_WHILE_EXCLUDED"
    assert rows[0]["action"] == "REPAIR_QUEUED"
    # the same engine machinery removed ONLY the re-added WL tag
    assert _inbounds_of(fx["remote"], child["username"]) == [NON_WL_A]
    assert _modify_count(fx["remote"], child["username"]) == baseline_mutations + 1
    state = db.wl_enforcement.get_state(fx["account"]["account_id"])
    assert state["state"] == "DISABLED" and state["last_direction"] == "EXCLUDED"


def test_repair_is_exactly_once_across_repeated_cycles(db):
    fx, _period = _disabled_fixture(db, "RECON_ONCE")
    child = fx["children"][0]
    fx["remote"].users[child["username"]]["inbounds"]["vless"] = sorted(
        set(_inbounds_of(fx["remote"], child["username"])) | {WL_B}
    )
    _recon(db, fx)
    after_first = _modify_count(fx["remote"], child["username"])
    assert after_first == 2  # initial disable + one repair mutation

    summary = _recon(db, fx, now=NOW + 60)
    assert summary["drift"]["detected"] == 0
    assert _modify_count(fx["remote"], child["username"]) == after_first


# --------------------------------------------------------------------------
# Post-terminal drift: ACTIVE child missing an entitled WL inbound
# --------------------------------------------------------------------------

def test_active_missing_baseline_wl_restored_while_entitled(db):
    fx, _period = _active_fixture(db, "RECON_WL_LOST")
    child = fx["children"][0]
    baseline_mutations = _modify_count(fx["remote"], child["username"])
    fx["remote"].users[child["username"]]["inbounds"]["vless"] = [
        t for t in _inbounds_of(fx["remote"], child["username"]) if t != WL_A
    ]

    summary = _recon(db, fx, now=NOW + 30)

    assert summary["drift"]["detected"] == 1
    assert summary["drift"]["repaired"] == 1
    rows = _drift_rows(db)
    assert rows[0]["drift_class"] == "WL_MISSING_WHILE_INCLUDED"
    assert rows[0]["action"] == "REPAIR_QUEUED"
    # restored exactly the entitled WL set; non-WL membership byte-stable
    assert _inbounds_of(fx["remote"], child["username"]) == sorted(
        {NON_WL_A, WL_A, WL_B}
    )
    assert _modify_count(fx["remote"], child["username"]) == baseline_mutations + 1
    state = db.wl_enforcement.get_state(fx["account"]["account_id"])
    assert state["state"] == "ACTIVE"


def test_active_missing_wl_not_restored_without_entitlement(db):
    fx, period = _active_fixture(db, "RECON_NO_ENTITLE")
    child = fx["children"][0]
    baseline_mutations = _modify_count(fx["remote"], child["username"])
    fx["remote"].users[child["username"]]["inbounds"]["vless"] = [
        t for t in _inbounds_of(fx["remote"], child["username"]) if t != WL_A
    ]
    db._conn.execute("UPDATE mgboost_wl_periods SET status='CLOSED' WHERE id=?", (period,))
    db._conn.commit()

    summary = _recon(db, fx, now=NOW + 30)

    # no active canonical WL period -> no entitlement signal -> no repair,
    # no drift row, no mutation (the decision path owns direction changes)
    assert summary["drift"]["detected"] == 0
    assert _drift_rows(db) == []
    assert _modify_count(fx["remote"], child["username"]) == baseline_mutations
    assert _inbounds_of(fx["remote"], child["username"]).count(WL_A) == 0


# --------------------------------------------------------------------------
# Unverifiable / ambiguous drift: flag, never guess, never mutate
# --------------------------------------------------------------------------

def test_remote_missing_child_flagged_without_autocreate(db):
    fx, _period = _disabled_fixture(db, "RECON_GONE")
    child = fx["children"][0]
    del fx["remote"].users[child["username"]]

    summary = _recon(db, fx)

    rows = _drift_rows(db)
    assert len(rows) == 1
    assert rows[0]["drift_class"] == "REMOTE_MISSING"
    assert rows[0]["action"] == "FLAGGED"
    assert summary["drift"]["flagged"] == 1
    assert summary["drift"]["repaired"] == 0
    state = db.wl_enforcement.get_state(fx["account"]["account_id"])
    assert state["state"] == "ERROR_RECONCILE"
    # no second child was ever created for the account
    usernames = [u for u in fx["remote"].users]
    assert all(u != child["username"] for u in usernames)


def test_uuid_mismatch_flagged_with_zero_mutations(db):
    fx, _period = _disabled_fixture(db, "RECON_UUID")
    child = fx["children"][0]
    baseline_mutations = _modify_count(fx["remote"], child["username"])
    fx["remote"].users[child["username"]]["proxies"]["vless"]["id"] = str(
        uuid_module.uuid4()
    )

    summary = _recon(db, fx)

    rows = _drift_rows(db)
    assert len(rows) == 1
    assert rows[0]["drift_class"] == "UUID_MISMATCH"
    assert rows[0]["action"] == "FLAGGED"
    state = db.wl_enforcement.get_state(fx["account"]["account_id"])
    assert state["state"] == "ERROR_RECONCILE"
    assert _modify_count(fx["remote"], child["username"]) == baseline_mutations


def test_non_wl_membership_lost_flagged_without_mutation(db):
    fx, _period = _disabled_fixture(db, "RECON_NONWL_LOST")
    child = fx["children"][0]
    baseline_mutations = _modify_count(fx["remote"], child["username"])
    fx["remote"].users[child["username"]]["inbounds"]["vless"] = [
        t for t in _inbounds_of(fx["remote"], child["username"]) if t != NON_WL_A
    ]

    summary = _recon(db, fx)

    rows = _drift_rows(db)
    assert len(rows) == 1
    assert rows[0]["drift_class"] == "NON_WL_MEMBERSHIP_LOST"
    assert rows[0]["action"] == "FLAGGED"
    state = db.wl_enforcement.get_state(fx["account"]["account_id"])
    assert state["state"] == "ERROR_RECONCILE"
    assert _modify_count(fx["remote"], child["username"]) == baseline_mutations


def test_partial_child_observation_outage_is_isolated(db):
    fx, _period = _disabled_fixture(db, "RECON_PARTIAL", n_children=2)
    healthy, flaky = fx["children"][0], fx["children"][1]
    for c in (healthy, flaky):
        fx["remote"].users[c["username"]]["inbounds"]["vless"] = sorted(
            set(_inbounds_of(fx["remote"], c["username"])) | {WL_A}
        )
    real_get = fx["client"].get_user

    def get_user(username):
        if username == flaky["username"]:
            raise RuntimeError("SimulatedMarzbanDown")
        return real_get(username)

    fx["client"].get_user = get_user

    summary = _recon(db, fx)

    assert summary["drift"]["observation_errors"] == 1
    # the reachable drifted child still converged; the flaky one is left
    # untouched for the next cycle (no false drift, no mutation)
    assert _inbounds_of(fx["remote"], healthy["username"]) == [NON_WL_A]
    assert WL_A in _inbounds_of(fx["remote"], flaky["username"])
    assert all(r["drift_class"] != "REMOTE_MISSING" for r in _drift_rows(db))


# --------------------------------------------------------------------------
# Newly-added APPROVED exact WL inbound (the documented PH6-06 gap)
# --------------------------------------------------------------------------

def test_newly_added_approved_wl_inbound_cleans_suspended_child(
    db, approved_topology_expansion,
):
    new_tag = approved_topology_expansion["new_tag"]
    fx, _period = _disabled_fixture(
        db, "RECON_NEW_WL", observer=approved_topology_expansion["observer"](),
    )
    child = fx["children"][0]
    baseline_mutations = _modify_count(fx["remote"], child["username"])
    # Marzban persistent excluded_inbounds semantics: the newly-added inbound
    # silently becomes part of the suspended child's effective membership.
    fx["remote"].users[child["username"]]["inbounds"]["vless"] = sorted(
        set(_inbounds_of(fx["remote"], child["username"])) | {new_tag}
    )

    summary = _recon(
        db, fx, observer=approved_topology_expansion["observer"](),
    )

    assert summary["drift"]["detected"] == 1
    assert summary["drift"]["repaired"] == 1
    rows = _drift_rows(db)
    assert rows[0]["drift_class"] == "WL_PRESENT_WHILE_EXCLUDED"
    # ONLY the newly-added WL tag was removed; non-WL membership byte-stable
    assert _inbounds_of(fx["remote"], child["username"]) == [NON_WL_A]
    assert _modify_count(fx["remote"], child["username"]) == baseline_mutations + 1


def test_unknown_wl_like_inbound_fails_closed_blocks_whole_cycle(db):
    fx, _period = _disabled_fixture(db, "RECON_UNKNOWN_TAG")
    child = fx["children"][0]
    baseline_mutations = _modify_count(fx["remote"], child["username"])
    tags, nodes = _ok_observer()()
    unknown = frozenset(set(tags) | {"wl-something-unapproved"})

    summary = _recon(
        db, fx, observer=lambda: (unknown, nodes),
    )

    assert summary["outcome"] == "BLOCKED_TOPOLOGY"
    assert _drift_rows(db) == []
    assert _modify_count(fx["remote"], child["username"]) == baseline_mutations
    cycles = _cycle_rows(db)
    assert cycles[-1]["outcome"] == "BLOCKED_TOPOLOGY"
    assert cycles[-1]["topology_ok"] == 0


# --------------------------------------------------------------------------
# Structural abstention: UNLIMITED / no-signal accounts stay invisible
# --------------------------------------------------------------------------

def test_legacy_unlimited_account_never_gets_rows_or_mutations(db):
    fx = _enforce_fixture(db, mapping="RECON_UNLIMITED")
    child = fx["children"][0]
    baseline_mutations = _modify_count(fx["remote"], child["username"])

    summary = _recon(db, fx)

    assert summary["outcome"] == "OK"
    assert summary["engine"]["accounts_evaluated"] == 1
    assert summary["engine"]["accounts_abstained"] == 1
    assert db.wl_enforcement.get_state(fx["account"]["account_id"]) is None
    assert _modify_count(fx["remote"], child["username"]) == baseline_mutations
    assert _drift_rows(db) == []


# --------------------------------------------------------------------------
# Crash/restart boundaries around the orchestrated cycle
# --------------------------------------------------------------------------

def test_crash_after_repair_epoch_before_dispatch_converges_next_cycle(db):
    fx, _period = _disabled_fixture(db, "RECON_CRASH_EPOCH")
    child = fx["children"][0]
    baseline_mutations = _modify_count(fx["remote"], child["username"])
    fx["remote"].users[child["username"]]["inbounds"]["vless"] = sorted(
        set(_inbounds_of(fx["remote"], child["username"])) | {WL_A}
    )

    # "crash" right after the scan minted the repair epoch: durable PENDING
    # ops exist, zero remote mutations happened yet
    drift = scan_terminal_drift(
        db, fx["client"], worker_id=WORKER, now=NOW, observer=_ok_observer(),
    )
    assert drift["repaired"] == 1
    assert _modify_count(fx["remote"], child["username"]) == baseline_mutations
    state = db.wl_enforcement.get_state(fx["account"]["account_id"])
    assert state["state"] == "DISABLE_PENDING"
    pending = [
        o for o in db.wl_enforcement.epoch_ops(fx["account"]["account_id"], state["epoch"])
        if o["state"] == "PENDING"
    ]
    assert len(pending) == 1

    summary = _recon(db, fx, now=NOW + 30)
    assert summary["outcome"] == "OK"
    assert _inbounds_of(fx["remote"], child["username"]) == [NON_WL_A]
    assert _modify_count(fx["remote"], child["username"]) == baseline_mutations + 1
    state = db.wl_enforcement.get_state(fx["account"]["account_id"])
    assert state["state"] == "DISABLED"


def test_expired_lease_from_dead_worker_is_reclaimed_once(db):
    fx, _period = _disabled_fixture(db, "RECON_LEASE")
    child = fx["children"][0]
    baseline_mutations = _modify_count(fx["remote"], child["username"])
    fx["remote"].users[child["username"]]["inbounds"]["vless"] = sorted(
        set(_inbounds_of(fx["remote"], child["username"])) | {WL_A}
    )
    scan_terminal_drift(
        db, fx["client"], worker_id=WORKER, now=NOW, observer=_ok_observer(),
    )
    # a worker claimed the repair op and died mid-flight, lease expired
    state = db.wl_enforcement.get_state(fx["account"]["account_id"])
    op = db.wl_enforcement.epoch_ops(fx["account"]["account_id"], state["epoch"])[0]
    claimed = db.wl_enforcement.claim(
        op["operation_id"], worker_id="dead-worker", now=NOW + 1, lease_seconds=5,
    )
    assert claimed is not None

    summary = _recon(db, fx, now=NOW + 120)

    assert summary["outcome"] == "OK"
    assert _inbounds_of(fx["remote"], child["username"]) == [NON_WL_A]
    # dead worker never mutated anything; the reclaiming cycle did, exactly once
    assert _modify_count(fx["remote"], child["username"]) == baseline_mutations + 1


def test_duplicate_scheduled_trigger_is_safe_via_cycle_lock(db, tmp_path):
    fx, _period = _disabled_fixture(db, "RECON_LOCK")
    child = fx["children"][0]
    lock_file = str(tmp_path / "wl-enforcement.lock")
    held = open(lock_file, "w")
    fcntl.flock(held, fcntl.LOCK_EX)

    summary = _recon(db, fx, lock_file=lock_file)

    assert summary["outcome"] == "SKIPPED_BUSY"
    held.close()
    fcntl.flock(open(lock_file, "w"), fcntl.LOCK_UN)


# --------------------------------------------------------------------------
# Backlog / observability read model (no identifiers ever)
# --------------------------------------------------------------------------

def test_cycles_and_backlog_snapshot_operator_read_model(db):
    fx, _period = _disabled_fixture(db, "RECON_OBS")
    child = fx["children"][0]
    _recon(db, fx, trigger="SCHEDULED", worker="sched-1")
    fx["remote"].users[child["username"]]["inbounds"]["vless"] = sorted(
        set(_inbounds_of(fx["remote"], child["username"])) | {WL_A}
    )
    _recon(db, fx, trigger="MANUAL", worker="operator-1", now=NOW + 60)

    cycles = _cycle_rows(db)
    assert len(cycles) == 2
    assert cycles[0]["outcome"] == "OK"
    assert cycles[0]["trigger"] == "SCHEDULED"
    assert cycles[0]["topology_ok"] == 1
    assert cycles[0]["config_version"]
    engine = json.loads(cycles[0]["engine_json"])
    assert engine["accounts_evaluated"] == 1

    snapshot = backlog_snapshot(db, now=NOW + 120)
    assert snapshot["last_cycle"]["outcome"] == "OK"
    assert snapshot["last_cycle"]["trigger"] == "MANUAL"
    assert snapshot["last_successful_cycle"]["outcome"] == "OK"
    assert snapshot["topology"]["ok"] is True
    assert snapshot["topology"]["config_version"]
    assert snapshot["account_states"] == {"DISABLED": 1}
    assert snapshot["op_counts"]["APPLIED"] == 2  # original disable + repair epoch
    assert snapshot["drift"]["detected"] == 1
    assert snapshot["drift"]["repaired"] == 1
    assert snapshot["drift"]["flagged"] == 0
    assert snapshot["oldest_backlog_age_seconds"] is None
    assert snapshot["last_error_class"] is None
    assert snapshot["worker_health"]["last_cycle_finished_at"] is not None

    # no raw identifiers anywhere in the serialized read model
    encoded = json.dumps(snapshot)
    assert child["username"] not in encoded
    assert '"account_id"' not in encoded
    assert "uuid_verifier" not in encoded
    assert "token" not in encoded


def test_backlog_snapshot_reports_backlog_age_and_error_class(db):
    fx, _period = _disabled_fixture(db, "RECON_OBS_ERR")
    child = fx["children"][0]
    del fx["remote"].users[child["username"]]
    _recon(db, fx)

    snapshot = backlog_snapshot(db, now=NOW + 120)
    assert snapshot["account_states"].get("ERROR_RECONCILE") == 1
    assert snapshot["drift"]["flagged"] == 1
    assert snapshot["last_error_class"] == "DRIFT_REMOTE_MISSING"


def test_scheduler_units_exist_in_repo():
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    service = os.path.join(repo, "mgboost-wl-enforcement.service")
    timer = os.path.join(repo, "mgboost-wl-enforcement.timer")
    assert os.path.exists(service)
    assert os.path.exists(timer)
    service_text = open(service).read()
    timer_text = open(timer).read()
    assert "run_wl_quota_enforcement.py" in service_text
    assert "--trigger SCHEDULED" in service_text
    assert "mgboost-wl-enforcement.service" in timer_text
    assert "OnUnitActiveSec" in timer_text
