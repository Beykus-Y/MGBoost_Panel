"""Additive PH3-08 v2: durable post-ACK stabilization + periodic drift audit.

Root cause: a `child.user.state.sync` ACK ("APPLIED") only ever meant "this
attempt's authoritative reread matched the target at that instant" -- it was
silently treated as "remote will stay this way forever". Production evidence
showed a race with Marzban's own background status scheduler flipping the
child back to `expired` a few seconds *after* a successful, verified sync.
Nothing ever looked again.

This migration adds exactly two durable primitives, no new business logic:

  * `verify_after` on the existing operations ledger -- when an op is next
    due for an authoritative re-observation. Sync `acknowledge()` schedules
    the first (short, "stabilization") check; each later confirmed-stable
    check reschedules a longer periodic ("drift audit") one. No new state
    enum: a drift finding demotes the op straight back to the existing
    `RETRY` state so it re-enters the exact same claim()/dispatch()/
    acknowledge() pipeline that already exists, including its staleness
    fencing and bounded-attempts exhaustion into `ERROR`.

  * `mgboost_convergence_sweep_cursor` -- a durable per-account due-cursor so
    a periodic worker tick can independently recompute + converge *every*
    account's entitlement, closing the crash window between any entitlement
    mutation (ADMIN_GRANT, Stars, MANUAL_RUB, signup, renewal, legacy
    transition, ...) committing and that mutation's caller remembering to
    invoke sync. This sweep re-derives desired state from the authoritative
    `mgboost_accounts`/`mgboost_subscriptions` rows -- it is never told what
    changed, so it does not care which caller (or none, if the process died)
    performed the mutation.
"""
from __future__ import annotations

import hashlib
import sqlite3
import time

from .parent_sync_schema import MIGRATION_ID as V1_MIGRATION_ID
from .parent_sync_schema import SCHEMA_CHECKSUM as V1_SCHEMA_CHECKSUM

MIGRATION_ID = "ph3_08_parent_sync_v2"

_S = (
    "ALTER TABLE mgboost_parent_sync_operations ADD COLUMN verify_after INTEGER",
    "ALTER TABLE mgboost_parent_sync_operations ADD COLUMN stabilized_at INTEGER",
    """CREATE INDEX IF NOT EXISTS ix_mgboost_parent_sync_verify_due
        ON mgboost_parent_sync_operations(state, verify_after)""",
    # Backfill: any pre-existing APPLIED op from before this migration has no
    # verify_after yet -- schedule it for an immediate audit pass rather than
    # leaving it invisible to the new mechanism until its next unrelated ACK.
    """UPDATE mgboost_parent_sync_operations SET verify_after=updated_at
        WHERE state='APPLIED' AND verify_after IS NULL""",
    """CREATE TABLE IF NOT EXISTS mgboost_convergence_sweep_cursor (
        account_id INTEGER PRIMARY KEY
            REFERENCES mgboost_accounts(id) ON DELETE RESTRICT,
        next_check_at INTEGER NOT NULL DEFAULT 0,
        last_swept_at INTEGER,
        updated_at INTEGER NOT NULL
    )""",
    """CREATE INDEX IF NOT EXISTS ix_mgboost_convergence_sweep_due
        ON mgboost_convergence_sweep_cursor(next_check_at)""",
    # A dedicated, separately-numbered audit trail for stabilization/drift
    # checks -- these can legitimately recur many times at the *same*
    # dispatch `attempts` count (an op can be re-verified any number of
    # times between real repairs), so they cannot share the dispatch
    # events table's UNIQUE(sync_operation_id, attempt_no, event_type).
    """CREATE TABLE IF NOT EXISTS mgboost_parent_sync_verification_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        sync_operation_id INTEGER NOT NULL,
        account_id INTEGER NOT NULL,
        event_type TEXT NOT NULL
            CHECK(event_type IN ('STABILIZATION_VERIFIED','STABILIZATION_DRIFTED')),
        remote_effect_verifier TEXT,
        safe_error_class TEXT,
        created_at INTEGER NOT NULL,
        FOREIGN KEY(sync_operation_id, account_id)
            REFERENCES mgboost_parent_sync_operations(id, account_id) ON DELETE RESTRICT
    )""",
    """CREATE INDEX IF NOT EXISTS ix_mgboost_parent_sync_verification_op
        ON mgboost_parent_sync_verification_events(sync_operation_id, id)""",
    """CREATE TRIGGER IF NOT EXISTS trg_mgboost_parent_sync_verification_no_update
        BEFORE UPDATE ON mgboost_parent_sync_verification_events
        BEGIN SELECT RAISE(ABORT, 'parent sync verification events are immutable'); END""",
    """CREATE TRIGGER IF NOT EXISTS trg_mgboost_parent_sync_verification_no_delete
        BEFORE DELETE ON mgboost_parent_sync_verification_events
        BEGIN SELECT RAISE(ABORT, 'parent sync verification events are immutable'); END""",
)

SCHEMA_CHECKSUM = hashlib.sha256("\n".join(value.strip() for value in _S).encode()).hexdigest()


def _verify(connection: sqlite3.Connection) -> None:
    columns = {row[1] for row in connection.execute(
        "PRAGMA table_info(mgboost_parent_sync_operations)"
    )}
    if not {"verify_after", "stabilized_at"}.issubset(columns):
        raise RuntimeError("PH3-08 v2 parent sync columns are incomplete")
    cursor_columns = {row[1] for row in connection.execute(
        "PRAGMA table_info(mgboost_convergence_sweep_cursor)"
    )}
    if not {"account_id", "next_check_at", "last_swept_at", "updated_at"}.issubset(cursor_columns):
        raise RuntimeError("PH3-08 v2 convergence sweep cursor table is incomplete")
    verification_columns = {row[1] for row in connection.execute(
        "PRAGMA table_info(mgboost_parent_sync_verification_events)"
    )}
    if not {"sync_operation_id", "account_id", "event_type", "remote_effect_verifier"}.issubset(
        verification_columns
    ):
        raise RuntimeError("PH3-08 v2 verification events table is incomplete")


def apply_parent_sync_schema_v2(connection: sqlite3.Connection, *, now: int | None = None) -> bool:
    timestamp = int(time.time()) if now is None else int(now)
    connection.execute("PRAGMA foreign_keys=ON")
    try:
        connection.execute("BEGIN IMMEDIATE")
        dependency = connection.execute(
            "SELECT schema_checksum FROM mgboost_schema_migrations WHERE migration_id=?",
            (V1_MIGRATION_ID,),
        ).fetchone()
        if not dependency or dependency[0] != V1_SCHEMA_CHECKSUM:
            raise RuntimeError("parent sync v2 dependency mismatch")
        row = connection.execute(
            "SELECT schema_checksum FROM mgboost_schema_migrations WHERE migration_id=?",
            (MIGRATION_ID,),
        ).fetchone()
        if row:
            if row[0] != SCHEMA_CHECKSUM:
                raise RuntimeError("parent sync v2 schema checksum mismatch")
            _verify(connection)
            connection.commit()
            return False
        for statement in _S:
            connection.execute(statement)
        _verify(connection)
        connection.execute(
            "INSERT INTO mgboost_schema_migrations (migration_id,schema_checksum,applied_at) VALUES (?,?,?)",
            (MIGRATION_ID, SCHEMA_CHECKSUM, timestamp),
        )
        connection.commit()
        return True
    except Exception:
        connection.rollback()
        raise
