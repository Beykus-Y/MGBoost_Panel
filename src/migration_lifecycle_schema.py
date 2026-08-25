"""Additive dormant PH4-02 durable migration state machine schema.

One row per (account_id, hwid_verifier) migration lineage -- one logical
device, one authoritative migration lineage, never two. `LEGACY` is the
implicit absence of a row here (mirrors PH4-01's own "no binding = fall
through" pattern) -- a row is created only at the exact moment a durable
MIGRATING decision has already been made (a slot has already been claimed
by `resolve_account_device()`), never before. Stores only immutable/non-secret
identity: account_id, legacy_alias_id (reviewed alias reference),
hwid_verifier (the same keyed HMAC form PH3-02 already uses -- never a raw
HWID), slot_generation_id/child_intent_id (references into the unmodified
PH3-02/03 tables, never a raw child UUID).
"""

from __future__ import annotations

import hashlib
import sqlite3
import time

from .account_schema import MIGRATION_ID as ACCOUNT_MIGRATION_ID
from .account_schema import SCHEMA_CHECKSUM as ACCOUNT_SCHEMA_CHECKSUM
from .device_slot_schema import MIGRATION_ID as DEVICE_SLOT_MIGRATION_ID
from .device_slot_schema import SCHEMA_CHECKSUM as DEVICE_SLOT_SCHEMA_CHECKSUM
from .child_provisioning_schema import MIGRATION_ID as CHILD_PROVISIONING_MIGRATION_ID
from .child_provisioning_schema import SCHEMA_CHECKSUM as CHILD_PROVISIONING_SCHEMA_CHECKSUM
from .legacy_bridge_schema import MIGRATION_ID as LEGACY_BRIDGE_MIGRATION_ID
from .legacy_bridge_schema import SCHEMA_CHECKSUM as LEGACY_BRIDGE_SCHEMA_CHECKSUM


MIGRATION_ID = "ph4_02_migration_lifecycle_v1"

