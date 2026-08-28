"""PH6-09 -- overshoot/outage fail-safe policy on top of the deployed
PH6-07 runtime.

The two governing invariants of this whole suite:

1.  **Uncertainty cannot increase WL access.**  Every access-INCREASING
    action (DISABLED -> ACTIVE restore, drift auto-add of a newly-approved
    exact WL inbound) requires FRESH trustworthy usage telemetry, a FRESH
    topology assertion and a FRESH entitlement proof; anything stale or
    unknown fails closed with zero remote mutation.
2.  **Uncertainty alone cannot blindly mass-disable already-active WL
    users.**  A collector/node outage never turns into an outage of all WL
    clients: stale telemetry freezes only what it cannot prove, and never
    fabricates quota exhaustion (a stale ledger can only UNDER-count, and
    an under-count can never produce a fresh `exceeded` proof that was not
    already there).

Plus the owner decision (ROADMAP Decision Log DL-059): an ACTIVE LIMITED
child below quota automatically gains a NEWLY-APPROVED exact WL inbound
(operator-approved versioned PH0-05 baseline update) through the EXISTING
PH6-07 drift-repair path -- legitimate topology convergence, never
ERROR_RECONCILE; unknown/wl-like tags stay fail-closed (PH6-01 contract
unchanged); the symmetric DISABLED case keeps removing such tags.
"""

import json
import os
import time

import pytest

from src.child_contract import credential_verifier
from src.wl_enforcement import run_wl_enforcement_cycle
from src.wl_reconciliation import (
    backlog_snapshot,
    run_wl_reconciliation_cycle,
    scan_terminal_drift,
)
from src.wl_usage_ledger import run_collection_cycle

from tests.test_wl_enforcement import (
    NOW,
    NON_WL_A,
    WL_A,
    WL_B,
    WlBackedClient,
    _burn_quota,
    _enforce_fixture,
    _inbounds_of,
    _modify_count,
    _ok_observer,
    _seed_limited_period,
)

WORKER = "ph609-worker"
FRESH_MAX_AGE = 1800  # technical default: 3x the 10-minute collector cadence


@pytest.fixture
def db(monkeypatch):
    import importlib
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
# Freshness helpers
# --------------------------------------------------------------------------

def _mark_collector_fresh(db, *, now=NOW, outcome="OK"):
    """Simulate a recently successful PH6-03 collector run (the real writer
    of this row is `run_collection_cycle`'s lease release)."""
    db._conn.execute(
        "UPDATE mgboost_wl_usage_collector_lease SET last_run_started_at=?,"
        "last_run_completed_at=?,last_run_outcome=?,last_run_error_class=NULL "
        "WHERE id=1",
        (now - 5, now, outcome),
    )
    db._conn.commit()


def _mark_collector_stale(db, *, now=NOW, outcome="OK", completed_at=None):
    db._conn.execute(
        "UPDATE mgboost_wl_usage_collector_lease SET last_run_started_at=?,"
        "last_run_completed_at=?,last_run_outcome=? WHERE id=1",
        (completed_at if completed_at is not None else now - 10 * FRESH_MAX_AGE,
         completed_at if completed_at is not None else now - 10 * FRESH_MAX_AGE,
         outcome),
    )
    db._conn.commit()


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------

def _active_fixture(db, mapping):
    """Account converged to ACTIVE (LIMITED, below quota), collector fresh."""
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
    _seed_limited_period(db, account_id=account_id, now=NOW + 10,
                         quota_bytes=5_000_000_000, starts_at=NOW + 10)
    _mark_collector_fresh(db, now=NOW + 20)
    run_wl_enforcement_cycle(
        db=db, service_marzban=fx["client"], worker_id="fix-restore",
        now=NOW + 20, topology_observer=_ok_observer(),
    )
    state = db.wl_enforcement.get_state(account_id)
    assert state["state"] == "ACTIVE" and state["last_direction"] == "INCLUDED"
    return fx


def _recon(db, fx, *, now=NOW, observer=None, worker=WORKER):
    return run_wl_reconciliation_cycle(
        db=db, service_marzban=fx["client"], worker_id=worker, now=now,
        trigger="SCHEDULED", topology_observer=observer or _ok_observer(),
    )


def _approve_expansion(monkeypatch):
    """The operator APPROVES a new exact WL inbound: PH0-05 baseline grows by
    one tag and the config version is bumped -- the exact owner-approved
    versioned-update shape from the PH6-01 contract. Called AFTER the child
    has already converged under the OLD version, so the expansion is a real
    post-convergence event."""
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
# Freshness contract
# --------------------------------------------------------------------------

