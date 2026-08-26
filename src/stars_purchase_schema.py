"""PH5-05 additive evidence for canonical Telegram Stars plan purchases.

The legacy ``stars_invoices`` rows are deliberately retained as immutable-in-
meaning expire-only historical records.  New rows opt into this schema through
``invoice_kind='CANONICAL_PLAN'`` and carry their own product snapshot.  The
separate evidence/application tables make a paid Telegram charge, its single
entitlement mutation and the later child-sync hand-off independently durable.
"""

from __future__ import annotations

import hashlib
import sqlite3
import time

from .account_schema import MIGRATION_ID as ACCOUNT_MIGRATION_ID
from .account_schema import SCHEMA_CHECKSUM as ACCOUNT_SCHEMA_CHECKSUM
from .plan_catalog_schema import MIGRATION_ID as CATALOG_MIGRATION_ID
from .plan_catalog_schema import SCHEMA_CHECKSUM as CATALOG_SCHEMA_CHECKSUM
from .parent_sync_schema import MIGRATION_ID as SYNC_MIGRATION_ID
from .parent_sync_schema import SCHEMA_CHECKSUM as SYNC_SCHEMA_CHECKSUM


MIGRATION_ID = "ph5_05_stars_purchase_v1"

_ALTER_COLUMNS = (
    ("invoice_kind", "TEXT NOT NULL DEFAULT 'LEGACY_EXPIRE'"),
    ("account_id", "INTEGER"),
    ("plan_version_id", "INTEGER"),
    ("duration_id", "INTEGER"),
    ("catalog_version_id", "INTEGER"),
    ("price_id", "INTEGER"),
    ("plan_code_snapshot", "TEXT"),
    ("plan_version_snapshot", "INTEGER"),
    ("catalog_version_snapshot", "TEXT"),
    ("price_amount_snapshot", "INTEGER"),
    ("canonical_applied_at", "INTEGER"),
    ("entitlement_mutation_id", "INTEGER"),
)

_ENTITLEMENT_STATE_ALTER_COLUMNS = (
    ("desired_expire", "INTEGER"),
)

