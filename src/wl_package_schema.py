"""PH5-03 additive package catalog, grants and immutable refund evidence."""

from __future__ import annotations

import hashlib
import sqlite3
import time

from .plan_catalog_schema import MIGRATION_ID as CATALOG_MIGRATION_ID
from .plan_catalog_schema import SCHEMA_CHECKSUM as CATALOG_SCHEMA_CHECKSUM
from .provenance_schema import MIGRATION_ID as PROVENANCE_MIGRATION_ID
from .provenance_schema import SCHEMA_CHECKSUM as PROVENANCE_SCHEMA_CHECKSUM
from .wl_usage_ledger_schema import MIGRATION_ID as USAGE_MIGRATION_ID
from .wl_usage_ledger_schema import SCHEMA_CHECKSUM as USAGE_SCHEMA_CHECKSUM


MIGRATION_ID = "ph5_03_wl_package_catalog_v1"

_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS mgboost_wl_package_products (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        sku TEXT NOT NULL,
        version INTEGER NOT NULL CHECK(version > 0),
        display_name TEXT NOT NULL,
        bytes INTEGER NOT NULL CHECK(bytes > 0),
        created_at INTEGER NOT NULL,
        UNIQUE(sku, version)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS mgboost_wl_package_prices (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        catalog_version_id INTEGER NOT NULL,
        package_product_id INTEGER NOT NULL,
        amount INTEGER NOT NULL CHECK(amount > 0),
        created_at INTEGER NOT NULL,
        UNIQUE(catalog_version_id, package_product_id),
        FOREIGN KEY(catalog_version_id) REFERENCES mgboost_price_catalog_versions(id)
            ON DELETE RESTRICT,
        FOREIGN KEY(package_product_id) REFERENCES mgboost_wl_package_products(id)
            ON DELETE RESTRICT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS mgboost_wl_package_grants (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        account_id INTEGER NOT NULL,
        package_product_id INTEGER NOT NULL,
        catalog_version_id INTEGER NOT NULL,
        package_price_id INTEGER NOT NULL,
        payment_id INTEGER NOT NULL,
        grant_mutation_id INTEGER NOT NULL,
        price_channel TEXT NOT NULL CHECK(price_channel IN ('TELEGRAM_STARS','RUB')),
        sku_snapshot TEXT NOT NULL,
        product_version_snapshot INTEGER NOT NULL CHECK(product_version_snapshot > 0),
        catalog_version_snapshot TEXT NOT NULL,
        granted_bytes INTEGER NOT NULL CHECK(granted_bytes > 0),
        price_amount_snapshot INTEGER NOT NULL CHECK(price_amount_snapshot > 0),
        granted_at INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'ACTIVE' CHECK(status IN ('ACTIVE','REVOKED')),
        revoked_at INTEGER,
        revoked_by_mutation_id INTEGER,
        idempotency_key_hash TEXT NOT NULL UNIQUE,
        request_hash TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        UNIQUE(payment_id),
        CHECK((status='ACTIVE' AND revoked_at IS NULL AND revoked_by_mutation_id IS NULL)
           OR (status='REVOKED' AND revoked_at IS NOT NULL AND revoked_by_mutation_id IS NOT NULL)),
        FOREIGN KEY(account_id) REFERENCES mgboost_accounts(id) ON DELETE RESTRICT,
        FOREIGN KEY(package_product_id) REFERENCES mgboost_wl_package_products(id) ON DELETE RESTRICT,
        FOREIGN KEY(catalog_version_id) REFERENCES mgboost_price_catalog_versions(id) ON DELETE RESTRICT,
        FOREIGN KEY(package_price_id) REFERENCES mgboost_wl_package_prices(id) ON DELETE RESTRICT,
        FOREIGN KEY(payment_id, account_id) REFERENCES mgboost_payment_records(id, account_id)
            ON DELETE RESTRICT,
        FOREIGN KEY(grant_mutation_id, account_id) REFERENCES mgboost_entitlement_mutations(id, account_id)
            ON DELETE RESTRICT,
        FOREIGN KEY(revoked_by_mutation_id, account_id)
            REFERENCES mgboost_entitlement_mutations(id, account_id) ON DELETE RESTRICT
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_mgboost_wl_package_grants_account_fifo
        ON mgboost_wl_package_grants(account_id, granted_at, id)
    """,
    """
    CREATE TABLE IF NOT EXISTS mgboost_wl_package_refunds (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        account_id INTEGER NOT NULL,
        package_grant_id INTEGER NOT NULL UNIQUE,
        refund_mutation_id INTEGER NOT NULL,
        refund_reference TEXT NOT NULL UNIQUE,
        evidence_json TEXT NOT NULL,
        idempotency_key_hash TEXT NOT NULL UNIQUE,
        request_hash TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        FOREIGN KEY(account_id) REFERENCES mgboost_accounts(id) ON DELETE RESTRICT,
        FOREIGN KEY(package_grant_id) REFERENCES mgboost_wl_package_grants(id)
            ON DELETE RESTRICT,
        FOREIGN KEY(refund_mutation_id, account_id)
            REFERENCES mgboost_entitlement_mutations(id, account_id) ON DELETE RESTRICT
    )
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mgboost_wl_package_products_no_update
        BEFORE UPDATE ON mgboost_wl_package_products
        BEGIN SELECT RAISE(ABORT, 'package products are immutable'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mgboost_wl_package_products_no_delete
        BEFORE DELETE ON mgboost_wl_package_products
        BEGIN SELECT RAISE(ABORT, 'package products are never deleted'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mgboost_wl_package_prices_no_update
        BEFORE UPDATE ON mgboost_wl_package_prices
        BEGIN SELECT RAISE(ABORT, 'package prices are immutable'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mgboost_wl_package_prices_no_delete
        BEFORE DELETE ON mgboost_wl_package_prices
        BEGIN SELECT RAISE(ABORT, 'package prices are never deleted'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mgboost_wl_package_grants_identity_immutable
        BEFORE UPDATE OF account_id, package_product_id, catalog_version_id, package_price_id,
                         payment_id, grant_mutation_id, price_channel, sku_snapshot,
                         product_version_snapshot, catalog_version_snapshot, granted_bytes,
                         price_amount_snapshot, granted_at, idempotency_key_hash, request_hash,
                         created_at ON mgboost_wl_package_grants
        BEGIN SELECT RAISE(ABORT, 'package grant identity is immutable'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mgboost_wl_package_grants_revoke_once
        BEFORE UPDATE OF status, revoked_at, revoked_by_mutation_id ON mgboost_wl_package_grants
        WHEN NOT (OLD.status='ACTIVE' AND NEW.status='REVOKED'
                  AND NEW.revoked_at IS NOT NULL AND NEW.revoked_by_mutation_id IS NOT NULL)
        BEGIN SELECT RAISE(ABORT, 'package grant may only be revoked once'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mgboost_wl_package_grants_no_delete
        BEFORE DELETE ON mgboost_wl_package_grants
        BEGIN SELECT RAISE(ABORT, 'package grants are never deleted'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mgboost_wl_package_refunds_no_update
        BEFORE UPDATE ON mgboost_wl_package_refunds
        BEGIN SELECT RAISE(ABORT, 'package refunds are immutable'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mgboost_wl_package_refunds_no_delete
        BEFORE DELETE ON mgboost_wl_package_refunds
        BEGIN SELECT RAISE(ABORT, 'package refunds are never deleted'); END
    """,
)

SCHEMA_CHECKSUM = hashlib.sha256(
    "\n".join(statement.strip() for statement in _SCHEMA_STATEMENTS).encode("utf-8")
).hexdigest()

NEW_RUNTIME_TABLES = (
    "mgboost_wl_package_products", "mgboost_wl_package_prices",
    "mgboost_wl_package_grants", "mgboost_wl_package_refunds",
)


def _verify(connection: sqlite3.Connection) -> None:
    required = {
        "mgboost_wl_package_products": {"id", "sku", "version", "display_name", "bytes", "created_at"},
        "mgboost_wl_package_prices": {"id", "catalog_version_id", "package_product_id", "amount", "created_at"},
        "mgboost_wl_package_grants": {"id", "account_id", "package_product_id", "catalog_version_id", "package_price_id", "payment_id", "grant_mutation_id", "price_channel", "sku_snapshot", "product_version_snapshot", "catalog_version_snapshot", "granted_bytes", "price_amount_snapshot", "granted_at", "status", "revoked_at", "revoked_by_mutation_id", "idempotency_key_hash", "request_hash", "created_at"},
        "mgboost_wl_package_refunds": {"id", "account_id", "package_grant_id", "refund_mutation_id", "refund_reference", "evidence_json", "idempotency_key_hash", "request_hash", "created_at"},
    }
    for table, columns in required.items():
        actual = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        if columns - actual:
            raise RuntimeError(f"PH5-03 incompatible table {table}: missing columns")


def apply_wl_package_schema(connection: sqlite3.Connection, *, now: int | None = None) -> bool:
    connection.execute("PRAGMA foreign_keys=ON")
    timestamp = int(time.time()) if now is None else int(now)
    try:
        connection.execute("BEGIN IMMEDIATE")
        for migration_id, checksum, label in (
            (CATALOG_MIGRATION_ID, CATALOG_SCHEMA_CHECKSUM, "PH5-01"),
            (PROVENANCE_MIGRATION_ID, PROVENANCE_SCHEMA_CHECKSUM, "PH3-09"),
            (USAGE_MIGRATION_ID, USAGE_SCHEMA_CHECKSUM, "PH6-03"),
        ):
            parent = connection.execute("SELECT schema_checksum FROM mgboost_schema_migrations WHERE migration_id=?", (migration_id,)).fetchone()
            if not parent or parent[0] != checksum:
                raise RuntimeError(f"PH5-03 requires exact {label} schema")
        row = connection.execute("SELECT schema_checksum FROM mgboost_schema_migrations WHERE migration_id=?", (MIGRATION_ID,)).fetchone()
        if row:
            if row[0] != SCHEMA_CHECKSUM:
                raise RuntimeError("PH5-03 schema checksum mismatch")
            _verify(connection)
            connection.commit()
            return False
        for statement in _SCHEMA_STATEMENTS:
            connection.execute(statement)
        _verify(connection)
        connection.execute("INSERT INTO mgboost_schema_migrations (migration_id,schema_checksum,applied_at) VALUES (?,?,?)", (MIGRATION_ID, SCHEMA_CHECKSUM, timestamp))
        connection.commit()
        return True
    except Exception:
        connection.rollback()
        raise
