"""Additive dormant PH2-01 opaque subscription credential schema.

Nothing in the legacy `/sub/{legacy_token}` path reads or writes this table.
Full contract: docs/PHASE2_OPAQUE_TOKEN_DESIGN.md.
"""

from __future__ import annotations

import hashlib
import sqlite3
import time

from .account_schema import MIGRATION_ID as ACCOUNT_MIGRATION_ID
from .account_schema import SCHEMA_CHECKSUM as ACCOUNT_SCHEMA_CHECKSUM


MIGRATION_ID = "ph2_01_subscription_credential_v1"

_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS mgboost_subscription_credentials (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        account_id INTEGER NOT NULL,
        token_hash TEXT NOT NULL UNIQUE
            CHECK(length(token_hash)=64 AND token_hash NOT GLOB '*[^0-9a-f]*'),
        version INTEGER NOT NULL DEFAULT 1 CHECK(version > 0),
        generation INTEGER NOT NULL CHECK(generation > 0),
        purpose TEXT NOT NULL DEFAULT 'EXTERNAL_SUBSCRIPTION'
            CHECK(purpose = 'EXTERNAL_SUBSCRIPTION'),
        status TEXT NOT NULL
            CHECK(status IN ('PENDING_DELIVERY','ACTIVE','REVOKED','EXPIRED')),
        revoke_reason TEXT
            CHECK(revoke_reason IS NULL OR revoke_reason IN (
                'ROTATED','COMPROMISE_SUSPECTED','ADMIN_MANUAL','ABANDONED_PENDING'
            )),
        rotated_from_id INTEGER,
        idempotency_key_hash TEXT NOT NULL UNIQUE
            CHECK(length(idempotency_key_hash)=64
                  AND idempotency_key_hash NOT GLOB '*[^0-9a-f]*'),
        created_at INTEGER NOT NULL,
        activated_at INTEGER,
        revoked_at INTEGER,
        last_used_at INTEGER,
        row_version INTEGER NOT NULL DEFAULT 1 CHECK(row_version > 0),
        UNIQUE(account_id, generation),
        UNIQUE(id, account_id),
        CHECK(
            (status='PENDING_DELIVERY' AND activated_at IS NULL AND revoked_at IS NULL
                AND revoke_reason IS NULL)
            OR (status='ACTIVE' AND activated_at IS NOT NULL AND revoked_at IS NULL
                AND revoke_reason IS NULL)
            OR (status IN ('REVOKED','EXPIRED') AND revoked_at IS NOT NULL
                AND revoke_reason IS NOT NULL)
        ),
        FOREIGN KEY(account_id) REFERENCES mgboost_accounts(id) ON DELETE RESTRICT,
        FOREIGN KEY(rotated_from_id, account_id)
            REFERENCES mgboost_subscription_credentials(id, account_id) ON DELETE RESTRICT
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS ux_mgboost_subscription_credential_active
        ON mgboost_subscription_credentials(account_id)
        WHERE status='ACTIVE'
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_mgboost_subscription_credential_account_history
        ON mgboost_subscription_credentials(account_id, generation)
    """,
    """
    CREATE TABLE IF NOT EXISTS mgboost_subscription_credential_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        credential_id INTEGER NOT NULL,
        account_id INTEGER NOT NULL,
        event_type TEXT NOT NULL
            CHECK(event_type IN (
                'PREPARED','ACTIVATED','REVOKED','EXPIRED_PENDING'
            )),
        actor_ref TEXT NOT NULL,
        reason TEXT,
        idempotency_key_hash TEXT NOT NULL
            CHECK(length(idempotency_key_hash)=64
                  AND idempotency_key_hash NOT GLOB '*[^0-9a-f]*'),
        created_at INTEGER NOT NULL,
        FOREIGN KEY(credential_id, account_id)
            REFERENCES mgboost_subscription_credentials(id, account_id) ON DELETE RESTRICT
    )
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mgboost_subscription_credential_identity_immutable
        BEFORE UPDATE OF account_id,token_hash,version,generation,purpose,
                         rotated_from_id,idempotency_key_hash,created_at
        ON mgboost_subscription_credentials
        BEGIN SELECT RAISE(ABORT, 'subscription credential identity is immutable'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mgboost_subscription_credential_no_delete
        BEFORE DELETE ON mgboost_subscription_credentials
        BEGIN SELECT RAISE(ABORT, 'subscription credential history is immutable'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mgboost_subscription_credential_terminal_immutable
        BEFORE UPDATE OF status,activated_at,revoked_at,revoke_reason
        ON mgboost_subscription_credentials
        WHEN OLD.status IN ('REVOKED','EXPIRED')
        BEGIN SELECT RAISE(ABORT, 'a terminal subscription credential can never change state'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mgboost_subscription_credential_events_no_update
        BEFORE UPDATE ON mgboost_subscription_credential_events
        BEGIN SELECT RAISE(ABORT, 'subscription credential events are immutable'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mgboost_subscription_credential_events_no_delete
        BEFORE DELETE ON mgboost_subscription_credential_events
        BEGIN SELECT RAISE(ABORT, 'subscription credential events are immutable'); END
    """,
)

SCHEMA_CHECKSUM = hashlib.sha256(
    "\n".join(statement.strip() for statement in _SCHEMA_STATEMENTS).encode("utf-8")
).hexdigest()

NEW_RUNTIME_TABLES = (
    "mgboost_subscription_credentials",
    "mgboost_subscription_credential_events",
)


def _verify(connection: sqlite3.Connection) -> None:
    required = {
        "mgboost_subscription_credentials": {
            "id", "account_id", "token_hash", "version", "generation", "purpose",
            "status", "revoke_reason", "rotated_from_id", "idempotency_key_hash",
            "created_at", "activated_at", "revoked_at", "last_used_at", "row_version",
        },
        "mgboost_subscription_credential_events": {
            "id", "credential_id", "account_id", "event_type", "actor_ref", "reason",
            "idempotency_key_hash", "created_at",
        },
    }
    for table, columns in required.items():
        actual = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        if columns - actual:
            raise RuntimeError(f"PH2-01 subscription credential table {table} is incompatible")
    objects = {row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE name LIKE 'ux_mgboost_subscription_credential_%' "
        "OR name LIKE 'trg_mgboost_subscription_credential_%'"
    )}
    expected = {
        "ux_mgboost_subscription_credential_active",
        "trg_mgboost_subscription_credential_identity_immutable",
        "trg_mgboost_subscription_credential_no_delete",
        "trg_mgboost_subscription_credential_terminal_immutable",
        "trg_mgboost_subscription_credential_events_no_update",
        "trg_mgboost_subscription_credential_events_no_delete",
    }
    if not expected.issubset(objects):
        raise RuntimeError("PH2-01 subscription credential schema objects incomplete")


def apply_subscription_credential_schema(connection: sqlite3.Connection, *, now: int | None = None) -> bool:
    connection.execute("PRAGMA foreign_keys=ON")
    timestamp = int(time.time()) if now is None else int(now)
    try:
        connection.execute("BEGIN IMMEDIATE")
        parent = connection.execute(
            "SELECT schema_checksum FROM mgboost_schema_migrations WHERE migration_id=?",
            (ACCOUNT_MIGRATION_ID,),
        ).fetchone()
        if not parent or parent[0] != ACCOUNT_SCHEMA_CHECKSUM:
            raise RuntimeError("PH2-01 subscription credential schema requires the exact PH3-01 schema")
        existing = connection.execute(
            "SELECT schema_checksum FROM mgboost_schema_migrations WHERE migration_id=?",
            (MIGRATION_ID,),
        ).fetchone()
        if existing:
            if existing[0] != SCHEMA_CHECKSUM:
                raise RuntimeError("PH2-01 subscription credential schema checksum mismatch")
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
