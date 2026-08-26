"""Additive PH5-01 versioned price-catalog schema.

Plan identity/terms (device limit, WL mode/quota/period) already live in the
PH3-01 `mgboost_plan_versions`/`mgboost_plan_durations` tables and stay
immutable there. This module adds the missing piece: what a given
plan-version/duration combination actually costs in a given payment channel,
kept in its own versioned, append-only catalog so a future price change
creates a new catalog version instead of mutating any row a past invoice or
subscription-term snapshot may reference.

Nothing in the legacy request, Stars, LK, Filin or Marzban paths reads these
tables yet -- purely additive, dormant until PH5-05/09 wire a real purchase
flow to it.
"""

from __future__ import annotations

import hashlib
import sqlite3
import time

from .account_schema import MIGRATION_ID as PARENT_MIGRATION_ID
from .account_schema import SCHEMA_CHECKSUM as PARENT_SCHEMA_CHECKSUM


MIGRATION_ID = "ph5_01_plan_catalog_v1"

_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS mgboost_price_catalog_versions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        channel TEXT NOT NULL CHECK(channel IN ('TELEGRAM_STARS','RUB')),
        catalog_version TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'ACTIVE' CHECK(status IN ('ACTIVE','RETIRED')),
        activated_at INTEGER NOT NULL,
        retired_at INTEGER,
        UNIQUE(channel, catalog_version),
        CHECK(status='ACTIVE' OR retired_at IS NOT NULL),
        CHECK(status='RETIRED' OR retired_at IS NULL)
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS ux_mgboost_price_catalog_active_channel
        ON mgboost_price_catalog_versions(channel)
        WHERE status='ACTIVE'
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mgboost_price_catalog_versions_no_delete
        BEFORE DELETE ON mgboost_price_catalog_versions
        BEGIN SELECT RAISE(ABORT, 'price catalog versions are never deleted'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mgboost_price_catalog_versions_identity_immutable
        BEFORE UPDATE OF channel, catalog_version, activated_at
        ON mgboost_price_catalog_versions
        BEGIN SELECT RAISE(ABORT, 'price catalog identity fields are immutable'); END
    """,
    """
    CREATE TABLE IF NOT EXISTS mgboost_plan_prices (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        catalog_version_id INTEGER NOT NULL,
        plan_version_id INTEGER NOT NULL,
        duration_id INTEGER NOT NULL,
        amount INTEGER NOT NULL CHECK(amount > 0),
        created_at INTEGER NOT NULL,
        UNIQUE(catalog_version_id, plan_version_id, duration_id),
        FOREIGN KEY(catalog_version_id)
            REFERENCES mgboost_price_catalog_versions(id) ON DELETE RESTRICT,
        FOREIGN KEY(plan_version_id) REFERENCES mgboost_plan_versions(id)
            ON DELETE RESTRICT,
        FOREIGN KEY(duration_id, plan_version_id)
            REFERENCES mgboost_plan_durations(id, plan_version_id) ON DELETE RESTRICT
    )
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mgboost_plan_prices_no_update
        BEFORE UPDATE ON mgboost_plan_prices
        BEGIN SELECT RAISE(ABORT, 'plan prices are immutable'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mgboost_plan_prices_no_delete
        BEFORE DELETE ON mgboost_plan_prices
        BEGIN SELECT RAISE(ABORT, 'plan prices are immutable'); END
    """,
)

SCHEMA_CHECKSUM = hashlib.sha256(
    "\n".join(statement.strip() for statement in _SCHEMA_STATEMENTS).encode("utf-8")
).hexdigest()

NEW_RUNTIME_TABLES = (
    "mgboost_price_catalog_versions",
    "mgboost_plan_prices",
)


def _verify(connection: sqlite3.Connection) -> None:
    required = {
        "mgboost_price_catalog_versions": {
            "id", "channel", "catalog_version", "status", "activated_at", "retired_at",
        },
        "mgboost_plan_prices": {
            "id", "catalog_version_id", "plan_version_id", "duration_id",
            "amount", "created_at",
        },
    }
    for table, columns in required.items():
        actual = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        if columns - actual:
            raise RuntimeError(f"PH5-01 incompatible table {table}: missing columns")
    objects = {row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE name LIKE 'ux_mgboost_price_catalog_%' "
        "OR name LIKE 'trg_mgboost_price_catalog_%' OR name LIKE 'trg_mgboost_plan_prices_%'"
    )}
    expected = {
        "ux_mgboost_price_catalog_active_channel",
        "trg_mgboost_price_catalog_versions_no_delete",
        "trg_mgboost_price_catalog_versions_identity_immutable",
        "trg_mgboost_plan_prices_no_update",
        "trg_mgboost_plan_prices_no_delete",
    }
    if not expected.issubset(objects):
        raise RuntimeError("PH5-01 schema indexes/triggers incomplete")


def apply_plan_catalog_schema(
    connection: sqlite3.Connection, *, now: int | None = None
) -> bool:
    """Apply PH5-01 transactionally. Return True only for the first apply."""
    connection.execute("PRAGMA foreign_keys=ON")
    timestamp = int(time.time()) if now is None else int(now)
    try:
        connection.execute("BEGIN IMMEDIATE")
        parent = connection.execute(
            "SELECT schema_checksum FROM mgboost_schema_migrations WHERE migration_id=?",
            (PARENT_MIGRATION_ID,),
        ).fetchone()
        if not parent or parent[0] != PARENT_SCHEMA_CHECKSUM:
            raise RuntimeError("PH5-01 requires exact PH3-01 schema")
        row = connection.execute(
            "SELECT schema_checksum FROM mgboost_schema_migrations WHERE migration_id=?",
            (MIGRATION_ID,),
        ).fetchone()
        if row:
            if row[0] != SCHEMA_CHECKSUM:
                raise RuntimeError("PH5-01 schema checksum mismatch")
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
