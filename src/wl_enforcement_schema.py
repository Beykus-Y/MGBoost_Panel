"""Additive dormant PH6-06 schema: exact inbound-only WL enforcement machine.

Three new tables, zero existing tables touched:

- `mgboost_wl_enforcement_states` -- ONE row per parent account. Local DB is
  the source of the desired WL state; the row records the decision epoch,
  the machine state
  (`ACTIVE -> DISABLE_PENDING -> DISABLED`,
   `DISABLED -> ENABLE_PENDING -> ACTIVE`,
   mismatch/failure -> `ERROR_RECONCILE`) and which period produced the
  last decision. `epoch` is monotonic (trigger-enforced): every desired-
  direction change opens a fresh epoch, and operations of a superseded
  epoch can never be dispatched again.
- `mgboost_wl_enforcement_ops` -- durable per-(epoch, child) operation rows
  mirroring the established outbox shape (`mgboost_parent_sync_operations`
  / `mgboost_child_lifecycle_operations`): lease claim, bounded attempts,
  request hash, frozen manifest, append-only attempt evidence.
- `mgboost_wl_enforcement_events` -- fully immutable attempt event log.

The manifest freeze column deserves emphasis: the first worker to observe a
child in a pending op durably writes {"baseline_full", "target"} for that
op and later attempts can never overwrite it (`WHERE manifest_json IS
NULL` at the store layer). A crash after the remote mutation but before
the local ACK therefore replays against the SAME recorded target instead
of re-deriving one from possibly-drifted remote state.
"""

from __future__ import annotations

import hashlib
import sqlite3
import time

from .account_schema import MIGRATION_ID as ACCOUNT_MIGRATION_ID
from .account_schema import SCHEMA_CHECKSUM as ACCOUNT_SCHEMA_CHECKSUM
from .child_provisioning_schema import MIGRATION_ID as CHILD_MIGRATION_ID
from .child_provisioning_schema import SCHEMA_CHECKSUM as CHILD_SCHEMA_CHECKSUM
from .wl_usage_ledger_schema import MIGRATION_ID as LEDGER_MIGRATION_ID
from .wl_usage_ledger_schema import SCHEMA_CHECKSUM as LEDGER_SCHEMA_CHECKSUM


MIGRATION_ID = "ph6_06_wl_enforcement_v1"

