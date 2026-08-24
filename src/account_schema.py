"""Additive PH3-01 parent-account schema.

Nothing in the legacy request, Stars, LK, Filin or Marzban paths reads these
tables yet.  Keeping the migration in a separate module makes that boundary
explicit and lets an older application binary safely ignore the new schema.
"""

from __future__ import annotations

import hashlib
import sqlite3
import time


MIGRATION_ID = "ph3_01_parent_account_v1"


_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS mgboost_schema_migrations (
        migration_id TEXT PRIMARY KEY,
        schema_checksum TEXT NOT NULL,
        applied_at INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS mgboost_accounts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        public_id TEXT NOT NULL UNIQUE,
        status TEXT NOT NULL DEFAULT 'ACTIVE'
            CHECK(status IN ('ACTIVE','DISABLED','CLOSED')),
        account_source TEXT NOT NULL
            CHECK(account_source IN ('DIRECT','INTERNAL','UNKNOWN_LEGACY')),
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        row_version INTEGER NOT NULL DEFAULT 1 CHECK(row_version > 0)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS mgboost_telegram_identities (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        account_id INTEGER NOT NULL,
        telegram_id INTEGER NOT NULL CHECK(telegram_id > 0),
        role TEXT NOT NULL DEFAULT 'OWNER' CHECK(role = 'OWNER'),
        provenance TEXT NOT NULL
            CHECK(provenance IN (
                'DIRECT_BIND','ADMIN_REBIND','MIGRATION','UNKNOWN_LEGACY'
            )),
        linked_at INTEGER NOT NULL,
        revoked_at INTEGER,
        revoke_reason TEXT,
        linked_by_actor TEXT,
        revoked_by_actor TEXT,
        CHECK(revoked_at IS NULL OR revoked_at >= linked_at),
        CHECK((revoked_at IS NULL AND revoke_reason IS NULL AND revoked_by_actor IS NULL)
              OR revoked_at IS NOT NULL),
        FOREIGN KEY(account_id) REFERENCES mgboost_accounts(id) ON DELETE RESTRICT
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS ux_mgboost_tg_active_identity
        ON mgboost_telegram_identities(telegram_id)
        WHERE revoked_at IS NULL
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS ux_mgboost_account_active_owner
        ON mgboost_telegram_identities(account_id)
        WHERE role='OWNER' AND revoked_at IS NULL
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_mgboost_tg_account_history
        ON mgboost_telegram_identities(account_id, linked_at)
    """,
    """
    CREATE TABLE IF NOT EXISTS mgboost_plan_versions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        plan_code TEXT NOT NULL,
        version INTEGER NOT NULL CHECK(version > 0),
        display_name TEXT NOT NULL,
        plan_kind TEXT NOT NULL CHECK(plan_kind IN ('COMMERCIAL','INTERNAL')),
        billing_required INTEGER NOT NULL CHECK(billing_required IN (0,1)),
        non_wl_unlimited INTEGER NOT NULL DEFAULT 1
            CHECK(non_wl_unlimited = 1),
        device_limit_mode TEXT NOT NULL
            CHECK(device_limit_mode IN ('LIMITED','UNLIMITED')),
        device_limit INTEGER,
        wl_mode TEXT NOT NULL CHECK(wl_mode IN ('NONE','LIMITED','UNLIMITED')),
        wl_quota_bytes INTEGER,
        wl_period_days INTEGER,
        created_at INTEGER NOT NULL,
        terms_json TEXT NOT NULL,
        UNIQUE(plan_code, version),
        CHECK(
            (device_limit_mode='LIMITED' AND device_limit BETWEEN 1 AND 99)
            OR (device_limit_mode='UNLIMITED' AND device_limit IS NULL)
        ),
        CHECK(
            (wl_mode='NONE' AND wl_quota_bytes IS NULL AND wl_period_days IS NULL)
            OR (wl_mode='LIMITED' AND wl_quota_bytes > 0 AND wl_period_days > 0)
            OR (wl_mode='UNLIMITED' AND wl_quota_bytes IS NULL)
        ),
        CHECK(plan_kind!='INTERNAL' OR billing_required=0)
    )
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mgboost_plan_versions_no_update
        BEFORE UPDATE ON mgboost_plan_versions
        BEGIN SELECT RAISE(ABORT, 'plan versions are immutable'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mgboost_plan_versions_no_delete
        BEFORE DELETE ON mgboost_plan_versions
        BEGIN SELECT RAISE(ABORT, 'plan versions are immutable'); END
    """,
    """
    CREATE TABLE IF NOT EXISTS mgboost_plan_durations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        plan_version_id INTEGER NOT NULL,
        duration_days INTEGER NOT NULL CHECK(duration_days > 0),
        duration_version INTEGER NOT NULL DEFAULT 1 CHECK(duration_version > 0),
        created_at INTEGER NOT NULL,
        metadata_json TEXT NOT NULL DEFAULT '{}',
        UNIQUE(plan_version_id, duration_days, duration_version),
        UNIQUE(id, plan_version_id),
        FOREIGN KEY(plan_version_id) REFERENCES mgboost_plan_versions(id)
            ON DELETE RESTRICT
    )
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mgboost_plan_durations_no_update
        BEFORE UPDATE ON mgboost_plan_durations
        BEGIN SELECT RAISE(ABORT, 'plan durations are immutable'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mgboost_plan_durations_no_delete
        BEFORE DELETE ON mgboost_plan_durations
        BEGIN SELECT RAISE(ABORT, 'plan durations are immutable'); END
    """,
    """
    CREATE TABLE IF NOT EXISTS mgboost_subscriptions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        account_id INTEGER NOT NULL,
        current_plan_version_id INTEGER,
        status TEXT NOT NULL
            CHECK(status IN (
                'PENDING','ACTIVE','EXPIRED','DISABLED','CANCELLED',
                'UNLIMITED','UNKNOWN_LEGACY'
            )),
        started_at INTEGER,
        current_expiry INTEGER,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        row_version INTEGER NOT NULL DEFAULT 1 CHECK(row_version > 0),
        UNIQUE(id, account_id),
        CHECK(
            (status='UNLIMITED' AND current_expiry IS NULL)
            OR status!='UNLIMITED'
        ),
        CHECK(status='UNKNOWN_LEGACY' OR current_plan_version_id IS NOT NULL),
        FOREIGN KEY(account_id) REFERENCES mgboost_accounts(id) ON DELETE RESTRICT,
        FOREIGN KEY(current_plan_version_id) REFERENCES mgboost_plan_versions(id)
            ON DELETE RESTRICT
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS ux_mgboost_account_live_subscription
        ON mgboost_subscriptions(account_id)
        WHERE status IN ('PENDING','ACTIVE','DISABLED','UNLIMITED','UNKNOWN_LEGACY')
    """,
    """
    CREATE TABLE IF NOT EXISTS mgboost_entitlement_mutations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        account_id INTEGER NOT NULL,
        subscription_id INTEGER,
        operation TEXT NOT NULL,
        payment_channel TEXT NOT NULL
            CHECK(payment_channel IN (
                'TELEGRAM_STARS','EXTERNAL_PAYMENT','ADMIN_GRANT',
                'NOT_APPLICABLE','UNKNOWN_LEGACY'
            )),
        mutation_source TEXT NOT NULL
            CHECK(mutation_source IN (
                'SYSTEM','DIRECT_PURCHASE','MANUAL_PAYMENT','ADMIN',
                'MIGRATION','PACKAGE','INTERNAL','UNKNOWN_LEGACY'
            )),
        actor_type TEXT NOT NULL,
        actor_ref TEXT,
        reason TEXT,
        external_reference TEXT,
        idempotency_key_hash TEXT,
        before_json TEXT,
        after_json TEXT,
        created_at INTEGER NOT NULL,
        UNIQUE(id, account_id),
        UNIQUE(payment_channel, external_reference),
        UNIQUE(idempotency_key_hash),
        FOREIGN KEY(account_id) REFERENCES mgboost_accounts(id) ON DELETE RESTRICT,
        FOREIGN KEY(subscription_id, account_id)
            REFERENCES mgboost_subscriptions(id, account_id) ON DELETE RESTRICT
    )
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mgboost_entitlement_mutations_no_update
        BEFORE UPDATE ON mgboost_entitlement_mutations
        BEGIN SELECT RAISE(ABORT, 'entitlement mutations are immutable'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mgboost_entitlement_mutations_no_delete
        BEFORE DELETE ON mgboost_entitlement_mutations
        BEGIN SELECT RAISE(ABORT, 'entitlement mutations are immutable'); END
    """,
    """
    CREATE TABLE IF NOT EXISTS mgboost_subscription_terms (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        account_id INTEGER NOT NULL,
        subscription_id INTEGER NOT NULL,
        sequence_no INTEGER NOT NULL CHECK(sequence_no > 0),
        plan_version_id INTEGER,
        duration_id INTEGER,
        duration_days INTEGER,
        starts_at INTEGER,
        ends_at INTEGER,
        billing_required_snapshot INTEGER
            CHECK(billing_required_snapshot IN (0,1)),
        device_limit_mode_snapshot TEXT
            CHECK(device_limit_mode_snapshot IN ('LIMITED','UNLIMITED')),
        device_limit_snapshot INTEGER,
        wl_mode_snapshot TEXT
            CHECK(wl_mode_snapshot IN ('NONE','LIMITED','UNLIMITED')),
        wl_quota_bytes_snapshot INTEGER,
        wl_period_days_snapshot INTEGER,
        plan_snapshot_json TEXT NOT NULL,
        mutation_id INTEGER NOT NULL,
        created_at INTEGER NOT NULL,
        UNIQUE(subscription_id, sequence_no),
        UNIQUE(id, subscription_id, account_id),
        CHECK(duration_days IS NULL OR duration_days > 0),
        CHECK(ends_at IS NULL OR starts_at IS NULL OR ends_at > starts_at),
        CHECK(
            (device_limit_mode_snapshot='LIMITED'
                AND device_limit_snapshot BETWEEN 1 AND 99)
            OR (device_limit_mode_snapshot='UNLIMITED'
                AND device_limit_snapshot IS NULL)
            OR (device_limit_mode_snapshot IS NULL
                AND device_limit_snapshot IS NULL)
        ),
        CHECK(
            (wl_mode_snapshot='NONE' AND wl_quota_bytes_snapshot IS NULL
                AND wl_period_days_snapshot IS NULL)
            OR (wl_mode_snapshot='LIMITED' AND wl_quota_bytes_snapshot > 0
                AND wl_period_days_snapshot > 0)
            OR (wl_mode_snapshot='UNLIMITED' AND wl_quota_bytes_snapshot IS NULL)
            OR (wl_mode_snapshot IS NULL AND wl_quota_bytes_snapshot IS NULL
                AND wl_period_days_snapshot IS NULL)
        ),
        FOREIGN KEY(subscription_id, account_id)
            REFERENCES mgboost_subscriptions(id, account_id) ON DELETE RESTRICT,
        FOREIGN KEY(plan_version_id) REFERENCES mgboost_plan_versions(id)
            ON DELETE RESTRICT,
        FOREIGN KEY(duration_id, plan_version_id)
            REFERENCES mgboost_plan_durations(id, plan_version_id) ON DELETE RESTRICT,
        FOREIGN KEY(mutation_id, account_id)
            REFERENCES mgboost_entitlement_mutations(id, account_id) ON DELETE RESTRICT
    )
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mgboost_subscription_terms_no_update
        BEFORE UPDATE ON mgboost_subscription_terms
        BEGIN SELECT RAISE(ABORT, 'subscription terms are immutable'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mgboost_subscription_terms_no_delete
        BEFORE DELETE ON mgboost_subscription_terms
        BEGIN SELECT RAISE(ABORT, 'subscription terms are immutable'); END
    """,
    """
    CREATE TABLE IF NOT EXISTS mgboost_entitlement_state (
        account_id INTEGER PRIMARY KEY,
        subscription_id INTEGER NOT NULL,
        desired_status TEXT NOT NULL
            CHECK(desired_status IN ('ACTIVE','DISABLED','EXPIRED','UNLIMITED')),
        revision INTEGER NOT NULL DEFAULT 1 CHECK(revision > 0),
        updated_at INTEGER NOT NULL,
        FOREIGN KEY(subscription_id, account_id)
            REFERENCES mgboost_subscriptions(id, account_id) ON DELETE RESTRICT,
        FOREIGN KEY(account_id) REFERENCES mgboost_accounts(id) ON DELETE RESTRICT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS mgboost_entitlement_overrides (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        account_id INTEGER NOT NULL,
        subscription_id INTEGER,
        entitlement_key TEXT NOT NULL
            CHECK(entitlement_key IN (
                'BILLING_REQUIRED','DEVICE_LIMIT','WL_ACCESS','WL_QUOTA_BYTES'
            )),
        value_type TEXT NOT NULL
            CHECK(value_type IN ('BOOLEAN','INTEGER','UNLIMITED')),
        boolean_value INTEGER CHECK(boolean_value IN (0,1)),
        integer_value INTEGER CHECK(integer_value >= 0),
        starts_at INTEGER NOT NULL,
        expires_at INTEGER NOT NULL,
        revoked_at INTEGER,
        reason TEXT NOT NULL,
        mutation_id INTEGER NOT NULL,
        created_at INTEGER NOT NULL,
        CHECK(expires_at > starts_at),
        CHECK(revoked_at IS NULL OR revoked_at >= created_at),
        CHECK(
            (value_type='BOOLEAN' AND boolean_value IS NOT NULL
                AND integer_value IS NULL)
            OR (value_type='INTEGER' AND integer_value IS NOT NULL
                AND boolean_value IS NULL)
            OR (value_type='UNLIMITED' AND boolean_value IS NULL
                AND integer_value IS NULL)
        ),
        FOREIGN KEY(account_id) REFERENCES mgboost_accounts(id) ON DELETE RESTRICT,
        FOREIGN KEY(subscription_id, account_id)
            REFERENCES mgboost_subscriptions(id, account_id) ON DELETE RESTRICT,
        FOREIGN KEY(mutation_id, account_id)
            REFERENCES mgboost_entitlement_mutations(id, account_id) ON DELETE RESTRICT
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_mgboost_active_overrides
        ON mgboost_entitlement_overrides(account_id, entitlement_key, expires_at)
        WHERE revoked_at IS NULL
    """,
    """
    CREATE TABLE IF NOT EXISTS mgboost_wl_periods (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        account_id INTEGER NOT NULL,
        subscription_id INTEGER NOT NULL,
        subscription_term_id INTEGER NOT NULL,
        sequence_no INTEGER NOT NULL CHECK(sequence_no > 0),
        starts_at INTEGER NOT NULL,
        ends_at INTEGER NOT NULL,
        quota_mode TEXT NOT NULL CHECK(quota_mode IN ('LIMITED','UNLIMITED')),
        base_quota_bytes INTEGER,
        status TEXT NOT NULL CHECK(status IN ('PLANNED','ACTIVE','CLOSED')),
        created_at INTEGER NOT NULL,
        UNIQUE(subscription_id, sequence_no),
        CHECK(ends_at > starts_at),
        CHECK(
            (quota_mode='LIMITED' AND base_quota_bytes > 0)
            OR (quota_mode='UNLIMITED' AND base_quota_bytes IS NULL)
        ),
        FOREIGN KEY(subscription_id, account_id)
            REFERENCES mgboost_subscriptions(id, account_id) ON DELETE RESTRICT,
        FOREIGN KEY(subscription_term_id, subscription_id, account_id)
            REFERENCES mgboost_subscription_terms(id, subscription_id, account_id)
            ON DELETE RESTRICT
    )
    """,
)


SCHEMA_CHECKSUM = hashlib.sha256(
    "\n".join(statement.strip() for statement in _SCHEMA_STATEMENTS[1:]).encode("utf-8")
).hexdigest()

_REQUIRED_COLUMNS = {
    "mgboost_accounts": {"id", "public_id", "status", "account_source", "row_version"},
    "mgboost_telegram_identities": {
        "id", "account_id", "telegram_id", "role", "provenance", "revoked_at",
    },
    "mgboost_plan_versions": {
        "id", "plan_code", "version", "billing_required", "device_limit_mode",
        "device_limit", "wl_mode", "wl_quota_bytes", "wl_period_days", "terms_json",
    },
    "mgboost_plan_durations": {"id", "plan_version_id", "duration_days", "duration_version"},
    "mgboost_subscriptions": {
        "id", "account_id", "current_plan_version_id", "status", "current_expiry",
        "row_version",
    },
    "mgboost_entitlement_mutations": {
        "id", "account_id", "subscription_id", "payment_channel", "mutation_source",
        "idempotency_key_hash",
    },
    "mgboost_subscription_terms": {
        "id", "account_id", "subscription_id", "sequence_no", "plan_version_id",
        "duration_days", "plan_snapshot_json", "mutation_id",
    },
    "mgboost_entitlement_state": {"account_id", "subscription_id", "desired_status", "revision"},
    "mgboost_entitlement_overrides": {
        "id", "account_id", "subscription_id", "entitlement_key", "value_type",
        "expires_at", "mutation_id",
    },
    "mgboost_wl_periods": {
        "id", "account_id", "subscription_id", "subscription_term_id",
        "sequence_no", "starts_at", "ends_at", "quota_mode", "base_quota_bytes",
    },
}

_REQUIRED_OBJECTS = {
    "ux_mgboost_tg_active_identity",
    "ux_mgboost_account_active_owner",
    "ux_mgboost_account_live_subscription",
    "ix_mgboost_tg_account_history",
    "ix_mgboost_active_overrides",
    "trg_mgboost_plan_versions_no_update",
    "trg_mgboost_plan_versions_no_delete",
    "trg_mgboost_plan_durations_no_update",
    "trg_mgboost_plan_durations_no_delete",
    "trg_mgboost_entitlement_mutations_no_update",
    "trg_mgboost_entitlement_mutations_no_delete",
    "trg_mgboost_subscription_terms_no_update",
    "trg_mgboost_subscription_terms_no_delete",
}


def _verify_schema_contract(connection: sqlite3.Connection) -> None:
    for table, required in _REQUIRED_COLUMNS.items():
        actual = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        missing = required - actual
        if missing:
            raise RuntimeError(f"PH3-01 incompatible table {table}: missing columns")
    objects = {
        row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE name LIKE 'ux_mgboost_%' "
            "OR name LIKE 'ix_mgboost_%' OR name LIKE 'trg_mgboost_%'"
        )
    }
    if not _REQUIRED_OBJECTS.issubset(objects):
        raise RuntimeError("PH3-01 schema indexes/triggers incomplete")


def apply_parent_account_schema(connection: sqlite3.Connection, *, now: int | None = None) -> bool:
    """Apply PH3-01 transactionally. Return True only for the first apply."""
    connection.execute("PRAGMA foreign_keys=ON")
    applied_at = int(time.time()) if now is None else int(now)
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(_SCHEMA_STATEMENTS[0])
        row = connection.execute(
            "SELECT schema_checksum FROM mgboost_schema_migrations WHERE migration_id=?",
            (MIGRATION_ID,),
        ).fetchone()
        if row is not None:
            stored = row[0]
            if stored != SCHEMA_CHECKSUM:
                raise RuntimeError("PH3-01 schema checksum mismatch")
            _verify_schema_contract(connection)
            connection.commit()
            return False

        for statement in _SCHEMA_STATEMENTS[1:]:
            connection.execute(statement)
        _verify_schema_contract(connection)
        connection.execute(
            "INSERT INTO mgboost_schema_migrations "
            "(migration_id, schema_checksum, applied_at) VALUES (?,?,?)",
            (MIGRATION_ID, SCHEMA_CHECKSUM, applied_at),
        )
        connection.commit()
        return True
    except Exception:
        connection.rollback()
        raise


NEW_RUNTIME_TABLES = (
    "mgboost_accounts",
    "mgboost_telegram_identities",
    "mgboost_plan_versions",
    "mgboost_plan_durations",
    "mgboost_subscriptions",
    "mgboost_entitlement_mutations",
    "mgboost_subscription_terms",
    "mgboost_entitlement_state",
    "mgboost_entitlement_overrides",
    "mgboost_wl_periods",
)
