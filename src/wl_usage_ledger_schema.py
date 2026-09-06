"""Additive PH6-03 schema: durable monotonic WL usage ledger/collector.

Observe/accounting-only. No enforcement (PH6-06/09), no shared parent pool
(PH6-04) and no purchase-flow wiring live here -- this is purely the
durable storage a future dormant-until-wired collector writes to, matching
this project's established "build the contract before its consumer exists"
discipline (PH6-01/PH6-02 precedent).

Requires the exact PH3-01 (`account_schema`, owns `mgboost_wl_periods`),
PH3-03-prerequisite (`child_provisioning_schema`, owns
`mgboost_child_user_intents`) and PH5-02 (`wl_period_lifecycle_schema`,
owns the WL-period immutability triggers) schema checksums -- same
multi-parent dependency pattern `parent_sync_schema.py` (PH3-08) already
uses.

Design (see `src/wl_usage_ledger.py` for the full rationale, backed by a
direct 2026-08-26 read of the real production Marzban 0.8.4 source
(`app/jobs/record_usages.py`, `app/db/crud.py`, `app/db/models.py`) via SSH):

- `mgboost_wl_usage_cursors`: one durable row per (child, node) holding the
  last raw cumulative `used_traffic` total this collector has observed for
  that child on that node (Marzban's own `NodeUserUsage` sum since its last
  full reset). This is mutable *by design* -- it is expected to decrease
  exactly once whenever an admin resets that Marzban user's traffic
  (`reset_user_data_usage` cascade-deletes all of that user's
  `NodeUserUsage` rows), which is how a reset is detected.
- `mgboost_wl_usage_samples`: one row per (child, node, UTC-hour bucket)
  holding our own ledger's `bytes_delta` for that hour -- a durable,
  DB-enforced *monotonic non-decreasing* accumulator (a trigger rejects any
  UPDATE that would lower `bytes_delta`), mirroring the exact
  extension-only trigger pattern `mgboost_legacy_grace_periods.
  current_end_at` already uses. "Never rewrites consumed" holds at the
  schema layer, not just by application discipline.
- `mgboost_wl_usage_sample_events`: fully immutable, append-only,
  fine-grained audit of every individual poll's (cursor_before,
  cursor_after) transition. This v1 definition's own
  `UNIQUE(child_intent_id, node_id, cursor_before)` was the idempotency key:
  replaying the exact same unconsumed cursor state twice (e.g. a crash after
  the Marzban read but before the commit that would have advanced the
  cursor) can only ever insert the same event row once. **Superseded by
  `wl_usage_ledger_schema_v3.py` (BUG-004 fix, see `BUGS.md`):** a reset
  legitimately returns the raw cumulative counter to a value already used as
  a `cursor_before` in an earlier epoch (most commonly `0`, the very first
  observation), which this two-column key could not tell apart from a true
  replay -- the real fix adds a durable `reset_generation` epoch number to
  the key. This module's own `_SCHEMA_STATEMENTS`/checksum are left exactly
  as originally applied (an already-applied migration is never rewritten);
  v3 rebuilds the table on top of it. Reconciliation-friendly: summing this
  table's `delta_bytes` per (child, node, sample_hour) must always equal
  the corresponding `mgboost_wl_usage_samples.bytes_delta`.
- `mgboost_wl_usage_collector_lease`: a single-row (`id=1`) CAS lease --
  the one-leader mechanism a durable collector run must hold for its whole
  cycle, mirroring the exact `lease_owner`/`lease_expires_at`/
  `row_version` shape PH3-03's own `mgboost_outbox` lease already uses.
  Not a process-local lock: any number of processes/hosts can race to
  claim it, only one wins per lease window.

`wl_period_id` on the sample/event rows is a best-effort, nullable
attribution of "which WL period was ACTIVE for this account at collection
time" -- resolved by the application layer, never a hard FK-enforced
cross-account check. It is always safe to be NULL (no ACTIVE period exists
yet for that account, e.g. every real account in production today, since
no purchase flow calls `apply_same_plan_purchase` live yet) -- this ledger
collects real per-node usage regardless of whether any WL period exists to
attribute it to. Every WL-period boundary that exists is exactly UTC-hour
aligned (`subscription_renewal.align_to_utc_hour`, DL-020), so no period
transition can ever land inside one UTC-hour sample bucket -- a single
sample_hour's usage is therefore never contested between two periods.
"""

from __future__ import annotations

import hashlib
import sqlite3
import time

from .account_schema import MIGRATION_ID as ACCOUNT_MIGRATION_ID
from .account_schema import SCHEMA_CHECKSUM as ACCOUNT_SCHEMA_CHECKSUM
from .child_provisioning_schema import MIGRATION_ID as CHILD_MIGRATION_ID
from .child_provisioning_schema import SCHEMA_CHECKSUM as CHILD_SCHEMA_CHECKSUM
from .wl_period_lifecycle_schema import MIGRATION_ID as WL_PERIOD_MIGRATION_ID
from .wl_period_lifecycle_schema import SCHEMA_CHECKSUM as WL_PERIOD_SCHEMA_CHECKSUM


