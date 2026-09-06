"""BUG-004 fix regression coverage: durable reset-generation replay identity.

See `BUGS.md` BUG-004 and `src/wl_usage_ledger_schema_v3.py` for the full root
cause / fix rationale. Before this fix, `mgboost_wl_usage_sample_events` keyed
replay identity on `(child_intent_id, node_id, cursor_before)` alone; a real
reset that returned the raw cumulative counter to a value already used as a
`cursor_before` in an earlier epoch (most commonly `0`, the very first
observation) collided with that old row, was misclassified as an exact
replay, and the cursor never advanced again -- the confirmed
`100 -> 0 -> 50 -> 200` scenario. The fix adds a durable `reset_generation`
epoch number to both the cursor and the event key.

This module is narrowly scoped to BUG-004 only -- it does not touch
BUG-001/002/003/005, PH6-08/packages, billing, promo, manual payments or
admin UI.
"""

from __future__ import annotations

import importlib
import os
import sqlite3
import tempfile

import pytest

from src.wl_topology import WL_NODES
from src.wl_usage_ledger import WLUsageLedgerError, run_collection_cycle

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


def _clean_topology_ok(db, now=1):
    from src.wl_topology import WL_INBOUND_TAGS
    tags = set(WL_INBOUND_TAGS)
    nodes = {n["id"]: {"role": n["role"], "address": n["address"], "usage_coefficient": n["usage_coefficient"]}
             for n in WL_NODES}
    db.wl_topology_guard.run_assertion(tags, nodes, now=now)


def _ids(db):
    fx = _build_applied_child(db)
    return fx["account"]["account_id"], fx["child_intent_id"]


def _events(db, child_intent_id, node_id=4):
    return db._conn.execute(
        "SELECT cursor_before,cursor_after,delta_bytes,reset_detected,reset_generation "
        "FROM mgboost_wl_usage_sample_events WHERE child_intent_id=? AND node_id=? ORDER BY id",
        (child_intent_id, node_id),
    ).fetchall()


def _total(db, child_intent_id, node_id=4):
    row = db._conn.execute(
        "SELECT bytes_delta FROM mgboost_wl_usage_samples WHERE child_intent_id=? AND node_id=?",
        (child_intent_id, node_id),
    ).fetchone()
    return row["bytes_delta"] if row is not None else 0


class FakeServiceMarzban:
    def __init__(self):
        self.responses = {}
        self.calls = []

    def set_usage(self, username, node_usages: dict):
        self.responses[username] = [
            {"node_id": node_id, "node_name": str(node_id), "used_traffic": value}
            for node_id, value in node_usages.items()
        ]

    def get_user_usage(self, username, admin_token=None, start="", end=""):
        self.calls.append((username, start, end))
        return {"usages": self.responses.get(username, []), "username": username}


# --- 1. normal monotonic ---------------------------------------------------

def test_normal_monotonic_sequence_accounts_every_delta(db):
    account_id, child_intent_id = _ids(db)
    for t, cumulative in [(100, 0), (200, 100), (300, 150), (400, 200)]:
        db.wl_usage_ledger.record_sample(
            account_id=account_id, child_intent_id=child_intent_id, node_id=4,
            cursor_after=cumulative, collector_id="w1", collected_at=t,
        )
    assert _total(db, child_intent_id) == 200
    events = _events(db, child_intent_id)
    assert [e["reset_detected"] for e in events] == [0, 0, 0]
    assert [e["reset_generation"] for e in events] == [0, 0, 0]
    assert all(e["delta_bytes"] >= 0 for e in events)


# --- 2. confirmed bug: the exact reproduced scenario -----------------------

