"""Additive PH3-09 account/payment/mutation provenance foundation."""

from __future__ import annotations

import hashlib
import sqlite3
import time

from .account_schema import MIGRATION_ID as PARENT_MIGRATION_ID
from .account_schema import SCHEMA_CHECKSUM as PARENT_SCHEMA_CHECKSUM


MIGRATION_ID = "ph3_09_provenance_v1"

_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS mgboost_payment_records (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        public_id TEXT NOT NULL UNIQUE,
        account_id INTEGER NOT NULL,
        payment_channel TEXT NOT NULL
            CHECK(payment_channel IN (
                'TELEGRAM_STARS','EXTERNAL_PAYMENT','ADMIN_GRANT','UNKNOWN_LEGACY'
            )),
        record_status TEXT NOT NULL
            CHECK(record_status IN ('CONFIRMED','ADMIN_GRANTED','UNKNOWN_LEGACY')),
        amount_minor INTEGER CHECK(amount_minor IS NULL OR amount_minor >= 0),
        currency TEXT,
        payment_method TEXT,
        external_reference TEXT,
        actor_type TEXT NOT NULL,
        actor_ref TEXT,
        evidence_json TEXT NOT NULL,
        idempotency_key_hash TEXT NOT NULL UNIQUE,
        request_hash TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        UNIQUE(id, account_id),
        UNIQUE(payment_channel, external_reference),
        CHECK(
            (amount_minor IS NULL AND currency IS NULL)
            OR (amount_minor IS NOT NULL AND length(currency) BETWEEN 2 AND 16)
        ),
        FOREIGN KEY(account_id) REFERENCES mgboost_accounts(id) ON DELETE RESTRICT
    )
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mgboost_payment_records_no_update
        BEFORE UPDATE ON mgboost_payment_records
        BEGIN SELECT RAISE(ABORT, 'payment records are immutable'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mgboost_payment_records_no_delete
        BEFORE DELETE ON mgboost_payment_records
        BEGIN SELECT RAISE(ABORT, 'payment records are immutable'); END
    """,
    """
    CREATE TABLE IF NOT EXISTS mgboost_mutation_payment_links (
        mutation_id INTEGER NOT NULL,
        payment_id INTEGER NOT NULL,
        account_id INTEGER NOT NULL,
        created_at INTEGER NOT NULL,
        PRIMARY KEY(mutation_id, payment_id),
        FOREIGN KEY(mutation_id, account_id)
            REFERENCES mgboost_entitlement_mutations(id, account_id) ON DELETE RESTRICT,
        FOREIGN KEY(payment_id, account_id)
            REFERENCES mgboost_payment_records(id, account_id) ON DELETE RESTRICT
    )
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mgboost_mutation_payment_links_no_update
        BEFORE UPDATE ON mgboost_mutation_payment_links
        BEGIN SELECT RAISE(ABORT, 'mutation payment links are immutable'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mgboost_mutation_payment_links_no_delete
        BEFORE DELETE ON mgboost_mutation_payment_links
        BEGIN SELECT RAISE(ABORT, 'mutation payment links are immutable'); END
    """,
)

SCHEMA_CHECKSUM = hashlib.sha256(
    "\n".join(statement.strip() for statement in _SCHEMA_STATEMENTS).encode("utf-8")
).hexdigest()

NEW_RUNTIME_TABLES = ("mgboost_payment_records", "mgboost_mutation_payment_links")


def _verify(connection: sqlite3.Connection) -> None:
    required = {
        "mgboost_payment_records": {
            "id", "public_id", "account_id", "payment_channel", "record_status",
            "amount_minor", "currency", "payment_method", "external_reference",
            "actor_type", "actor_ref", "evidence_json", "idempotency_key_hash",
            "request_hash", "created_at",
        },
        "mgboost_mutation_payment_links": {
            "mutation_id", "payment_id", "account_id", "created_at",
        },
    }
    for table, columns in required.items():
        actual = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        if columns - actual:
            raise RuntimeError(f"PH3-09 incompatible table {table}: missing columns")
    objects = {row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE name LIKE 'trg_mgboost_payment_%' "
        "OR name LIKE 'trg_mgboost_mutation_payment_%'"
    )}
    expected = {
        "trg_mgboost_payment_records_no_update",
        "trg_mgboost_payment_records_no_delete",
        "trg_mgboost_mutation_payment_links_no_update",
        "trg_mgboost_mutation_payment_links_no_delete",
    }
    if not expected.issubset(objects):
        raise RuntimeError("PH3-09 schema triggers incomplete")


def apply_provenance_schema(
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
            raise RuntimeError("PH3-09 requires exact PH3-01 schema")
        row = connection.execute(
            "SELECT schema_checksum FROM mgboost_schema_migrations WHERE migration_id=?",
            (MIGRATION_ID,),
        ).fetchone()
        if row:
            if row[0] != SCHEMA_CHECKSUM:
                raise RuntimeError("PH3-09 schema checksum mismatch")
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
