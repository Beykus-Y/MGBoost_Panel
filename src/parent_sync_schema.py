"""Additive dormant PH3-08 parent status/expiry -> active children sync schema.

Reuses PH3-01's existing `mgboost_entitlement_state` table (desired_status +
monotonic revision) as the canonical parent desired-state -- that table was
defined in PH3-01 but nothing wrote to it until this module. This schema adds
only the durable per-child convergence outbox on top of it.
"""

from __future__ import annotations

import hashlib
import sqlite3
import time

from .child_provisioning_schema import MIGRATION_ID as CHILD_MIGRATION_ID
from .child_provisioning_schema import SCHEMA_CHECKSUM as CHILD_SCHEMA_CHECKSUM
from .account_schema import MIGRATION_ID as ACCOUNT_MIGRATION_ID
from .account_schema import SCHEMA_CHECKSUM as ACCOUNT_SCHEMA_CHECKSUM


MIGRATION_ID = "ph3_08_parent_sync_v1"

_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS mgboost_parent_sync_operations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        operation_id TEXT NOT NULL UNIQUE
            CHECK(substr(operation_id,1,3)='sy_'
                  AND length(operation_id)=29
                  AND substr(operation_id,4) NOT GLOB '*[^a-z2-7]*'),
        account_id INTEGER NOT NULL,
        child_intent_id INTEGER NOT NULL,
        parent_revision INTEGER NOT NULL CHECK(parent_revision > 0),
        desired_status TEXT NOT NULL CHECK(desired_status IN ('active','disabled')),
        desired_expire INTEGER CHECK(desired_expire IS NULL OR desired_expire >= 0),
        state TEXT NOT NULL DEFAULT 'PENDING'
            CHECK(state IN ('PENDING','IN_FLIGHT','RETRY','APPLIED','ERROR','SUPERSEDED')),
        idempotency_key_hash TEXT NOT NULL UNIQUE
            CHECK(length(idempotency_key_hash)=64
                  AND idempotency_key_hash NOT GLOB '*[^0-9a-f]*'),
        request_hash TEXT NOT NULL
            CHECK(length(request_hash)=64 AND request_hash NOT GLOB '*[^0-9a-f]*'),
        payload_json TEXT NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
        next_attempt_at INTEGER NOT NULL,
        lease_owner TEXT,
        lease_expires_at INTEGER,
        last_error_class TEXT,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        row_version INTEGER NOT NULL DEFAULT 1 CHECK(row_version > 0),
        UNIQUE(child_intent_id, parent_revision),
        UNIQUE(id, account_id),
        CHECK((state='IN_FLIGHT' AND lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)
              OR (state!='IN_FLIGHT' AND lease_owner IS NULL AND lease_expires_at IS NULL)),
        CHECK(desired_status='active' OR desired_expire IS NULL),
        FOREIGN KEY(account_id) REFERENCES mgboost_accounts(id) ON DELETE RESTRICT,
        FOREIGN KEY(child_intent_id, account_id)
            REFERENCES mgboost_child_user_intents(id, account_id) ON DELETE RESTRICT
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_mgboost_parent_sync_ready
        ON mgboost_parent_sync_operations(state, next_attempt_at, id)
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_mgboost_parent_sync_child
        ON mgboost_parent_sync_operations(child_intent_id, parent_revision DESC)
    """,
    """
    CREATE TABLE IF NOT EXISTS mgboost_parent_sync_attempt_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        sync_operation_id INTEGER NOT NULL,
        account_id INTEGER NOT NULL,
        attempt_no INTEGER NOT NULL CHECK(attempt_no > 0),
        event_type TEXT NOT NULL
            CHECK(event_type IN ('STARTED','SUCCEEDED','FAILED','SUPERSEDED','RECONCILED')),
        outcome TEXT,
        remote_effect_verifier TEXT,
        safe_error_class TEXT,
        created_at INTEGER NOT NULL,
        UNIQUE(sync_operation_id, attempt_no, event_type),
        FOREIGN KEY(sync_operation_id, account_id)
            REFERENCES mgboost_parent_sync_operations(id, account_id) ON DELETE RESTRICT
    )
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mgboost_parent_sync_identity_immutable
        BEFORE UPDATE OF operation_id,account_id,child_intent_id,parent_revision,
                         desired_status,desired_expire,idempotency_key_hash,
                         request_hash,payload_json,created_at
        ON mgboost_parent_sync_operations
        BEGIN SELECT RAISE(ABORT, 'parent sync operation identity is immutable'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mgboost_parent_sync_no_delete
        BEFORE DELETE ON mgboost_parent_sync_operations
        BEGIN SELECT RAISE(ABORT, 'parent sync operation history is immutable'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mgboost_parent_sync_events_no_update
        BEFORE UPDATE ON mgboost_parent_sync_attempt_events
        BEGIN SELECT RAISE(ABORT, 'parent sync attempt events are immutable'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mgboost_parent_sync_events_no_delete
        BEFORE DELETE ON mgboost_parent_sync_attempt_events
        BEGIN SELECT RAISE(ABORT, 'parent sync attempt events are immutable'); END
    """,
)

SCHEMA_CHECKSUM = hashlib.sha256(
    "\n".join(statement.strip() for statement in _SCHEMA_STATEMENTS).encode("utf-8")
).hexdigest()

NEW_RUNTIME_TABLES = (
    "mgboost_parent_sync_operations",
    "mgboost_parent_sync_attempt_events",
)


def _verify(connection: sqlite3.Connection) -> None:
    required = {
        "mgboost_parent_sync_operations": {
            "id", "operation_id", "account_id", "child_intent_id", "parent_revision",
            "desired_status", "desired_expire", "state", "idempotency_key_hash",
            "request_hash", "payload_json", "attempts", "next_attempt_at",
            "lease_owner", "lease_expires_at", "row_version",
        },
        "mgboost_parent_sync_attempt_events": {
            "id", "sync_operation_id", "account_id", "attempt_no", "event_type",
            "outcome", "remote_effect_verifier", "safe_error_class", "created_at",
        },
    }
    for table, columns in required.items():
        actual = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        if columns - actual:
            raise RuntimeError(f"PH3-08 parent sync table {table} is incompatible")
    objects = {row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE name LIKE 'trg_mgboost_parent_sync_%'"
    )}
    expected = {
        "trg_mgboost_parent_sync_identity_immutable", "trg_mgboost_parent_sync_no_delete",
        "trg_mgboost_parent_sync_events_no_update", "trg_mgboost_parent_sync_events_no_delete",
    }
    if not expected.issubset(objects):
        raise RuntimeError("PH3-08 parent sync schema objects incomplete")


def apply_parent_sync_schema(connection: sqlite3.Connection, *, now: int | None = None) -> bool:
    connection.execute("PRAGMA foreign_keys=ON")
    timestamp = int(time.time()) if now is None else int(now)
    try:
        connection.execute("BEGIN IMMEDIATE")
        for dep_id, dep_checksum, label in (
            (ACCOUNT_MIGRATION_ID, ACCOUNT_SCHEMA_CHECKSUM, "PH3-01"),
            (CHILD_MIGRATION_ID, CHILD_SCHEMA_CHECKSUM, "PH3-03"),
        ):
            parent = connection.execute(
                "SELECT schema_checksum FROM mgboost_schema_migrations WHERE migration_id=?",
                (dep_id,),
            ).fetchone()
            if not parent or parent[0] != dep_checksum:
                raise RuntimeError(f"PH3-08 parent sync schema requires the exact {label} schema")
        existing = connection.execute(
            "SELECT schema_checksum FROM mgboost_schema_migrations WHERE migration_id=?",
            (MIGRATION_ID,),
        ).fetchone()
        if existing:
            if existing[0] != SCHEMA_CHECKSUM:
                raise RuntimeError("PH3-08 parent sync schema checksum mismatch")
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
