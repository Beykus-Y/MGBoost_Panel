"""PH6 compatibility migration: period-aware WL usage-sample buckets.

PH6-03 deliberately used one row per ``(child, node, UTC hour)`` because
all WL boundaries were then UTC-hour aligned.  Promo periods may now start
at an arbitrary epoch second, so that key can merge two immutable periods.

This is intentionally a new migration.  It preserves every historical row
and replaces only the physical uniqueness constraint with a NULL-safe
period-aware unique expression index.  ``NULL`` remains the canonical value
for usage observed while no WL period is active; ``COALESCE(..., 0)`` makes
that bucket unique as well (SQLite UNIQUE otherwise treats NULL values as
distinct).
"""

from __future__ import annotations

import hashlib
import sqlite3
import time

from .wl_usage_ledger_schema import (
    MIGRATION_ID as V1_MIGRATION_ID,
    SCHEMA_CHECKSUM as V1_SCHEMA_CHECKSUM,
)


MIGRATION_ID = "ph6_10_wl_usage_ledger_period_bucket_v1"

_SAMPLE_COLUMNS = (
    "id,account_id,child_intent_id,node_id,wl_period_id,sample_hour,bytes_delta,"
    "first_collected_at,last_collected_at,row_version,created_at,updated_at"
)

_FINAL_TABLE = """
    CREATE TABLE mgboost_wl_usage_samples (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        account_id INTEGER NOT NULL,
        child_intent_id INTEGER NOT NULL,
        node_id INTEGER NOT NULL,
        wl_period_id INTEGER,
        sample_hour INTEGER NOT NULL CHECK(sample_hour % 3600 = 0),
        bytes_delta INTEGER NOT NULL DEFAULT 0 CHECK(bytes_delta >= 0),
        first_collected_at INTEGER NOT NULL,
        last_collected_at INTEGER NOT NULL,
        row_version INTEGER NOT NULL DEFAULT 1 CHECK(row_version > 0),
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        FOREIGN KEY(child_intent_id, account_id)
            REFERENCES mgboost_child_user_intents(id, account_id)
            ON DELETE RESTRICT,
        FOREIGN KEY(wl_period_id) REFERENCES mgboost_wl_periods(id)
            ON DELETE RESTRICT
    )
"""

_FINAL_OBJECTS = (
    """
    CREATE UNIQUE INDEX ux_mgboost_wl_usage_samples_period_bucket
        ON mgboost_wl_usage_samples(
            child_intent_id, node_id, sample_hour, COALESCE(wl_period_id, 0)
        )
    """,
    """
    CREATE INDEX ix_mgboost_wl_usage_samples_period
        ON mgboost_wl_usage_samples(wl_period_id, node_id)
    """,
    """
    CREATE TRIGGER trg_mgboost_wl_usage_sample_identity_immutable
        BEFORE UPDATE OF account_id, child_intent_id, node_id, sample_hour,
                         wl_period_id, created_at
        ON mgboost_wl_usage_samples
        BEGIN SELECT RAISE(ABORT, 'usage sample identity is immutable'); END
    """,
    """
    CREATE TRIGGER trg_mgboost_wl_usage_sample_monotonic
        BEFORE UPDATE OF bytes_delta ON mgboost_wl_usage_samples
        WHEN NEW.bytes_delta < OLD.bytes_delta
        BEGIN SELECT RAISE(ABORT, 'usage sample bytes_delta cannot decrease'); END
    """,
    """
    CREATE TRIGGER trg_mgboost_wl_usage_sample_no_delete
        BEFORE DELETE ON mgboost_wl_usage_samples
        BEGIN SELECT RAISE(ABORT, 'usage sample history is immutable'); END
    """,
)

SCHEMA_CHECKSUM = hashlib.sha256(
    (MIGRATION_ID + "\n" + _FINAL_TABLE + "\n" + "\n".join(_FINAL_OBJECTS)).encode("utf-8")
).hexdigest()

_V1_SAMPLE_TRIGGERS = {
    "trg_mgboost_wl_usage_sample_identity_immutable",
    "trg_mgboost_wl_usage_sample_monotonic",
    "trg_mgboost_wl_usage_sample_no_delete",
}


def _table_names(connection: sqlite3.Connection) -> set[str]:
    return {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _sample_stats(connection: sqlite3.Connection, table: str) -> tuple[int, int]:
    row = connection.execute(
        f"SELECT COUNT(*) AS n, COALESCE(SUM(bytes_delta),0) AS total FROM {table}"
    ).fetchone()
    return int(row["n"]), int(row["total"])


def _verify_v1_source(connection: sqlite3.Connection) -> None:
    if "mgboost_wl_usage_samples" not in _table_names(connection):
        raise RuntimeError("PH6 ledger v1 usage-samples table is missing")
    columns = {row[1] for row in connection.execute("PRAGMA table_info(mgboost_wl_usage_samples)")}
    expected = set(_SAMPLE_COLUMNS.split(","))
    if columns != expected:
        raise RuntimeError("PH6 ledger v1 usage-samples columns are unknown or corrupt")
    triggers = {row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='mgboost_wl_usage_samples'"
    )}
    if triggers != _V1_SAMPLE_TRIGGERS:
        raise RuntimeError("PH6 ledger v1 usage-samples triggers are unknown or corrupt")
    indexes = [row for row in connection.execute("PRAGMA index_list(mgboost_wl_usage_samples)")]
    unique_indexes = [row[1] for row in indexes if int(row[2]) == 1]
    if len(unique_indexes) != 1:
        raise RuntimeError("PH6 ledger v1 usage-samples uniqueness is unknown or corrupt")
    index_columns = [row[2] for row in connection.execute(f"PRAGMA index_info({unique_indexes[0]})")]
    if index_columns != ["child_intent_id", "node_id", "sample_hour"]:
        raise RuntimeError("PH6 ledger v1 usage-samples key is unknown or corrupt")
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise RuntimeError("foreign-key corruption blocks PH6 ledger migration")


