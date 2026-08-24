"""Additive durable worker/reconciliation state for PH3-03 child provisioning."""

from __future__ import annotations

import hashlib
import sqlite3
import time

from .child_provisioning_schema import MIGRATION_ID as CHILD_MIGRATION_ID
from .child_provisioning_schema import SCHEMA_CHECKSUM as CHILD_SCHEMA_CHECKSUM


MIGRATION_ID = "ph3_03_child_workflow_v1"

_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS mgboost_child_workflow_state (
        outbox_id INTEGER PRIMARY KEY,
        account_id INTEGER NOT NULL,
        child_intent_id INTEGER NOT NULL UNIQUE,
        reconcile_state TEXT NOT NULL DEFAULT 'PENDING'
            CHECK(reconcile_state IN (
                'PENDING','IN_SYNC','REMOTE_ABSENT','REMOTE_MATCH',
                'REMOTE_MISSING','REMOTE_MISMATCH','REMOTE_AMBIGUOUS',
                'UNAVAILABLE','MANUAL_REVIEW'
            )),
        failure_count INTEGER NOT NULL DEFAULT 0 CHECK(failure_count >= 0),
        reconcile_count INTEGER NOT NULL DEFAULT 0 CHECK(reconcile_count >= 0),
        next_check_at INTEGER NOT NULL,
        lease_owner TEXT,
        lease_expires_at INTEGER,
        last_error_class TEXT,
        last_remote_effect_verifier TEXT,
        last_checked_at INTEGER,
        last_success_at INTEGER,
        manual_review_reason TEXT,
        manual_review_at INTEGER,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        row_version INTEGER NOT NULL DEFAULT 1 CHECK(row_version > 0),
        UNIQUE(outbox_id, account_id),
        CHECK((lease_owner IS NULL AND lease_expires_at IS NULL)
              OR (lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)),
        CHECK((reconcile_state='MANUAL_REVIEW' AND manual_review_reason IS NOT NULL
               AND manual_review_at IS NOT NULL)
              OR reconcile_state!='MANUAL_REVIEW'),
        FOREIGN KEY(outbox_id, account_id)
            REFERENCES mgboost_outbox(id, account_id) ON DELETE RESTRICT,
        FOREIGN KEY(child_intent_id, account_id)
            REFERENCES mgboost_child_user_intents(id, account_id) ON DELETE RESTRICT
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_mgboost_child_workflow_ready
        ON mgboost_child_workflow_state(reconcile_state, next_check_at, outbox_id)
    """,
    """
    CREATE TABLE IF NOT EXISTS mgboost_child_reconciliation_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        outbox_id INTEGER NOT NULL,
        account_id INTEGER NOT NULL,
        event_type TEXT NOT NULL CHECK(event_type IN (
            'CHECK_STARTED','MATCHED','ABSENT','MISMATCH','AMBIGUOUS',
            'UNAVAILABLE','STALE_LEASE_RECOVERED','MANUAL_REVIEW','RECOVERED'
        )),
        safe_reason TEXT,
        remote_effect_verifier TEXT,
        worker_id TEXT,
        created_at INTEGER NOT NULL,
        FOREIGN KEY(outbox_id, account_id)
            REFERENCES mgboost_child_workflow_state(outbox_id, account_id)
            ON DELETE RESTRICT
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_mgboost_child_reconcile_events_operation
        ON mgboost_child_reconciliation_events(outbox_id, created_at, id)
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mgboost_child_workflow_identity_immutable
        BEFORE UPDATE OF outbox_id,account_id,child_intent_id,created_at
        ON mgboost_child_workflow_state
        BEGIN SELECT RAISE(ABORT, 'child workflow identity is immutable'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mgboost_child_workflow_no_delete
        BEFORE DELETE ON mgboost_child_workflow_state
        BEGIN SELECT RAISE(ABORT, 'child workflow history is immutable'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mgboost_child_reconcile_events_no_update
        BEFORE UPDATE ON mgboost_child_reconciliation_events
        BEGIN SELECT RAISE(ABORT, 'child reconciliation events are immutable'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mgboost_child_reconcile_events_no_delete
        BEFORE DELETE ON mgboost_child_reconciliation_events
        BEGIN SELECT RAISE(ABORT, 'child reconciliation events are immutable'); END
    """,
)

SCHEMA_CHECKSUM = hashlib.sha256(
    "\n".join(statement.strip() for statement in _SCHEMA_STATEMENTS).encode("utf-8")
).hexdigest()

NEW_RUNTIME_TABLES = (
    "mgboost_child_workflow_state",
    "mgboost_child_reconciliation_events",
)


def _verify(connection: sqlite3.Connection) -> None:
    required = {
        "mgboost_child_workflow_state": {
            "outbox_id", "account_id", "child_intent_id", "reconcile_state",
            "failure_count", "reconcile_count", "next_check_at", "lease_owner",
            "lease_expires_at", "last_error_class", "last_remote_effect_verifier",
            "last_checked_at", "last_success_at", "manual_review_reason",
            "manual_review_at", "row_version",
        },
        "mgboost_child_reconciliation_events": {
            "id", "outbox_id", "account_id", "event_type", "safe_reason",
            "remote_effect_verifier", "worker_id", "created_at",
        },
    }
    for table, columns in required.items():
        actual = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        if columns - actual:
            raise RuntimeError(f"PH3-03 workflow table {table} is incompatible")


def apply_child_workflow_schema(
    connection: sqlite3.Connection, *, now: int | None = None
) -> bool:
    connection.execute("PRAGMA foreign_keys=ON")
    timestamp = int(time.time()) if now is None else int(now)
    try:
        connection.execute("BEGIN IMMEDIATE")
        parent = connection.execute(
            "SELECT schema_checksum FROM mgboost_schema_migrations WHERE migration_id=?",
            (CHILD_MIGRATION_ID,),
        ).fetchone()
        if not parent or parent[0] != CHILD_SCHEMA_CHECKSUM:
            raise RuntimeError("PH3-03 workflow requires exact child prerequisite schema")
        existing = connection.execute(
            "SELECT schema_checksum FROM mgboost_schema_migrations WHERE migration_id=?",
            (MIGRATION_ID,),
        ).fetchone()
        if existing:
            if existing[0] != SCHEMA_CHECKSUM:
                raise RuntimeError("PH3-03 workflow schema checksum mismatch")
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
