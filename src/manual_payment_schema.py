"""PH5-09 additive manual external-payment lifecycle evidence.

One durable record per manually confirmed RUB payment (primary admin actor),
its append-only pending-edit audit trail, its single immutable entitlement
application link and its durable child-expiry sync hand-off.  The applied
record and its edit history are historical facts: SQLite triggers (not just
application discipline) refuse any later UPDATE of an APPLIED/CANCELLED
record and every UPDATE/DELETE of the audit/application rows.  Pending
correction before apply is the only sanctioned modification path (DL-039).
"""

from __future__ import annotations

import hashlib
import sqlite3
import time

from .account_schema import MIGRATION_ID as ACCOUNT_MIGRATION_ID
from .account_schema import SCHEMA_CHECKSUM as ACCOUNT_SCHEMA_CHECKSUM
from .plan_catalog_schema import MIGRATION_ID as CATALOG_MIGRATION_ID
from .plan_catalog_schema import SCHEMA_CHECKSUM as CATALOG_SCHEMA_CHECKSUM
from .provenance_schema import MIGRATION_ID as PROVENANCE_MIGRATION_ID
from .provenance_schema import SCHEMA_CHECKSUM as PROVENANCE_SCHEMA_CHECKSUM
from .wl_package_schema import MIGRATION_ID as PACKAGE_MIGRATION_ID
from .wl_package_schema import SCHEMA_CHECKSUM as PACKAGE_SCHEMA_CHECKSUM


MIGRATION_ID = "ph5_09_manual_payment_v1"

