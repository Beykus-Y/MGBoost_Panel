"""Additive PH5-13 promo codes schema.

Three new tables (`mgboost_promo_definitions`/`_versions`/`_redemptions`)
plus additive nullable columns on the existing `mgboost_manual_payment_records`
table (an `ALTER TABLE ... ADD COLUMN`, not a CHECK-constraint rewrite --
SQLite supports this natively without a table rebuild, and it does not
change `manual_payment_schema.py`'s own already-checksummed migration).

Deliberately does NOT touch `mgboost_entitlement_mutations`'s CHECK
constraint (checksum-pinned on PH3-01, already live in production) to add
a `PROMO_GRANT` payment_channel -- in this phase every redemption is
admin-initiated and reuses the already-legal `mutation_source='ADMIN'` /
`payment_channel='ADMIN_GRANT'` combination. All promo-specific detail
(which promo, which version, trial_class, discount) lives in these new
tables instead. `PROMO_GRANT` as its own channel is deferred to the future
bot self-service phase, where the actor genuinely isn't an admin.

`mgboost_promo_redemptions.status='PENDING_APPLY'` is the durable-intent
row `PromoStore.redeem_extend_or_trial` writes BEFORE calling the engine
(`subscription_renewal.append_promo_wl_period` /
`subscription_admin_ops.apply_adjustment`) -- see `src/promo.py` for the
full crash-consistency sequencing.
"""

from __future__ import annotations

import hashlib
import sqlite3
import time

from .account_schema import MIGRATION_ID as ACCOUNT_MIGRATION_ID
from .account_schema import SCHEMA_CHECKSUM as ACCOUNT_SCHEMA_CHECKSUM
from .manual_payment_schema import MIGRATION_ID as MANUAL_PAYMENT_MIGRATION_ID
from .manual_payment_schema import SCHEMA_CHECKSUM as MANUAL_PAYMENT_SCHEMA_CHECKSUM

MIGRATION_ID = "ph5_13_promo_codes_v1"

