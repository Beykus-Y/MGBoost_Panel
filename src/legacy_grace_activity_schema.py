"""Additive PH4-05 privacy-safe grace-period activity counters.

Stores only: which account (an internal integer id already stored
unguarded by every other Phase 3/4 table -- `mgboost_migration_bindings`,
`mgboost_legacy_bridge_bindings`, etc. -- never PII by itself), which
channel (`LEGACY` or `OPAQUE`), a day bucket and a monotonic request count.
Never the raw legacy/opaque token, full subscription URL, UUID, full HWID,
cookies/auth header or bearer path -- mirrors PH3-07's own privacy
discipline, one level simpler since no per-client pseudonym is needed here.
"""

from __future__ import annotations

import hashlib
import sqlite3
import time

from .account_schema import MIGRATION_ID as ACCOUNT_MIGRATION_ID
from .account_schema import SCHEMA_CHECKSUM as ACCOUNT_SCHEMA_CHECKSUM


MIGRATION_ID = "ph4_05_legacy_grace_activity_v1"

CHANNELS = ("LEGACY", "OPAQUE")
_CHANNEL_SQL = ",".join(f"'{value}'" for value in CHANNELS)

_SCHEMA_STATEMENTS = (
    f"""
    CREATE TABLE IF NOT EXISTS mgboost_legacy_grace_activity_daily (
        day_start INTEGER NOT NULL CHECK(day_start >= 0 AND day_start % 86400 = 0),
        account_id INTEGER NOT NULL,
        channel TEXT NOT NULL CHECK(channel IN ({_CHANNEL_SQL})),
        request_count INTEGER NOT NULL CHECK(request_count > 0),
        first_seen INTEGER NOT NULL,
        last_seen INTEGER NOT NULL,
        PRIMARY KEY(day_start, account_id, channel),
        CHECK(first_seen <= last_seen),
        FOREIGN KEY(account_id) REFERENCES mgboost_accounts(id) ON DELETE RESTRICT
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_mgboost_legacy_grace_activity_account
        ON mgboost_legacy_grace_activity_daily(account_id, channel, day_start)
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_mgboost_legacy_grace_activity_retention
        ON mgboost_legacy_grace_activity_daily(day_start)
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mgboost_legacy_grace_activity_monotonic
        BEFORE UPDATE ON mgboost_legacy_grace_activity_daily
        WHEN NEW.day_start != OLD.day_start
          OR NEW.account_id != OLD.account_id
          OR NEW.channel != OLD.channel
          OR NEW.request_count < OLD.request_count
          OR NEW.first_seen != OLD.first_seen
          OR NEW.last_seen < OLD.last_seen
        BEGIN SELECT RAISE(ABORT, 'grace activity counter must be monotonic'); END
    """,
)

SCHEMA_CHECKSUM = hashlib.sha256(
    "\n".join(statement.strip() for statement in _SCHEMA_STATEMENTS).encode("utf-8")
).hexdigest()

NEW_RUNTIME_TABLES = ("mgboost_legacy_grace_activity_daily",)

_REQUIRED_COLUMNS = {
    "day_start", "account_id", "channel", "request_count", "first_seen", "last_seen",
}

_REQUIRED_OBJECTS = {
    "ix_mgboost_legacy_grace_activity_account",
    "ix_mgboost_legacy_grace_activity_retention",
    "trg_mgboost_legacy_grace_activity_monotonic",
}


def _verify(connection: sqlite3.Connection) -> None:
    actual = {
        row[1] for row in connection.execute(
            "PRAGMA table_info(mgboost_legacy_grace_activity_daily)"
        )
    }
    if _REQUIRED_COLUMNS - actual:
        raise RuntimeError("PH4-05 grace activity table is incompatible")
    objects = {
        row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE name LIKE 'ix_mgboost_legacy_grace_activity_%' "
            "OR name LIKE 'trg_mgboost_legacy_grace_activity_%'"
        )
    }
    if not _REQUIRED_OBJECTS.issubset(objects):
        raise RuntimeError("PH4-05 grace activity schema objects incomplete")


def apply_legacy_grace_activity_schema(
    connection: sqlite3.Connection, *, now: int | None = None
) -> bool:
    connection.execute("PRAGMA foreign_keys=ON")
    applied_at = int(time.time()) if now is None else int(now)
    try:
        connection.execute("BEGIN IMMEDIATE")
        parent = connection.execute(
            "SELECT schema_checksum FROM mgboost_schema_migrations WHERE migration_id=?",
            (ACCOUNT_MIGRATION_ID,),
        ).fetchone()
        if not parent or parent[0] != ACCOUNT_SCHEMA_CHECKSUM:
            raise RuntimeError("PH4-05 grace activity schema requires the exact PH3-01 schema")
        existing = connection.execute(
            "SELECT schema_checksum FROM mgboost_schema_migrations WHERE migration_id=?",
            (MIGRATION_ID,),
        ).fetchone()
        if existing is not None:
            if existing[0] != SCHEMA_CHECKSUM:
                raise RuntimeError("PH4-05 grace activity schema checksum mismatch")
            _verify(connection)
            connection.commit()
            return False
        for statement in _SCHEMA_STATEMENTS:
            connection.execute(statement)
        _verify(connection)
        connection.execute(
            "INSERT INTO mgboost_schema_migrations (migration_id,schema_checksum,applied_at) "
            "VALUES (?,?,?)",
            (MIGRATION_ID, SCHEMA_CHECKSUM, applied_at),
        )
        connection.commit()
        return True
    except Exception:
        connection.rollback()
        raise
