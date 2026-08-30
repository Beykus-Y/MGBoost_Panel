"""PH6 period-aware ledger migration contract."""

from __future__ import annotations

import pytest

from src.wl_usage_ledger_schema import _SCHEMA_STATEMENTS as V1_STATEMENTS
from src.wl_usage_ledger_schema_v2 import (
    MIGRATION_ID,
    apply_wl_usage_ledger_schema_v2,
)
from tests.test_wl_usage_ledger import _ids, _seed_active_wl_period, db


def _restore_production_shaped_v1_samples(connection):
    """Turn only the samples table back into the checksum-pinned v1 shape.

    This deliberately preserves valid historical rows and every surrounding
    production table, allowing the versioned rebuild path to be exercised
    rather than merely testing a fresh database.
    """
    sample_trigger_names = (
        "trg_mgboost_wl_usage_sample_identity_immutable",
        "trg_mgboost_wl_usage_sample_monotonic",
        "trg_mgboost_wl_usage_sample_no_delete",
    )
    connection.execute("BEGIN IMMEDIATE")
    try:
        for name in sample_trigger_names:
            connection.execute(f"DROP TRIGGER {name}")
        connection.execute("DROP INDEX ux_mgboost_wl_usage_samples_period_bucket")
        connection.execute("DROP INDEX ix_mgboost_wl_usage_samples_period")
        connection.execute("ALTER TABLE mgboost_wl_usage_samples RENAME TO mgboost_wl_usage_samples_v2_source")
        connection.execute(V1_STATEMENTS[1])
        connection.execute(
            "INSERT INTO mgboost_wl_usage_samples "
            "SELECT * FROM mgboost_wl_usage_samples_v2_source"
        )
        connection.execute("DROP TABLE mgboost_wl_usage_samples_v2_source")
        for statement in V1_STATEMENTS:
            if (
                "ix_mgboost_wl_usage_samples_period" in statement
                or any(name in statement for name in sample_trigger_names)
            ):
                connection.execute(statement)
        connection.execute("DELETE FROM mgboost_schema_migrations WHERE migration_id=?", (MIGRATION_ID,))
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def test_production_shaped_v1_migration_preserves_rows_ids_and_byte_totals(db):
    account_id, child_id = _ids(db)
    period_id = _seed_active_wl_period(
        db, account_id=account_id, starts_at=0, ends_at=7200, now=1,
    )
    db.wl_usage_ledger.record_sample(
        account_id=account_id, child_intent_id=child_id, node_id=4,
        cursor_after=100, collector_id="w1", collected_at=100, wl_period_id=period_id,
    )
    db.wl_usage_ledger.record_sample(
        account_id=account_id, child_intent_id=child_id, node_id=7,
        cursor_after=50, collector_id="w1", collected_at=100, wl_period_id=None,
    )
    before = [tuple(row) for row in db._conn.execute(
        "SELECT id,account_id,child_intent_id,node_id,wl_period_id,sample_hour,bytes_delta "
        "FROM mgboost_wl_usage_samples ORDER BY id"
    )]
    before_total = db._conn.execute(
        "SELECT COALESCE(SUM(bytes_delta),0) FROM mgboost_wl_usage_samples"
    ).fetchone()[0]

    _restore_production_shaped_v1_samples(db._conn)
    assert apply_wl_usage_ledger_schema_v2(db._conn, now=1234) is True
    after = [tuple(row) for row in db._conn.execute(
        "SELECT id,account_id,child_intent_id,node_id,wl_period_id,sample_hour,bytes_delta "
        "FROM mgboost_wl_usage_samples ORDER BY id"
    )]
    after_total = db._conn.execute(
        "SELECT COALESCE(SUM(bytes_delta),0) FROM mgboost_wl_usage_samples"
    ).fetchone()[0]

    assert after == before
    assert after_total == before_total
    assert apply_wl_usage_ledger_schema_v2(db._conn, now=5678) is False


def test_fresh_database_has_the_same_period_aware_schema(db):
    marker = db._conn.execute(
        "SELECT migration_id FROM mgboost_schema_migrations WHERE migration_id=?", (MIGRATION_ID,)
    ).fetchone()
    index_sql = db._conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND name='ux_mgboost_wl_usage_samples_period_bucket'"
    ).fetchone()[0]
    assert marker is not None
    assert "COALESCE(wl_period_id, 0)" in index_sql


def test_unknown_v1_source_state_fails_closed_without_partial_rebuild(db):
    _restore_production_shaped_v1_samples(db._conn)
    db._conn.execute("DROP TRIGGER trg_mgboost_wl_usage_sample_monotonic")
    db._conn.commit()

    with pytest.raises(RuntimeError, match="unknown or corrupt"):
        apply_wl_usage_ledger_schema_v2(db._conn, now=1234)

    assert db._conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='mgboost_wl_usage_samples'"
    ).fetchone() is not None
    assert db._conn.execute(
        "SELECT 1 FROM mgboost_schema_migrations WHERE migration_id=?", (MIGRATION_ID,)
    ).fetchone() is None
