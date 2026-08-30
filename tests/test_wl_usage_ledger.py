"""PH6-03 durable monotonic WL usage ledger/collector.

Every scenario proves the exact contract the roadmap's own Require/Tests
lines name: unique period/child/node/sample-hour, idempotency, non-decreasing
usage, cursor/snapshot, one leader or CAS/shared lock, retry/reconcile --
duplicate/out-of-order/node-reset/two-collectors/clock-skew/delay.
"""

import importlib
import os
import sqlite3
import tempfile

import pytest

from src.wl_topology import WL_NODES
from src.wl_usage_ledger import WLUsageLedgerError, run_collection_cycle

from tests.test_child_lifecycle import _build_applied_child
from tests.test_child_provisioning import PRIMARY, PRIMARY_LOGIN, _account


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


def _clean_topology_ok(db, now=1):
    from src.wl_topology import WL_INBOUND_TAGS
    tags = set(WL_INBOUND_TAGS)
    nodes = {n["id"]: {"role": n["role"], "address": n["address"], "usage_coefficient": n["usage_coefficient"]}
             for n in WL_NODES}
    db.wl_topology_guard.run_assertion(tags, nodes, now=now)


def _seed_wl_period(db, *, account_id, starts_at, ends_at, now, status="ACTIVE",
                    quota_mode="UNLIMITED", base_quota_bytes=None):
    conn = db._conn
    subscription = conn.execute(
        "SELECT id FROM mgboost_subscriptions WHERE account_id=?", (account_id,)
    ).fetchone()
    subscription_id = subscription["id"]
    mutation_id = conn.execute(
        "INSERT INTO mgboost_entitlement_mutations "
        "(account_id,operation,payment_channel,mutation_source,actor_type,created_at) "
        "VALUES (?,'TEST_SEED','NOT_APPLICABLE','SYSTEM','SYSTEM',?)",
        (account_id, now),
    ).lastrowid
    next_seq = (conn.execute(
        "SELECT COALESCE(MAX(sequence_no),0)+1 FROM mgboost_subscription_terms WHERE subscription_id=?",
        (subscription_id,),
    ).fetchone()[0])
    term_id = conn.execute(
        "INSERT INTO mgboost_subscription_terms "
        "(account_id,subscription_id,sequence_no,plan_snapshot_json,mutation_id,created_at) "
        "VALUES (?,?,?,'{}',?,?)",
        (account_id, subscription_id, next_seq, mutation_id, now),
    ).lastrowid
    period_seq = (conn.execute(
        "SELECT COALESCE(MAX(sequence_no),0)+1 FROM mgboost_wl_periods WHERE subscription_id=?",
        (subscription_id,),
    ).fetchone()[0])
    period_id = conn.execute(
        "INSERT INTO mgboost_wl_periods "
        "(account_id,subscription_id,subscription_term_id,sequence_no,starts_at,ends_at,"
        "quota_mode,base_quota_bytes,status,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (account_id, subscription_id, term_id, period_seq, starts_at, ends_at,
         quota_mode, base_quota_bytes, status, now),
    ).lastrowid
    conn.commit()
    return period_id


def _seed_active_wl_period(db, *, account_id, starts_at, ends_at, now,
                           quota_mode="UNLIMITED", base_quota_bytes=None):
    return _seed_wl_period(
        db, account_id=account_id, starts_at=starts_at, ends_at=ends_at, now=now,
        status="ACTIVE", quota_mode=quota_mode, base_quota_bytes=base_quota_bytes,
    )


class FakeServiceMarzban:
    """Test double standing in for `ServiceMarzbanClient.get_user_usage`."""

    def __init__(self):
        self.responses = {}  # username -> list[{"node_id":..,"used_traffic":..}]
        self.calls = []
        self.raise_for = set()

    def set_usage(self, username, node_usages: dict):
        self.responses[username] = [
            {"node_id": node_id, "node_name": str(node_id), "used_traffic": value}
            for node_id, value in node_usages.items()
        ]

    def get_user_usage(self, username, admin_token=None, start="", end=""):
        self.calls.append((username, start, end))
        if username in self.raise_for:
            raise RuntimeError("simulated Marzban outage")
        return {"usages": self.responses.get(username, []), "username": username}


# --- record_sample: the core idempotent write ---------------------------

def _ids(db):
    fx = _build_applied_child(db)
    return fx["account"]["account_id"], fx["child_intent_id"]


