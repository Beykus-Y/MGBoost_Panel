"""Additive dormant PH6-07 schema: reconciliation cycles + drift evidence.

Two new tables, zero existing tables touched (the PH6-06 enforcement
machine's own tables are extended by NOTHING -- PH6-07 owns only the
continuous-convergence wrapper around that engine):

- `mgboost_wl_reconciliation_cycles` -- one row per orchestrated cycle
  (scheduler timer or manual invocation): when it started/finished, its
  outcome, the PH6-01 topology assertion result it ran under, the EXISTING
  engine's aggregate summary and the drift counters. This table IS the
  scheduler heartbeat and the operator's first read (no telemetry DB, no
  identifiers -- only counts, outcome classes and safe error classes).
- `mgboost_wl_reconciliation_drift` -- append-only evidence of every REAL
  post-terminal drift finding (never a per-scan row for converged children,
  so a steady-state fleet stays silent): the account/child scope, a typed
  drift class, and whether the EXISTING engine machinery was armed to repair
  it (REPAIR_QUEUED) or the finding was ambiguous/unverifiable and the
  account was only flagged ERROR_RECONCILE (FLAGGED -- never guessed at,
  never mutated away).

Rows are never updated or deleted; `finished_at`/`outcome` on a cycle row is
the single deliberate completion write performed once by the cycle itself.
"""

from __future__ import annotations

import hashlib
import sqlite3
import time

from .account_schema import MIGRATION_ID as ACCOUNT_MIGRATION_ID
from .account_schema import SCHEMA_CHECKSUM as ACCOUNT_SCHEMA_CHECKSUM
from .wl_enforcement_schema import MIGRATION_ID as ENFORCEMENT_MIGRATION_ID
from .wl_enforcement_schema import SCHEMA_CHECKSUM as ENFORCEMENT_SCHEMA_CHECKSUM


MIGRATION_ID = "ph6_07_wl_reconciliation_v1"

_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS mgboost_wl_reconciliation_cycles (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        trigger TEXT NOT NULL CHECK(trigger IN ('SCHEDULED','MANUAL')),
        started_at INTEGER NOT NULL,
        finished_at INTEGER,
        outcome TEXT
            CHECK(outcome IN ('OK','PARTIAL','BLOCKED_TOPOLOGY','ERROR')),
        config_version TEXT,
        topology_ok INTEGER CHECK(topology_ok IN (0,1)),
        engine_json TEXT,
        drift_detected INTEGER NOT NULL DEFAULT 0 CHECK(drift_detected >= 0),
        drift_repaired INTEGER NOT NULL DEFAULT 0 CHECK(drift_repaired >= 0),
        drift_flagged INTEGER NOT NULL DEFAULT 0 CHECK(drift_flagged >= 0),
        last_error_class TEXT,
        summary_json TEXT
    )
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mgboost_wl_recon_cycles_identity_immutable
        BEFORE UPDATE OF trigger, started_at ON mgboost_wl_reconciliation_cycles
        BEGIN SELECT RAISE(ABORT, 'reconciliation cycle identity is immutable'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mgboost_wl_recon_cycles_no_delete
        BEFORE DELETE ON mgboost_wl_reconciliation_cycles
        BEGIN SELECT RAISE(ABORT, 'reconciliation cycles are never deleted'); END
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_mgboost_wl_recon_cycles_started
        ON mgboost_wl_reconciliation_cycles(started_at)
    """,
    """
    CREATE TABLE IF NOT EXISTS mgboost_wl_reconciliation_drift (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        account_id INTEGER NOT NULL
            REFERENCES mgboost_accounts(id) ON DELETE RESTRICT,
        child_intent_id INTEGER,
        drift_class TEXT NOT NULL CHECK(drift_class IN (
            'WL_PRESENT_WHILE_EXCLUDED', 'WL_MISSING_WHILE_INCLUDED',
            'WL_UNEXPECTED_WHILE_INCLUDED', 'NON_WL_MEMBERSHIP_LOST',
            'REMOTE_MISSING', 'UUID_MISMATCH', 'REMOTE_UNREADABLE')),
        action TEXT NOT NULL CHECK(action IN ('REPAIR_QUEUED','FLAGGED')),
        epoch INTEGER,
        detected_at INTEGER NOT NULL
    )
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mgboost_wl_recon_drift_no_update
        BEFORE UPDATE ON mgboost_wl_reconciliation_drift
        BEGIN SELECT RAISE(ABORT, 'drift evidence is append-only'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mgboost_wl_recon_drift_no_delete
        BEFORE DELETE ON mgboost_wl_reconciliation_drift
        BEGIN SELECT RAISE(ABORT, 'drift evidence is append-only'); END
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_mgboost_wl_recon_drift_account
        ON mgboost_wl_reconciliation_drift(account_id, detected_at)
    """,
)

SCHEMA_CHECKSUM = hashlib.sha256(
    "\n".join(statement.strip() for statement in _SCHEMA_STATEMENTS).encode("utf-8")
).hexdigest()


def _verify(connection: sqlite3.Connection) -> None:
    tables = {row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name LIKE 'mgboost_wl_reconciliation_%'"
    )}
    if not {
        "mgboost_wl_reconciliation_cycles",
        "mgboost_wl_reconciliation_drift",
    }.issubset(tables):
        raise RuntimeError("PH6-07 reconciliation tables incomplete")
    triggers = {row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE name LIKE 'trg_mgboost_wl_recon_%'"
    )}
    if not {
        "trg_mgboost_wl_recon_cycles_identity_immutable",
        "trg_mgboost_wl_recon_cycles_no_delete",
        "trg_mgboost_wl_recon_drift_no_update",
        "trg_mgboost_wl_recon_drift_no_delete",
    }.issubset(triggers):
        raise RuntimeError("PH6-07 reconciliation triggers incomplete")


def apply_wl_reconciliation_schema(connection: sqlite3.Connection, *, now: int | None = None) -> bool:
    connection.execute("PRAGMA foreign_keys=ON")
    timestamp = int(time.time()) if now is None else int(now)
    try:
        connection.execute("BEGIN IMMEDIATE")
        for migration_id, checksum, label in (
            (ACCOUNT_MIGRATION_ID, ACCOUNT_SCHEMA_CHECKSUM, "PH3-01"),
            (ENFORCEMENT_MIGRATION_ID, ENFORCEMENT_SCHEMA_CHECKSUM, "PH6-06 enforcement"),
        ):
            row = connection.execute(
                "SELECT schema_checksum FROM mgboost_schema_migrations WHERE migration_id=?",
                (migration_id,),
            ).fetchone()
            if not row or row[0] != checksum:
                raise RuntimeError(f"PH6-07 requires exact {label} schema")
        row = connection.execute(
            "SELECT schema_checksum FROM mgboost_schema_migrations WHERE migration_id=?",
            (MIGRATION_ID,),
        ).fetchone()
        if row:
            if row[0] != SCHEMA_CHECKSUM:
                raise RuntimeError("PH6-07 schema checksum mismatch")
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
