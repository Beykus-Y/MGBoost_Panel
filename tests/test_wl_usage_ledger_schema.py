import os
import tempfile

import pytest


@pytest.fixture
def db():
    tmp = tempfile.mkdtemp()
    os.environ["DATA_DIR"] = tmp
    from importlib import reload
    import src.config as cfg
    reload(cfg)
    cfg.DATA_DIR = tmp
    import src.database as db_mod
    reload(db_mod)
    db_mod.DB_PATH = os.path.join(tmp, "db.sqlite3")
    instance = db_mod.Database()
    # These tests exercise trigger/constraint mechanics on rows keyed by
    # child_intent_id/account_id values that are not real FK targets --
    # disable FK enforcement here so only the triggers under test can fail.
    instance._conn.execute("PRAGMA foreign_keys=OFF")
    yield instance
    instance._conn.close()


def test_schema_applied_and_idempotent(db):
    from src.wl_usage_ledger_schema import apply_wl_usage_ledger_schema
    assert apply_wl_usage_ledger_schema(db._conn) is False  # already applied by Database()
    row = db._conn.execute(
        "SELECT schema_checksum FROM mgboost_schema_migrations WHERE migration_id='ph6_03_wl_usage_ledger_v1'"
    ).fetchone()
    assert row is not None


def test_collector_lease_singleton_row_bootstrapped(db):
    row = db._conn.execute("SELECT * FROM mgboost_wl_usage_collector_lease WHERE id=1").fetchone()
    assert row is not None
    assert row["lease_owner"] is None


def test_requires_exact_parent_schema_checksum(db):
    from src.wl_usage_ledger_schema import apply_wl_usage_ledger_schema
    db._conn.execute(
        "UPDATE mgboost_schema_migrations SET schema_checksum='tampered' WHERE migration_id='ph3_01_parent_account_v1'"
    )
    db._conn.commit()
    with pytest.raises(RuntimeError):
        apply_wl_usage_ledger_schema(db._conn)


def test_cursor_identity_immutable(db):
    db._conn.execute(
        "INSERT INTO mgboost_wl_usage_cursors (account_id,child_intent_id,node_id,created_at,updated_at) "
        "VALUES (1,1,4,100,100)"
    )
    with pytest.raises(Exception):
        db._conn.execute("UPDATE mgboost_wl_usage_cursors SET node_id=7 WHERE child_intent_id=1")


def test_cursor_history_no_delete(db):
    db._conn.execute(
        "INSERT INTO mgboost_wl_usage_cursors (account_id,child_intent_id,node_id,created_at,updated_at) "
        "VALUES (1,1,4,100,100)"
    )
    with pytest.raises(Exception):
        db._conn.execute("DELETE FROM mgboost_wl_usage_cursors WHERE child_intent_id=1")


def test_sample_bytes_delta_cannot_decrease(db):
    db._conn.execute(
        "INSERT INTO mgboost_wl_usage_samples "
        "(account_id,child_intent_id,node_id,sample_hour,bytes_delta,first_collected_at,"
        "last_collected_at,created_at,updated_at) VALUES (1,1,4,0,1000,1,1,1,1)"
    )
    with pytest.raises(Exception):
        db._conn.execute("UPDATE mgboost_wl_usage_samples SET bytes_delta=500 WHERE child_intent_id=1")
    db._conn.execute("UPDATE mgboost_wl_usage_samples SET bytes_delta=1500 WHERE child_intent_id=1")  # increase OK


def test_sample_identity_immutable(db):
    db._conn.execute(
        "INSERT INTO mgboost_wl_usage_samples "
        "(account_id,child_intent_id,node_id,sample_hour,bytes_delta,first_collected_at,"
        "last_collected_at,created_at,updated_at) VALUES (1,1,4,0,0,1,1,1,1)"
    )
    with pytest.raises(Exception):
        db._conn.execute("UPDATE mgboost_wl_usage_samples SET node_id=7 WHERE child_intent_id=1")


def test_sample_no_delete(db):
    db._conn.execute(
        "INSERT INTO mgboost_wl_usage_samples "
        "(account_id,child_intent_id,node_id,sample_hour,bytes_delta,first_collected_at,"
        "last_collected_at,created_at,updated_at) VALUES (1,1,4,0,0,1,1,1,1)"
    )
    with pytest.raises(Exception):
        db._conn.execute("DELETE FROM mgboost_wl_usage_samples WHERE child_intent_id=1")


def test_sample_events_fully_immutable(db):
    db._conn.execute(
        "INSERT INTO mgboost_wl_usage_sample_events "
        "(account_id,child_intent_id,node_id,sample_hour,cursor_before,cursor_after,delta_bytes,"
        "reset_detected,collector_id,collected_at,created_at) VALUES (1,1,4,0,0,100,100,0,'w',1,1)"
    )
    with pytest.raises(Exception):
        db._conn.execute("UPDATE mgboost_wl_usage_sample_events SET delta_bytes=1 WHERE child_intent_id=1")
    with pytest.raises(Exception):
        db._conn.execute("DELETE FROM mgboost_wl_usage_sample_events WHERE child_intent_id=1")


