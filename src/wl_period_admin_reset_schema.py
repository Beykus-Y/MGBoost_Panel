"""Additive PH6-02 schema: WL period ADMIN_RESET close/successor audit.

`mgboost_wl_periods` (PH3-01) already has a mutable `status` column ("status
stays mutable for Phase 6's own future runtime state machine" -- PH5-02's
own migration docstring). This module adds the missing runtime piece: a
durable, append-only record of every ADMIN_RESET (close the current period,
create a successor with the same account/subscription/term and remaining
window, never touch any usage ledger -- the ledger doesn't exist yet
(PH6-03), and keying it by period id is exactly what makes "never rewrites
consumed" true by construction: a closed period keeps its own id and its
own (future) ledger rows untouched; only a brand-new period id starts
counting from zero).

Requires the exact PH3-01 parent schema checksum, same pattern as PH5-02's
own `wl_period_lifecycle_schema`.
"""

from __future__ import annotations

import hashlib
import sqlite3
import time

from .account_schema import MIGRATION_ID as PARENT_MIGRATION_ID
from .account_schema import SCHEMA_CHECKSUM as PARENT_SCHEMA_CHECKSUM


MIGRATION_ID = "ph6_02_wl_period_admin_reset_v1"

_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS mgboost_wl_period_resets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        account_id INTEGER NOT NULL,
        subscription_id INTEGER NOT NULL,
        closed_period_id INTEGER NOT NULL,
        successor_period_id INTEGER NOT NULL,
        reason TEXT NOT NULL,
        actor_type TEXT NOT NULL,
        actor_ref TEXT,
        created_at INTEGER NOT NULL,
        UNIQUE(closed_period_id),
        UNIQUE(successor_period_id),
        FOREIGN KEY(closed_period_id) REFERENCES mgboost_wl_periods(id) ON DELETE RESTRICT,
        FOREIGN KEY(successor_period_id) REFERENCES mgboost_wl_periods(id) ON DELETE RESTRICT
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_mgboost_wl_period_resets_account
        ON mgboost_wl_period_resets(account_id)
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mgboost_wl_period_resets_no_update
        BEFORE UPDATE ON mgboost_wl_period_resets
        BEGIN SELECT RAISE(ABORT, 'WL period reset audit rows are append-only'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mgboost_wl_period_resets_no_delete
        BEFORE DELETE ON mgboost_wl_period_resets
        BEGIN SELECT RAISE(ABORT, 'WL period reset audit rows are append-only'); END
    """,
)

SCHEMA_CHECKSUM = hashlib.sha256(
    "\n".join(statement.strip() for statement in _SCHEMA_STATEMENTS).encode("utf-8")
).hexdigest()


def _verify(connection: sqlite3.Connection) -> None:
    tables = {row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='mgboost_wl_period_resets'"
    )}
    if "mgboost_wl_period_resets" not in tables:
        raise RuntimeError("PH6-02 WL period reset table missing")
    triggers = {row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE name LIKE 'trg_mgboost_wl_period_resets_%'"
    )}
    expected = {
        "trg_mgboost_wl_period_resets_no_update",
        "trg_mgboost_wl_period_resets_no_delete",
    }
    if not expected.issubset(triggers):
        raise RuntimeError("PH6-02 WL period reset triggers incomplete")


def apply_wl_period_admin_reset_schema(connection: sqlite3.Connection, *, now: int | None = None) -> bool:
    connection.execute("PRAGMA foreign_keys=ON")
    timestamp = int(time.time()) if now is None else int(now)
    try:
        connection.execute("BEGIN IMMEDIATE")
        parent = connection.execute(
            "SELECT schema_checksum FROM mgboost_schema_migrations WHERE migration_id=?",
            (PARENT_MIGRATION_ID,),
        ).fetchone()
        if not parent or parent[0] != PARENT_SCHEMA_CHECKSUM:
            raise RuntimeError("PH6-02 requires exact PH3-01 schema")
        row = connection.execute(
            "SELECT schema_checksum FROM mgboost_schema_migrations WHERE migration_id=?",
            (MIGRATION_ID,),
        ).fetchone()
        if row:
            if row[0] != SCHEMA_CHECKSUM:
                raise RuntimeError("PH6-02 schema checksum mismatch")
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