def test_first_sample_records_full_observed_value_as_delta(db):
    account_id, child_intent_id = _ids(db)
    result = db.wl_usage_ledger.record_sample(
        account_id=account_id, child_intent_id=child_intent_id, node_id=4, cursor_after=1000,
        collector_id="w1", collected_at=100,
    )
    assert result["cursor_before"] == 0
    assert result["delta_bytes"] == 1000
    assert result["reset_detected"] is False
    row = db._conn.execute(
        "SELECT bytes_delta FROM mgboost_wl_usage_samples WHERE child_intent_id=? AND node_id=4",
        (child_intent_id,),
    ).fetchone()
    assert row["bytes_delta"] == 1000


def test_second_sample_records_only_the_new_delta(db):
    account_id, child_intent_id = _ids(db)
    db.wl_usage_ledger.record_sample(
        account_id=account_id, child_intent_id=child_intent_id, node_id=4, cursor_after=1000,
        collector_id="w1", collected_at=100,
    )
    result = db.wl_usage_ledger.record_sample(
        account_id=account_id, child_intent_id=child_intent_id, node_id=4, cursor_after=1500,
        collector_id="w1", collected_at=200,
    )
    assert result["cursor_before"] == 1000
    assert result["delta_bytes"] == 500
    row = db._conn.execute(
        "SELECT bytes_delta FROM mgboost_wl_usage_samples WHERE child_intent_id=? AND node_id=4",
        (child_intent_id,),
    ).fetchone()
    assert row["bytes_delta"] == 1500  # same UTC hour: accumulated, not overwritten


def test_same_child_node_hour_different_wl_periods_stay_separate(db):
    """PH6 compatibility regression: a promo boundary may be inside a UTC
    hour.  A pre-boundary and post-boundary observation must produce two
    immutable period-attributed buckets, never old_period=200."""
    fx = _build_applied_child(db)
    account_id = fx["account"]["account_id"]
    child_intent_id = fx["child_intent_id"]
    old_period = _seed_active_wl_period(
        db, account_id=account_id, starts_at=0, ends_at=1500, now=1,
        quota_mode="LIMITED", base_quota_bytes=100,
    )
    new_period = _seed_wl_period(
        db, account_id=account_id, starts_at=1500, ends_at=5100, now=1, status="PLANNED",
        quota_mode="LIMITED", base_quota_bytes=100,
    )

    db.wl_usage_ledger.record_sample(
        account_id=account_id, child_intent_id=child_intent_id, node_id=4,
        cursor_after=100, collector_id="w1", collected_at=1400, wl_period_id=old_period,
    )
    db.wl_usage_ledger.record_sample(
        account_id=account_id, child_intent_id=child_intent_id, node_id=4,
        cursor_after=200, collector_id="w1", collected_at=1600, wl_period_id=new_period,
    )
    rows = db._conn.execute(
        "SELECT sample_hour,wl_period_id,bytes_delta FROM mgboost_wl_usage_samples "
        "WHERE child_intent_id=? AND node_id=4 ORDER BY wl_period_id",
        (child_intent_id,),
    ).fetchall()
    assert [(r["sample_hour"], r["wl_period_id"], r["bytes_delta"]) for r in rows] == [
        (0, old_period, 100), (0, new_period, 100),
    ]
    from src.wl_parent_pool import compute_parent_wl_pool
    old_pool = compute_parent_wl_pool(db._conn, account_id=account_id, wl_period_id=old_period)
    new_pool = compute_parent_wl_pool(db._conn, account_id=account_id, wl_period_id=new_period)
    assert (old_pool["consumed_bytes"], old_pool["exceeded"]) == (100, True)
    assert (new_pool["consumed_bytes"], new_pool["exceeded"]) == (100, True)


def test_same_child_node_hour_same_wl_period_still_aggregates(db):
    account_id, child_intent_id = _ids(db)
    period_id = _seed_active_wl_period(
        db, account_id=account_id, starts_at=0, ends_at=5100, now=1,
    )
    db.wl_usage_ledger.record_sample(
        account_id=account_id, child_intent_id=child_intent_id, node_id=4,
        cursor_after=100, collector_id="w1", collected_at=1400, wl_period_id=period_id,
    )
    db.wl_usage_ledger.record_sample(
        account_id=account_id, child_intent_id=child_intent_id, node_id=4,
        cursor_after=200, collector_id="w1", collected_at=1600, wl_period_id=period_id,
    )
    rows = db._conn.execute(
        "SELECT wl_period_id,bytes_delta FROM mgboost_wl_usage_samples "
        "WHERE child_intent_id=? AND node_id=4", (child_intent_id,),
    ).fetchall()
    assert [(r["wl_period_id"], r["bytes_delta"]) for r in rows] == [(period_id, 200)]