def test_sample_events_unique_on_cursor_before(db):
    db._conn.execute(
        "INSERT INTO mgboost_wl_usage_sample_events "
        "(account_id,child_intent_id,node_id,sample_hour,cursor_before,cursor_after,delta_bytes,"
        "reset_detected,collector_id,collected_at,created_at) VALUES (1,1,4,0,0,100,100,0,'w',1,1)"
    )
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        db._conn.execute(
            "INSERT INTO mgboost_wl_usage_sample_events "
            "(account_id,child_intent_id,node_id,sample_hour,cursor_before,cursor_after,delta_bytes,"
            "reset_detected,collector_id,collected_at,created_at) VALUES (1,1,4,0,0,150,150,0,'w2',2,2)"
        )


def test_collector_lease_singleton_check(db):
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        db._conn.execute(
            "INSERT INTO mgboost_wl_usage_collector_lease (id,row_version,created_at,updated_at) "
            "VALUES (2,1,1,1)"
        )


# --- BUG-004 fix: reset_generation migration (v3) --------------------------
#
# This module (`apply_wl_usage_ledger_schema_v3`) is exercised end-to-end on
# a fresh bootstrap by every other test in this file (via the `db` fixture,
# which already includes it). What is NOT covered elsewhere is the actual
# *upgrade* path over pre-existing v1/v2-shaped history -- i.e. a real
# database that already recorded events under the old two-column
# `(child_intent_id, node_id, cursor_before)` key before this fix existed.
# This builds that pre-existing shape directly (bypassing `Database()`,
# which now always applies v3 immediately after v1/v2) and proves the
# migration backfills `reset_generation` deterministically from each row's
# own already-recorded `reset_detected` flag, preserves every row/column
# value, and is idempotent on a second call.

