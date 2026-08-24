"""Additive PH3-07 privacy-safe compatibility telemetry schema."""

from __future__ import annotations

import hashlib
import sqlite3
import time

from .device_slot_schema import MIGRATION_ID as SLOT_MIGRATION_ID
from .device_slot_schema import SCHEMA_CHECKSUM as SLOT_SCHEMA_CHECKSUM


MIGRATION_ID = "ph3_07_hwid_compat_telemetry_v1"

COMPATIBILITY_CATEGORIES = (
    "SUPPORTED_HWID_PRESENT",
    "HWID_MISSING",
    "HWID_UNSUPPORTED_OR_MALFORMED",
)

_CATEGORY_SQL = ",".join(f"'{value}'" for value in COMPATIBILITY_CATEGORIES)

_SCHEMA_STATEMENTS = (
    f"""
    CREATE TABLE IF NOT EXISTS mgboost_hwid_compat_subjects (
        day_start INTEGER NOT NULL CHECK(day_start >= 0 AND day_start % 86400 = 0),
        client_ref_version INTEGER NOT NULL DEFAULT 1
            CHECK(client_ref_version BETWEEN 1 AND 1000000),
        client_ref TEXT NOT NULL
            CHECK(length(client_ref)=76 AND client_ref LIKE 'hmac-sha256:%'),
        client_name TEXT NOT NULL CHECK(length(client_name) BETWEEN 1 AND 64),
        client_version TEXT NOT NULL CHECK(length(client_version) BETWEEN 1 AND 64),
        platform TEXT NOT NULL CHECK(length(platform) BETWEEN 1 AND 32),
        compatibility_category TEXT NOT NULL
            CHECK(compatibility_category IN ({_CATEGORY_SQL})),
        request_count INTEGER NOT NULL DEFAULT 1 CHECK(request_count > 0),
        first_seen INTEGER NOT NULL,
        last_seen INTEGER NOT NULL,
        PRIMARY KEY(
            day_start, client_ref_version, client_ref, client_name,
            client_version, platform, compatibility_category
        ),
        CHECK(first_seen <= last_seen)
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS mgboost_hwid_compat_daily (
        day_start INTEGER NOT NULL CHECK(day_start >= 0 AND day_start % 86400 = 0),
        client_name TEXT NOT NULL CHECK(length(client_name) BETWEEN 1 AND 64),
        client_version TEXT NOT NULL CHECK(length(client_version) BETWEEN 1 AND 64),
        platform TEXT NOT NULL CHECK(length(platform) BETWEEN 1 AND 32),
        compatibility_category TEXT NOT NULL
            CHECK(compatibility_category IN ({_CATEGORY_SQL})),
        request_count INTEGER NOT NULL CHECK(request_count > 0),
        correlated_subject_count INTEGER NOT NULL
            CHECK(correlated_subject_count > 0),
        repeat_request_count INTEGER NOT NULL DEFAULT 0
            CHECK(repeat_request_count >= 0),
        first_seen INTEGER NOT NULL,
        last_seen INTEGER NOT NULL,
        PRIMARY KEY(
            day_start, client_name, client_version, platform,
            compatibility_category
        ),
        CHECK(request_count = correlated_subject_count + repeat_request_count),
        CHECK(first_seen <= last_seen)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_mgboost_hwid_compat_subjects_retention
        ON mgboost_hwid_compat_subjects(day_start)
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_mgboost_hwid_compat_daily_retention
        ON mgboost_hwid_compat_daily(day_start)
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mgboost_hwid_compat_subject_monotonic
        BEFORE UPDATE ON mgboost_hwid_compat_subjects
        WHEN NEW.day_start != OLD.day_start
          OR NEW.client_ref_version != OLD.client_ref_version
          OR NEW.client_ref != OLD.client_ref
          OR NEW.client_name != OLD.client_name
          OR NEW.client_version != OLD.client_version
          OR NEW.platform != OLD.platform
          OR NEW.compatibility_category != OLD.compatibility_category
          OR NEW.request_count < OLD.request_count
          OR NEW.first_seen != OLD.first_seen
          OR NEW.last_seen < OLD.last_seen
        BEGIN SELECT RAISE(ABORT, 'compatibility subject must be monotonic'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mgboost_hwid_compat_daily_monotonic
        BEFORE UPDATE ON mgboost_hwid_compat_daily
        WHEN NEW.day_start != OLD.day_start
          OR NEW.client_name != OLD.client_name
          OR NEW.client_version != OLD.client_version
          OR NEW.platform != OLD.platform
          OR NEW.compatibility_category != OLD.compatibility_category
          OR NEW.request_count < OLD.request_count
          OR NEW.correlated_subject_count < OLD.correlated_subject_count
          OR NEW.repeat_request_count < OLD.repeat_request_count
          OR NEW.first_seen != OLD.first_seen
          OR NEW.last_seen < OLD.last_seen
        BEGIN SELECT RAISE(ABORT, 'compatibility rollup must be monotonic'); END
    """,
)

