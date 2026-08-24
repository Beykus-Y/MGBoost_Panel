"""Additive PH3-02 device-slot schema with monotonic generations."""

from __future__ import annotations

import hashlib
import sqlite3
import time

from .account_schema import MIGRATION_ID as PARENT_MIGRATION_ID
from .account_schema import SCHEMA_CHECKSUM as PARENT_SCHEMA_CHECKSUM


MIGRATION_ID = "ph3_02_device_slots_v1"

_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS mgboost_device_slots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        account_id INTEGER NOT NULL,
        slot_number INTEGER NOT NULL CHECK(slot_number BETWEEN 1 AND 99),
        slot_kind TEXT NOT NULL CHECK(slot_kind IN ('BASE','ADDON','INTERNAL')),
        current_generation INTEGER NOT NULL DEFAULT 0 CHECK(current_generation >= 0),
        desired_state TEXT NOT NULL DEFAULT 'FREE'
            CHECK(desired_state IN ('FREE','ACTIVE','DISABLED')),
        observed_state TEXT NOT NULL DEFAULT 'FREE'
            CHECK(observed_state IN ('FREE','ACTIVE','DISABLED','UNKNOWN')),
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        row_version INTEGER NOT NULL DEFAULT 1 CHECK(row_version > 0),
        UNIQUE(account_id, slot_number),
        UNIQUE(id, account_id, slot_number),
        FOREIGN KEY(account_id) REFERENCES mgboost_accounts(id) ON DELETE RESTRICT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS mgboost_device_slot_generations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        account_id INTEGER NOT NULL,
        slot_id INTEGER NOT NULL,
        slot_number INTEGER NOT NULL CHECK(slot_number BETWEEN 1 AND 99),
        generation INTEGER NOT NULL CHECK(generation > 0),
        hwid_verifier_version INTEGER NOT NULL DEFAULT 1
            CHECK(hwid_verifier_version BETWEEN 1 AND 1000000),
        hwid_verifier TEXT NOT NULL
            CHECK(length(hwid_verifier)=76 AND hwid_verifier LIKE 'hmac-sha256:%'),
        hwid_masked TEXT NOT NULL
            CHECK(length(hwid_masked)=17 AND hwid_masked LIKE 'hwid_%'),
        status TEXT NOT NULL CHECK(status IN ('ACTIVE','RELEASED','REVOKED')),
        claimed_at INTEGER NOT NULL,
        ended_at INTEGER,
        end_reason TEXT,
        UNIQUE(slot_id, generation),
        UNIQUE(id, account_id),
        CHECK(
            (status='ACTIVE' AND ended_at IS NULL AND end_reason IS NULL)
            OR (status IN ('RELEASED','REVOKED')
                AND ended_at IS NOT NULL AND end_reason IS NOT NULL)
        ),
        FOREIGN KEY(slot_id, account_id, slot_number)
            REFERENCES mgboost_device_slots(id, account_id, slot_number)
            ON DELETE RESTRICT
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS ux_mgboost_active_hwid
        ON mgboost_device_slot_generations(hwid_verifier_version, hwid_verifier)
        WHERE status='ACTIVE'
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS ux_mgboost_active_slot_generation
        ON mgboost_device_slot_generations(slot_id)
        WHERE status='ACTIVE'
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_mgboost_slot_account_state
        ON mgboost_device_slots(account_id, desired_state, slot_number)
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_mgboost_generation_account_history
        ON mgboost_device_slot_generations(account_id, slot_number, generation)
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mgboost_slots_identity_immutable
        BEFORE UPDATE OF account_id, slot_number, slot_kind, created_at
        ON mgboost_device_slots
        BEGIN SELECT RAISE(ABORT, 'slot identity is immutable'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mgboost_slots_generation_monotonic
        BEFORE UPDATE OF current_generation ON mgboost_device_slots
        WHEN NEW.current_generation < OLD.current_generation
          OR NEW.current_generation > OLD.current_generation + 1
        BEGIN SELECT RAISE(ABORT, 'slot generation must increase by one'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mgboost_slots_generation_has_active_claim
        BEFORE UPDATE OF current_generation ON mgboost_device_slots
        WHEN NEW.current_generation = OLD.current_generation + 1
          AND NOT EXISTS (
              SELECT 1 FROM mgboost_device_slot_generations AS g
              WHERE g.slot_id=OLD.id AND g.account_id=OLD.account_id
                AND g.slot_number=OLD.slot_number
                AND g.generation=NEW.current_generation AND g.status='ACTIVE'
          )
        BEGIN SELECT RAISE(ABORT, 'new slot generation requires active claim'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mgboost_slots_active_has_generation
        BEFORE UPDATE OF desired_state ON mgboost_device_slots
        WHEN NEW.desired_state='ACTIVE'
          AND NOT EXISTS (
              SELECT 1 FROM mgboost_device_slot_generations AS g
              WHERE g.slot_id=OLD.id AND g.generation=NEW.current_generation
                AND g.status='ACTIVE'
          )
        BEGIN SELECT RAISE(ABORT, 'active slot requires active generation'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mgboost_slots_free_has_no_active_generation
        BEFORE UPDATE OF desired_state ON mgboost_device_slots
        WHEN NEW.desired_state='FREE'
          AND EXISTS (
              SELECT 1 FROM mgboost_device_slot_generations AS g
              WHERE g.slot_id=OLD.id AND g.status='ACTIVE'
          )
        BEGIN SELECT RAISE(ABORT, 'free slot cannot retain active generation'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mgboost_slots_no_delete
        BEFORE DELETE ON mgboost_device_slots
        BEGIN SELECT RAISE(ABORT, 'stable slots cannot be deleted'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mgboost_generation_identity_immutable
        BEFORE UPDATE OF account_id, slot_id, slot_number, generation,
                         hwid_verifier_version, hwid_verifier, hwid_masked, claimed_at
        ON mgboost_device_slot_generations
        BEGIN SELECT RAISE(ABORT, 'slot generation identity is immutable'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mgboost_generation_terminal
        BEFORE UPDATE OF status ON mgboost_device_slot_generations
        WHEN OLD.status!='ACTIVE' OR NEW.status NOT IN ('RELEASED','REVOKED')
        BEGIN SELECT RAISE(ABORT, 'slot generation cannot be reactivated'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mgboost_generations_no_delete
        BEFORE DELETE ON mgboost_device_slot_generations
        BEGIN SELECT RAISE(ABORT, 'slot generation history is immutable'); END
    """,
)

SCHEMA_CHECKSUM = hashlib.sha256(
    "\n".join(statement.strip() for statement in _SCHEMA_STATEMENTS).encode("utf-8")
).hexdigest()

NEW_RUNTIME_TABLES = (
    "mgboost_device_slots",
    "mgboost_device_slot_generations",
)

_REQUIRED_COLUMNS = {
    "mgboost_device_slots": {
        "id", "account_id", "slot_number", "slot_kind", "current_generation",
        "desired_state", "observed_state", "row_version",
    },
    "mgboost_device_slot_generations": {
        "id", "account_id", "slot_id", "slot_number", "generation",
        "hwid_verifier_version", "hwid_verifier", "hwid_masked", "status",
        "claimed_at", "ended_at",
    },
}

_REQUIRED_OBJECTS = {
    "ux_mgboost_active_hwid",
    "ux_mgboost_active_slot_generation",
    "ix_mgboost_slot_account_state",
    "ix_mgboost_generation_account_history",
    "trg_mgboost_slots_identity_immutable",
    "trg_mgboost_slots_generation_monotonic",
    "trg_mgboost_slots_generation_has_active_claim",
    "trg_mgboost_slots_active_has_generation",
    "trg_mgboost_slots_free_has_no_active_generation",
    "trg_mgboost_slots_no_delete",
    "trg_mgboost_generation_identity_immutable",
    "trg_mgboost_generation_terminal",
    "trg_mgboost_generations_no_delete",
}


def _verify_schema(connection: sqlite3.Connection) -> None:
    for table, required in _REQUIRED_COLUMNS.items():
        actual = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        if required - actual:
            raise RuntimeError(f"PH3-02 incompatible table {table}: missing columns")
    objects = {
        row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE name LIKE 'ux_mgboost_%' "
            "OR name LIKE 'ix_mgboost_%' OR name LIKE 'trg_mgboost_%'"
        )
    }
    if not _REQUIRED_OBJECTS.issubset(objects):
        raise RuntimeError("PH3-02 schema indexes/triggers incomplete")


def apply_device_slot_schema(connection: sqlite3.Connection, *, now: int | None = None) -> bool:
    """Apply PH3-02 transactionally after the exact PH3-01 foundation."""
    connection.execute("PRAGMA foreign_keys=ON")
    applied_at = int(time.time()) if now is None else int(now)
    try:
        connection.execute("BEGIN IMMEDIATE")
        parent = connection.execute(
            "SELECT schema_checksum FROM mgboost_schema_migrations WHERE migration_id=?",
            (PARENT_MIGRATION_ID,),
        ).fetchone()
        if parent is None or parent[0] != PARENT_SCHEMA_CHECKSUM:
            raise RuntimeError("PH3-02 requires exact PH3-01 schema")
        existing = connection.execute(
            "SELECT schema_checksum FROM mgboost_schema_migrations WHERE migration_id=?",
            (MIGRATION_ID,),
        ).fetchone()
        if existing is not None:
            if existing[0] != SCHEMA_CHECKSUM:
                raise RuntimeError("PH3-02 schema checksum mismatch")
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
