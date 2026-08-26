"""Additive dormant PH4-05 legacy grace-period schema.

One durable row per account -- an account can only ever start its shared
legacy-URL grace period exactly once (`UNIQUE(account_id)`); a genuine
restart/reset is never possible, matching this project's own
`LEGACY_REVOKED can never transition again` (PH4-02) precedent. The 14-day
duration itself (OPD-09/DL-023, fixed policy) is enforced by a CHECK
constraint tying `original_end_at` to `started_at`, not by application code
alone -- a future code change cannot silently ship a different default.
`current_end_at` starts equal to `original_end_at` and can only ever move
forward (a DB trigger rejects any decrease), and only together with an
appended, immutable `mgboost_legacy_grace_events` row -- there is no silent
extension and no silent shortening/restart of an account's clock.

This schema does not itself gate, deny or revoke any legacy/opaque request --
no route imports it yet. PH4-06 (the actual revoke) is a separate, later
phase and is explicitly not implemented here.
"""

from __future__ import annotations

import hashlib
import sqlite3
import time

from .account_schema import MIGRATION_ID as ACCOUNT_MIGRATION_ID
from .account_schema import SCHEMA_CHECKSUM as ACCOUNT_SCHEMA_CHECKSUM


MIGRATION_ID = "ph4_05_legacy_grace_period_v1"

# OPD-09/DL-023: fixed policy, 14 days -- not environment-configurable.
GRACE_PERIOD_SECONDS = 14 * 86400

_SCHEMA_STATEMENTS = (
    f"""
    CREATE TABLE IF NOT EXISTS mgboost_legacy_grace_periods (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        account_id INTEGER NOT NULL UNIQUE,
        cohort_ref TEXT NOT NULL CHECK(length(cohort_ref) BETWEEN 1 AND 128),
        started_at INTEGER NOT NULL,
        original_end_at INTEGER NOT NULL
            CHECK(original_end_at = started_at + {GRACE_PERIOD_SECONDS}),
        current_end_at INTEGER NOT NULL CHECK(current_end_at >= original_end_at),
        revision INTEGER NOT NULL DEFAULT 1 CHECK(revision > 0),
        idempotency_key_hash TEXT NOT NULL UNIQUE
            CHECK(length(idempotency_key_hash)=64
                  AND idempotency_key_hash NOT GLOB '*[^0-9a-f]*'),
        request_hash TEXT NOT NULL
            CHECK(length(request_hash)=64 AND request_hash NOT GLOB '*[^0-9a-f]*'),
        actor_ref TEXT NOT NULL CHECK(length(actor_ref) BETWEEN 1 AND 256),
        reason TEXT NOT NULL CHECK(length(reason) BETWEEN 3 AND 300),
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        row_version INTEGER NOT NULL DEFAULT 1 CHECK(row_version > 0),
        UNIQUE(id, account_id),
        FOREIGN KEY(account_id) REFERENCES mgboost_accounts(id) ON DELETE RESTRICT
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_mgboost_legacy_grace_periods_end
        ON mgboost_legacy_grace_periods(current_end_at)
    """,
    """
    CREATE TABLE IF NOT EXISTS mgboost_legacy_grace_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        grace_period_id INTEGER NOT NULL,
        account_id INTEGER NOT NULL,
        event_type TEXT NOT NULL CHECK(event_type IN ('STARTED','EXTENDED')),
        from_end_at INTEGER,
        to_end_at INTEGER NOT NULL,
        actor_ref TEXT NOT NULL CHECK(length(actor_ref) BETWEEN 1 AND 256),
        reason TEXT NOT NULL CHECK(length(reason) BETWEEN 3 AND 300),
        evidence_ref TEXT CHECK(evidence_ref IS NULL OR length(evidence_ref) BETWEEN 1 AND 256),
        created_at INTEGER NOT NULL,
        FOREIGN KEY(grace_period_id, account_id)
            REFERENCES mgboost_legacy_grace_periods(id, account_id) ON DELETE RESTRICT
    )
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_legacy_grace_periods_identity_immutable
        BEFORE UPDATE OF account_id,cohort_ref,started_at,original_end_at,
                         idempotency_key_hash,request_hash,actor_ref,reason,created_at
        ON mgboost_legacy_grace_periods
        BEGIN SELECT RAISE(ABORT, 'legacy grace period identity is immutable'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_legacy_grace_periods_no_delete
        BEFORE DELETE ON mgboost_legacy_grace_periods
        BEGIN SELECT RAISE(ABORT, 'legacy grace period history is immutable'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_legacy_grace_periods_end_monotonic
        BEFORE UPDATE OF current_end_at ON mgboost_legacy_grace_periods
        WHEN NEW.current_end_at < OLD.current_end_at
        BEGIN SELECT RAISE(ABORT,
            'legacy grace current_end_at can only be extended, never shortened'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_legacy_grace_events_no_update
        BEFORE UPDATE ON mgboost_legacy_grace_events
        BEGIN SELECT RAISE(ABORT, 'legacy grace events are immutable'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_legacy_grace_events_no_delete
        BEFORE DELETE ON mgboost_legacy_grace_events
        BEGIN SELECT RAISE(ABORT, 'legacy grace events are immutable'); END
    """,
)