def test_freshness_ok_requires_recent_successful_collector_run(db):
    from src.wl_freshness import usage_freshness

    # never ran: UNKNOWN, never fresh
    snapshot = usage_freshness(db, now=NOW)
    assert snapshot["fresh"] is False
    assert snapshot["last_ok_run_at"] is None

    _mark_collector_fresh(db, now=NOW)
    snapshot = usage_freshness(db, now=NOW + 60)
    assert snapshot["fresh"] is True
    assert snapshot["age_seconds"] == 60

    # too old
    snapshot = usage_freshness(db, now=NOW + FRESH_MAX_AGE + 1)
    assert snapshot["fresh"] is False

    # a failed run is not a trusted observation even when recent
    _mark_collector_fresh(db, now=NOW + FRESH_MAX_AGE + 10, outcome="ERROR")
    assert usage_freshness(db, now=NOW + FRESH_MAX_AGE + 12)["fresh"] is False

    # a partial run never proves the fleet's telemetry either
    _mark_collector_fresh(db, now=NOW + FRESH_MAX_AGE + 20, outcome="PARTIAL")
    assert usage_freshness(db, now=NOW + FRESH_MAX_AGE + 22)["fresh"] is False


def test_freshness_rejects_future_completed_at_clock_skew(db):
    """A `last_run_completed_at` in the future relative to `now` (clock
    skew, or a corrupted row) must never be treated as maximally fresh.
    Clamping a negative age to 0 would make an untrustworthy timestamp
    look like the freshest possible observation -- fail closed instead."""
    from src.wl_freshness import usage_freshness

    _mark_collector_fresh(db, now=NOW + 3600, outcome="OK")
    snapshot = usage_freshness(db, now=NOW)
    assert snapshot["fresh"] is False
    assert snapshot["age_seconds"] == NOW - (NOW + 3600)


def test_stale_usage_cannot_restore_disabled_account(db):
    """Invariant 1: DISABLED -> ACTIVE (access-increasing) is refused while
    usage telemetry is stale -- fail closed, zero mutation, observable."""
    fx = _enforce_fixture(db, mapping="PH609_STALE_RESTORE")
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

    # quota available again (new period), but the collector is stale
    db._conn.execute("UPDATE mgboost_wl_periods SET status='CLOSED' WHERE id=?", (period,))
    _seed_limited_period(db, account_id=account_id, now=NOW + 10,
                         quota_bytes=5_000_000_000, starts_at=NOW + 10)
    _mark_collector_stale(db, now=NOW + 20)
    child = fx["children"][0]
    baseline = _modify_count(fx["remote"], child["username"])

    summary = run_wl_enforcement_cycle(
        db=db, service_marzban=fx["client"], worker_id="stale-cycle",
        now=NOW + 20, topology_observer=_ok_observer(),
    )

    # the access-increase was BLOCKED, not performed
    assert summary["accounts_skipped_stale_usage"] == 1
    state = db.wl_enforcement.get_state(account_id)
    assert state["state"] == "DISABLED"
    assert _modify_count(fx["remote"], child["username"]) == baseline


