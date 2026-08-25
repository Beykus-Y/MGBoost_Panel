"""Additive dormant PH4-01 legacy subscription alias bridge schema.

Mirrors PH3-03's shadow-resolver-binding pattern exactly: an explicit,
root-only-created, per-account opt-in gate. No account is ever bridged
without a matching `enabled=1` row here, created ahead of time -- this is
the staged rollout mechanism PH4-01/PH4-03 require, independent of and in
addition to `OPAQUE_SUBSCRIPTION_ENABLED`-style flags.
"""

from __future__ import annotations

import hashlib
import sqlite3
import time

from .child_provisioning_schema import MIGRATION_ID as CHILD_MIGRATION_ID
from .child_provisioning_schema import SCHEMA_CHECKSUM as CHILD_SCHEMA_CHECKSUM


MIGRATION_ID = "ph4_01_legacy_bridge_v1"

_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS mgboost_legacy_bridge_bindings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        account_id INTEGER NOT NULL UNIQUE,
        legacy_alias_id INTEGER NOT NULL,
        enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
        decision_ref TEXT NOT NULL CHECK(length(decision_ref) BETWEEN 3 AND 128),
        created_by_actor TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        row_version INTEGER NOT NULL DEFAULT 1 CHECK(row_version > 0),
        UNIQUE(id, account_id),
        FOREIGN KEY(account_id) REFERENCES mgboost_accounts(id) ON DELETE RESTRICT,
        FOREIGN KEY(legacy_alias_id, account_id)
            REFERENCES mgboost_legacy_account_aliases(id, account_id) ON DELETE RESTRICT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS mgboost_legacy_bridge_binding_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        binding_id INTEGER NOT NULL,
        account_id INTEGER NOT NULL,
        event_type TEXT NOT NULL CHECK(event_type IN ('CREATED','ENABLED','DISABLED')),
        actor_ref TEXT NOT NULL,
        reason TEXT,
        created_at INTEGER NOT NULL,
        FOREIGN KEY(binding_id, account_id)
            REFERENCES mgboost_legacy_bridge_bindings(id, account_id) ON DELETE RESTRICT
    )
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_legacy_bridge_binding_identity_immutable
    BEFORE UPDATE OF account_id,legacy_alias_id,decision_ref,created_by_actor,created_at
    ON mgboost_legacy_bridge_bindings
    BEGIN SELECT RAISE(ABORT,'legacy bridge binding identity is immutable'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_legacy_bridge_binding_no_delete
    BEFORE DELETE ON mgboost_legacy_bridge_bindings
    BEGIN SELECT RAISE(ABORT,'legacy bridge binding history is immutable'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_legacy_bridge_binding_events_no_update
    BEFORE UPDATE ON mgboost_legacy_bridge_binding_events
    BEGIN SELECT RAISE(ABORT,'legacy bridge binding events are immutable'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_legacy_bridge_binding_events_no_delete
    BEFORE DELETE ON mgboost_legacy_bridge_binding_events
    BEGIN SELECT RAISE(ABORT,'legacy bridge binding events are immutable'); END
    """,
)

SCHEMA_CHECKSUM = hashlib.sha256(
    "\n".join(statement.strip() for statement in _SCHEMA_STATEMENTS).encode("utf-8")
).hexdigest()

NEW_RUNTIME_TABLES = (
    "mgboost_legacy_bridge_bindings",
    "mgboost_legacy_bridge_binding_events",
)


def _verify(connection: sqlite3.Connection) -> None:
    required = {
        "mgboost_legacy_bridge_bindings": {
            "id", "account_id", "legacy_alias_id", "enabled", "decision_ref",
            "created_by_actor", "created_at", "updated_at", "row_version",
        },
        "mgboost_legacy_bridge_binding_events": {
            "id", "binding_id", "account_id", "event_type", "actor_ref", "reason", "created_at",
        },
    }
    for table, columns in required.items():
        actual = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        if columns - actual:
            raise RuntimeError(f"PH4-01 legacy bridge table {table} is incompatible")
    objects = {row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE name LIKE 'trg_legacy_bridge_binding_%'"
    )}
    expected = {
        "trg_legacy_bridge_binding_identity_immutable", "trg_legacy_bridge_binding_no_delete",
        "trg_legacy_bridge_binding_events_no_update", "trg_legacy_bridge_binding_events_no_delete",
    }
    if not expected.issubset(objects):
        raise RuntimeError("PH4-01 legacy bridge schema objects incomplete")


def apply_legacy_bridge_schema(connection: sqlite3.Connection, *, now: int | None = None) -> bool:
    connection.execute("PRAGMA foreign_keys=ON")
    timestamp = int(time.time()) if now is None else int(now)
    try:
        connection.execute("BEGIN IMMEDIATE")
        parent = connection.execute(
            "SELECT schema_checksum FROM mgboost_schema_migrations WHERE migration_id=?",
            (CHILD_MIGRATION_ID,),
        ).fetchone()
        if not parent or parent[0] != CHILD_SCHEMA_CHECKSUM:
            raise RuntimeError("PH4-01 legacy bridge schema requires the exact PH3-03 schema")
        existing = connection.execute(
            "SELECT schema_checksum FROM mgboost_schema_migrations WHERE migration_id=?",
            (MIGRATION_ID,),
        ).fetchone()
        if existing:
            if existing[0] != SCHEMA_CHECKSUM:
                raise RuntimeError("PH4-01 legacy bridge schema checksum mismatch")
            _verify(connection)
            connection.commit()
            return False
        for statement in _SCHEMA_STATEMENTS:
            connection.execute(statement)
        _verify(connection)
        connection.execute(
            "INSERT INTO mgboost_schema_migrations (migration_id,schema_checksum,applied_at) "
            "VALUES (?,?,?)",
            (MIGRATION_ID, SCHEMA_CHECKSUM, timestamp),
        )
        connection.commit()
        return True
    except Exception:
        connection.rollback()
        raise
