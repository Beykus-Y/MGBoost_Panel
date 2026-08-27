"""PH5-12 additive operational delivery-routing schema.

This is deliberately NOT tariff data: host membership lives in its own
operational tables so that replacing Germany/Estonia/etc. never requires a
new immutable plan version or any repurchase. The only seeded profile is the
STANDARD delivery profile; future WL plans would map to additional profiles
without touching this schema.

Every admin mutation is audited into the append-only
``mgboost_delivery_profile_events`` table (the entitlement-mutations ledger
requires a NOT NULL account_id, which a routing mutation does not have, so
this is a sibling ledger with the same no-update/no-delete guarantees and
the same UNIQUE idempotency-key discipline).
"""

from __future__ import annotations

import hashlib
import sqlite3
import time

from .account_schema import MIGRATION_ID as ACCOUNT_MIGRATION_ID
from .account_schema import SCHEMA_CHECKSUM as ACCOUNT_SCHEMA_CHECKSUM


MIGRATION_ID = "ph5_12_delivery_routing_v1"

_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS mgboost_delivery_profiles (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        profile_code TEXT NOT NULL UNIQUE,
        row_version INTEGER NOT NULL DEFAULT 1 CHECK(row_version > 0),
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS mgboost_delivery_profile_hosts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        profile_id INTEGER NOT NULL,
        inbound_tag TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        UNIQUE(profile_id, inbound_tag),
        FOREIGN KEY(profile_id) REFERENCES mgboost_delivery_profiles(id)
            ON DELETE RESTRICT
    )
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mgboost_delivery_profile_hosts_no_update
        BEFORE UPDATE ON mgboost_delivery_profile_hosts
        BEGIN SELECT RAISE(ABORT, 'delivery profile membership is change-tracked; remove and re-add instead'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mgboost_delivery_profile_hosts_removal_needs_event
        BEFORE DELETE ON mgboost_delivery_profile_hosts
        WHEN NOT EXISTS (
            SELECT 1 FROM mgboost_delivery_profile_events e
            JOIN mgboost_delivery_profiles p ON p.profile_code=e.profile_code
            WHERE e.event_type='HOST_REMOVED' AND e.inbound_tag=OLD.inbound_tag
              AND p.id=OLD.profile_id
        )
        BEGIN SELECT RAISE(ABORT, 'membership removal requires a prior HOST_REMOVED audit event'); END
    """,
    """
    CREATE TABLE IF NOT EXISTS mgboost_plan_delivery_profiles (
        plan_code TEXT PRIMARY KEY,
        profile_id INTEGER NOT NULL,
        created_at INTEGER NOT NULL,
        FOREIGN KEY(profile_id) REFERENCES mgboost_delivery_profiles(id)
            ON DELETE RESTRICT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS mgboost_delivery_profile_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_type TEXT NOT NULL CHECK(event_type IN
            ('PROFILE_SEEDED','HOST_ADDED','HOST_REMOVED')),
        profile_code TEXT NOT NULL,
        inbound_tag TEXT,
        actor_type TEXT NOT NULL,
        actor_ref TEXT,
        reason TEXT NOT NULL,
        before_json TEXT NOT NULL,
        after_json TEXT NOT NULL,
        idempotency_key_hash TEXT UNIQUE,
        created_at INTEGER NOT NULL
    )
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mgboost_delivery_profile_events_no_update
        BEFORE UPDATE ON mgboost_delivery_profile_events
        BEGIN SELECT RAISE(ABORT, 'delivery routing events are immutable'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mgboost_delivery_profile_events_no_delete
        BEFORE DELETE ON mgboost_delivery_profile_events
        BEGIN SELECT RAISE(ABORT, 'delivery routing events are never deleted'); END
    """,
)

SCHEMA_CHECKSUM = hashlib.sha256(
    "\n".join(statement.strip() for statement in _SCHEMA_STATEMENTS).encode("utf-8")
).hexdigest()

_REQUIRED_COLUMNS = {
    "mgboost_delivery_profiles": {"id", "profile_code", "row_version", "updated_at"},
    "mgboost_delivery_profile_hosts": {"id", "profile_id", "inbound_tag", "created_at"},
    "mgboost_plan_delivery_profiles": {"plan_code", "profile_id", "created_at"},
    "mgboost_delivery_profile_events": {
        "id", "event_type", "profile_code", "inbound_tag", "actor_type", "actor_ref",
        "reason", "before_json", "after_json", "idempotency_key_hash", "created_at",
    },
}


def _verify(connection: sqlite3.Connection) -> None:
    for table, required in _REQUIRED_COLUMNS.items():
        actual = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        if required - actual:
            raise RuntimeError(f"PH5-12 incompatible table {table}")
    triggers = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
    required_triggers = {
        "trg_mgboost_delivery_profile_hosts_no_update",
        "trg_mgboost_delivery_profile_hosts_removal_needs_event",
        "trg_mgboost_delivery_profile_events_no_update",
        "trg_mgboost_delivery_profile_events_no_delete",
    }
    if required_triggers - triggers:
        raise RuntimeError(
            f"PH5-12 immutable routing triggers missing: {sorted(required_triggers - triggers)}"
        )


def apply_delivery_routing_schema(connection: sqlite3.Connection, *, now: int | None = None) -> bool:
    connection.execute("PRAGMA foreign_keys=ON")
    timestamp = int(time.time()) if now is None else int(now)
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT schema_checksum FROM mgboost_schema_migrations WHERE migration_id=?",
            (ACCOUNT_MIGRATION_ID,),
        ).fetchone()
        if not row or row[0] != ACCOUNT_SCHEMA_CHECKSUM:
            raise RuntimeError("PH5-12 requires exact PH3-01 schema")
        row = connection.execute(
            "SELECT schema_checksum FROM mgboost_schema_migrations WHERE migration_id=?",
            (MIGRATION_ID,),
        ).fetchone()
        if row:
            if row[0] != SCHEMA_CHECKSUM:
                raise RuntimeError("PH5-12 schema checksum mismatch")
            _verify(connection)
            connection.commit()
            return False
        for statement in _SCHEMA_STATEMENTS:
            connection.execute(statement)
        _verify(connection)
        connection.execute(
            "INSERT INTO mgboost_schema_migrations (migration_id,schema_checksum,applied_at) VALUES (?,?,?)",
            (MIGRATION_ID, SCHEMA_CHECKSUM, timestamp),
        )
        connection.commit()
        return True
    except Exception:
        connection.rollback()
        raise