_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS mgboost_manual_payment_records (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        public_id TEXT NOT NULL UNIQUE,
        kind TEXT NOT NULL CHECK(kind IN ('PLAN_PRODUCT','WL_PACKAGE')),
        status TEXT NOT NULL DEFAULT 'PENDING'
            CHECK(status IN ('PENDING','APPLIED','CANCELLED','MANUAL_REVIEW')),
        account_id INTEGER NOT NULL,
        plan_version_id INTEGER,
        duration_id INTEGER,
        catalog_version_id INTEGER NOT NULL,
        catalog_version_snapshot TEXT NOT NULL,
        plan_price_id INTEGER,
        package_price_id INTEGER,
        package_product_id INTEGER,
        plan_code_snapshot TEXT,
        plan_version_snapshot INTEGER,
        duration_days_snapshot INTEGER,
        package_sku_snapshot TEXT,
        package_product_version_snapshot INTEGER,
        package_bytes_snapshot INTEGER,
        expected_amount_minor INTEGER NOT NULL CHECK(expected_amount_minor > 0),
        recorded_amount_minor INTEGER NOT NULL CHECK(recorded_amount_minor > 0),
        currency TEXT NOT NULL CHECK(currency='RUB'),
        payment_method TEXT NOT NULL,
        external_reference TEXT NOT NULL UNIQUE,
        comment TEXT,
        actor_type TEXT NOT NULL DEFAULT 'PRIMARY_ADMIN',
        actor_ref TEXT NOT NULL,
        idempotency_key_hash TEXT NOT NULL UNIQUE,
        request_hash TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        applied_at INTEGER,
        entitlement_mutation_id INTEGER,
        applied_operation TEXT,
        applied_expiry INTEGER,
        cancelled_at INTEGER,
        cancel_reason TEXT,
        review_reason TEXT,
        review_at INTEGER,
        CHECK(expected_amount_minor=recorded_amount_minor),
        CHECK((kind='PLAN_PRODUCT' AND plan_price_id IS NOT NULL AND package_price_id IS NULL
               AND package_product_id IS NULL AND plan_code_snapshot IS NOT NULL
               AND plan_version_snapshot IS NOT NULL AND duration_days_snapshot > 0
               AND package_sku_snapshot IS NULL)
           OR (kind='WL_PACKAGE' AND package_price_id IS NOT NULL AND plan_price_id IS NULL
               AND package_product_id IS NOT NULL AND package_sku_snapshot IS NOT NULL
               AND package_product_version_snapshot IS NOT NULL AND package_bytes_snapshot > 0
               AND plan_code_snapshot IS NULL AND duration_days_snapshot IS NULL
               AND plan_version_id IS NULL AND duration_id IS NULL)),
        FOREIGN KEY(account_id) REFERENCES mgboost_accounts(id) ON DELETE RESTRICT,
        FOREIGN KEY(plan_version_id) REFERENCES mgboost_plan_versions(id) ON DELETE RESTRICT,
        FOREIGN KEY(duration_id, plan_version_id)
            REFERENCES mgboost_plan_durations(id, plan_version_id) ON DELETE RESTRICT,
        FOREIGN KEY(catalog_version_id) REFERENCES mgboost_price_catalog_versions(id) ON DELETE RESTRICT,
        FOREIGN KEY(plan_price_id) REFERENCES mgboost_plan_prices(id) ON DELETE RESTRICT,
        FOREIGN KEY(package_price_id) REFERENCES mgboost_wl_package_prices(id) ON DELETE RESTRICT,
        FOREIGN KEY(package_product_id) REFERENCES mgboost_wl_package_products(id) ON DELETE RESTRICT
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_mgboost_manual_payment_records_status
        ON mgboost_manual_payment_records(status, created_at, id)
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_mgboost_manual_payment_records_account
        ON mgboost_manual_payment_records(account_id, created_at, id)
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mgboost_manual_payment_record_applied_immutable
        BEFORE UPDATE ON mgboost_manual_payment_records
        WHEN OLD.status IN ('APPLIED','CANCELLED')
        BEGIN SELECT RAISE(ABORT, 'applied or cancelled manual payment record is immutable'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mgboost_manual_payment_record_no_delete
        BEFORE DELETE ON mgboost_manual_payment_records
        BEGIN SELECT RAISE(ABORT, 'manual payment records are never deleted'); END
    """,
    """
    CREATE TABLE IF NOT EXISTS mgboost_manual_payment_edits (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        payment_record_id INTEGER NOT NULL,
        account_id INTEGER NOT NULL,
        edit_kind TEXT NOT NULL,       -- FIELD_EDIT | RESOLVE_REVIEW | CANCEL
        reason TEXT NOT NULL,
        before_json TEXT NOT NULL,
        after_json TEXT NOT NULL,
        actor_type TEXT NOT NULL,
        actor_ref TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        FOREIGN KEY(payment_record_id) REFERENCES mgboost_manual_payment_records(id) ON DELETE RESTRICT,
        FOREIGN KEY(account_id) REFERENCES mgboost_accounts(id) ON DELETE RESTRICT
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_mgboost_manual_payment_edits_record
        ON mgboost_manual_payment_edits(payment_record_id, created_at, id)
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mgboost_manual_payment_edit_no_update
        BEFORE UPDATE ON mgboost_manual_payment_edits
        BEGIN SELECT RAISE(ABORT, 'manual payment edit history is append-only'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mgboost_manual_payment_edit_no_delete
        BEFORE DELETE ON mgboost_manual_payment_edits
        BEGIN SELECT RAISE(ABORT, 'manual payment edit history is never deleted'); END
    """,
    """
    CREATE TABLE IF NOT EXISTS mgboost_manual_payment_applications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        payment_record_id INTEGER NOT NULL UNIQUE,
        account_id INTEGER NOT NULL,
        entitlement_mutation_id INTEGER NOT NULL UNIQUE,
        applied_operation TEXT NOT NULL CHECK(applied_operation IN ('CREATE','RENEW','PACKAGE_GRANT')),
        applied_expiry INTEGER,
        related_grant_id INTEGER,
        entitlement_snapshot_json TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        FOREIGN KEY(payment_record_id) REFERENCES mgboost_manual_payment_records(id) ON DELETE RESTRICT,
        FOREIGN KEY(account_id) REFERENCES mgboost_accounts(id) ON DELETE RESTRICT,
        FOREIGN KEY(entitlement_mutation_id, account_id)
            REFERENCES mgboost_entitlement_mutations(id, account_id) ON DELETE RESTRICT
    )
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mgboost_manual_payment_application_no_update
        BEFORE UPDATE ON mgboost_manual_payment_applications
        BEGIN SELECT RAISE(ABORT, 'manual payment application is immutable'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mgboost_manual_payment_application_no_delete
        BEFORE DELETE ON mgboost_manual_payment_applications
        BEGIN SELECT RAISE(ABORT, 'manual payment application is never deleted'); END
    """,
    """
    CREATE TABLE IF NOT EXISTS mgboost_manual_payment_sync_jobs (
        payment_record_id INTEGER PRIMARY KEY,
        account_id INTEGER NOT NULL,
        entitlement_mutation_id INTEGER NOT NULL,
        state TEXT NOT NULL DEFAULT 'PENDING' CHECK(state IN ('PENDING','SYNCED','MANUAL_REVIEW')),
        attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
        last_error_class TEXT,
        last_attempt_at INTEGER,
        synced_at INTEGER,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        FOREIGN KEY(payment_record_id) REFERENCES mgboost_manual_payment_records(id) ON DELETE RESTRICT,
        FOREIGN KEY(account_id) REFERENCES mgboost_accounts(id) ON DELETE RESTRICT,
        FOREIGN KEY(entitlement_mutation_id, account_id)
            REFERENCES mgboost_entitlement_mutations(id, account_id) ON DELETE RESTRICT
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_mgboost_manual_payment_sync_ready
        ON mgboost_manual_payment_sync_jobs(state, updated_at, payment_record_id)
    """,
)

SCHEMA_CHECKSUM = hashlib.sha256(
    "\n".join(statement.strip() for statement in _SCHEMA_STATEMENTS).encode("utf-8")
).hexdigest()


def _verify(connection: sqlite3.Connection) -> None:
    required_tables = {
        "mgboost_manual_payment_records": {
            "kind", "status", "account_id", "catalog_version_id", "expected_amount_minor",
            "recorded_amount_minor", "currency", "payment_method", "external_reference",
            "idempotency_key_hash", "request_hash", "entitlement_mutation_id",
        },
        "mgboost_manual_payment_edits": {
            "payment_record_id", "edit_kind", "before_json", "after_json", "actor_ref",
        },
        "mgboost_manual_payment_applications": {
            "payment_record_id", "account_id", "entitlement_mutation_id", "applied_operation",
        },
        "mgboost_manual_payment_sync_jobs": {
            "payment_record_id", "account_id", "entitlement_mutation_id", "state",
        },
    }
    for table, fields in required_tables.items():
        actual = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        if fields - actual:
            raise RuntimeError(f"PH5-09 incompatible table {table}")
    triggers = {
        row[0]
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type='trigger'")
    }
    required_triggers = {
        "trg_mgboost_manual_payment_record_applied_immutable",
        "trg_mgboost_manual_payment_record_no_delete",
        "trg_mgboost_manual_payment_edit_no_update",
        "trg_mgboost_manual_payment_edit_no_delete",
        "trg_mgboost_manual_payment_application_no_update",
        "trg_mgboost_manual_payment_application_no_delete",
    }
    if required_triggers - triggers:
        raise RuntimeError(
            f"PH5-09 immutable evidence triggers missing: {sorted(required_triggers - triggers)}"
        )


def apply_manual_payment_schema(connection: sqlite3.Connection, *, now: int | None = None) -> bool:
    connection.execute("PRAGMA foreign_keys=ON")
    timestamp = int(time.time()) if now is None else int(now)
    try:
        connection.execute("BEGIN IMMEDIATE")
        for migration_id, checksum, label in (
            (ACCOUNT_MIGRATION_ID, ACCOUNT_SCHEMA_CHECKSUM, "PH3-01"),
            (CATALOG_MIGRATION_ID, CATALOG_SCHEMA_CHECKSUM, "PH5-01"),
            (PROVENANCE_MIGRATION_ID, PROVENANCE_SCHEMA_CHECKSUM, "PH3-09"),
            (PACKAGE_MIGRATION_ID, PACKAGE_SCHEMA_CHECKSUM, "PH5-03"),
        ):
            row = connection.execute(
                "SELECT schema_checksum FROM mgboost_schema_migrations WHERE migration_id=?",
                (migration_id,),
            ).fetchone()
            if not row or row[0] != checksum:
                raise RuntimeError(f"PH5-09 requires exact {label} schema")
        row = connection.execute(
            "SELECT schema_checksum FROM mgboost_schema_migrations WHERE migration_id=?",
            (MIGRATION_ID,),
        ).fetchone()
        if row:
            if row[0] != SCHEMA_CHECKSUM:
                raise RuntimeError("PH5-09 schema checksum mismatch")
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