def test_negative_observed_value_rejected(db):
    account_id, child_intent_id = _ids(db)
    with pytest.raises(WLUsageLedgerError):
        db.wl_usage_ledger.record_sample(
            account_id=account_id, child_intent_id=child_intent_id, node_id=4, cursor_after=-1,
            collector_id="w1", collected_at=100,
        )


def test_different_nodes_and_hours_never_collide(db):
    account_id, child_intent_id = _ids(db)
    db.wl_usage_ledger.record_sample(
        account_id=account_id, child_intent_id=child_intent_id, node_id=4, cursor_after=100,
        collector_id="w1", collected_at=100,
    )
    db.wl_usage_ledger.record_sample(
        account_id=account_id, child_intent_id=child_intent_id, node_id=7, cursor_after=999,
        collector_id="w1", collected_at=100,
    )
    db.wl_usage_ledger.record_sample(
        account_id=account_id, child_intent_id=child_intent_id, node_id=4, cursor_after=100,
        collector_id="w1", collected_at=100 + 3600,
    )
    rows = db._conn.execute(
        "SELECT node_id, sample_hour, bytes_delta FROM mgboost_wl_usage_samples "
        "WHERE child_intent_id=? ORDER BY node_id, sample_hour",
        (child_intent_id,),
    ).fetchall()
    assert len(rows) == 3


# --- duplicate / two-collectors idempotency -----------------------------

def test_duplicate_stale_cursor_read_does_not_double_count(db):
    """Simulates two collectors racing on the same unconsumed cursor state:
    both read cursor_before=0 at the same time, one wins the race and
    commits first; the second's identical (child, node, cursor_before)
    transition must be rejected as a duplicate, not double-added."""
    account_id, child_intent_id = _ids(db)
    first = db.wl_usage_ledger.record_sample(
        account_id=account_id, child_intent_id=child_intent_id, node_id=4, cursor_after=100,
        collector_id="collector-a", collected_at=100,
    )
    assert first["delta_bytes"] == 100
    # Simulate collector B's concurrent read of the pre-commit cursor state.
    db._conn.execute(
        "UPDATE mgboost_wl_usage_cursors SET last_observed_cumulative_bytes=0 "
        "WHERE child_intent_id=? AND node_id=4",
        (child_intent_id,),
    )
    db._conn.commit()
    replay = db.wl_usage_ledger.record_sample(
        account_id=account_id, child_intent_id=child_intent_id, node_id=4, cursor_after=100,
        collector_id="collector-b", collected_at=105,
    )
    assert replay["cursor_before"] == 0
    assert replay["cursor_after"] == 100
    assert replay["delta_bytes"] == 100  # returns the ORIGINAL recorded event, not a new one
    row = db._conn.execute(
        "SELECT bytes_delta FROM mgboost_wl_usage_samples WHERE child_intent_id=? AND node_id=4",
        (child_intent_id,),
    ).fetchone()
    assert row["bytes_delta"] == 100  # not 200 -- no double count
    events = db._conn.execute(
        "SELECT COUNT(*) c FROM mgboost_wl_usage_sample_events WHERE child_intent_id=? AND node_id=4",
        (child_intent_id,),
    ).fetchone()
    assert events["c"] == 1


# --- node reset / decrease ------------------------------------------------

