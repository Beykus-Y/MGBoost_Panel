"""Additive dormant PH2-05 Telegram ownership rebind schema.

Reuses PH3-01's existing `mgboost_telegram_identities` table verbatim --
its unique partial indexes (`ux_mgboost_tg_active_identity`,
`ux_mgboost_account_active_owner`) already structurally guarantee "at most
one ACTIVE identity per Telegram ID" and "at most one ACTIVE owner per
account", so dual ownership is impossible at the schema level, not just by
convention. This module adds only the durable operation/audit record a
rebind needs on top of that.
"""

from __future__ import annotations

import hashlib
import sqlite3
import time

from .account_schema import MIGRATION_ID as ACCOUNT_MIGRATION_ID
from .account_schema import SCHEMA_CHECKSUM as ACCOUNT_SCHEMA_CHECKSUM
from .subscription_credential_schema import MIGRATION_ID as CREDENTIAL_MIGRATION_ID
from .subscription_credential_schema import SCHEMA_CHECKSUM as CREDENTIAL_SCHEMA_CHECKSUM


MIGRATION_ID = "ph2_05_ownership_rebind_v1"

_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS mgboost_ownership_rebind_operations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        operation_id TEXT NOT NULL UNIQUE
            CHECK(substr(operation_id,1,3)='rb_'
                  AND length(operation_id)=29
                  AND substr(operation_id,4) NOT GLOB '*[^a-z2-7]*'),
        account_id INTEGER NOT NULL,
        expected_old_telegram_id INTEGER NOT NULL CHECK(expected_old_telegram_id > 0),
        new_telegram_id INTEGER NOT NULL CHECK(new_telegram_id > 0),
        mode TEXT NOT NULL CHECK(mode IN ('ORDINARY','COMPROMISE')),
        reason TEXT NOT NULL CHECK(length(reason) BETWEEN 3 AND 300),
        state TEXT NOT NULL DEFAULT 'PENDING'
            CHECK(state IN ('PENDING','IN_FLIGHT','RETRY','APPLIED','ERROR')),
        old_identity_id INTEGER,
        new_identity_id INTEGER,
        old_credential_id INTEGER,
        new_credential_id INTEGER,
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
        UNIQUE(id, account_id),
        CHECK((state='IN_FLIGHT' AND lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)
              OR (state!='IN_FLIGHT' AND lease_owner IS NULL AND lease_expires_at IS NULL)),
        CHECK(mode='COMPROMISE' OR (old_credential_id IS NULL AND new_credential_id IS NULL)),
        FOREIGN KEY(account_id) REFERENCES mgboost_accounts(id) ON DELETE RESTRICT,
        FOREIGN KEY(old_identity_id) REFERENCES mgboost_telegram_identities(id) ON DELETE RESTRICT,
        FOREIGN KEY(new_identity_id) REFERENCES mgboost_telegram_identities(id) ON DELETE RESTRICT,
        FOREIGN KEY(old_credential_id, account_id)
            REFERENCES mgboost_subscription_credentials(id, account_id) ON DELETE RESTRICT,
        FOREIGN KEY(new_credential_id, account_id)
            REFERENCES mgboost_subscription_credentials(id, account_id) ON DELETE RESTRICT
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_mgboost_ownership_rebind_ready
        ON mgboost_ownership_rebind_operations(state, next_attempt_at, id)
    """,
    """
    CREATE TABLE IF NOT EXISTS mgboost_ownership_rebind_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        rebind_operation_id INTEGER NOT NULL,
        account_id INTEGER NOT NULL,
        attempt_no INTEGER NOT NULL CHECK(attempt_no > 0),
        event_type TEXT NOT NULL
            CHECK(event_type IN ('STARTED','IDENTITY_REBOUND','CREDENTIAL_ROTATED','SUCCEEDED','FAILED')),
        safe_error_class TEXT,
        created_at INTEGER NOT NULL,
        UNIQUE(rebind_operation_id, attempt_no, event_type),
        FOREIGN KEY(rebind_operation_id, account_id)
            REFERENCES mgboost_ownership_rebind_operations(id, account_id) ON DELETE RESTRICT
    )
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_ownership_rebind_identity_immutable
        BEFORE UPDATE OF operation_id,account_id,expected_old_telegram_id,new_telegram_id,
                         mode,reason,idempotency_key_hash,request_hash,actor_ref,created_at
        ON mgboost_ownership_rebind_operations
        BEGIN SELECT RAISE(ABORT, 'ownership rebind operation identity is immutable'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_ownership_rebind_no_delete
        BEFORE DELETE ON mgboost_ownership_rebind_operations
        BEGIN SELECT RAISE(ABORT, 'ownership rebind operation history is immutable'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_ownership_rebind_terminal_immutable
        BEFORE UPDATE OF state,old_identity_id,new_identity_id,old_credential_id,new_credential_id
        ON mgboost_ownership_rebind_operations
        WHEN OLD.state IN ('APPLIED','ERROR')
        BEGIN SELECT RAISE(ABORT, 'a terminal ownership rebind operation can never change state'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_ownership_rebind_events_no_update
        BEFORE UPDATE ON mgboost_ownership_rebind_events
        BEGIN SELECT RAISE(ABORT, 'ownership rebind events are immutable'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_ownership_rebind_events_no_delete
        BEFORE DELETE ON mgboost_ownership_rebind_events
        BEGIN SELECT RAISE(ABORT, 'ownership rebind events are immutable'); END
    """,
)

SCHEMA_CHECKSUM = hashlib.sha256(
    "\n".join(statement.strip() for statement in _SCHEMA_STATEMENTS).encode("utf-8")
).hexdigest()

NEW_RUNTIME_TABLES = (
    "mgboost_ownership_rebind_operations",
    "mgboost_ownership_rebind_events",
)


def _verify(connection: sqlite3.Connection) -> None:
    required = {
        "mgboost_ownership_rebind_operations": {
            "id", "operation_id", "account_id", "expected_old_telegram_id", "new_telegram_id",
            "mode", "reason", "state", "old_identity_id", "new_identity_id", "old_credential_id",
            "new_credential_id", "idempotency_key_hash", "request_hash", "actor_ref", "attempts",
            "next_attempt_at", "lease_owner", "lease_expires_at", "row_version",
        },
        "mgboost_ownership_rebind_events": {
            "id", "rebind_operation_id", "account_id", "attempt_no", "event_type",
            "safe_error_class", "created_at",
        },
    }
    for table, columns in required.items():
        actual = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        if columns - actual:
            raise RuntimeError(f"PH2-05 ownership rebind table {table} is incompatible")
    objects = {row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE name LIKE 'trg_ownership_rebind_%'"
    )}
    expected = {
        "trg_ownership_rebind_identity_immutable", "trg_ownership_rebind_no_delete",
        "trg_ownership_rebind_terminal_immutable", "trg_ownership_rebind_events_no_update",
        "trg_ownership_rebind_events_no_delete",
    }
    if not expected.issubset(objects):
        raise RuntimeError("PH2-05 ownership rebind schema objects incomplete")


def apply_ownership_rebind_schema(connection: sqlite3.Connection, *, now: int | None = None) -> bool:
    connection.execute("PRAGMA foreign_keys=ON")
    timestamp = int(time.time()) if now is None else int(now)
    try:
        connection.execute("BEGIN IMMEDIATE")
        for dep_id, dep_checksum, label in (
            (ACCOUNT_MIGRATION_ID, ACCOUNT_SCHEMA_CHECKSUM, "PH3-01"),
            (CREDENTIAL_MIGRATION_ID, CREDENTIAL_SCHEMA_CHECKSUM, "PH2-01"),
        ):
            parent = connection.execute(
                "SELECT schema_checksum FROM mgboost_schema_migrations WHERE migration_id=?",
                (dep_id,),
            ).fetchone()
            if not parent or parent[0] != dep_checksum:
                raise RuntimeError(f"PH2-05 ownership rebind schema requires the exact {label} schema")
        existing = connection.execute(
            "SELECT schema_checksum FROM mgboost_schema_migrations WHERE migration_id=?",
            (MIGRATION_ID,),
        ).fetchone()
        if existing:
            if existing[0] != SCHEMA_CHECKSUM:
                raise RuntimeError("PH2-05 ownership rebind schema checksum mismatch")
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
