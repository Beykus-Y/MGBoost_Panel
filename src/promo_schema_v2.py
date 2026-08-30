"""PH5-13 v2 compatibility migration.

The deployed v1 marker is immutable.  v2 accepts only two checksum-pinned
pre-v2 representations: the historical deployed v1 and the local expanded
v1 that was authored before this repair.  It never rewrites either marker.
"""

from __future__ import annotations

import hashlib
import sqlite3
import time

from .promo_schema import (
    CURRENT_SCHEMA_CHECKSUM,
    LEGACY_SCHEMA_CHECKSUM,
    MIGRATION_ID as V1_MIGRATION_ID,
    verify_current_v1_schema,
    verify_legacy_v1_schema,
)

MIGRATION_ID = "ph5_13_promo_codes_v2_snapshot_immutable"

_FINAL_REDEMPTIONS_TABLE = """
    CREATE TABLE mgboost_promo_redemptions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        promo_id INTEGER NOT NULL,
        promo_version INTEGER NOT NULL,
        trial_class TEXT,
        owner_telegram_id INTEGER,
        account_id INTEGER,
        status TEXT NOT NULL
            CHECK(status IN ('PENDING_APPLY','REDEEMED','RESERVED','COMMITTED','CANCELLED')),
        reserved_until INTEGER,
        per_user_limit_snapshot INTEGER NOT NULL DEFAULT 1 CHECK(per_user_limit_snapshot >= 1),
        applied_mutation_id INTEGER,
        idempotency_key_hash TEXT NOT NULL UNIQUE,
        request_hash TEXT NOT NULL,
        actor_type TEXT NOT NULL,
        actor_ref TEXT,
        reason TEXT,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        row_version INTEGER NOT NULL DEFAULT 1 CHECK(row_version > 0),
        bound_kind TEXT,
        bound_invoice_id INTEGER,
        FOREIGN KEY(promo_id, promo_version)
            REFERENCES mgboost_promo_versions(promo_id, version) ON DELETE RESTRICT,
        FOREIGN KEY(account_id) REFERENCES mgboost_accounts(id) ON DELETE RESTRICT,
        FOREIGN KEY(applied_mutation_id, account_id)
            REFERENCES mgboost_entitlement_mutations(id, account_id) ON DELETE RESTRICT
    )
"""

_FINAL_INDEX_STATEMENTS = (
    """
    CREATE UNIQUE INDEX ux_promo_trial_class_identity
        ON mgboost_promo_redemptions(trial_class, owner_telegram_id)
        WHERE status IN ('PENDING_APPLY','REDEEMED') AND trial_class IS NOT NULL
    """,
    """
    CREATE UNIQUE INDEX ux_promo_single_use_user
        ON mgboost_promo_redemptions(promo_id, owner_telegram_id)
        WHERE per_user_limit_snapshot = 1 AND status != 'CANCELLED'
            AND owner_telegram_id IS NOT NULL
    """,
    """
    CREATE INDEX ix_mgboost_promo_redemptions_account
        ON mgboost_promo_redemptions(account_id, status)
    """,
    """
    CREATE UNIQUE INDEX ux_stars_invoices_promo_redemption
        ON stars_invoices(promo_redemption_id) WHERE promo_redemption_id IS NOT NULL
    """,
)

_FINAL_TRIGGER = """
    CREATE TRIGGER trg_stars_invoices_promo_snapshot_immutable
        BEFORE UPDATE OF promo_redemption_id, original_stars_price, discount_minor
        ON stars_invoices
        WHEN (
            NEW.promo_redemption_id IS NOT OLD.promo_redemption_id
            OR NEW.original_stars_price IS NOT OLD.original_stars_price
            OR NEW.discount_minor IS NOT OLD.discount_minor
        )
        BEGIN SELECT RAISE(ABORT, 'stars invoice promo discount snapshot is immutable'); END
"""