def test_reset_decrease_is_detected_and_never_subtracted(db):
    account_id, child_intent_id = _ids(db)
    db.wl_usage_ledger.record_sample(
        account_id=account_id, child_intent_id=child_intent_id, node_id=4, cursor_after=5000,
        collector_id="w1", collected_at=100,
    )
    result = db.wl_usage_ledger.record_sample(
        account_id=account_id, child_intent_id=child_intent_id, node_id=4, cursor_after=200,  # admin reset happened
        collector_id="w1", collected_at=200,
    )
    assert result["reset_detected"] is True
    assert result["delta_bytes"] == 200  # the post-reset value, never a negative delta
    row = db._conn.execute(
        "SELECT bytes_delta FROM mgboost_wl_usage_samples WHERE child_intent_id=? AND node_id=4",
        (child_intent_id,),
    ).fetchone()
    assert row["bytes_delta"] == 5200  # 5000 (real, already recorded) + 200 -- never decreases
    cursor = db._conn.execute(
        "SELECT last_observed_cumulative_bytes, reset_count FROM mgboost_wl_usage_cursors "
        "WHERE child_intent_id=? AND node_id=4",
        (child_intent_id,),
    ).fetchone()
    assert cursor["last_observed_cumulative_bytes"] == 200
    assert cursor["reset_count"] == 1


def test_sample_row_itself_refuses_any_direct_decrease(db):
    account_id, child_intent_id = _ids(db)
    db.wl_usage_ledger.record_sample(
        account_id=account_id, child_intent_id=child_intent_id, node_id=4, cursor_after=1000,
        collector_id="w1", collected_at=100,
    )
    with pytest.raises(sqlite3.IntegrityError):
        db._conn.execute(
            "UPDATE mgboost_wl_usage_samples SET bytes_delta=1 WHERE child_intent_id=? AND node_id=4",
            (child_intent_id,),
        )


# --- out-of-order / clock skew / delay ------------------------------------

def test_out_of_order_delayed_sample_with_smaller_value_treated_as_safe_reset(db):
    account_id, child_intent_id = _ids(db)
    db.wl_usage_ledger.record_sample(
        account_id=account_id, child_intent_id=child_intent_id, node_id=4, cursor_after=1000,
        collector_id="w1", collected_at=500,
    )
    # A delayed poll response arrives late, timestamped earlier than the one
    # already processed, reporting a smaller cumulative value.
    result = db.wl_usage_ledger.record_sample(
        account_id=account_id, child_intent_id=child_intent_id, node_id=4, cursor_after=300,
        collector_id="w1", collected_at=400,
    )
    assert result["reset_detected"] is True
    assert result["delta_bytes"] == 300
    # Never raises, never corrupts state with a negative delta.


def test_delayed_sample_hour_is_bucketed_by_processing_time_not_by_delay(db):
    account_id, child_intent_id = _ids(db)
    db.wl_usage_ledger.record_sample(
        account_id=account_id, child_intent_id=child_intent_id, node_id=4, cursor_after=100,
        collector_id="w1", collected_at=10,
    )
    db.wl_usage_ledger.record_sample(
        account_id=account_id, child_intent_id=child_intent_id, node_id=4, cursor_after=200,
        collector_id="w1", collected_at=3650,  # next UTC hour
    )
    rows = db._conn.execute(
        "SELECT sample_hour, bytes_delta FROM mgboost_wl_usage_samples "
        "WHERE child_intent_id=? AND node_id=4 ORDER BY sample_hour",
        (child_intent_id,),
    ).fetchall()
    assert [r["sample_hour"] for r in rows] == [0, 3600]
    assert [r["bytes_delta"] for r in rows] == [100, 100]


# --- collector lease: one leader / two collectors -------------------------

def test_collector_lease_is_exclusive(db):
    assert db.wl_usage_ledger.claim_collector_lease(worker_id="a", now=100, lease_seconds=60) is True
    assert db.wl_usage_ledger.claim_collector_lease(worker_id="b", now=110, lease_seconds=60) is False


def test_collector_lease_reclaimable_after_expiry(db):
    assert db.wl_usage_ledger.claim_collector_lease(worker_id="a", now=100, lease_seconds=10) is True
    assert db.wl_usage_ledger.claim_collector_lease(worker_id="b", now=111, lease_seconds=10) is True


def test_release_requires_matching_owner(db):
    db.wl_usage_ledger.claim_collector_lease(worker_id="a", now=100, lease_seconds=60)
    with pytest.raises(WLUsageLedgerError):
        db.wl_usage_ledger.release_collector_lease(worker_id="b", now=105)


def test_release_frees_lease_for_next_claimant(db):
    db.wl_usage_ledger.claim_collector_lease(worker_id="a", now=100, lease_seconds=60)
    db.wl_usage_ledger.release_collector_lease(worker_id="a", now=101)
    assert db.wl_usage_ledger.claim_collector_lease(worker_id="b", now=102, lease_seconds=60) is True


# --- wl_period attribution -------------------------------------------------

