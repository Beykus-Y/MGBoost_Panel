"""Additive PH6-01 schema: append-only WL topology assertion log.

One row per runtime topology check (`WLTopologyGuardStore.run_assertion`).
Mirrors the project's established append-only-audit-log pattern (no
UPDATE/DELETE ever) rather than mutating any prior row -- the history of
what the topology looked like at each check is itself the evidence a
future PH6-06 enforcement decision, or a human alert, would need.
"""

from __future__ import annotations

import hashlib
import sqlite3
import time


MIGRATION_ID = "ph6_01_wl_topology_guard_v1"

_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS mgboost_wl_topology_assertions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        config_version TEXT NOT NULL,
        ok INTEGER NOT NULL CHECK(ok IN (0,1)),
        missing_tags_json TEXT NOT NULL,
        extra_wl_like_tags_json TEXT NOT NULL,
        missing_node_ids_json TEXT NOT NULL,
        node_field_mismatches_json TEXT NOT NULL,
        checked_at INTEGER NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_mgboost_wl_topology_assertions_checked_at
        ON mgboost_wl_topology_assertions(checked_at)
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mgboost_wl_topology_assertions_no_update
        BEFORE UPDATE ON mgboost_wl_topology_assertions
        BEGIN SELECT RAISE(ABORT, 'WL topology assertions are append-only'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mgboost_wl_topology_assertions_no_delete
        BEFORE DELETE ON mgboost_wl_topology_assertions
        BEGIN SELECT RAISE(ABORT, 'WL topology assertions are append-only'); END
    """,
)

SCHEMA_CHECKSUM = hashlib.sha256(
    "\n".join(statement.strip() for statement in _SCHEMA_STATEMENTS).encode("utf-8")
).hexdigest()


def _verify(connection: sqlite3.Connection) -> None:
    tables = {row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='mgboost_wl_topology_assertions'"
    )}
    if "mgboost_wl_topology_assertions" not in tables:
        raise RuntimeError("PH6-01 WL topology assertion table missing")
    triggers = {row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE name LIKE 'trg_mgboost_wl_topology_assertions_%'"
    )}
    expected = {
        "trg_mgboost_wl_topology_assertions_no_update",
        "trg_mgboost_wl_topology_assertions_no_delete",
    }
    if not expected.issubset(triggers):
        raise RuntimeError("PH6-01 WL topology assertion triggers incomplete")


def apply_wl_topology_guard_schema(connection: sqlite3.Connection, *, now: int | None = None) -> bool:
    connection.execute("PRAGMA foreign_keys=ON")
    timestamp = int(time.time()) if now is None else int(now)
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT schema_checksum FROM mgboost_schema_migrations WHERE migration_id=?",
            (MIGRATION_ID,),
        ).fetchone()
        if row:
            if row[0] != SCHEMA_CHECKSUM:
                raise RuntimeError("PH6-01 schema checksum mismatch")
            _verify(connection)
            connection.commit()
            return False
        for statement in _SCHEMA_STATEMENTS:
            connection.execute(statement)
        _verify(connection)
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