def _verify_final_schema(connection: sqlite3.Connection) -> None:
    if "mgboost_wl_usage_samples" not in _table_names(connection):
        raise RuntimeError("PH6 ledger v2 usage-samples table is missing")
    columns = {row[1] for row in connection.execute("PRAGMA table_info(mgboost_wl_usage_samples)")}
    if columns != set(_SAMPLE_COLUMNS.split(",")):
        raise RuntimeError("PH6 ledger v2 usage-samples columns are incompatible")
    objects = {row[0]: row[1] or "" for row in connection.execute(
        "SELECT name,sql FROM sqlite_master WHERE type IN ('index','trigger')"
    )}
    required = {
        "ux_mgboost_wl_usage_samples_period_bucket",
        "ix_mgboost_wl_usage_samples_period",
        *_V1_SAMPLE_TRIGGERS,
    }
    if not required.issubset(objects):
        raise RuntimeError("PH6 ledger v2 usage-samples indexes/triggers incomplete")
    key_sql = " ".join(objects["ux_mgboost_wl_usage_samples_period_bucket"].upper().split())
    if "COALESCE(WL_PERIOD_ID, 0)" not in key_sql:
        raise RuntimeError("PH6 ledger v2 period-aware key is incompatible")
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise RuntimeError("foreign-key corruption blocks PH6 ledger startup")


def apply_wl_usage_ledger_schema_v2(connection: sqlite3.Connection, *, now: int | None = None) -> bool:
    """Migrate PH6-03 samples to the period-aware uniqueness contract.

    SQLite cannot alter an inline UNIQUE constraint, so the table is rebuilt
    under one ``BEGIN IMMEDIATE`` transaction.  The source definition is
    pinned before any DDL, the copied row count and byte sum are checked, and
    any mismatch rolls the complete transaction back.
    """
    connection.execute("PRAGMA foreign_keys=ON")
    timestamp = int(time.time()) if now is None else int(now)
    try:
        connection.execute("BEGIN IMMEDIATE")
        v1 = connection.execute(
            "SELECT schema_checksum FROM mgboost_schema_migrations WHERE migration_id=?",
            (V1_MIGRATION_ID,),
        ).fetchone()
        if v1 is None or v1[0] != V1_SCHEMA_CHECKSUM:
            raise RuntimeError("PH6 ledger v2 requires exact PH6-03 v1 schema")
        existing = connection.execute(
            "SELECT schema_checksum FROM mgboost_schema_migrations WHERE migration_id=?",
            (MIGRATION_ID,),
        ).fetchone()
        if existing is not None:
            if existing[0] != SCHEMA_CHECKSUM:
                raise RuntimeError("PH6 ledger v2 schema checksum mismatch")
            _verify_final_schema(connection)
            connection.commit()
            return False

        _verify_v1_source(connection)
        before_count, before_total = _sample_stats(connection, "mgboost_wl_usage_samples")

        for trigger in _V1_SAMPLE_TRIGGERS:
            connection.execute(f"DROP TRIGGER {trigger}")
        connection.execute("DROP INDEX ix_mgboost_wl_usage_samples_period")
        connection.execute("ALTER TABLE mgboost_wl_usage_samples RENAME TO mgboost_wl_usage_samples_ph603_v1")
        connection.execute(_FINAL_TABLE)
        connection.execute(
            f"INSERT INTO mgboost_wl_usage_samples ({_SAMPLE_COLUMNS}) "
            f"SELECT {_SAMPLE_COLUMNS} FROM mgboost_wl_usage_samples_ph603_v1"
        )
        copied_count, copied_total = _sample_stats(connection, "mgboost_wl_usage_samples")
        if (copied_count, copied_total) != (before_count, before_total):
            raise RuntimeError("PH6 ledger migration row-count or byte-total mismatch")
        connection.execute("DROP TABLE mgboost_wl_usage_samples_ph603_v1")
        for statement in _FINAL_OBJECTS:
            connection.execute(statement)
        after_count, after_total = _sample_stats(connection, "mgboost_wl_usage_samples")
        if (after_count, after_total) != (before_count, before_total):
            raise RuntimeError("PH6 ledger migration post-rebuild verification mismatch")
        _verify_final_schema(connection)
        connection.execute(
            "INSERT INTO mgboost_schema_migrations "
            "(migration_id,schema_checksum,applied_at) VALUES (?,?,?)",
            (MIGRATION_ID, SCHEMA_CHECKSUM, timestamp),
        )
        connection.commit()
        return True
    except Exception:
        connection.rollback()
        raise