def test_resolve_active_wl_period_none_when_no_periods_exist(db):
    fx = _build_applied_child(db)
    account_id = fx["account"]["account_id"]
    assert db.wl_usage_ledger.resolve_active_wl_period(account_id, 500) is None


def test_resolve_active_wl_period_matches_covering_window(db):
    fx = _build_applied_child(db)
    account_id = fx["account"]["account_id"]
    period_id = _seed_active_wl_period(db, account_id=account_id, starts_at=0, ends_at=3600, now=1)
    assert db.wl_usage_ledger.resolve_active_wl_period(account_id, 1800) == period_id
    assert db.wl_usage_ledger.resolve_active_wl_period(account_id, 3600) is None  # end exclusive
    assert db.wl_usage_ledger.resolve_active_wl_period(account_id, -1) is None


# --- PH6-04 corrective addition: the PLANNED -> ACTIVE -> CLOSED status --
# machine wl_period_lifecycle_schema.py's own docstring reserved but never
# built -- a real gap this session found while building PH6-04 (a real
# purchase only ever creates PLANNED rows; nothing ever promoted one).

def _status(db, period_id):
    return db._conn.execute(
        "SELECT status FROM mgboost_wl_periods WHERE id=?", (period_id,)
    ).fetchone()["status"]


def test_sync_promotes_planned_to_active_once_starts_at_arrives(db):
    fx = _build_applied_child(db)
    account_id = fx["account"]["account_id"]
    period_id = _seed_wl_period(db, account_id=account_id, starts_at=1000, ends_at=2000, now=1, status="PLANNED")

    db.wl_usage_ledger.sync_wl_period_statuses(account_id=account_id, now=500)
    assert _status(db, period_id) == "PLANNED"  # not yet started

    db.wl_usage_ledger.sync_wl_period_statuses(account_id=account_id, now=1000)
    assert _status(db, period_id) == "ACTIVE"  # starts_at is inclusive


def test_sync_closes_active_period_once_ends_at_passes(db):
    fx = _build_applied_child(db)
    account_id = fx["account"]["account_id"]
    period_id = _seed_wl_period(db, account_id=account_id, starts_at=0, ends_at=1000, now=1, status="ACTIVE")

    db.wl_usage_ledger.sync_wl_period_statuses(account_id=account_id, now=999)
    assert _status(db, period_id) == "ACTIVE"

    db.wl_usage_ledger.sync_wl_period_statuses(account_id=account_id, now=1000)
    assert _status(db, period_id) == "CLOSED"  # ends_at is exclusive/closes-on-arrival


def test_sync_closes_a_planned_period_directly_if_its_window_already_fully_elapsed(db):
    """A period that was never separately activated (e.g. after a long
    collector gap) must still close -- otherwise it would be stuck in
    PLANNED forever and block every later sequential period from ever
    resolving as ACTIVE."""
    fx = _build_applied_child(db)
    account_id = fx["account"]["account_id"]
    period_id = _seed_wl_period(db, account_id=account_id, starts_at=0, ends_at=1000, now=1, status="PLANNED")

    db.wl_usage_ledger.sync_wl_period_statuses(account_id=account_id, now=5000)
    assert _status(db, period_id) == "CLOSED"


def test_sync_handles_the_contiguous_boundary_between_two_sequential_periods(db):
    fx = _build_applied_child(db)
    account_id = fx["account"]["account_id"]
    p1 = _seed_wl_period(db, account_id=account_id, starts_at=0, ends_at=1000, now=1, status="ACTIVE")
    p2 = _seed_wl_period(db, account_id=account_id, starts_at=1000, ends_at=2000, now=1, status="PLANNED")

    # At the exact contiguous boundary, one atomic sync closes the first and
    # activates the second -- never both ACTIVE, never a gap.
    db.wl_usage_ledger.sync_wl_period_statuses(account_id=account_id, now=1000)
    assert _status(db, p1) == "CLOSED"
    assert _status(db, p2) == "ACTIVE"
    assert db.wl_usage_ledger.resolve_active_wl_period(account_id, 1000) == p2


def test_sync_never_revives_a_closed_period(db):
    fx = _build_applied_child(db)
    account_id = fx["account"]["account_id"]
    period_id = _seed_wl_period(db, account_id=account_id, starts_at=0, ends_at=1000, now=1, status="CLOSED")

    db.wl_usage_ledger.sync_wl_period_statuses(account_id=account_id, now=500)  # inside the old window
    assert _status(db, period_id) == "CLOSED"