def test_confirmed_bug004_scenario_advances_correctly_after_reset(db):
    """100 -> 0 -> 50 -> 200: post-reset traffic must never be lost."""
    account_id, child_intent_id = _ids(db)
    sequence = [(100, 100), (200, 0), (300, 50), (400, 200)]
    results = [
        db.wl_usage_ledger.record_sample(
            account_id=account_id, child_intent_id=child_intent_id, node_id=4,
            cursor_after=cumulative, collector_id="w1", collected_at=t,
        )
        for t, cumulative in sequence
    ]
    assert _total(db, child_intent_id) == 300  # 100 + 0 (reset) + 50 + 150, never lost
    assert all(r["delta_bytes"] >= 0 for r in results)
    cursor = db._conn.execute(
        "SELECT last_observed_cumulative_bytes, reset_count, reset_generation "
        "FROM mgboost_wl_usage_cursors WHERE child_intent_id=? AND node_id=4",
        (child_intent_id,),
    ).fetchone()
    assert cursor["last_observed_cumulative_bytes"] == 200
    assert cursor["reset_count"] == 1
    assert cursor["reset_generation"] == 1
    events = _events(db, child_intent_id)
    assert len(events) == 4  # every transition durably recorded, none silently dropped
    # The pre-reset cursor_before=0 (generation 0) and the post-reset
    # cursor_before=0 (generation 1) are two distinct durable rows.
    zero_cb_events = [e for e in events if e["cursor_before"] == 0]
    assert len(zero_cb_events) == 2
    assert {e["reset_generation"] for e in zero_cb_events} == {0, 1}


# --- 3. repeated zero resets -------------------------------------------------

def test_repeated_zero_resets_each_advance_a_new_epoch(db):
    """100 -> 0 -> 50 -> 0 -> 25."""
    account_id, child_intent_id = _ids(db)
    sequence = [(100, 100), (200, 0), (300, 50), (400, 0), (500, 25)]
    for t, cumulative in sequence:
        db.wl_usage_ledger.record_sample(
            account_id=account_id, child_intent_id=child_intent_id, node_id=4,
            cursor_after=cumulative, collector_id="w1", collected_at=t,
        )
    assert _total(db, child_intent_id) == 175  # 100 + 0 + 50 + 0 + 25
    cursor = db._conn.execute(
        "SELECT reset_count, reset_generation FROM mgboost_wl_usage_cursors "
        "WHERE child_intent_id=? AND node_id=4", (child_intent_id,),
    ).fetchone()
    assert cursor["reset_count"] == 2
    assert cursor["reset_generation"] == 2
    events = _events(db, child_intent_id)
    assert len(events) == 5
    assert [e["reset_generation"] for e in events] == [0, 0, 1, 1, 2]


# --- 4. return to a previously-encountered non-zero cumulative value -------

def test_return_to_previously_seen_nonzero_value_in_a_later_epoch(db):
    """Epoch 0 uses cursor_before=100 once (its own closing reset event).
    A later epoch legitimately passes through the same raw value 100 again
    -- this must be a distinct durable row, not a collision."""
    account_id, child_intent_id = _ids(db)
    sequence = [
        (100, 100),   # gen0: cb=0 -> 100
        (200, 50),    # reset: gen0 cb=100 -> 50 (closes gen0)
        (300, 0),     # reset: gen1 cb=50 -> 0 (closes gen1)
        (400, 100),   # gen2: cb=0 -> 100
        (500, 180),   # gen2: cb=100 -> 180  <-- reuses raw value 100 from gen0's closing event
    ]
    results = []
    for t, cumulative in sequence:
        results.append(db.wl_usage_ledger.record_sample(
            account_id=account_id, child_intent_id=child_intent_id, node_id=4,
            cursor_after=cumulative, collector_id="w1", collected_at=t,
        ))
    assert all(r["delta_bytes"] >= 0 for r in results)
    events = _events(db, child_intent_id)
    cb_100_events = [e for e in events if e["cursor_before"] == 100]
    assert len(cb_100_events) == 2
    assert {e["reset_generation"] for e in cb_100_events} == {0, 2}
    # 100 (gen0) + 50 (reset delta=cursor_after) + 0 (reset) + 100 (gen2) + 80 (gen2, 180-100)
    assert _total(db, child_intent_id) == 330