SCHEMA_CHECKSUM = hashlib.sha256(
    "\n".join(statement.strip() for statement in _SCHEMA_STATEMENTS).encode("utf-8")
).hexdigest()

NEW_RUNTIME_TABLES = (
    "mgboost_legacy_grace_periods",
    "mgboost_legacy_grace_events",
)


def _verify(connection: sqlite3.Connection) -> None:
    required = {
        "mgboost_legacy_grace_periods": {
            "id", "account_id", "cohort_ref", "started_at", "original_end_at",
            "current_end_at", "revision", "idempotency_key_hash", "request_hash",
            "actor_ref", "reason", "created_at", "updated_at", "row_version",
        },
        "mgboost_legacy_grace_events": {
            "id", "grace_period_id", "account_id", "event_type", "from_end_at",
            "to_end_at", "actor_ref", "reason", "evidence_ref", "created_at",
        },
    }
    for table, columns in required.items():
        actual = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        if columns - actual:
            raise RuntimeError(f"PH4-05 legacy grace table {table} is incompatible")
    objects = {row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE name LIKE 'trg_legacy_grace%' "
        "OR name LIKE 'ix_mgboost_legacy_grace%'"
    )}
    expected = {
        "trg_legacy_grace_periods_identity_immutable", "trg_legacy_grace_periods_no_delete",
        "trg_legacy_grace_periods_end_monotonic", "trg_legacy_grace_events_no_update",
        "trg_legacy_grace_events_no_delete", "ix_mgboost_legacy_grace_periods_end",
    }
    if not expected.issubset(objects):
        raise RuntimeError("PH4-05 legacy grace schema objects incomplete")


def apply_legacy_grace_schema(connection: sqlite3.Connection, *, now: int | None = None) -> bool:
    connection.execute("PRAGMA foreign_keys=ON")
    timestamp = int(time.time()) if now is None else int(now)
    try:
        connection.execute("BEGIN IMMEDIATE")
        parent = connection.execute(
            "SELECT schema_checksum FROM mgboost_schema_migrations WHERE migration_id=?",
            (ACCOUNT_MIGRATION_ID,),
        ).fetchone()
        if not parent or parent[0] != ACCOUNT_SCHEMA_CHECKSUM:
            raise RuntimeError("PH4-05 legacy grace schema requires the exact PH3-01 schema")
        existing = connection.execute(
            "SELECT schema_checksum FROM mgboost_schema_migrations WHERE migration_id=?",
            (MIGRATION_ID,),
        ).fetchone()
        if existing:
            if existing[0] != SCHEMA_CHECKSUM:
                raise RuntimeError("PH4-05 legacy grace schema checksum mismatch")
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