def test_recovery_after_collector_outage_restores_exactly_once(db):
    """Two consecutive outage/recovery shapes: while stale the account stays
    frozen; the first FRESH cycle converges it, and a replay of the same
    fresh cycle writes nothing."""
    fx = _enforce_fixture(db, mapping="PH609_RECOVERY")
    account_id = fx["account"]["account_id"]
    period = _seed_limited_period(db, account_id=account_id, now=NOW)
    _burn_quota(db, account_id=account_id,
                child_intent_id=fx["children"][0]["fx_child"], period_id=period,
                total_bytes=2_000_000_000)
    run_wl_enforcement_cycle(
        db=db, service_marzban=fx["client"], worker_id="fix-disable",
        now=NOW, topology_observer=_ok_observer(),
    )
    db._conn.execute("UPDATE mgboost_wl_periods SET status='CLOSED' WHERE id=?", (period,))
    _seed_limited_period(db, account_id=account_id, now=NOW + 10,
                         quota_bytes=5_000_000_000, starts_at=NOW + 10)
    child = fx["children"][0]
    _mark_collector_stale(db, now=NOW + 20)
    run_wl_enforcement_cycle(
        db=db, service_marzban=fx["client"], worker_id="stale-1",
        now=NOW + 20, topology_observer=_ok_observer(),
    )
    _mark_collector_stale(db, now=NOW + 40)
    run_wl_enforcement_cycle(
        db=db, service_marzban=fx["client"], worker_id="stale-2",
        now=NOW + 40, topology_observer=_ok_observer(),
    )
    assert db.wl_enforcement.get_state(account_id)["state"] == "DISABLED"
    baseline = _modify_count(fx["remote"], child["username"])

    _mark_collector_fresh(db, now=NOW + 60)
    summary = run_wl_enforcement_cycle(
        db=db, service_marzban=fx["client"], worker_id="fresh-1",
        now=NOW + 60, topology_observer=_ok_observer(),
    )
    assert summary["accounts_enabled"] == 1
    assert db.wl_enforcement.get_state(account_id)["state"] == "ACTIVE"
    after_first = _modify_count(fx["remote"], child["username"])
    assert after_first == baseline + 1

    summary = run_wl_enforcement_cycle(
        db=db, service_marzban=fx["client"], worker_id="fresh-2",
        now=NOW + 80, topology_observer=_ok_observer(),
    )
    assert summary["ops_applied"] == 0
    assert _modify_count(fx["remote"], child["username"]) == after_first


def test_stale_usage_cannot_fabricate_quota_exhaustion(db):
    """Invariant 2: a stale ledger can only UNDER-count, so it can never
    produce a NEW disable -- an already-ACTIVE user survives a collector
    outage untouched (no mass-disable)."""
    fx = _active_fixture(db, "PH609_STALE_ACTIVE")
    account_id = fx["account"]["account_id"]
    child = fx["children"][0]
    baseline = _modify_count(fx["remote"], child["username"])

    _mark_collector_stale(db, now=NOW + 40)
    summary = run_wl_enforcement_cycle(
        db=db, service_marzban=fx["client"], worker_id="stale-active",
        now=NOW + 40, topology_observer=_ok_observer(),
    )

    assert summary["accounts_disabled"] == 0
    state = db.wl_enforcement.get_state(account_id)
    assert state["state"] == "ACTIVE"
    assert _modify_count(fx["remote"], child["username"]) == baseline


# --------------------------------------------------------------------------
# DL-059: ACTIVE + newly-approved exact WL inbound auto-add
# --------------------------------------------------------------------------

def test_active_child_auto_adds_newly_approved_wl_inbound(db, monkeypatch):
    fx = _active_fixture(db, "PH609_AUTO_ADD")
    expansion = _approve_expansion(monkeypatch)
    new_tag = expansion["new_tag"]
    account_id = fx["account"]["account_id"]
    child = fx["children"][0]
    baseline_mutations = _modify_count(fx["remote"], child["username"])
    before = _inbounds_of(fx["remote"], child["username"])
    # the version registry must know the child's converged version BEFORE the
    # expansion: assert the OLD topology once more at the old version -- done
    # implicitly, because the fixture above ran full cycles under it.

    _mark_collector_fresh(db, now=NOW + 30)
    summary = _recon(
        db, fx, now=NOW + 30, observer=expansion["observer"](),
    )

    assert summary["outcome"] == "OK"
    assert summary["drift"]["detected"] == 1
    assert summary["drift"]["repaired"] == 1
    rows = [dict(r) for r in db._conn.execute(
        "SELECT * FROM mgboost_wl_reconciliation_drift").fetchall()]
    assert rows[0]["drift_class"] == "WL_MISSING_WHILE_INCLUDED"
    assert rows[0]["action"] == "REPAIR_QUEUED"
    # ONLY the newly-approved tag was added; everything else byte-identical
    after = _inbounds_of(fx["remote"], child["username"])
    assert set(after) - set(before) == {new_tag}
    assert set(before) - set(after) == set()
    assert _modify_count(fx["remote"], child["username"]) == baseline_mutations + 1
    state = db.wl_enforcement.get_state(account_id)
    assert state["state"] == "ACTIVE"

    # replay cycle: exactly-once -- zero writes
    _mark_collector_fresh(db, now=NOW + 60)
    summary = _recon(
        db, fx, now=NOW + 60, observer=expansion["observer"](),
    )
    assert summary["drift"]["detected"] == 0
    assert _inbounds_of(fx["remote"], child["username"]) == after
    assert _modify_count(fx["remote"], child["username"]) == baseline_mutations + 1