def test_sync_is_idempotent_across_repeated_calls(db):
    fx = _build_applied_child(db)
    account_id = fx["account"]["account_id"]
    period_id = _seed_wl_period(db, account_id=account_id, starts_at=0, ends_at=1000, now=1, status="PLANNED")

    for _ in range(3):
        db.wl_usage_ledger.sync_wl_period_statuses(account_id=account_id, now=500)
    assert _status(db, period_id) == "ACTIVE"


def test_sync_never_touches_a_different_accounts_periods(db):
    fx1 = _build_applied_child(db, mapping="SYNC_SCOPE_1", tg=555101, alias="alice1")
    fx2 = _build_applied_child(db, mapping="SYNC_SCOPE_2", tg=555102, alias="alice2")
    account_1 = fx1["account"]["account_id"]
    account_2 = fx2["account"]["account_id"]
    period_1 = _seed_wl_period(db, account_id=account_1, starts_at=0, ends_at=1000, now=1, status="PLANNED")
    period_2 = _seed_wl_period(db, account_id=account_2, starts_at=0, ends_at=1000, now=1, status="PLANNED")

    db.wl_usage_ledger.sync_wl_period_statuses(account_id=account_1, now=500)
    assert _status(db, period_1) == "ACTIVE"
    assert _status(db, period_2) == "PLANNED"  # untouched -- different account


# --- full collection cycle -------------------------------------------------

def test_run_collection_cycle_requires_topology_ok(db):
    from src.wl_topology_guard import TopologyMismatchError
    with pytest.raises(TopologyMismatchError):
        run_collection_cycle(db=db, service_marzban=FakeServiceMarzban(), worker_id="w1", now=100)


def test_run_collection_cycle_records_live_children_only(db):
    _clean_topology_ok(db)
    fx = _build_applied_child(db)
    fake = FakeServiceMarzban()
    fake.set_usage(fx["child_username"], {4: 1_000_000, 7: 0})
    summary = run_collection_cycle(db=db, service_marzban=fake, worker_id="w1", now=100)
    assert summary["outcome"] == "OK"
    assert summary["children_seen"] == 1
    assert summary["samples_recorded"] == 2  # both WL nodes
    row = db._conn.execute(
        "SELECT bytes_delta FROM mgboost_wl_usage_samples WHERE child_intent_id=? AND node_id=4",
        (fx["child_intent_id"],),
    ).fetchone()
    assert row["bytes_delta"] == 1_000_000


def test_run_collection_cycle_second_cycle_only_records_new_delta(db):
    _clean_topology_ok(db)
    fx = _build_applied_child(db)
    fake = FakeServiceMarzban()
    fake.set_usage(fx["child_username"], {4: 1_000_000, 7: 0})
    run_collection_cycle(db=db, service_marzban=fake, worker_id="w1", now=100)
    fake.set_usage(fx["child_username"], {4: 1_500_000, 7: 0})
    run_collection_cycle(db=db, service_marzban=fake, worker_id="w1", now=200)
    row = db._conn.execute(
        "SELECT bytes_delta FROM mgboost_wl_usage_samples WHERE child_intent_id=? AND node_id=4",
        (fx["child_intent_id"],),
    ).fetchone()
    assert row["bytes_delta"] == 1_500_000


def test_run_collection_cycle_second_collector_skips_while_lease_held(db):
    _clean_topology_ok(db)
    fx = _build_applied_child(db)
    fake = FakeServiceMarzban()
    fake.set_usage(fx["child_username"], {4: 100, 7: 0})
    db.wl_usage_ledger.claim_collector_lease(worker_id="other-host", now=99, lease_seconds=300)
    summary = run_collection_cycle(db=db, service_marzban=fake, worker_id="w1", now=100)
    assert summary == {"skipped": "lease_held_by_other_collector"}
    assert fake.calls == []