SCHEMA_CHECKSUM = hashlib.sha256(
    "\n".join(statement.strip() for statement in _SCHEMA_STATEMENTS).encode("utf-8")
).hexdigest()

NEW_RUNTIME_TABLES = (
    "mgboost_hwid_compat_subjects",
    "mgboost_hwid_compat_daily",
)

_REQUIRED_COLUMNS = {
    "mgboost_hwid_compat_subjects": {
        "day_start", "client_ref_version", "client_ref", "client_name",
        "client_version", "platform", "compatibility_category",
        "request_count", "first_seen", "last_seen",
    },
    "mgboost_hwid_compat_daily": {
        "day_start", "client_name", "client_version", "platform",
        "compatibility_category", "request_count", "correlated_subject_count",
        "repeat_request_count", "first_seen", "last_seen",
    },
}

_REQUIRED_OBJECTS = {
    "ix_mgboost_hwid_compat_subjects_retention",
    "ix_mgboost_hwid_compat_daily_retention",
    "trg_mgboost_hwid_compat_subject_monotonic",
    "trg_mgboost_hwid_compat_daily_monotonic",
}


def _verify_schema(connection: sqlite3.Connection) -> None:
    for table, required in _REQUIRED_COLUMNS.items():
        actual = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        if required - actual:
            raise RuntimeError(f"PH3-07 incompatible table {table}: missing columns")
    objects = {
        row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE name LIKE 'ix_mgboost_hwid_compat_%' "
            "OR name LIKE 'trg_mgboost_hwid_compat_%'"
        )
    }
    if not _REQUIRED_OBJECTS.issubset(objects):
        raise RuntimeError("PH3-07 schema indexes/triggers incomplete")


def apply_compat_telemetry_schema(
    connection: sqlite3.Connection, *, now: int | None = None
) -> bool:
    """Apply PH3-07 transactionally after the exact PH3-02 schema."""
    applied_at = int(time.time()) if now is None else int(now)
    try:
        connection.execute("BEGIN IMMEDIATE")
        parent = connection.execute(
            "SELECT schema_checksum FROM mgboost_schema_migrations WHERE migration_id=?",
            (SLOT_MIGRATION_ID,),
        ).fetchone()
        if parent is None or parent[0] != SLOT_SCHEMA_CHECKSUM:
            raise RuntimeError("PH3-07 requires exact PH3-02 schema")
        existing = connection.execute(
            "SELECT schema_checksum FROM mgboost_schema_migrations WHERE migration_id=?",
            (MIGRATION_ID,),
        ).fetchone()
        if existing is not None:
            if existing[0] != SCHEMA_CHECKSUM:
                raise RuntimeError("PH3-07 schema checksum mismatch")
            _verify_schema(connection)
            connection.commit()
            return False
        for statement in _SCHEMA_STATEMENTS:
            connection.execute(statement)
        _verify_schema(connection)
        connection.execute(
            "INSERT INTO mgboost_schema_migrations "
            "(migration_id,schema_checksum,applied_at) VALUES (?,?,?)",
            (MIGRATION_ID, SCHEMA_CHECKSUM, applied_at),
        )
        connection.commit()
        return True
    except Exception:
        connection.rollback()
        raise