_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS mgboost_promo_definitions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code TEXT NOT NULL UNIQUE
            CHECK(code = UPPER(code) AND length(code) BETWEEN 3 AND 64),
        effect_kind TEXT NOT NULL
            CHECK(effect_kind IN (
                'EXTEND_SUBSCRIPTION','TRIAL_GRANT','PURCHASE_DISCOUNT'
            )),
        trial_class TEXT
            CHECK(trial_class IS NULL OR length(trial_class) BETWEEN 1 AND 64),
        status TEXT NOT NULL DEFAULT 'ACTIVE' CHECK(status IN ('ACTIVE','DISABLED')),
        created_by_actor TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        CHECK(effect_kind='TRIAL_GRANT' OR trial_class IS NULL)
    )
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mgboost_promo_definitions_identity_immutable
        BEFORE UPDATE OF code, effect_kind, trial_class, created_by_actor, created_at
        ON mgboost_promo_definitions
        BEGIN SELECT RAISE(ABORT, 'promo definition identity is immutable'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mgboost_promo_definitions_no_delete
        BEFORE DELETE ON mgboost_promo_definitions
        BEGIN SELECT RAISE(ABORT, 'promo definitions are never deleted'); END
    """,
    """
    CREATE TABLE IF NOT EXISTS mgboost_promo_versions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        promo_id INTEGER NOT NULL,
        version INTEGER NOT NULL CHECK(version > 0),
        effect_params_json TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'ACTIVE' CHECK(status IN ('ACTIVE','SUPERSEDED')),
        created_by_actor TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        UNIQUE(promo_id, version),
        FOREIGN KEY(promo_id) REFERENCES mgboost_promo_definitions(id) ON DELETE RESTRICT
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS ux_mgboost_promo_active_version
        ON mgboost_promo_versions(promo_id) WHERE status='ACTIVE'
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mgboost_promo_versions_no_delete
        BEFORE DELETE ON mgboost_promo_versions
        BEGIN SELECT RAISE(ABORT, 'promo versions are never deleted'); END
    """,
    """
    CREATE TABLE IF NOT EXISTS mgboost_promo_redemptions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        promo_id INTEGER NOT NULL,
        promo_version INTEGER NOT NULL,
        trial_class TEXT,
        owner_telegram_id INTEGER,
        account_id INTEGER,
        status TEXT NOT NULL
            CHECK(status IN ('PENDING_APPLY','REDEEMED','RESERVED','CANCELLED')),
        reserved_until INTEGER,
        applied_mutation_id INTEGER,
        idempotency_key_hash TEXT NOT NULL UNIQUE,
        request_hash TEXT NOT NULL,
        actor_type TEXT NOT NULL,
        actor_ref TEXT,
        reason TEXT,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        row_version INTEGER NOT NULL DEFAULT 1 CHECK(row_version > 0),
        FOREIGN KEY(promo_id, promo_version)
            REFERENCES mgboost_promo_versions(promo_id, version) ON DELETE RESTRICT,
        FOREIGN KEY(account_id) REFERENCES mgboost_accounts(id) ON DELETE RESTRICT,
        FOREIGN KEY(applied_mutation_id, account_id)
            REFERENCES mgboost_entitlement_mutations(id, account_id) ON DELETE RESTRICT
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS ux_mgboost_promo_versions_promo_version
        ON mgboost_promo_versions(promo_id, version)
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS ux_promo_trial_class_identity
        ON mgboost_promo_redemptions(trial_class, owner_telegram_id)
        WHERE status IN ('PENDING_APPLY','REDEEMED') AND trial_class IS NOT NULL
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_mgboost_promo_redemptions_account
        ON mgboost_promo_redemptions(account_id, status)
    """,
    """
    ALTER TABLE mgboost_manual_payment_records ADD COLUMN promo_id INTEGER
    """,
    """
    ALTER TABLE mgboost_manual_payment_records ADD COLUMN promo_version INTEGER
    """,
    """
    ALTER TABLE mgboost_manual_payment_records ADD COLUMN promo_redemption_id INTEGER
    """,
    """
    ALTER TABLE mgboost_manual_payment_records ADD COLUMN original_amount_minor INTEGER
    """,
    """
    ALTER TABLE mgboost_manual_payment_records ADD COLUMN discount_snapshot_json TEXT
    """,
)

SCHEMA_CHECKSUM = hashlib.sha256(
    "\n".join(statement.strip() for statement in _SCHEMA_STATEMENTS).encode("utf-8")
).hexdigest()

_REQUIRED_COLUMNS = {
    "mgboost_promo_definitions": {
        "id", "code", "effect_kind", "trial_class", "status", "created_by_actor",
    },
    "mgboost_promo_versions": {
        "id", "promo_id", "version", "effect_params_json", "status",
    },
    "mgboost_promo_redemptions": {
        "id", "promo_id", "promo_version", "trial_class", "owner_telegram_id",
        "account_id", "status", "reserved_until", "applied_mutation_id",
        "idempotency_key_hash", "request_hash", "row_version",
    },
    "mgboost_manual_payment_records": {
        "promo_id", "promo_version", "promo_redemption_id", "original_amount_minor",
        "discount_snapshot_json",
    },
}

_REQUIRED_OBJECTS = {
    "ux_mgboost_promo_active_version",
    "ux_promo_trial_class_identity",
    "ix_mgboost_promo_redemptions_account",
}


def _verify(connection: sqlite3.Connection) -> None:
    for table, required in _REQUIRED_COLUMNS.items():
        actual = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        if required - actual:
            raise RuntimeError(f"PH5-13 incompatible table {table}")
    objects = {
        row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('index','trigger')"
        )
    }
    if not _REQUIRED_OBJECTS.issubset(objects):
        raise RuntimeError("PH5-13 promo codes schema objects incomplete")


def apply_promo_schema(connection: sqlite3.Connection, *, now: int | None = None) -> bool:
    connection.execute("PRAGMA foreign_keys=ON")
    timestamp = int(time.time()) if now is None else int(now)
    try:
        connection.execute("BEGIN IMMEDIATE")
        for migration_id, checksum, label in (
            (ACCOUNT_MIGRATION_ID, ACCOUNT_SCHEMA_CHECKSUM, "PH3-01"),
            (MANUAL_PAYMENT_MIGRATION_ID, MANUAL_PAYMENT_SCHEMA_CHECKSUM, "PH5-09"),
        ):
            row = connection.execute(
                "SELECT schema_checksum FROM mgboost_schema_migrations WHERE migration_id=?",
                (migration_id,),
            ).fetchone()
            if not row or row[0] != checksum:
                raise RuntimeError(f"PH5-13 requires exact {label} schema")
        existing = connection.execute(
            "SELECT schema_checksum FROM mgboost_schema_migrations WHERE migration_id=?",
            (MIGRATION_ID,),
        ).fetchone()
        if existing is not None:
            if existing[0] != SCHEMA_CHECKSUM:
                raise RuntimeError("PH5-13 schema checksum mismatch")
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
