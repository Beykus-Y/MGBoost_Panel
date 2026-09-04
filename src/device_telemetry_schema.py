"""PH8-06 additive canonical opaque device telemetry schema.

One row per `mgboost_device_slot_generations.id` (UNIQUE) -- this is the
entire rebind-safety guarantee: `device_slots.claim()`/`rebind()` always
mint a brand new `slot_generation_id` for a new HWID, so a successor
generation always starts with zero telemetry rows of its own; a
predecessor's row is left untouched (immutable historical evidence),
never copied/migrated forward and never matched against the new
generation's identity.

Never stores raw HWID, raw UUID, opaque token, IP, or full User-Agent --
`hwid_verifier` is the exact same privacy-safe keyed-HMAC value
`mgboost_device_slot_generations.hwid_verifier` already carries, reused
so `device_real_projection.project_real_device()`'s existing exact-match
proof contract needs no change to consume this table.
"""

from __future__ import annotations

import hashlib
import sqlite3
import time

from .device_slot_schema import MIGRATION_ID as SLOT_MIGRATION_ID
from .device_slot_schema import SCHEMA_CHECKSUM as SLOT_SCHEMA_CHECKSUM


MIGRATION_ID = "ph8_06_device_telemetry_v1"

_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS mgboost_device_telemetry (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        account_id INTEGER NOT NULL,
        slot_generation_id INTEGER NOT NULL,
        hwid_verifier TEXT NOT NULL
            CHECK(length(hwid_verifier)=76 AND hwid_verifier LIKE 'hmac-sha256:%'),
        model TEXT,
        platform TEXT,
        client_name TEXT,
        client_version TEXT,
        observation_count INTEGER NOT NULL DEFAULT 1 CHECK(observation_count > 0),
        first_seen_at INTEGER NOT NULL,
        last_seen_at INTEGER NOT NULL,
        UNIQUE(slot_generation_id),
        FOREIGN KEY(slot_generation_id) REFERENCES mgboost_device_slot_generations(id)
            ON DELETE RESTRICT
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_mgboost_device_telemetry_account_verifier
        ON mgboost_device_telemetry(account_id, hwid_verifier)
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mgboost_device_telemetry_identity_immutable
        BEFORE UPDATE OF account_id, slot_generation_id, hwid_verifier, first_seen_at
        ON mgboost_device_telemetry
        BEGIN SELECT RAISE(ABORT, 'device telemetry identity is immutable'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mgboost_device_telemetry_no_delete
        BEFORE DELETE ON mgboost_device_telemetry
        BEGIN SELECT RAISE(ABORT, 'device telemetry history is immutable'); END
    """,
)

SCHEMA_CHECKSUM = hashlib.sha256(
    "\n".join(statement.strip() for statement in _SCHEMA_STATEMENTS).encode("utf-8")
).hexdigest()

NEW_RUNTIME_TABLES = ("mgboost_device_telemetry",)

_REQUIRED_COLUMNS = {
    "mgboost_device_telemetry": {
        "id", "account_id", "slot_generation_id", "hwid_verifier", "model",
        "platform", "client_name", "client_version", "observation_count",
        "first_seen_at", "last_seen_at",
    },
}

_REQUIRED_OBJECTS = {
    "ix_mgboost_device_telemetry_account_verifier",
    "trg_mgboost_device_telemetry_identity_immutable",
    "trg_mgboost_device_telemetry_no_delete",
}


def _verify_schema(connection: sqlite3.Connection) -> None:
    for table, required in _REQUIRED_COLUMNS.items():
        actual = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        if required - actual:
            raise RuntimeError(f"PH8-06 incompatible table {table}: missing columns")
    objects = {
        row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE name LIKE 'ix_mgboost_device_telemetry_%' "
            "OR name LIKE 'trg_mgboost_device_telemetry_%'"
        )
    }
    if not _REQUIRED_OBJECTS.issubset(objects):
        raise RuntimeError("PH8-06 schema indexes/triggers incomplete")


def apply_device_telemetry_schema(connection: sqlite3.Connection, *, now: int | None = None) -> bool:
    """Apply PH8-06 transactionally after the exact PH3-02 slot foundation."""
    connection.execute("PRAGMA foreign_keys=ON")
    applied_at = int(time.time()) if now is None else int(now)
    try:
        connection.execute("BEGIN IMMEDIATE")
        parent = connection.execute(
            "SELECT schema_checksum FROM mgboost_schema_migrations WHERE migration_id=?",
            (SLOT_MIGRATION_ID,),
        ).fetchone()
        if parent is None or parent[0] != SLOT_SCHEMA_CHECKSUM:
            raise RuntimeError("PH8-06 requires exact PH3-02 device slot schema")
        existing = connection.execute(
            "SELECT schema_checksum FROM mgboost_schema_migrations WHERE migration_id=?",
            (MIGRATION_ID,),
        ).fetchone()
        if existing is not None:
            if existing[0] != SCHEMA_CHECKSUM:
                raise RuntimeError("PH8-06 schema checksum mismatch")
            _verify_schema(connection)
            connection.commit()
            return False
        for statement in _SCHEMA_STATEMENTS:
            connection.execute(statement)
        _verify_schema(connection)
        connection.execute(
            "INSERT INTO mgboost_schema_migrations "
            "(migration_id,schema_checksum,applied_at) VALUES (?,?,?)",
            (MIGRATION_ID, SCHEMA_CHECKSUM, applied_at),
        )
        connection.commit()
        return True
    except Exception:
        connection.rollback()
        raise