# --- 5. exact replay must never double-count -------------------------------

def test_exact_replay_of_same_observation_is_idempotent(db):
    account_id, child_intent_id = _ids(db)
    first = db.wl_usage_ledger.record_sample(
        account_id=account_id, child_intent_id=child_intent_id, node_id=4, cursor_after=100,
        collector_id="w1", collected_at=100,
    )
    # Simulate a crash-retry: another call observes the *same* pre-commit
    # cursor state (cursor_before=0, generation=0) and the same cursor_after.
    db._conn.execute(
        "UPDATE mgboost_wl_usage_cursors SET last_observed_cumulative_bytes=0 "
        "WHERE child_intent_id=? AND node_id=4", (child_intent_id,),
    )
    db._conn.commit()
    replay = db.wl_usage_ledger.record_sample(
        account_id=account_id, child_intent_id=child_intent_id, node_id=4, cursor_after=100,
        collector_id="w2", collected_at=105,
    )
    assert replay["delta_bytes"] == first["delta_bytes"] == 100
    assert _total(db, child_intent_id) == 100  # not 200 -- no double count
    assert len(_events(db, child_intent_id)) == 1


def test_exact_replay_after_a_real_reset_is_still_idempotent(db):
    """Idempotency must hold *within* the post-reset epoch too, not just at
    generation 0."""
    account_id, child_intent_id = _ids(db)
    db.wl_usage_ledger.record_sample(
        account_id=account_id, child_intent_id=child_intent_id, node_id=4, cursor_after=100,
        collector_id="w1", collected_at=100,
    )
    db.wl_usage_ledger.record_sample(  # reset -> generation 1, cursor 0
        account_id=account_id, child_intent_id=child_intent_id, node_id=4, cursor_after=0,
        collector_id="w1", collected_at=200,
    )
    first_post_reset = db.wl_usage_ledger.record_sample(
        account_id=account_id, child_intent_id=child_intent_id, node_id=4, cursor_after=50,
        collector_id="w1", collected_at=300,
    )
    # Simulate the same crash-retry pattern, this time inside generation 1.
    db._conn.execute(
        "UPDATE mgboost_wl_usage_cursors SET last_observed_cumulative_bytes=0 "
        "WHERE child_intent_id=? AND node_id=4", (child_intent_id,),
    )
    db._conn.commit()
    replay = db.wl_usage_ledger.record_sample(
        account_id=account_id, child_intent_id=child_intent_id, node_id=4, cursor_after=50,
        collector_id="w2", collected_at=305,
    )
    assert replay["delta_bytes"] == first_post_reset["delta_bytes"] == 50
    assert _total(db, child_intent_id) == 150  # 100 + 0 (reset) + 50, not 100+0+50+50
    assert len(_events(db, child_intent_id)) == 3


# --- 6. restart/reopen DB between reset and next sample --------------------

def test_restart_between_reset_and_next_sample_preserves_generation(db):
    account_id, child_intent_id = _ids(db)
    db.wl_usage_ledger.record_sample(
        account_id=account_id, child_intent_id=child_intent_id, node_id=4, cursor_after=100,
        collector_id="w1", collected_at=100,
    )
    db.wl_usage_ledger.record_sample(  # reset -> generation bumps to 1
        account_id=account_id, child_intent_id=child_intent_id, node_id=4, cursor_after=0,
        collector_id="w1", collected_at=200,
    )
    db._conn.close()

    import src.database as database
    reopened = database.Database()
    try:
        result = reopened.wl_usage_ledger.record_sample(
            account_id=account_id, child_intent_id=child_intent_id, node_id=4, cursor_after=50,
            collector_id="w1", collected_at=300,
        )
        assert result["delta_bytes"] == 50
        assert result["reset_generation"] == 1
        assert _total(reopened, child_intent_id) == 150
    finally:
        reopened._conn.close()