_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS mgboost_migration_bindings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        operation_id TEXT NOT NULL UNIQUE
            CHECK(substr(operation_id,1,3)='mg_'
                  AND length(operation_id)=29
                  AND substr(operation_id,4) NOT GLOB '*[^a-z2-7]*'),
        account_id INTEGER NOT NULL,
        legacy_alias_id INTEGER NOT NULL,
        hwid_verifier TEXT NOT NULL
            CHECK(length(hwid_verifier)=76 AND hwid_verifier LIKE 'hmac-sha256:%'),
        slot_generation_id INTEGER,
        child_intent_id INTEGER,
        state TEXT NOT NULL DEFAULT 'MIGRATING'
            CHECK(state IN ('MIGRATING','MIGRATED','LEGACY_REVOKE_PENDING',
                             'LEGACY_REVOKED','ERROR_RECONCILE')),
        revision INTEGER NOT NULL DEFAULT 1 CHECK(revision > 0),
        idempotency_key_hash TEXT NOT NULL UNIQUE
            CHECK(length(idempotency_key_hash)=64
                  AND idempotency_key_hash NOT GLOB '*[^0-9a-f]*'),
        request_hash TEXT NOT NULL
            CHECK(length(request_hash)=64 AND request_hash NOT GLOB '*[^0-9a-f]*'),
        actor_ref TEXT NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
        next_attempt_at INTEGER NOT NULL,
        lease_owner TEXT,
        lease_expires_at INTEGER,
        last_error_class TEXT,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        row_version INTEGER NOT NULL DEFAULT 1 CHECK(row_version > 0),
        UNIQUE(account_id, hwid_verifier),
        UNIQUE(id, account_id),
        FOREIGN KEY(account_id) REFERENCES mgboost_accounts(id) ON DELETE RESTRICT,
        FOREIGN KEY(legacy_alias_id) REFERENCES mgboost_legacy_account_aliases(id) ON DELETE RESTRICT,
        FOREIGN KEY(slot_generation_id) REFERENCES mgboost_device_slot_generations(id) ON DELETE RESTRICT,
        FOREIGN KEY(child_intent_id) REFERENCES mgboost_child_user_intents(id) ON DELETE RESTRICT
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_mgboost_migration_bindings_ready
        ON mgboost_migration_bindings(state, next_attempt_at, id)
    """,
    """
    CREATE TABLE IF NOT EXISTS mgboost_migration_binding_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        migration_binding_id INTEGER NOT NULL,
        account_id INTEGER NOT NULL,
        attempt_no INTEGER NOT NULL CHECK(attempt_no >= 0),
        event_type TEXT NOT NULL
            CHECK(event_type IN ('CREATED','SLOT_RECORDED','CHILD_RECORDED','MIGRATED',
                                  'RETRY','ERROR_RECONCILE','RECONCILE_TO_MIGRATING',
                                  'RECONCILE_TO_MIGRATED','RECONCILE_STALE',
                                  'REVOKE_PENDING_STARTED','LEGACY_REVOKED')),
        from_state TEXT,
        to_state TEXT,
        safe_error_class TEXT,
        reason TEXT,
        created_at INTEGER NOT NULL,
        FOREIGN KEY(migration_binding_id, account_id)
            REFERENCES mgboost_migration_bindings(id, account_id) ON DELETE RESTRICT
    )
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_migration_bindings_identity_immutable
        BEFORE UPDATE OF operation_id,account_id,legacy_alias_id,hwid_verifier,
                         idempotency_key_hash,request_hash,actor_ref,created_at
        ON mgboost_migration_bindings
        BEGIN SELECT RAISE(ABORT, 'migration binding identity is immutable'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_migration_bindings_no_delete
        BEFORE DELETE ON mgboost_migration_bindings
        BEGIN SELECT RAISE(ABORT, 'migration binding history is immutable'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_migration_bindings_terminal_immutable
        BEFORE UPDATE OF state,slot_generation_id,child_intent_id
        ON mgboost_migration_bindings
        WHEN OLD.state = 'LEGACY_REVOKED'
        BEGIN SELECT RAISE(ABORT,
            'a LEGACY_REVOKED migration binding can never transition again'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_migration_binding_events_no_update
        BEFORE UPDATE ON mgboost_migration_binding_events
        BEGIN SELECT RAISE(ABORT, 'migration binding events are immutable'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_migration_binding_events_no_delete
        BEFORE DELETE ON mgboost_migration_binding_events
        BEGIN SELECT RAISE(ABORT, 'migration binding events are immutable'); END
    """,
)

SCHEMA_CHECKSUM = hashlib.sha256(
    "\n".join(statement.strip() for statement in _SCHEMA_STATEMENTS).encode("utf-8")
).hexdigest()

NEW_RUNTIME_TABLES = (
    "mgboost_migration_bindings",
    "mgboost_migration_binding_events",
)


def _verify(connection: sqlite3.Connection) -> None:
    required = {
        "mgboost_migration_bindings": {
            "id", "operation_id", "account_id", "legacy_alias_id", "hwid_verifier",
            "slot_generation_id", "child_intent_id", "state", "revision",
            "idempotency_key_hash", "request_hash", "actor_ref", "attempts",
            "next_attempt_at", "lease_owner", "lease_expires_at", "row_version",
        },
        "mgboost_migration_binding_events": {
            "id", "migration_binding_id", "account_id", "attempt_no", "event_type",
            "from_state", "to_state", "safe_error_class", "reason", "created_at",
        },
    }
    for table, columns in required.items():
        actual = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        if columns - actual:
            raise RuntimeError(f"PH4-02 migration lifecycle table {table} is incompatible")
    objects = {row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE name LIKE 'trg_migration_bind%'"
    )}
    expected = {
        "trg_migration_bindings_identity_immutable", "trg_migration_bindings_no_delete",
        "trg_migration_bindings_terminal_immutable", "trg_migration_binding_events_no_update",
        "trg_migration_binding_events_no_delete",
    }
    if not expected.issubset(objects):
        raise RuntimeError("PH4-02 migration lifecycle schema objects incomplete")


def apply_migration_lifecycle_schema(connection: sqlite3.Connection, *, now: int | None = None) -> bool:
    connection.execute("PRAGMA foreign_keys=ON")
    timestamp = int(time.time()) if now is None else int(now)
    try:
        connection.execute("BEGIN IMMEDIATE")
        for dep_id, dep_checksum, label in (
            (ACCOUNT_MIGRATION_ID, ACCOUNT_SCHEMA_CHECKSUM, "PH3-01"),
            (DEVICE_SLOT_MIGRATION_ID, DEVICE_SLOT_SCHEMA_CHECKSUM, "PH3-02"),
            (CHILD_PROVISIONING_MIGRATION_ID, CHILD_PROVISIONING_SCHEMA_CHECKSUM, "PH3-03"),
            (LEGACY_BRIDGE_MIGRATION_ID, LEGACY_BRIDGE_SCHEMA_CHECKSUM, "PH4-01"),
        ):
            parent = connection.execute(
                "SELECT schema_checksum FROM mgboost_schema_migrations WHERE migration_id=?",
                (dep_id,),
            ).fetchone()
            if not parent or parent[0] != dep_checksum:
                raise RuntimeError(f"PH4-02 migration lifecycle schema requires the exact {label} schema")
        existing = connection.execute(
            "SELECT schema_checksum FROM mgboost_schema_migrations WHERE migration_id=?",
            (MIGRATION_ID,),
        ).fetchone()
        if existing:
            if existing[0] != SCHEMA_CHECKSUM:
                raise RuntimeError("PH4-02 migration lifecycle schema checksum mismatch")
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