def test_run_collection_cycle_isolates_per_child_errors(db):
    _clean_topology_ok(db)
    fx1 = _build_applied_child(db, mapping="LEDGER_OK", tg=700001, alias="good_child")
    fx2 = _build_applied_child(db, mapping="LEDGER_ERR", tg=700002, alias="bad_child")
    fake = FakeServiceMarzban()
    fake.set_usage(fx1["child_username"], {4: 500, 7: 0})
    fake.raise_for.add(fx2["child_username"])
    summary = run_collection_cycle(db=db, service_marzban=fake, worker_id="w1", now=100)
    assert summary["outcome"] == "PARTIAL"
    assert summary["children_seen"] == 2
    assert summary["samples_recorded"] == 2  # only fx1's two nodes
    assert "RuntimeError" in summary["errors"]
    row = db._conn.execute(
        "SELECT bytes_delta FROM mgboost_wl_usage_samples WHERE child_intent_id=? AND node_id=4",
        (fx1["child_intent_id"],),
    ).fetchone()
    assert row["bytes_delta"] == 500


def test_run_collection_cycle_attributes_active_wl_period(db):
    _clean_topology_ok(db, now=1)
    fx = _build_applied_child(db)
    account_id = fx["account"]["account_id"]
    period_id = _seed_active_wl_period(db, account_id=account_id, starts_at=0, ends_at=100_000, now=1)
    fake = FakeServiceMarzban()
    fake.set_usage(fx["child_username"], {4: 42, 7: 0})
    run_collection_cycle(db=db, service_marzban=fake, worker_id="w1", now=50)
    row = db._conn.execute(
        "SELECT wl_period_id FROM mgboost_wl_usage_samples WHERE child_intent_id=? AND node_id=4",
        (fx["child_intent_id"],),
    ).fetchone()
    assert row["wl_period_id"] == period_id


def test_collector_attributes_arbitrary_second_boundary_inside_one_sample_hour(db):
    _clean_topology_ok(db, now=1)
    fx = _build_applied_child(db)
    account_id = fx["account"]["account_id"]
    old_period = _seed_active_wl_period(
        db, account_id=account_id, starts_at=0, ends_at=1500, now=1,
    )
    new_period = _seed_wl_period(
        db, account_id=account_id, starts_at=1500, ends_at=5100, now=1, status="PLANNED",
    )
    fake = FakeServiceMarzban()
    fake.set_usage(fx["child_username"], {4: 100, 7: 0})
    run_collection_cycle(db=db, service_marzban=fake, worker_id="w1", now=1499)
    fake.set_usage(fx["child_username"], {4: 200, 7: 0})
    run_collection_cycle(db=db, service_marzban=fake, worker_id="w1", now=1500)

    rows = db._conn.execute(
        "SELECT wl_period_id,bytes_delta FROM mgboost_wl_usage_samples "
        "WHERE child_intent_id=? AND node_id=4 ORDER BY wl_period_id",
        (fx["child_intent_id"],),
    ).fetchall()
    assert [(r["wl_period_id"], r["bytes_delta"]) for r in rows] == [
        (old_period, 100), (new_period, 100),
    ]


def test_cross_boundary_interval_is_right_edge_attributed_once_without_split(db):
    """The upstream counter exposes only cumulative values.

    The second observation crosses an arbitrary-second WL boundary, so its
    150-byte delta is deliberately assigned in full to the period active at
    that observation.  This is operational quota accounting, not a claim
    that all 150 bytes physically travelled after the boundary.
    """
    _clean_topology_ok(db, now=1)
    fx = _build_applied_child(db)
    account_id = fx["account"]["account_id"]
    old_period = _seed_active_wl_period(
        db, account_id=account_id, starts_at=0, ends_at=1_500, now=1,
        quota_mode="LIMITED", base_quota_bytes=200,
    )
    new_period = _seed_wl_period(
        db, account_id=account_id, starts_at=1_500, ends_at=5_100, now=1,
        status="PLANNED", quota_mode="LIMITED", base_quota_bytes=200,
    )
    fake = FakeServiceMarzban()
    fake.set_usage(fx["child_username"], {4: 100, 7: 0})
    run_collection_cycle(db=db, service_marzban=fake, worker_id="w1", now=1_499)
    fake.set_usage(fx["child_username"], {4: 250, 7: 0})
    run_collection_cycle(db=db, service_marzban=fake, worker_id="w1", now=1_501)

    buckets = db._conn.execute(
        "SELECT wl_period_id,bytes_delta FROM mgboost_wl_usage_samples "
        "WHERE child_intent_id=? AND node_id=4 ORDER BY wl_period_id",
        (fx["child_intent_id"],),
    ).fetchall()
    assert [(row["wl_period_id"], row["bytes_delta"]) for row in buckets] == [
        (old_period, 100), (new_period, 150),
    ]
    events = db._conn.execute(
        "SELECT cursor_before,cursor_after,delta_bytes FROM mgboost_wl_usage_sample_events "
        "WHERE child_intent_id=? AND node_id=4 ORDER BY id",
        (fx["child_intent_id"],),
    ).fetchall()
    assert [tuple(row) for row in events] == [(0, 100, 100), (100, 250, 150)]

    from src.wl_parent_pool import compute_parent_wl_pool
    assert compute_parent_wl_pool(
        db._conn, account_id=account_id, wl_period_id=old_period,
    )["consumed_bytes"] == 100
    new_pool = compute_parent_wl_pool(
        db._conn, account_id=account_id, wl_period_id=new_period)
    assert (new_pool["consumed_bytes"], new_pool["exceeded"]) == (150, False)