MIGRATION_ID = "ph6_03_wl_usage_ledger_v1"

_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS mgboost_wl_usage_cursors (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        account_id INTEGER NOT NULL,
        child_intent_id INTEGER NOT NULL,
        node_id INTEGER NOT NULL,
        last_observed_cumulative_bytes INTEGER NOT NULL DEFAULT 0
            CHECK(last_observed_cumulative_bytes >= 0),
        last_polled_at INTEGER NOT NULL DEFAULT 0,
        reset_count INTEGER NOT NULL DEFAULT 0 CHECK(reset_count >= 0),
        row_version INTEGER NOT NULL DEFAULT 1 CHECK(row_version > 0),
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        UNIQUE(child_intent_id, node_id),
        FOREIGN KEY(child_intent_id, account_id)
            REFERENCES mgboost_child_user_intents(id, account_id)
            ON DELETE RESTRICT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS mgboost_wl_usage_samples (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        account_id INTEGER NOT NULL,
        child_intent_id INTEGER NOT NULL,
        node_id INTEGER NOT NULL,
        wl_period_id INTEGER,
        sample_hour INTEGER NOT NULL CHECK(sample_hour % 3600 = 0),
        bytes_delta INTEGER NOT NULL DEFAULT 0 CHECK(bytes_delta >= 0),
        first_collected_at INTEGER NOT NULL,
        last_collected_at INTEGER NOT NULL,
        row_version INTEGER NOT NULL DEFAULT 1 CHECK(row_version > 0),
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        UNIQUE(child_intent_id, node_id, sample_hour),
        FOREIGN KEY(child_intent_id, account_id)
            REFERENCES mgboost_child_user_intents(id, account_id)
            ON DELETE RESTRICT,
        FOREIGN KEY(wl_period_id) REFERENCES mgboost_wl_periods(id)
            ON DELETE RESTRICT
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_mgboost_wl_usage_samples_period
        ON mgboost_wl_usage_samples(wl_period_id, node_id)
    """,
    """
    CREATE TABLE IF NOT EXISTS mgboost_wl_usage_sample_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        account_id INTEGER NOT NULL,
        child_intent_id INTEGER NOT NULL,
        node_id INTEGER NOT NULL,
        sample_hour INTEGER NOT NULL CHECK(sample_hour % 3600 = 0),
        cursor_before INTEGER NOT NULL CHECK(cursor_before >= 0),
        cursor_after INTEGER NOT NULL CHECK(cursor_after >= 0),
        delta_bytes INTEGER NOT NULL CHECK(delta_bytes >= 0),
        reset_detected INTEGER NOT NULL CHECK(reset_detected IN (0,1)),
        collector_id TEXT NOT NULL CHECK(length(collector_id) BETWEEN 1 AND 128),
        collected_at INTEGER NOT NULL,
        created_at INTEGER NOT NULL,
        UNIQUE(child_intent_id, node_id, cursor_before),
        FOREIGN KEY(child_intent_id, account_id)
            REFERENCES mgboost_child_user_intents(id, account_id)
            ON DELETE RESTRICT
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_mgboost_wl_usage_events_lookup
        ON mgboost_wl_usage_sample_events(child_intent_id, node_id, sample_hour)
    """,
    """
    CREATE TABLE IF NOT EXISTS mgboost_wl_usage_collector_lease (
        id INTEGER PRIMARY KEY CHECK(id = 1),
        lease_owner TEXT,
        lease_expires_at INTEGER,
        row_version INTEGER NOT NULL DEFAULT 1 CHECK(row_version > 0),
        last_run_started_at INTEGER,
        last_run_completed_at INTEGER,
        last_run_outcome TEXT CHECK(last_run_outcome IN ('OK','PARTIAL','ERROR')),
        last_run_error_class TEXT,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        CHECK(
            (lease_owner IS NULL AND lease_expires_at IS NULL)
            OR (lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)
        )
    )
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mgboost_wl_usage_cursor_identity_immutable
        BEFORE UPDATE OF account_id, child_intent_id, node_id, created_at
        ON mgboost_wl_usage_cursors
        BEGIN SELECT RAISE(ABORT, 'usage cursor identity is immutable'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mgboost_wl_usage_cursor_no_delete
        BEFORE DELETE ON mgboost_wl_usage_cursors
        BEGIN SELECT RAISE(ABORT, 'usage cursor history is immutable'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mgboost_wl_usage_sample_identity_immutable
        BEFORE UPDATE OF account_id, child_intent_id, node_id, sample_hour,
                         wl_period_id, created_at
        ON mgboost_wl_usage_samples
        BEGIN SELECT RAISE(ABORT, 'usage sample identity is immutable'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mgboost_wl_usage_sample_monotonic
        BEFORE UPDATE OF bytes_delta ON mgboost_wl_usage_samples
        WHEN NEW.bytes_delta < OLD.bytes_delta
        BEGIN SELECT RAISE(ABORT, 'usage sample bytes_delta cannot decrease'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mgboost_wl_usage_sample_no_delete
        BEFORE DELETE ON mgboost_wl_usage_samples
        BEGIN SELECT RAISE(ABORT, 'usage sample history is immutable'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mgboost_wl_usage_events_no_update
        BEFORE UPDATE ON mgboost_wl_usage_sample_events
        BEGIN SELECT RAISE(ABORT, 'usage sample events are immutable'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mgboost_wl_usage_events_no_delete
        BEFORE DELETE ON mgboost_wl_usage_sample_events
        BEGIN SELECT RAISE(ABORT, 'usage sample events are immutable'); END
    """,
)

SCHEMA_CHECKSUM = hashlib.sha256(
    "\n".join(statement.strip() for statement in _SCHEMA_STATEMENTS).encode("utf-8")
).hexdigest()

NEW_RUNTIME_TABLES = (
    "mgboost_wl_usage_cursors",
    "mgboost_wl_usage_samples",
    "mgboost_wl_usage_sample_events",
    "mgboost_wl_usage_collector_lease",
)

_REQUIRED_COLUMNS = {
    "mgboost_wl_usage_cursors": {
        "id", "account_id", "child_intent_id", "node_id",
        "last_observed_cumulative_bytes", "last_polled_at", "reset_count",
        "row_version",
    },
    "mgboost_wl_usage_samples": {
        "id", "account_id", "child_intent_id", "node_id", "wl_period_id",
        "sample_hour", "bytes_delta", "first_collected_at", "last_collected_at",
        "row_version",
    },
    "mgboost_wl_usage_sample_events": {
        "id", "account_id", "child_intent_id", "node_id", "sample_hour",
        "cursor_before", "cursor_after", "delta_bytes", "reset_detected",
        "collector_id", "collected_at",
    },
    "mgboost_wl_usage_collector_lease": {
        "id", "lease_owner", "lease_expires_at", "row_version",
        "last_run_started_at", "last_run_completed_at", "last_run_outcome",
        "last_run_error_class",
    },
}

_REQUIRED_OBJECTS = {
    "ix_mgboost_wl_usage_samples_period",
    "ix_mgboost_wl_usage_events_lookup",
    "trg_mgboost_wl_usage_cursor_identity_immutable",
    "trg_mgboost_wl_usage_cursor_no_delete",
    "trg_mgboost_wl_usage_sample_identity_immutable",
    "trg_mgboost_wl_usage_sample_monotonic",
    "trg_mgboost_wl_usage_sample_no_delete",
    "trg_mgboost_wl_usage_events_no_update",
    "trg_mgboost_wl_usage_events_no_delete",
}


def _verify(connection: sqlite3.Connection) -> None:
    for table, required in _REQUIRED_COLUMNS.items():
        actual = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        if required - actual:
            raise RuntimeError(f"PH6-03 usage ledger table {table} is incompatible")
    objects = {
        row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE name LIKE 'ix_mgboost_wl_usage_%' "
            "OR name LIKE 'trg_mgboost_wl_usage_%'"
        )
    }
    if not _REQUIRED_OBJECTS.issubset(objects):
        raise RuntimeError("PH6-03 usage ledger schema indexes/triggers incomplete")


def apply_wl_usage_ledger_schema(connection: sqlite3.Connection, *, now: int | None = None) -> bool:
    """Apply PH6-03 transactionally after the exact PH3-01/PH3-03/PH5-02 foundations."""
    connection.execute("PRAGMA foreign_keys=ON")
    timestamp = int(time.time()) if now is None else int(now)
    try:
        connection.execute("BEGIN IMMEDIATE")
        for dep_id, dep_checksum, label in (
            (ACCOUNT_MIGRATION_ID, ACCOUNT_SCHEMA_CHECKSUM, "PH3-01"),
            (CHILD_MIGRATION_ID, CHILD_SCHEMA_CHECKSUM, "PH3-03 prerequisite"),
            (WL_PERIOD_MIGRATION_ID, WL_PERIOD_SCHEMA_CHECKSUM, "PH5-02 WL period lifecycle"),
        ):
            parent = connection.execute(
                "SELECT schema_checksum FROM mgboost_schema_migrations WHERE migration_id=?",
                (dep_id,),
            ).fetchone()
            if not parent or parent[0] != dep_checksum:
                raise RuntimeError(f"PH6-03 usage ledger schema requires the exact {label} schema")
        existing = connection.execute(
            "SELECT schema_checksum FROM mgboost_schema_migrations WHERE migration_id=?",
            (MIGRATION_ID,),
        ).fetchone()
        if existing is not None:
            if existing[0] != SCHEMA_CHECKSUM:
                raise RuntimeError("PH6-03 usage ledger schema checksum mismatch")
            _verify(connection)
            connection.commit()
            return False
        for statement in _SCHEMA_STATEMENTS:
            connection.execute(statement)
        connection.execute(
            "INSERT INTO mgboost_wl_usage_collector_lease "
            "(id, lease_owner, lease_expires_at, row_version, created_at, updated_at) "
            "VALUES (1, NULL, NULL, 1, ?, ?)",
            (timestamp, timestamp),
        )
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
