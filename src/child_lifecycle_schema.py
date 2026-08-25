"""Additive dormant PH3-05 durable device revoke/free/rebind lifecycle schema."""

from __future__ import annotations

import hashlib
import sqlite3
import time

from .child_provisioning_schema import MIGRATION_ID as CHILD_MIGRATION_ID
from .child_provisioning_schema import SCHEMA_CHECKSUM as CHILD_SCHEMA_CHECKSUM


MIGRATION_ID = "ph3_05_child_lifecycle_v1"

_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS mgboost_child_lifecycle_operations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        operation_id TEXT NOT NULL UNIQUE
            CHECK(substr(operation_id,1,3)='lc_'
                  AND length(operation_id)=29
                  AND substr(operation_id,4) NOT GLOB '*[^a-z2-7]*'),
        account_id INTEGER NOT NULL,
        slot_id INTEGER NOT NULL,
        old_slot_generation_id INTEGER NOT NULL,
        old_child_intent_id INTEGER NOT NULL,
        operation_kind TEXT NOT NULL CHECK(operation_kind IN ('REVOKE','FREE','REBIND')),
        new_slot_generation_id INTEGER,
        new_child_intent_id INTEGER,
        state TEXT NOT NULL DEFAULT 'PENDING'
            CHECK(state IN ('PENDING','IN_FLIGHT','RETRY','APPLIED','ERROR')),
        idempotency_key_hash TEXT NOT NULL UNIQUE
            CHECK(length(idempotency_key_hash)=64
                  AND idempotency_key_hash NOT GLOB '*[^0-9a-f]*'),
        request_hash TEXT NOT NULL
            CHECK(length(request_hash)=64 AND request_hash NOT GLOB '*[^0-9a-f]*'),
        payload_json TEXT NOT NULL,
        reason TEXT NOT NULL CHECK(length(reason) BETWEEN 3 AND 300),
        attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
        next_attempt_at INTEGER NOT NULL,
        lease_owner TEXT,
        lease_expires_at INTEGER,
        last_error_class TEXT,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        row_version INTEGER NOT NULL DEFAULT 1 CHECK(row_version > 0),
        UNIQUE(old_child_intent_id, operation_kind),
        UNIQUE(id, account_id),
        UNIQUE(new_child_intent_id),
        CHECK((state='IN_FLIGHT' AND lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)
              OR (state!='IN_FLIGHT' AND lease_owner IS NULL AND lease_expires_at IS NULL)),
        CHECK(operation_kind='REBIND'
              OR (new_slot_generation_id IS NULL AND new_child_intent_id IS NULL)),
        FOREIGN KEY(account_id) REFERENCES mgboost_accounts(id) ON DELETE RESTRICT,
        FOREIGN KEY(old_child_intent_id, account_id)
            REFERENCES mgboost_child_user_intents(id, account_id) ON DELETE RESTRICT,
        FOREIGN KEY(new_child_intent_id, account_id)
            REFERENCES mgboost_child_user_intents(id, account_id) ON DELETE RESTRICT
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_mgboost_lifecycle_ready
        ON mgboost_child_lifecycle_operations(state, next_attempt_at, id)
    """,
    """
    CREATE TABLE IF NOT EXISTS mgboost_child_lifecycle_attempt_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        lifecycle_operation_id INTEGER NOT NULL,
        account_id INTEGER NOT NULL,
        attempt_no INTEGER NOT NULL CHECK(attempt_no > 0),
        event_type TEXT NOT NULL
            CHECK(event_type IN ('STARTED','SUCCEEDED','FAILED','RECONCILED')),
        outcome TEXT,
        remote_effect_verifier TEXT,
        safe_error_class TEXT,
        created_at INTEGER NOT NULL,
        UNIQUE(lifecycle_operation_id, attempt_no, event_type),
        FOREIGN KEY(lifecycle_operation_id, account_id)
            REFERENCES mgboost_child_lifecycle_operations(id, account_id) ON DELETE RESTRICT
    )
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mgboost_lifecycle_identity_immutable
        BEFORE UPDATE OF operation_id,account_id,slot_id,old_slot_generation_id,
                         old_child_intent_id,operation_kind,idempotency_key_hash,
                         request_hash,payload_json,reason,created_at
        ON mgboost_child_lifecycle_operations
        BEGIN SELECT RAISE(ABORT, 'lifecycle operation identity is immutable'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mgboost_lifecycle_no_delete
        BEFORE DELETE ON mgboost_child_lifecycle_operations
        BEGIN SELECT RAISE(ABORT, 'lifecycle operation history is immutable'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mgboost_lifecycle_new_child_once
        BEFORE UPDATE OF new_slot_generation_id, new_child_intent_id
        ON mgboost_child_lifecycle_operations
        WHEN OLD.new_child_intent_id IS NOT NULL
        BEGIN SELECT RAISE(ABORT, 'rebind new-child identity is immutable once set'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mgboost_lifecycle_events_no_update
        BEFORE UPDATE ON mgboost_child_lifecycle_attempt_events
        BEGIN SELECT RAISE(ABORT, 'lifecycle attempt events are immutable'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mgboost_lifecycle_events_no_delete
        BEFORE DELETE ON mgboost_child_lifecycle_attempt_events
        BEGIN SELECT RAISE(ABORT, 'lifecycle attempt events are immutable'); END
    """,
)

SCHEMA_CHECKSUM = hashlib.sha256(
    "\n".join(statement.strip() for statement in _SCHEMA_STATEMENTS).encode("utf-8")
).hexdigest()

NEW_RUNTIME_TABLES = (
    "mgboost_child_lifecycle_operations",
    "mgboost_child_lifecycle_attempt_events",
)


def _verify(connection: sqlite3.Connection) -> None:
    required = {
        "mgboost_child_lifecycle_operations": {
            "id", "operation_id", "account_id", "slot_id", "old_slot_generation_id",
            "old_child_intent_id", "operation_kind", "new_slot_generation_id",
            "new_child_intent_id", "state", "idempotency_key_hash", "request_hash",
            "payload_json", "reason", "attempts", "next_attempt_at", "lease_owner",
            "lease_expires_at", "row_version",
        },
        "mgboost_child_lifecycle_attempt_events": {
            "id", "lifecycle_operation_id", "account_id", "attempt_no", "event_type",
            "outcome", "remote_effect_verifier", "safe_error_class", "created_at",
        },
    }
    for table, columns in required.items():
        actual = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        if columns - actual:
            raise RuntimeError(f"PH3-05 lifecycle table {table} is incompatible")
    objects = {row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE name LIKE 'trg_mgboost_lifecycle_%'"
    )}
    expected = {
        "trg_mgboost_lifecycle_identity_immutable", "trg_mgboost_lifecycle_no_delete",
        "trg_mgboost_lifecycle_new_child_once", "trg_mgboost_lifecycle_events_no_update",
        "trg_mgboost_lifecycle_events_no_delete",
    }
    if not expected.issubset(objects):
        raise RuntimeError("PH3-05 lifecycle schema objects incomplete")


def apply_child_lifecycle_schema(connection: sqlite3.Connection, *, now: int | None = None) -> bool:
    connection.execute("PRAGMA foreign_keys=ON")
    timestamp = int(time.time()) if now is None else int(now)
    try:
        connection.execute("BEGIN IMMEDIATE")
        parent = connection.execute(
            "SELECT schema_checksum FROM mgboost_schema_migrations WHERE migration_id=?",
            (CHILD_MIGRATION_ID,),
        ).fetchone()
        if not parent or parent[0] != CHILD_SCHEMA_CHECKSUM:
            raise RuntimeError("PH3-05 lifecycle schema requires the exact PH3-03 parent schema")
        existing = connection.execute(
            "SELECT schema_checksum FROM mgboost_schema_migrations WHERE migration_id=?",
            (MIGRATION_ID,),
        ).fetchone()
        if existing:
            if existing[0] != SCHEMA_CHECKSUM:
                raise RuntimeError("PH3-05 lifecycle schema checksum mismatch")
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
