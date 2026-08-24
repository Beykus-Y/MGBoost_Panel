"""Additive PH3-06 reviewed internal-entitlement foundation."""

from __future__ import annotations

import hashlib
import sqlite3
import time

from .account_schema import MIGRATION_ID as PARENT_MIGRATION_ID
from .account_schema import SCHEMA_CHECKSUM as PARENT_SCHEMA_CHECKSUM


MIGRATION_ID = "ph3_06_internal_entitlements_v1"

_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS mgboost_internal_account_reviews (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        account_id INTEGER NOT NULL UNIQUE,
        legacy_username TEXT NOT NULL UNIQUE,
        ownership_evidence TEXT NOT NULL
            CHECK(ownership_evidence IN ('PROVEN','ABSENT')),
        legacy_status TEXT NOT NULL
            CHECK(legacy_status IN ('ACTIVE','DISABLED','EXPIRED','UNLIMITED')),
        legacy_expiry INTEGER,
        device_evidence_count INTEGER NOT NULL CHECK(device_evidence_count >= 0),
        hwid_evidence_count INTEGER NOT NULL CHECK(hwid_evidence_count >= 0),
        internal_reason TEXT NOT NULL CHECK(length(trim(internal_reason)) BETWEEN 8 AND 1000),
        migration_confidence TEXT NOT NULL
            CHECK(migration_confidence IN ('HIGH','MEDIUM','LOW')),
        proposed_plan_version_id INTEGER NOT NULL,
        reviewed_by_actor TEXT NOT NULL,
        mutation_id INTEGER NOT NULL,
        evidence_json TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        UNIQUE(id, account_id),
        FOREIGN KEY(account_id) REFERENCES mgboost_accounts(id) ON DELETE RESTRICT,
        FOREIGN KEY(proposed_plan_version_id) REFERENCES mgboost_plan_versions(id)
            ON DELETE RESTRICT,
        FOREIGN KEY(mutation_id, account_id)
            REFERENCES mgboost_entitlement_mutations(id, account_id) ON DELETE RESTRICT
    )
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mgboost_internal_reviews_validate
        BEFORE INSERT ON mgboost_internal_account_reviews
        WHEN NOT EXISTS (
            SELECT 1 FROM mgboost_accounts AS a
            JOIN mgboost_plan_versions AS p ON p.id=NEW.proposed_plan_version_id
            WHERE a.id=NEW.account_id AND a.account_source='INTERNAL'
              AND p.plan_kind='INTERNAL' AND p.billing_required=0
        )
        BEGIN SELECT RAISE(ABORT, 'review requires internal account and plan'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mgboost_internal_reviews_no_update
        BEFORE UPDATE ON mgboost_internal_account_reviews
        BEGIN SELECT RAISE(ABORT, 'internal account reviews are immutable'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mgboost_internal_reviews_no_delete
        BEFORE DELETE ON mgboost_internal_account_reviews
        BEGIN SELECT RAISE(ABORT, 'internal account reviews are immutable'); END
    """,
    """
    CREATE TABLE IF NOT EXISTS mgboost_internal_entitlement_revisions (
        account_id INTEGER PRIMARY KEY,
        revision INTEGER NOT NULL CHECK(revision > 0),
        updated_at INTEGER NOT NULL,
        FOREIGN KEY(account_id) REFERENCES mgboost_accounts(id) ON DELETE RESTRICT
    )
    """,
)

SCHEMA_CHECKSUM = hashlib.sha256(
    "\n".join(statement.strip() for statement in _SCHEMA_STATEMENTS).encode("utf-8")
).hexdigest()

NEW_RUNTIME_TABLES = (
    "mgboost_internal_account_reviews",
    "mgboost_internal_entitlement_revisions",
)


def _verify(connection: sqlite3.Connection) -> None:
    required = {
        "mgboost_internal_account_reviews": {
            "id", "account_id", "legacy_username", "ownership_evidence",
            "legacy_status", "legacy_expiry", "device_evidence_count",
            "hwid_evidence_count", "internal_reason", "migration_confidence",
            "proposed_plan_version_id", "reviewed_by_actor", "mutation_id",
            "evidence_json", "created_at",
        },
        "mgboost_internal_entitlement_revisions": {
            "account_id", "revision", "updated_at",
        },
    }
    for table, columns in required.items():
        actual = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        if columns - actual:
            raise RuntimeError(f"PH3-06 incompatible table {table}: missing columns")
    objects = {row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE name LIKE 'trg_mgboost_internal_%'"
    )}
    expected = {
        "trg_mgboost_internal_reviews_validate",
        "trg_mgboost_internal_reviews_no_update",
        "trg_mgboost_internal_reviews_no_delete",
    }
    if not expected.issubset(objects):
        raise RuntimeError("PH3-06 schema triggers incomplete")


def apply_internal_entitlement_schema(
    connection: sqlite3.Connection, *, now: int | None = None
) -> bool:
    connection.execute("PRAGMA foreign_keys=ON")
    timestamp = int(time.time()) if now is None else int(now)
    try:
        connection.execute("BEGIN IMMEDIATE")
        parent = connection.execute(
            "SELECT schema_checksum FROM mgboost_schema_migrations WHERE migration_id=?",
            (PARENT_MIGRATION_ID,),
        ).fetchone()
        if not parent or parent[0] != PARENT_SCHEMA_CHECKSUM:
            raise RuntimeError("PH3-06 requires exact PH3-01 schema")
        row = connection.execute(
            "SELECT schema_checksum FROM mgboost_schema_migrations WHERE migration_id=?",
            (MIGRATION_ID,),
        ).fetchone()
        if row:
            if row[0] != SCHEMA_CHECKSUM:
                raise RuntimeError("PH3-06 schema checksum mismatch")
            _verify(connection)
            connection.commit()
            return False
        for statement in _SCHEMA_STATEMENTS:
            connection.execute(statement)
        _verify(connection)
        connection.execute(
            "INSERT INTO mgboost_schema_migrations "
            "(migration_id,schema_checksum,applied_at) VALUES (?,?,?)",
            (MIGRATION_ID, SCHEMA_CHECKSUM, timestamp),
        )
        connection.commit()
        return True
    except Exception:
        connection.rollback()
        raise