_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS mgboost_stars_payment_evidence (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        invoice_id INTEGER NOT NULL UNIQUE,
        account_id INTEGER NOT NULL,
        telegram_payment_charge_id TEXT NOT NULL UNIQUE,
        provider_payment_charge_id TEXT,
        payer_telegram_id INTEGER NOT NULL,
        currency TEXT NOT NULL CHECK(currency='XTR'),
        amount INTEGER NOT NULL CHECK(amount > 0),
        invoice_snapshot_json TEXT NOT NULL,
        captured_at INTEGER NOT NULL,
        FOREIGN KEY(invoice_id) REFERENCES stars_invoices(id) ON DELETE RESTRICT,
        FOREIGN KEY(account_id) REFERENCES mgboost_accounts(id) ON DELETE RESTRICT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS mgboost_stars_purchase_applications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        invoice_id INTEGER NOT NULL UNIQUE,
        account_id INTEGER NOT NULL,
        entitlement_mutation_id INTEGER NOT NULL UNIQUE,
        applied_operation TEXT NOT NULL CHECK(applied_operation IN ('CREATE','RENEW')),
        applied_expiry INTEGER NOT NULL,
        entitlement_snapshot_json TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        FOREIGN KEY(invoice_id) REFERENCES stars_invoices(id) ON DELETE RESTRICT,
        FOREIGN KEY(account_id) REFERENCES mgboost_accounts(id) ON DELETE RESTRICT,
        FOREIGN KEY(entitlement_mutation_id, account_id)
            REFERENCES mgboost_entitlement_mutations(id, account_id) ON DELETE RESTRICT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS mgboost_stars_purchase_sync_jobs (
        invoice_id INTEGER PRIMARY KEY,
        account_id INTEGER NOT NULL,
        entitlement_mutation_id INTEGER NOT NULL,
        state TEXT NOT NULL DEFAULT 'PENDING' CHECK(state IN ('PENDING','SYNCED','MANUAL_REVIEW')),
        attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
        last_error_class TEXT,
        last_attempt_at INTEGER,
        synced_at INTEGER,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        FOREIGN KEY(invoice_id) REFERENCES stars_invoices(id) ON DELETE RESTRICT,
        FOREIGN KEY(account_id) REFERENCES mgboost_accounts(id) ON DELETE RESTRICT,
        FOREIGN KEY(entitlement_mutation_id, account_id)
            REFERENCES mgboost_entitlement_mutations(id, account_id) ON DELETE RESTRICT
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_mgboost_stars_purchase_sync_ready
        ON mgboost_stars_purchase_sync_jobs(state, updated_at, invoice_id)
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_stars_canonical_product_snapshot_immutable
        BEFORE UPDATE OF invoice_kind,account_id,plan_version_id,duration_id,
                         catalog_version_id,price_id,plan_code_snapshot,
                         plan_version_snapshot,catalog_version_snapshot,price_amount_snapshot
        ON stars_invoices
        WHEN OLD.invoice_kind='CANONICAL_PLAN'
        BEGIN SELECT RAISE(ABORT, 'canonical Stars product snapshot is immutable'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_stars_legacy_invoice_kind_immutable
        BEFORE UPDATE OF invoice_kind ON stars_invoices
        WHEN OLD.invoice_kind='LEGACY_EXPIRE' AND NEW.invoice_kind!='LEGACY_EXPIRE'
        BEGIN SELECT RAISE(ABORT, 'legacy Stars invoice cannot be reinterpreted'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mgboost_stars_payment_evidence_no_update
        BEFORE UPDATE ON mgboost_stars_payment_evidence
        BEGIN SELECT RAISE(ABORT, 'Stars payment evidence is immutable'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mgboost_stars_payment_evidence_no_delete
        BEFORE DELETE ON mgboost_stars_payment_evidence
        BEGIN SELECT RAISE(ABORT, 'Stars payment evidence is never deleted'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mgboost_stars_purchase_application_no_update
        BEFORE UPDATE ON mgboost_stars_purchase_applications
        BEGIN SELECT RAISE(ABORT, 'Stars purchase application is immutable'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mgboost_stars_purchase_application_no_delete
        BEFORE DELETE ON mgboost_stars_purchase_applications
        BEGIN SELECT RAISE(ABORT, 'Stars purchase application is never deleted'); END
    """,
)

SCHEMA_CHECKSUM = hashlib.sha256(
    ("\n".join(f"ALTER stars_invoices {name} {typ}" for name, typ in _ALTER_COLUMNS) + "\n" +
     "\n".join(f"ALTER mgboost_entitlement_state {name} {typ}" for name, typ in _ENTITLEMENT_STATE_ALTER_COLUMNS) + "\n" +
     "\n".join(statement.strip() for statement in _SCHEMA_STATEMENTS)).encode("utf-8")
).hexdigest()


def _verify(connection: sqlite3.Connection) -> None:
    invoice_columns = {row[1] for row in connection.execute("PRAGMA table_info(stars_invoices)")}
    missing = {name for name, _ in _ALTER_COLUMNS} - invoice_columns
    if missing:
        raise RuntimeError(f"PH5-05 stars invoice columns missing: {sorted(missing)}")
    state_columns = {row[1] for row in connection.execute("PRAGMA table_info(mgboost_entitlement_state)")}
    if "desired_expire" not in state_columns:
        raise RuntimeError("PH5-05 entitlement state expiry column missing")
    required = {
        "mgboost_stars_payment_evidence": {"invoice_id", "account_id", "telegram_payment_charge_id", "invoice_snapshot_json"},
        "mgboost_stars_purchase_applications": {"invoice_id", "account_id", "entitlement_mutation_id", "applied_operation", "entitlement_snapshot_json"},
        "mgboost_stars_purchase_sync_jobs": {"invoice_id", "account_id", "entitlement_mutation_id", "state"},
    }
    for table, fields in required.items():
        actual = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        if fields - actual:
            raise RuntimeError(f"PH5-05 incompatible table {table}")
    triggers = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
    required_triggers = {
        "trg_stars_canonical_product_snapshot_immutable",
        "trg_stars_legacy_invoice_kind_immutable",
        "trg_mgboost_stars_payment_evidence_no_update",
        "trg_mgboost_stars_payment_evidence_no_delete",
        "trg_mgboost_stars_purchase_application_no_update",
        "trg_mgboost_stars_purchase_application_no_delete",
    }
    if required_triggers - triggers:
        raise RuntimeError(f"PH5-05 immutable evidence triggers missing: {sorted(required_triggers - triggers)}")


def apply_stars_purchase_schema(connection: sqlite3.Connection, *, now: int | None = None) -> bool:
    connection.execute("PRAGMA foreign_keys=ON")
    timestamp = int(time.time()) if now is None else int(now)
    try:
        connection.execute("BEGIN IMMEDIATE")
        for migration_id, checksum, label in (
            (ACCOUNT_MIGRATION_ID, ACCOUNT_SCHEMA_CHECKSUM, "PH3-01"),
            (CATALOG_MIGRATION_ID, CATALOG_SCHEMA_CHECKSUM, "PH5-01"),
            (SYNC_MIGRATION_ID, SYNC_SCHEMA_CHECKSUM, "PH3-08"),
        ):
            row = connection.execute(
                "SELECT schema_checksum FROM mgboost_schema_migrations WHERE migration_id=?", (migration_id,)
            ).fetchone()
            if not row or row[0] != checksum:
                raise RuntimeError(f"PH5-05 requires exact {label} schema")
        row = connection.execute(
            "SELECT schema_checksum FROM mgboost_schema_migrations WHERE migration_id=?", (MIGRATION_ID,)
        ).fetchone()
        if row:
            if row[0] != SCHEMA_CHECKSUM:
                raise RuntimeError("PH5-05 schema checksum mismatch")
            _verify(connection)
            connection.commit()
            return False
        existing = {item[1] for item in connection.execute("PRAGMA table_info(stars_invoices)")}
        for name, column_type in _ALTER_COLUMNS:
            if name not in existing:
                connection.execute(f"ALTER TABLE stars_invoices ADD COLUMN {name} {column_type}")
        state_existing = {item[1] for item in connection.execute("PRAGMA table_info(mgboost_entitlement_state)")}
        for name, column_type in _ENTITLEMENT_STATE_ALTER_COLUMNS:
            if name not in state_existing:
                connection.execute(f"ALTER TABLE mgboost_entitlement_state ADD COLUMN {name} {column_type}")
        connection.execute(
            "UPDATE mgboost_entitlement_state SET desired_expire=("
            "SELECT current_expiry FROM mgboost_subscriptions s WHERE s.id=mgboost_entitlement_state.subscription_id) "
            "WHERE desired_expire IS NULL"
        )
        for statement in _SCHEMA_STATEMENTS:
            connection.execute(statement)
        _verify(connection)
        connection.execute(
            "INSERT INTO mgboost_schema_migrations (migration_id,schema_checksum,applied_at) VALUES (?,?,?)",
            (MIGRATION_ID, SCHEMA_CHECKSUM, timestamp),
        )
        connection.commit()
        return True
    except Exception:
        connection.rollback()
        raise
