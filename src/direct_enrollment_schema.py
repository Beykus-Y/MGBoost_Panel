"""Additive PH4-03 reviewed DIRECT account enrollment foundation.

Mirrors PH3-06's `mgboost_internal_account_reviews` pattern exactly, but for
`account_source='DIRECT'` instead of `'INTERNAL'` -- this module never
reuses/relaxes the INTERNAL-only review table or its trigger. It reuses the
already-generic `mgboost_legacy_alias_groups`/`mgboost_legacy_account_aliases`
tables (PH3-03), which were never INTERNAL-specific.

`mgboost_direct_enrollment_intents` is the crash-safe anchor: it is written
BEFORE `AccountStore.create_account()` is ever called, so a retry after a
crash between "account created" and "alias/review written" reuses the same
account id instead of allocating an orphan second one.
"""

from __future__ import annotations

import hashlib
import sqlite3
import time

from .child_provisioning_schema import MIGRATION_ID as CHILD_MIGRATION_ID
from .child_provisioning_schema import SCHEMA_CHECKSUM as CHILD_SCHEMA_CHECKSUM


MIGRATION_ID = "ph4_03_direct_enrollment_v1"

_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS mgboost_direct_enrollment_intents (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        idempotency_key_hash TEXT NOT NULL UNIQUE,
        legacy_username TEXT NOT NULL,
        request_hash TEXT NOT NULL,
        account_id INTEGER,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        FOREIGN KEY(account_id) REFERENCES mgboost_accounts(id) ON DELETE RESTRICT
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_mgboost_direct_intent_username
        ON mgboost_direct_enrollment_intents(legacy_username)
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mgboost_direct_intent_identity_immutable
        BEFORE UPDATE OF idempotency_key_hash,legacy_username,request_hash,created_at
        ON mgboost_direct_enrollment_intents
        BEGIN SELECT RAISE(ABORT, 'direct enrollment intent identity is immutable'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mgboost_direct_intent_account_fill_once
        BEFORE UPDATE OF account_id ON mgboost_direct_enrollment_intents
        WHEN OLD.account_id IS NOT NULL
        BEGIN SELECT RAISE(ABORT, 'direct enrollment intent account_id is fill-once'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mgboost_direct_intent_no_delete
        BEFORE DELETE ON mgboost_direct_enrollment_intents
        BEGIN SELECT RAISE(ABORT, 'direct enrollment intent history is immutable'); END
    """,
    """
    CREATE TABLE IF NOT EXISTS mgboost_direct_account_reviews (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        account_id INTEGER NOT NULL UNIQUE,
        legacy_username TEXT NOT NULL UNIQUE,
        ownership_evidence TEXT NOT NULL
            CHECK(ownership_evidence IN ('PROVEN','ABSENT')),
        decision_ref TEXT NOT NULL CHECK(length(decision_ref) BETWEEN 3 AND 128),
        reviewed_by_actor TEXT NOT NULL,
        evidence_json TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        UNIQUE(id, account_id),
        FOREIGN KEY(account_id) REFERENCES mgboost_accounts(id) ON DELETE RESTRICT
    )
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mgboost_direct_reviews_validate
        BEFORE INSERT ON mgboost_direct_account_reviews
        WHEN NOT EXISTS (
            SELECT 1 FROM mgboost_accounts AS a
            WHERE a.id=NEW.account_id AND a.account_source='DIRECT'
        )
        BEGIN SELECT RAISE(ABORT, 'review requires a DIRECT account'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mgboost_direct_reviews_no_update
        BEFORE UPDATE ON mgboost_direct_account_reviews
        BEGIN SELECT RAISE(ABORT, 'direct account reviews are immutable'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mgboost_direct_reviews_no_delete
        BEFORE DELETE ON mgboost_direct_account_reviews
        BEGIN SELECT RAISE(ABORT, 'direct account reviews are immutable'); END
    """,
)

SCHEMA_CHECKSUM = hashlib.sha256(
    "\n".join(statement.strip() for statement in _SCHEMA_STATEMENTS).encode("utf-8")
).hexdigest()

NEW_RUNTIME_TABLES = (
    "mgboost_direct_enrollment_intents",
    "mgboost_direct_account_reviews",
)


def _verify(connection: sqlite3.Connection) -> None:
    required = {
        "mgboost_direct_enrollment_intents": {
            "id", "idempotency_key_hash", "legacy_username", "request_hash",
            "account_id", "created_at", "updated_at",
        },
        "mgboost_direct_account_reviews": {
            "id", "account_id", "legacy_username", "ownership_evidence",
            "decision_ref", "reviewed_by_actor", "evidence_json", "created_at",
        },
    }
    for table, columns in required.items():
        actual = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        if columns - actual:
            raise RuntimeError(f"PH4-03 direct enrollment table {table} is incompatible")
    objects = {row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE name LIKE 'trg_mgboost_direct_%'"
    )}
    expected = {
        "trg_mgboost_direct_intent_identity_immutable",
        "trg_mgboost_direct_intent_account_fill_once",
        "trg_mgboost_direct_intent_no_delete",
        "trg_mgboost_direct_reviews_validate",
        "trg_mgboost_direct_reviews_no_update",
        "trg_mgboost_direct_reviews_no_delete",
    }
    if not expected.issubset(objects):
        raise RuntimeError("PH4-03 direct enrollment schema objects incomplete")


def apply_direct_enrollment_schema(connection: sqlite3.Connection, *, now: int | None = None) -> bool:
    connection.execute("PRAGMA foreign_keys=ON")
    timestamp = int(time.time()) if now is None else int(now)
    try:
        connection.execute("BEGIN IMMEDIATE")
        parent = connection.execute(
            "SELECT schema_checksum FROM mgboost_schema_migrations WHERE migration_id=?",
            (CHILD_MIGRATION_ID,),
        ).fetchone()
        if not parent or parent[0] != CHILD_SCHEMA_CHECKSUM:
            raise RuntimeError("PH4-03 direct enrollment schema requires the exact PH3-03 schema")
        existing = connection.execute(
            "SELECT schema_checksum FROM mgboost_schema_migrations WHERE migration_id=?",
            (MIGRATION_ID,),
        ).fetchone()
        if existing:
            if existing[0] != SCHEMA_CHECKSUM:
                raise RuntimeError("PH4-03 direct enrollment schema checksum mismatch")
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