_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS mgboost_wl_enforcement_states (
        account_id INTEGER PRIMARY KEY
            REFERENCES mgboost_accounts(id) ON DELETE RESTRICT,
        epoch INTEGER NOT NULL DEFAULT 0 CHECK(epoch >= 0),
        state TEXT NOT NULL DEFAULT 'ACTIVE'
            CHECK(state IN ('ACTIVE','DISABLE_PENDING','DISABLED',
                            'ENABLE_PENDING','ERROR_RECONCILE')),
        last_direction TEXT NOT NULL DEFAULT 'INCLUDED'
            CHECK(last_direction IN ('EXCLUDED','INCLUDED')),
        wl_period_id INTEGER,
        decision_source TEXT NOT NULL DEFAULT 'INITIAL'
            CHECK(decision_source IN ('INITIAL','QUOTA_EXCEEDED',
                                      'QUOTA_AVAILABLE')),
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        row_version INTEGER NOT NULL DEFAULT 1 CHECK(row_version > 0)
    )
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mgboost_wl_states_epoch_monotonic
        BEFORE UPDATE ON mgboost_wl_enforcement_states
        WHEN NEW.epoch < OLD.epoch
        BEGIN SELECT RAISE(ABORT, 'WL enforcement epoch must never decrease'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mgboost_wl_states_identity_immutable
        BEFORE UPDATE OF account_id, created_at ON mgboost_wl_enforcement_states
        BEGIN SELECT RAISE(ABORT, 'WL enforcement state identity is immutable'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mgboost_wl_states_no_delete
        BEFORE DELETE ON mgboost_wl_enforcement_states
        BEGIN SELECT RAISE(ABORT, 'WL enforcement states are never deleted'); END
    """,
    """
    CREATE TABLE IF NOT EXISTS mgboost_wl_enforcement_ops (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        account_id INTEGER NOT NULL,
        epoch INTEGER NOT NULL CHECK(epoch >= 1),
        child_intent_id INTEGER NOT NULL,
        direction TEXT NOT NULL CHECK(direction IN ('EXCLUDED','INCLUDED')),
        operation_id TEXT NOT NULL UNIQUE
            CHECK(substr(operation_id,1,4)='wla_'
                  AND length(operation_id)=30
                  AND substr(operation_id,5) NOT GLOB '*[^a-z2-7]*'),
        state TEXT NOT NULL DEFAULT 'PENDING'
            CHECK(state IN ('PENDING','IN_FLIGHT','RETRY','APPLIED','ERROR')),
        manifest_json TEXT,
        payload_json TEXT NOT NULL,
        request_hash TEXT NOT NULL
            CHECK(length(request_hash)=64 AND request_hash NOT GLOB '*[^0-9a-f]*'),
        attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts BETWEEN 0 AND 8),
        next_attempt_at INTEGER NOT NULL,
        lease_owner TEXT,
        lease_expires_at INTEGER,
        last_error_class TEXT,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        row_version INTEGER NOT NULL DEFAULT 1 CHECK(row_version > 0),
        UNIQUE(account_id, epoch, child_intent_id),
        CHECK((state='IN_FLIGHT' AND lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)
              OR (state!='IN_FLIGHT' AND lease_owner IS NULL AND lease_expires_at IS NULL)),
        FOREIGN KEY(account_id) REFERENCES mgboost_accounts(id) ON DELETE RESTRICT,
        FOREIGN KEY(child_intent_id, account_id)
            REFERENCES mgboost_child_user_intents(id, account_id) ON DELETE RESTRICT
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_mgboost_wl_ops_account_epoch
        ON mgboost_wl_enforcement_ops(account_id, epoch, state)
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mgboost_wl_ops_identity_immutable
        BEFORE UPDATE OF account_id, epoch, child_intent_id, direction, operation_id,
            created_at ON mgboost_wl_enforcement_ops
        BEGIN SELECT RAISE(ABORT, 'WL enforcement op identity is immutable'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mgboost_wl_ops_no_delete
        BEFORE DELETE ON mgboost_wl_enforcement_ops
        BEGIN SELECT RAISE(ABORT, 'WL enforcement ops are never deleted'); END
    """,
    """
    CREATE TABLE IF NOT EXISTS mgboost_wl_enforcement_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        op_row_id INTEGER NOT NULL
            REFERENCES mgboost_wl_enforcement_ops(id) ON DELETE RESTRICT,
        account_id INTEGER NOT NULL,
        epoch INTEGER NOT NULL,
        attempt_no INTEGER NOT NULL CHECK(attempt_no > 0),
        event_type TEXT NOT NULL
            CHECK(event_type IN ('STARTED','SUCCEEDED','FAILED','SUPERSEDED',
                                 'MANIFEST_FROZEN','VERIFY_FAILED')),
        outcome TEXT,
        safe_error_class TEXT,
        created_at INTEGER NOT NULL,
        FOREIGN KEY(op_row_id) REFERENCES mgboost_wl_enforcement_ops(id) ON DELETE RESTRICT
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_mgboost_wl_events_op
        ON mgboost_wl_enforcement_events(op_row_id, id)
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mgboost_wl_events_no_update
        BEFORE UPDATE ON mgboost_wl_enforcement_events
        BEGIN SELECT RAISE(ABORT, 'WL enforcement events are append-only'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mgboost_wl_events_no_delete
        BEFORE DELETE ON mgboost_wl_enforcement_events
        BEGIN SELECT RAISE(ABORT, 'WL enforcement events are append-only'); END
    """,
)

SCHEMA_CHECKSUM = hashlib.sha256(
    "\n".join(statement.strip() for statement in _SCHEMA_STATEMENTS).encode("utf-8")
).hexdigest()


def _verify(connection: sqlite3.Connection) -> None:
    tables = {row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'mgboost_wl_enforcement_%'"
    )}
    expected_tables = {
        "mgboost_wl_enforcement_states",
        "mgboost_wl_enforcement_ops",
        "mgboost_wl_enforcement_events",
    }
    if not expected_tables.issubset(tables):
        raise RuntimeError("PH6-06 WL enforcement tables incomplete")
    triggers = {row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE name LIKE 'trg_mgboost_wl_%'"
    )}
    expected_triggers = {
        "trg_mgboost_wl_states_epoch_monotonic",
        "trg_mgboost_wl_states_identity_immutable",
        "trg_mgboost_wl_states_no_delete",
        "trg_mgboost_wl_ops_identity_immutable",
        "trg_mgboost_wl_ops_no_delete",
        "trg_mgboost_wl_events_no_update",
        "trg_mgboost_wl_events_no_delete",
    }
    if not expected_triggers.issubset(triggers):
        raise RuntimeError("PH6-06 WL enforcement triggers incomplete")


def apply_wl_enforcement_schema(connection: sqlite3.Connection, *, now: int | None = None) -> bool:
    connection.execute("PRAGMA foreign_keys=ON")
    timestamp = int(time.time()) if now is None else int(now)
    try:
        connection.execute("BEGIN IMMEDIATE")
        for migration_id, checksum, label in (
            (ACCOUNT_MIGRATION_ID, ACCOUNT_SCHEMA_CHECKSUM, "PH3-01"),
            (CHILD_MIGRATION_ID, CHILD_SCHEMA_CHECKSUM, "PH3-03 prerequisite"),
            (LEDGER_MIGRATION_ID, LEDGER_SCHEMA_CHECKSUM, "PH6-03 usage ledger"),
        ):
            row = connection.execute(
                "SELECT schema_checksum FROM mgboost_schema_migrations WHERE migration_id=?",
                (migration_id,),
            ).fetchone()
            if not row or row[0] != checksum:
                raise RuntimeError(f"PH6-06 requires exact {label} schema")
        row = connection.execute(
            "SELECT schema_checksum FROM mgboost_schema_migrations WHERE migration_id=?",
            (MIGRATION_ID,),
        ).fetchone()
        if row:
            if row[0] != SCHEMA_CHECKSUM:
                raise RuntimeError("PH6-06 schema checksum mismatch")
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