_SCHEMA_STATEMENTS = (
    "ALTER TABLE mgboost_promo_definitions ADD COLUMN per_user_limit INTEGER NOT NULL DEFAULT 1 CHECK(per_user_limit >= 1)",
    "DROP INDEX ux_promo_trial_class_identity",
    "DROP INDEX ix_mgboost_promo_redemptions_account",
    "ALTER TABLE mgboost_promo_redemptions RENAME TO mgboost_promo_redemptions_v1",
    _FINAL_REDEMPTIONS_TABLE,
    """
    INSERT INTO mgboost_promo_redemptions (
        id,promo_id,promo_version,trial_class,owner_telegram_id,account_id,status,
        reserved_until,per_user_limit_snapshot,applied_mutation_id,idempotency_key_hash,
        request_hash,actor_type,actor_ref,reason,created_at,updated_at,row_version,
        bound_kind,bound_invoice_id
    )
    SELECT id,promo_id,promo_version,trial_class,owner_telegram_id,account_id,status,
        reserved_until,1,applied_mutation_id,idempotency_key_hash,request_hash,actor_type,
        actor_ref,reason,created_at,updated_at,row_version,NULL,NULL
    FROM mgboost_promo_redemptions_v1
    """,
    "DROP TABLE mgboost_promo_redemptions_v1",
    *_FINAL_INDEX_STATEMENTS[:3],
    "ALTER TABLE stars_invoices ADD COLUMN promo_redemption_id INTEGER",
    "ALTER TABLE stars_invoices ADD COLUMN original_stars_price INTEGER",
    "ALTER TABLE stars_invoices ADD COLUMN discount_minor INTEGER",
    _FINAL_INDEX_STATEMENTS[3],
    _FINAL_TRIGGER,
)

SCHEMA_CHECKSUM = hashlib.sha256(
    "\n".join(statement.strip() for statement in _SCHEMA_STATEMENTS).encode("utf-8")
).hexdigest()


def _verify_final_schema(connection: sqlite3.Connection) -> None:
    required_columns = {
        "mgboost_promo_definitions": {"per_user_limit"},
        "mgboost_promo_redemptions": {
            "per_user_limit_snapshot", "bound_kind", "bound_invoice_id",
        },
        "stars_invoices": {
            "promo_redemption_id", "original_stars_price", "discount_minor",
        },
    }
    for table, required in required_columns.items():
        actual = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        if required - actual:
            raise RuntimeError(f"PH5-13 v2 incompatible table {table}")
    objects = {row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('index','trigger')"
    )}
    required_objects = {
        "ux_promo_trial_class_identity", "ux_promo_single_use_user",
        "ix_mgboost_promo_redemptions_account",
        "ux_stars_invoices_promo_redemption",
        "trg_stars_invoices_promo_snapshot_immutable",
    }
    if not required_objects.issubset(objects):
        raise RuntimeError("PH5-13 v2 schema objects incomplete")
    redemption_sql = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
        ("mgboost_promo_redemptions",),
    ).fetchone()
    trigger_sql = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
        ("trg_stars_invoices_promo_snapshot_immutable",),
    ).fetchone()
    if (
        redemption_sql is None
        or "'COMMITTED'" not in redemption_sql[0]
        or trigger_sql is None
        or "OLD.promo_redemption_id IS NOT NULL" in trigger_sql[0]
    ):
        raise RuntimeError("PH5-13 v2 schema definition is missing or obsolete")


def _apply_legacy_upgrade(connection: sqlite3.Connection) -> None:
    for statement in _SCHEMA_STATEMENTS:
        connection.execute(statement)


def _apply_current_upgrade(connection: sqlite3.Connection) -> None:
    connection.execute("DROP TRIGGER trg_stars_invoices_promo_snapshot_immutable")
    connection.execute(_FINAL_TRIGGER)


def apply_promo_schema_v2(connection: sqlite3.Connection, *, now: int | None = None) -> bool:
    timestamp = int(time.time()) if now is None else int(now)
    try:
        connection.execute("BEGIN IMMEDIATE")
        parent = connection.execute(
            "SELECT schema_checksum FROM mgboost_schema_migrations WHERE migration_id=?",
            (V1_MIGRATION_ID,),
        ).fetchone()
        if parent is None or parent[0] not in {
            LEGACY_SCHEMA_CHECKSUM, CURRENT_SCHEMA_CHECKSUM,
        }:
            raise RuntimeError("PH5-13 v2 requires a known PH5-13 v1 schema")
        existing = connection.execute(
            "SELECT schema_checksum FROM mgboost_schema_migrations WHERE migration_id=?",
            (MIGRATION_ID,),
        ).fetchone()
        if existing is not None:
            if existing[0] != SCHEMA_CHECKSUM:
                raise RuntimeError("PH5-13 v2 schema checksum mismatch")
            _verify_final_schema(connection)
            connection.commit()
            return False
        if parent[0] == LEGACY_SCHEMA_CHECKSUM:
            verify_legacy_v1_schema(connection)
            _apply_legacy_upgrade(connection)
        else:
            verify_current_v1_schema(connection)
            _apply_current_upgrade(connection)
        _verify_final_schema(connection)
        connection.execute(
            "INSERT INTO mgboost_schema_migrations (migration_id,schema_checksum,applied_at) VALUES (?,?,?)",
            (MIGRATION_ID, SCHEMA_CHECKSUM, timestamp),
        )
        connection.commit()
        return True
    except Exception:
        connection.rollback()
        raise