def test_auto_add_blocked_while_usage_stale(db, monkeypatch):
    """Access-increasing auto-add fails closed on stale telemetry."""
    fx = _active_fixture(db, "PH609_AUTO_ADD_STALE")
    expansion = _approve_expansion(monkeypatch)
    new_tag = expansion["new_tag"]
    child = fx["children"][0]
    baseline_mutations = _modify_count(fx["remote"], child["username"])
    before = _inbounds_of(fx["remote"], child["username"])

    _mark_collector_stale(db, now=NOW + 30)
    summary = _recon(
        db, fx, now=NOW + 30, observer=expansion["observer"](),
    )

    assert summary["drift"].get("access_increase_blocked", 0) >= 1
    assert summary["drift"]["repaired"] == 0
    assert _inbounds_of(fx["remote"], child["username"]) == before
    assert _modify_count(fx["remote"], child["username"]) == baseline_mutations
    # the account is left ACTIVE and untouched (never mass-disabled)
    assert db.wl_enforcement.get_state(fx["account"]["account_id"])["state"] == "ACTIVE"


def test_newly_approved_tag_on_active_child_is_legitimate_not_error(
    db, monkeypatch,
):
    """An approved tag that already reached the child (e.g. added during the
    same approved update) is legitimate convergence -- NOT the old
    conservative WL_UNEXPECTED_WHILE_INCLUDED flag."""
    fx = _active_fixture(db, "PH609_LEGIT_NEW_TAG")
    expansion = _approve_expansion(monkeypatch)
    new_tag = expansion["new_tag"]
    child = fx["children"][0]
    baseline_mutations = _modify_count(fx["remote"], child["username"])
    fx["remote"].users[child["username"]]["inbounds"]["vless"] = sorted(
        set(_inbounds_of(fx["remote"], child["username"])) | {new_tag}
    )

    _mark_collector_fresh(db, now=NOW + 30)
    summary = _recon(
        db, fx, now=NOW + 30, observer=expansion["observer"](),
    )

    assert summary["drift"]["flagged"] == 0
    assert summary["drift"]["repaired"] == 0
    state = db.wl_enforcement.get_state(fx["account"]["account_id"])
    assert state["state"] == "ACTIVE"
    assert new_tag in _inbounds_of(fx["remote"], child["username"])
    assert _modify_count(fx["remote"], child["username"]) == baseline_mutations


def test_suspended_child_still_loses_newly_approved_tag(db, monkeypatch):
    """Symmetry (section 9): the DISABLED case keeps removing the tag."""
    expansion = _approve_expansion(monkeypatch)
    new_tag = expansion["new_tag"]
    fx = _enforce_fixture(db, mapping="PH609_SUSPENDED_SYM")
    account_id = fx["account"]["account_id"]
    period = _seed_limited_period(db, account_id=account_id, now=NOW)
    _burn_quota(db, account_id=account_id,
                child_intent_id=fx["children"][0]["fx_child"], period_id=period,
                total_bytes=2_000_000_000)
    run_wl_enforcement_cycle(
        db=db, service_marzban=fx["client"], worker_id="fix-disable",
        now=NOW, topology_observer=expansion["observer"](),
    )
    assert db.wl_enforcement.get_state(account_id)["state"] == "DISABLED"
    child = fx["children"][0]
    baseline_mutations = _modify_count(fx["remote"], child["username"])
    fx["remote"].users[child["username"]]["inbounds"]["vless"] = sorted(
        set(_inbounds_of(fx["remote"], child["username"])) | {new_tag}
    )

    summary = _recon(
        db, fx, observer=expansion["observer"](),
    )

    rows = [dict(r) for r in db._conn.execute(
        "SELECT * FROM mgboost_wl_reconciliation_drift").fetchall()]
    assert rows[0]["drift_class"] == "WL_PRESENT_WHILE_EXCLUDED"
    assert _inbounds_of(fx["remote"], child["username"]) == [NON_WL_A]
    assert _modify_count(fx["remote"], child["username"]) == baseline_mutations + 1


def test_unknown_wl_tag_still_blocks_whole_cycle(db):
    fx = _active_fixture(db, "PH609_UNKNOWN_STILL_CLOSED")
    child = fx["children"][0]
    baseline_mutations = _modify_count(fx["remote"], child["username"])
    tags, nodes = _ok_observer()()
    unknown = frozenset(set(tags) | {"wl-something-unapproved"})

    summary = _recon(db, fx, observer=lambda: (unknown, nodes))

    assert summary["outcome"] == "BLOCKED_TOPOLOGY"
    assert _modify_count(fx["remote"], child["username"]) == baseline_mutations