# --- 7. two independent connections: a held write lock must not corrupt ---

def test_second_connection_holding_a_write_lock_blocks_rather_than_races(db):
    import src.database as database
    account_id, child_intent_id = _ids(db)  # fixture setup first, before the lock is taken

    second_conn = sqlite3.connect(database.DB_PATH, timeout=0.3)
    second_conn.execute("BEGIN IMMEDIATE")
    second_conn.execute(
        "UPDATE mgboost_wl_usage_collector_lease SET updated_at=updated_at WHERE id=1"
    )
    try:
        with pytest.raises(sqlite3.OperationalError):
            db.wl_usage_ledger.record_sample(
                account_id=account_id, child_intent_id=child_intent_id, node_id=4,
                cursor_after=100, collector_id="w1", collected_at=100,
            )
    finally:
        second_conn.rollback()
        second_conn.close()
    # The blocked write must not have partially applied anything.
    assert _total(db, child_intent_id) == 0
    assert len(_events(db, child_intent_id)) == 0


# --- 8. no delta ever goes negative -----------------------------------------

def test_delta_bytes_never_negative_across_a_reset(db):
    account_id, child_intent_id = _ids(db)
    for t, cumulative in [(100, 5000), (200, 200), (300, 0), (400, 10)]:
        result = db.wl_usage_ledger.record_sample(
            account_id=account_id, child_intent_id=child_intent_id, node_id=4,
            cursor_after=cumulative, collector_id="w1", collected_at=t,
        )
        assert result["delta_bytes"] >= 0
    events = _events(db, child_intent_id)
    assert all(e["delta_bytes"] >= 0 for e in events)


# --- 9. an unrelated/ambiguous constraint failure is never silently -------
#        treated as a harmless duplicate.

def test_non_replay_integrity_error_is_never_swallowed_as_a_duplicate(db, monkeypatch):
    account_id, child_intent_id = _ids(db)
    real_conn = db.wl_usage_ledger._conn

    class _FaultInjectingConn:
        def execute(self, sql, params=()):
            if sql.strip().startswith("INSERT INTO mgboost_wl_usage_sample_events"):
                raise sqlite3.IntegrityError("FOREIGN KEY constraint failed")
            return real_conn.execute(sql, params)

        def __getattr__(self, name):
            return getattr(real_conn, name)

    monkeypatch.setattr(db.wl_usage_ledger, "_conn", _FaultInjectingConn())
    with pytest.raises(sqlite3.IntegrityError):
        db.wl_usage_ledger.record_sample(
            account_id=account_id, child_intent_id=child_intent_id, node_id=4, cursor_after=100,
            collector_id="w1", collected_at=100,
        )
    monkeypatch.setattr(db.wl_usage_ledger, "_conn", real_conn)
    assert _total(db, child_intent_id) == 0  # nothing silently applied


# --- 10. collector result must not report honest OK for an unclassifiable -
#         observation.

def test_collector_never_reports_ok_when_a_write_is_unclassifiable(db, monkeypatch):
    _clean_topology_ok(db)
    fx = _build_applied_child(db)
    fake = FakeServiceMarzban()
    fake.set_usage(fx["child_username"], {4: 100, 7: 0})
    real_conn = db.wl_usage_ledger._conn

    class _FaultInjectingConn:
        def execute(self, sql, params=()):
            if sql.strip().startswith("INSERT INTO mgboost_wl_usage_sample_events"):
                raise sqlite3.IntegrityError("FOREIGN KEY constraint failed")
            return real_conn.execute(sql, params)

        def __getattr__(self, name):
            return getattr(real_conn, name)

    monkeypatch.setattr(db.wl_usage_ledger, "_conn", _FaultInjectingConn())
    summary = run_collection_cycle(db=db, service_marzban=fake, worker_id="w1", now=100)
    assert summary["outcome"] != "OK"
    assert "IntegrityError" in summary["errors"]
