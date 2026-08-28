"""Additive PH6-09 schema: versioned WL topology registry.

One row per topology config_version this deployment has ever positively
asserted (assertion OK under that exact version), recording the exact
`WL_INBOUND_TAGS` set that version named. Append-only, no identifiers.

This registry is what makes "NEWLY-APPROVED WL inbound" a *durable,
provable* concept for the DL-059 owner decision: the tags added by an
approved baseline update are exactly `current_tags - tags(version the
child last converged under)`. Without it there is no way to distinguish a
newly-approved tag from a tag a child's provisioning deliberately never
included -- and guessing is precisely what this project never does.
"""

from __future__ import annotations

import hashlib
import sqlite3
import time


MIGRATION_ID = "ph6_09_wl_topology_versions_v1"

_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS mgboost_wl_topology_versions (
        config_version TEXT PRIMARY KEY,
        wl_tags_json TEXT NOT NULL,
        first_seen_at INTEGER NOT NULL
    )
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mgboost_wl_topology_versions_no_update
        BEFORE UPDATE ON mgboost_wl_topology_versions
        BEGIN SELECT RAISE(ABORT, 'WL topology versions are append-only'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mgboost_wl_topology_versions_no_delete
        BEFORE DELETE ON mgboost_wl_topology_versions
        BEGIN SELECT RAISE(ABORT, 'WL topology versions are append-only'); END
    """,
)

SCHEMA_CHECKSUM = hashlib.sha256(
    "\n".join(statement.strip() for statement in _SCHEMA_STATEMENTS).encode("utf-8")
).hexdigest()


def _verify(connection: sqlite3.Connection) -> None:
    tables = {row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name='mgboost_wl_topology_versions'"
    )}
    if "mgboost_wl_topology_versions" not in tables:
        raise RuntimeError("PH6-09 WL topology version registry missing")
    triggers = {row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE name LIKE "
        "'trg_mgboost_wl_topology_versions_%'"
    )}
    if not {
        "trg_mgboost_wl_topology_versions_no_update",
        "trg_mgboost_wl_topology_versions_no_delete",
    }.issubset(triggers):
        raise RuntimeError("PH6-09 WL topology version triggers incomplete")


def apply_wl_topology_versions_schema(connection: sqlite3.Connection, *, now: int | None = None) -> bool:
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
                raise RuntimeError("PH6-09 topology-versions schema checksum mismatch")
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