def test_version_registry_unknown_version_yields_no_added_tags(db):
    """Fail closed: a child whose frozen manifest version is unknown to the
    registry auto-adds nothing."""
    from src.wl_topology_versions import tags_added_since

    assert tags_added_since(
        db._conn, frozenset({"a", "b"}), "never-seen-version",
    ) == frozenset()
    assert tags_added_since(db._conn, frozenset({"a"}), None) == frozenset()


# --------------------------------------------------------------------------
# Collector / enforcement runtime chain (the PH6-09 scheduling blocker)
# --------------------------------------------------------------------------

def test_collector_units_exist_and_match_enforcement_shape():
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    service = os.path.join(repo, "mgboost-wl-usage-collector.service")
    timer = os.path.join(repo, "mgboost-wl-usage-collector.timer")
    service_text = open(service).read()
    timer_text = open(timer).read()
    assert "run_wl_usage_collector.py" in service_text
    assert "EnvironmentFile=/opt/MGBoost_Panel/.env" in service_text
    assert "mgboost-wl-usage-collector.service" in timer_text
    assert "OnUnitActiveSec" in timer_text
    # the collector must run at least as often as enforcement can act
    assert "OnBootSec" in timer_text


def test_enforcement_and_collector_cadence_bounds_documented():
    """The demonstrated overshoot window is derived from the real unit
    cadences, not invented: collector 10min + enforcement 15min + bounded
    retry. The freshness default must cover normal jitter (>= the collector
    cadence) while staying tight enough to keep the bound meaningful."""
    from src.wl_freshness import USAGE_FRESHNESS_MAX_AGE_SECONDS

    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    collector_timer = open(
        os.path.join(repo, "mgboost-wl-usage-collector.timer")).read()
    enforcement_timer = open(
        os.path.join(repo, "mgboost-wl-enforcement.timer")).read()
    assert "OnUnitActiveSec=10min" in collector_timer
    assert "OnUnitActiveSec=15min" in enforcement_timer
    assert USAGE_FRESHNESS_MAX_AGE_SECONDS >= 600
    assert USAGE_FRESHNESS_MAX_AGE_SECONDS <= 3600


def test_real_collector_cycle_then_enforcement_chain(db):
    """The full production chain: a REAL run_collection_cycle (fake remote)
    makes the ledger fresh; the enforcement chain then acts; the snapshot
    exposes the freshness and the demonstrated overshoot bounds."""
    fx = _enforce_fixture(db, mapping="PH609_CHAIN")
    account_id = fx["account"]["account_id"]
    child = fx["children"][0]
    # seed the remote usage endpoint through the fixture's backing client
    period = _seed_limited_period(db, account_id=account_id, now=NOW)
    _burn_quota(db, account_id=account_id,
                child_intent_id=child["fx_child"], period_id=period,
                total_bytes=2_000_000_000)
    _mark_collector_stale(db, now=NOW)

    usage_payload = {"usages": [{"node_id": nid, "used_traffic": 0} for nid in (4, 7)]}
    client = WlBackedClient(fx["remote"])
    fx["client"].get_user_usage = lambda username, start, end: usage_payload
    db.wl_topology_guard.run_assertion(*_ok_observer()(), now=NOW + 5)

    collected = run_collection_cycle(
        db=db, service_marzban=fx["client"], worker_id="chain-collector",
        now=NOW + 5,
    )
    assert collected["outcome"] == "OK"

    summary = run_wl_reconciliation_cycle(
        db=db, service_marzban=fx["client"], worker_id="chain-enforce",
        now=NOW + 300, trigger="SCHEDULED", topology_observer=_ok_observer(),
    )
    assert summary["outcome"] in ("OK", "PARTIAL")
    # the collector stamps its release with the real wall clock, so judge
    # freshness against the same clock
    snapshot = backlog_snapshot(db, now=int(time.time()))
    assert snapshot["collector_freshness"]["fresh"] is True
    assert snapshot["collector_freshness"]["last_run_outcome"] == "OK"
    assert "overshoot_bounds" in snapshot
    encoded = json.dumps(snapshot)
    assert child["username"] not in encoded
    assert "uuid" not in encoded.lower()
