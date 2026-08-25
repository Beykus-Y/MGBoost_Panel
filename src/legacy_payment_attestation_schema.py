"""Additive PH4-03 owner-attested historical legacy payment schema.

Owner decision (2026-08-26): every real paying legacy MGBoost user paid the
owner directly, never via Telegram Stars, and no canonical payment ledger
existed at the time. `mgboost_payment_records` (PH3-09) cannot represent
this: its `record_status` CHECK constraint and `external_reference`
requirement are part of an already-deployed, checksum-locked migration and
must never be edited in place (would break `apply_provenance_schema` on
every other environment that already applied it).

This sibling additive table instead canonically records the *fact* that a
reviewed DIRECT account's legacy subscription was paid through
`EXTERNAL_PAYMENT` historically, attested by the primary admin/owner, with
no invented amount/date/reference -- deliberately distinct from a real new
`EXTERNAL_PAYMENT` with known details (`mgboost_payment_records`,
`record_status='CONFIRMED'`, via `DirectEnrollmentStore.record_external_payment`).
At most one such attestation exists per account (`UNIQUE(account_id)`), and
it may only be attested for an account that already has a
`mgboost_direct_account_reviews` row (never for INTERNAL/unreviewed
accounts).
"""

from __future__ import annotations

import hashlib
import sqlite3
import time

from .direct_enrollment_schema import MIGRATION_ID as DIRECT_MIGRATION_ID
from .direct_enrollment_schema import SCHEMA_CHECKSUM as DIRECT_SCHEMA_CHECKSUM


MIGRATION_ID = "ph4_03_legacy_payment_attestation_v1"

_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS mgboost_owner_attested_legacy_payments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        account_id INTEGER NOT NULL UNIQUE,
        payment_channel TEXT NOT NULL CHECK(payment_channel='EXTERNAL_PAYMENT'),
        decision_ref TEXT NOT NULL CHECK(length(decision_ref) BETWEEN 3 AND 128),
        attestation_note TEXT NOT NULL
            CHECK(length(trim(attestation_note)) BETWEEN 8 AND 1000),
        attested_by_actor TEXT NOT NULL,
        evidence_json TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        FOREIGN KEY(account_id) REFERENCES mgboost_accounts(id) ON DELETE RESTRICT
    )
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mgboost_owner_attested_legacy_validate
        BEFORE INSERT ON mgboost_owner_attested_legacy_payments
        WHEN NOT EXISTS (
            SELECT 1 FROM mgboost_direct_account_reviews WHERE account_id=NEW.account_id
        )
        BEGIN SELECT RAISE(ABORT, 'owner attestation requires an already-reviewed DIRECT account'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mgboost_owner_attested_legacy_no_update
        BEFORE UPDATE ON mgboost_owner_attested_legacy_payments
        BEGIN SELECT RAISE(ABORT, 'owner-attested legacy payments are immutable'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mgboost_owner_attested_legacy_no_delete
        BEFORE DELETE ON mgboost_owner_attested_legacy_payments
        BEGIN SELECT RAISE(ABORT, 'owner-attested legacy payments are immutable'); END
    """,
)

SCHEMA_CHECKSUM = hashlib.sha256(
    "\n".join(statement.strip() for statement in _SCHEMA_STATEMENTS).encode("utf-8")
).hexdigest()

NEW_RUNTIME_TABLES = ("mgboost_owner_attested_legacy_payments",)


def _verify(connection: sqlite3.Connection) -> None:
    required = {
        "mgboost_owner_attested_legacy_payments": {
            "id", "account_id", "payment_channel", "decision_ref",
            "attestation_note", "attested_by_actor", "evidence_json", "created_at",
        },
    }
    for table, columns in required.items():
        actual = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        if columns - actual:
            raise RuntimeError(f"PH4-03 legacy payment attestation table {table} is incompatible")
    objects = {row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE name LIKE 'trg_mgboost_owner_attested_legacy_%'"
    )}
    expected = {
        "trg_mgboost_owner_attested_legacy_validate",
        "trg_mgboost_owner_attested_legacy_no_update",
        "trg_mgboost_owner_attested_legacy_no_delete",
    }
    if not expected.issubset(objects):
        raise RuntimeError("PH4-03 legacy payment attestation schema objects incomplete")


def apply_legacy_payment_attestation_schema(
    connection: sqlite3.Connection, *, now: int | None = None
) -> bool:
    connection.execute("PRAGMA foreign_keys=ON")
    timestamp = int(time.time()) if now is None else int(now)
    try:
        connection.execute("BEGIN IMMEDIATE")
        parent = connection.execute(
            "SELECT schema_checksum FROM mgboost_schema_migrations WHERE migration_id=?",
            (DIRECT_MIGRATION_ID,),
        ).fetchone()
        if not parent or parent[0] != DIRECT_SCHEMA_CHECKSUM:
            raise RuntimeError(
                "PH4-03 legacy payment attestation schema requires the exact "
                "PH4-03 direct-enrollment schema"
            )
        existing = connection.execute(
            "SELECT schema_checksum FROM mgboost_schema_migrations WHERE migration_id=?",
            (MIGRATION_ID,),
        ).fetchone()
        if existing:
            if existing[0] != SCHEMA_CHECKSUM:
                raise RuntimeError("PH4-03 legacy payment attestation schema checksum mismatch")
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