def test_no_current_period_uses_existing_null_bucket_without_cross_period_merge(db):
    """No active period remains the existing NULL/COALESCE(…, 0) bucket."""
    account_id, child_intent_id = _ids(db)
    db.wl_usage_ledger.record_sample(
        account_id=account_id, child_intent_id=child_intent_id, node_id=4,
        cursor_after=100, collector_id="w1", collected_at=100, wl_period_id=None,
    )
    db.wl_usage_ledger.record_sample(
        account_id=account_id, child_intent_id=child_intent_id, node_id=4,
        cursor_after=250, collector_id="w1", collected_at=200, wl_period_id=None,
    )
    row = db._conn.execute(
        "SELECT wl_period_id,bytes_delta FROM mgboost_wl_usage_samples "
        "WHERE child_intent_id=? AND node_id=4",
        (child_intent_id,),
    ).fetchone()
    assert (row["wl_period_id"], row["bytes_delta"]) == (None, 250)


def test_collector_attributes_exact_hour_boundary(db):
    _clean_topology_ok(db, now=1)
    fx = _build_applied_child(db)
    account_id = fx["account"]["account_id"]
    old_period = _seed_active_wl_period(
        db, account_id=account_id, starts_at=0, ends_at=3600, now=1,
    )
    new_period = _seed_wl_period(
        db, account_id=account_id, starts_at=3600, ends_at=7200, now=1, status="PLANNED",
    )
    fake = FakeServiceMarzban()
    fake.set_usage(fx["child_username"], {4: 100, 7: 0})
    run_collection_cycle(db=db, service_marzban=fake, worker_id="w1", now=3599)
    fake.set_usage(fx["child_username"], {4: 200, 7: 0})
    run_collection_cycle(db=db, service_marzban=fake, worker_id="w1", now=3600)
    rows = db._conn.execute(
        "SELECT sample_hour,wl_period_id,bytes_delta FROM mgboost_wl_usage_samples "
        "WHERE child_intent_id=? AND node_id=4 ORDER BY sample_hour",
        (fx["child_intent_id"],),
    ).fetchall()
    assert [(r["sample_hour"], r["wl_period_id"], r["bytes_delta"]) for r in rows] == [
        (0, old_period, 100), (3600, new_period, 100),
    ]


def test_run_collection_cycle_leaves_lease_free_after_completion(db):
    _clean_topology_ok(db)
    fx = _build_applied_child(db)
    fake = FakeServiceMarzban()
    fake.set_usage(fx["child_username"], {4: 1, 7: 0})
    run_collection_cycle(db=db, service_marzban=fake, worker_id="w1", now=100)
    lease = db._conn.execute("SELECT lease_owner, last_run_outcome FROM mgboost_wl_usage_collector_lease WHERE id=1").fetchone()
    assert lease["lease_owner"] is None
    assert lease["last_run_outcome"] == "OK"


def test_reused_child_username_never_leaked_into_ledger_rows(db):
    _clean_topology_ok(db)
    fx = _build_applied_child(db)
    fake = FakeServiceMarzban()
    fake.set_usage(fx["child_username"], {4: 1, 7: 0})
    run_collection_cycle(db=db, service_marzban=fake, worker_id="w1", now=100)
    for table in (
        "mgboost_wl_usage_cursors", "mgboost_wl_usage_samples", "mgboost_wl_usage_sample_events",
    ):
        columns = {r[1] for r in db._conn.execute(f"PRAGMA table_info({table})")}
        assert "child_username" not in columns
        assert "username" not in columns