def test_v3_migration_backfills_reset_generation_from_existing_history():
    import sqlite3

    from src.wl_usage_ledger_schema import apply_wl_usage_ledger_schema
    from src.wl_usage_ledger_schema_v2 import apply_wl_usage_ledger_schema_v2
    from src.wl_usage_ledger_schema_v3 import apply_wl_usage_ledger_schema_v3
    from src.account_schema import MIGRATION_ID as ACC_ID, SCHEMA_CHECKSUM as ACC_SUM
    from src.child_provisioning_schema import MIGRATION_ID as CHILD_ID, SCHEMA_CHECKSUM as CHILD_SUM
    from src.wl_period_lifecycle_schema import MIGRATION_ID as WLP_ID, SCHEMA_CHECKSUM as WLP_SUM

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute(
        "CREATE TABLE mgboost_schema_migrations (migration_id TEXT PRIMARY KEY, "
        "schema_checksum TEXT NOT NULL, applied_at INTEGER NOT NULL)"
    )
    for migration_id, checksum in ((ACC_ID, ACC_SUM), (CHILD_ID, CHILD_SUM), (WLP_ID, WLP_SUM)):
        conn.execute(
            "INSERT INTO mgboost_schema_migrations VALUES (?,?,0)", (migration_id, checksum)
        )
    # Minimal stand-ins so the real FK clauses in the v1/v2 DDL can resolve
    # their referenced table name (FK enforcement itself stays OFF below;
    # this is only about SQLite being able to name-resolve the reference
    # during the v2 rebuild's INSERT...SELECT).
    conn.execute("CREATE TABLE mgboost_wl_periods (id INTEGER PRIMARY KEY)")
    conn.execute(
        "CREATE TABLE mgboost_child_user_intents (id INTEGER PRIMARY KEY, account_id INTEGER, "
        "UNIQUE(id, account_id))"
    )
    conn.commit()

    assert apply_wl_usage_ledger_schema(conn) is True
    assert apply_wl_usage_ledger_schema_v2(conn) is True

    # v2 leaves `PRAGMA foreign_keys=ON` on this connection; satisfy the real
    # composite FK with one matching stub row rather than disabling it again.
    conn.execute("INSERT INTO mgboost_child_user_intents (id, account_id) VALUES (1, 1)")

    # Hand-insert exactly the durable state a real pre-fix database would
    # have reached after BUG-004 actually triggered: the genesis observation
    # (cursor_before=0 -> 100) and the reset-closing event (100 -> 0) both
    # succeeded under the OLD two-column key (neither cursor_before value had
    # been used before), then every later legitimate write collided with the
    # genesis row's cursor_before=0 and was silently treated as a duplicate
    # -- so the cursor is durably stuck at 0 with exactly these two events on
    # disk, precisely the reproduced `BUGS.md` BUG-004 end state.
    conn.execute(
        "INSERT INTO mgboost_wl_usage_cursors (account_id,child_intent_id,node_id,"
        "last_observed_cumulative_bytes,last_polled_at,reset_count,created_at,updated_at) "
        "VALUES (1,1,4,0,200,1,100,200)"
    )
    history = [  # (cursor_before, cursor_after, delta_bytes, reset_detected)
        (0, 100, 100, 0),
        (100, 0, 0, 1),  # closes epoch 0; this IS the (already stuck) current state
    ]
    for i, (cursor_before, cursor_after, delta, reset) in enumerate(history):
        conn.execute(
            "INSERT INTO mgboost_wl_usage_sample_events (account_id,child_intent_id,node_id,"
            "sample_hour,cursor_before,cursor_after,delta_bytes,reset_detected,collector_id,"
            "collected_at,created_at) VALUES (1,1,4,0,?,?,?,?,'legacy',?,?)",
            (cursor_before, cursor_after, delta, reset, 100 + i, 100 + i),
        )
    conn.commit()

    # Sanity: under the OLD (pre-migration) schema, the exact collision BUG-004
    # describes is still reproducible here -- a legitimate post-reset write
    # with cursor_before=0 collides with the genesis row.
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO mgboost_wl_usage_sample_events (account_id,child_intent_id,node_id,"
            "sample_hour,cursor_before,cursor_after,delta_bytes,reset_detected,collector_id,"
            "collected_at,created_at) VALUES (1,1,4,0,0,50,50,0,'stuck-collector',300,300)"
        )
    conn.rollback()  # the failed INSERT above left an implicit open transaction

    assert apply_wl_usage_ledger_schema_v3(conn) is True

    rows = conn.execute(
        "SELECT cursor_before,cursor_after,delta_bytes,reset_detected,reset_generation "
        "FROM mgboost_wl_usage_sample_events ORDER BY id"
    ).fetchall()
    assert [dict(r) for r in rows] == [
        {"cursor_before": 0, "cursor_after": 100, "delta_bytes": 100, "reset_detected": 0, "reset_generation": 0},
        {"cursor_before": 100, "cursor_after": 0, "delta_bytes": 0, "reset_detected": 1, "reset_generation": 0},
    ]
    cursor = conn.execute(
        "SELECT reset_count, reset_generation FROM mgboost_wl_usage_cursors WHERE child_intent_id=1"
    ).fetchone()
    assert cursor["reset_count"] == 1
    assert cursor["reset_generation"] == 1  # deterministically backfilled from reset_count

    # The fix: the exact same post-reset write that collided above now
    # succeeds, because it belongs to generation 1, not the genesis
    # generation 0 that still legitimately owns cursor_before=0.
    conn.execute(
        "INSERT INTO mgboost_wl_usage_sample_events (account_id,child_intent_id,node_id,"
        "sample_hour,cursor_before,cursor_after,delta_bytes,reset_detected,reset_generation,"
        "collector_id,collected_at,created_at) VALUES (1,1,4,0,0,50,50,0,1,'unstuck-collector',300,300)"
    )
    unstuck = conn.execute(
        "SELECT COUNT(*) c FROM mgboost_wl_usage_sample_events WHERE child_intent_id=1"
    ).fetchone()["c"]
    assert unstuck == 3
    conn.commit()

    # Idempotent re-run: no further change, no error.
    assert apply_wl_usage_ledger_schema_v3(conn) is False
    conn.close()


def test_v3_events_unique_key_includes_reset_generation(db):
    import sqlite3
    db._conn.execute(
        "INSERT INTO mgboost_wl_usage_sample_events "
        "(account_id,child_intent_id,node_id,sample_hour,cursor_before,cursor_after,delta_bytes,"
        "reset_detected,reset_generation,collector_id,collected_at,created_at) "
        "VALUES (1,1,4,0,0,100,100,0,0,'w',1,1)"
    )
    # Same (child,node,cursor_before) but a *different* reset_generation is
    # no longer a collision -- this is the entire BUG-004 fix.
    db._conn.execute(
        "INSERT INTO mgboost_wl_usage_sample_events "
        "(account_id,child_intent_id,node_id,sample_hour,cursor_before,cursor_after,delta_bytes,"
        "reset_detected,reset_generation,collector_id,collected_at,created_at) "
        "VALUES (1,1,4,0,0,50,50,0,1,'w2',2,2)"
    )
    count = db._conn.execute(
        "SELECT COUNT(*) c FROM mgboost_wl_usage_sample_events WHERE child_intent_id=1"
    ).fetchone()["c"]
    assert count == 2
    # But the exact same (child,node,reset_generation,cursor_before) is still
    # rejected -- idempotency within one epoch is unchanged.
    with pytest.raises(sqlite3.IntegrityError):
        db._conn.execute(
            "INSERT INTO mgboost_wl_usage_sample_events "
            "(account_id,child_intent_id,node_id,sample_hour,cursor_before,cursor_after,delta_bytes,"
            "reset_detected,reset_generation,collector_id,collected_at,created_at) "
            "VALUES (1,1,4,0,0,999,999,0,0,'w3',3,3)"
        )
