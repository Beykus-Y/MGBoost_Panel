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
