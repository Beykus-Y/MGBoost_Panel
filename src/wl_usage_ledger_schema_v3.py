"""BUG-004 fix: durable reset-generation identity for the WL usage ledger.

Root cause (see `BUGS.md` BUG-004): `mgboost_wl_usage_sample_events` keyed
replay identity purely on ``(child_intent_id, node_id, cursor_before)``. The
very first observation for any (child, node) always starts at
``cursor_before=0``. After a real Marzban-side reset, the cursor legitimately
returns to (or through) ``0`` again -- the next honest post-reset transition
then collides with that original ``cursor_before=0`` row, is misclassified
by the broad ``except sqlite3.IntegrityError`` as an exact replay, and the
cursor never advances again: every later poll keeps colliding with the same
stale event forever (the reproduced ``100 -> 0 -> 50 -> 200`` scenario).

Fix: every logical accounting *epoch* -- the span between two resets, or
between genesis and the first reset -- gets its own durable
``reset_generation`` number, stored both on the cursor (the current/next
generation to write into) and on every sample event (the generation that
transition belongs to). The uniqueness/replay key becomes
``(child_intent_id, node_id, reset_generation, cursor_before)``. A genuine
replay (same generation, same cursor_before) still collides exactly as
before -- idempotency is unchanged. A legitimate post-reset transition now
lands in a new generation and can never collide with a pre-reset event that
happened to use the same raw cursor_before value (most commonly ``0``, but
also any other cumulative value the counter passes through again in a later
epoch).

Generation assignment: the event closing an epoch (the one whose
``cursor_after < cursor_before``, i.e. ``reset_detected=1``) is stamped with
the epoch it is *closing* (its own ``cursor_before`` still belongs to the old
epoch); the cursor's stored generation is then advanced by one for every
following write. This exactly mirrors, and is derived only from, the
``reset_count``/``reset_detected`` bookkeeping this ledger already recorded
in v1/v2 -- no historical delta/traffic value is invented or changed.

Migration (additive, idempotent, no destructive cleanup):

- ``mgboost_wl_usage_cursors`` gains ``reset_generation`` (default ``0``,
  ``CHECK(reset_generation >= 0)``), backfilled to the existing
  ``reset_count`` for every already-existing row -- deterministic, not a
  guess: the number of resets already durably recorded *is* the current
  generation under this scheme.
- ``mgboost_wl_usage_sample_events`` is rebuilt (SQLite cannot ALTER an
  inline UNIQUE constraint, so this follows the exact rename/copy/verify
  pattern `wl_usage_ledger_schema_v2.py` already established for the
  sibling samples table) with the new ``reset_generation`` column and the
  new four-column unique key. Every existing row is preserved byte-for-byte
  except for this one new column, whose value is deterministically
  backfilled per ``(child_intent_id, node_id)`` group in strict ``id``
  order from that group's own already-recorded ``reset_detected`` flags
  (generation starts at 0, increments by one immediately *after* each row
  where ``reset_detected=1``) -- the exact same rule applied prospectively
  by `wl_usage_ledger.py::record_sample`. Row count and the
  ``(cursor_before, cursor_after, delta_bytes, reset_detected)`` tuple set
  are verified unchanged before and after.
"""

from __future__ import annotations

import hashlib
import sqlite3
import time

from .wl_usage_ledger_schema import (
    MIGRATION_ID as V1_MIGRATION_ID,
    SCHEMA_CHECKSUM as V1_SCHEMA_CHECKSUM,
)


MIGRATION_ID = "bug004_wl_usage_ledger_reset_generation_v1"

_EVENT_COLUMNS = (
    "id,account_id,child_intent_id,node_id,sample_hour,cursor_before,cursor_after,"
    "delta_bytes,reset_detected,reset_generation,collector_id,collected_at,created_at"
)
_V1_EVENT_COLUMNS = {
    "id", "account_id", "child_intent_id", "node_id", "sample_hour", "cursor_before",
    "cursor_after", "delta_bytes", "reset_detected", "collector_id", "collected_at", "created_at",
}
_V1_EVENT_TRIGGERS = {
    "trg_mgboost_wl_usage_events_no_update",
    "trg_mgboost_wl_usage_events_no_delete",
}

_FINAL_EVENTS_TABLE = """
    CREATE TABLE mgboost_wl_usage_sample_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        account_id INTEGER NOT NULL,
        child_intent_id INTEGER NOT NULL,
        node_id INTEGER NOT NULL,
        sample_hour INTEGER NOT NULL CHECK(sample_hour % 3600 = 0),
        cursor_before INTEGER NOT NULL CHECK(cursor_before >= 0),
        cursor_after INTEGER NOT NULL CHECK(cursor_after >= 0),
        delta_bytes INTEGER NOT NULL CHECK(delta_bytes >= 0),
        reset_detected INTEGER NOT NULL CHECK(reset_detected IN (0,1)),
        reset_generation INTEGER NOT NULL DEFAULT 0 CHECK(reset_generation >= 0),
        collector_id TEXT NOT NULL CHECK(length(collector_id) BETWEEN 1 AND 128),
        collected_at INTEGER NOT NULL,
        created_at INTEGER NOT NULL,
        UNIQUE(child_intent_id, node_id, reset_generation, cursor_before),
        FOREIGN KEY(child_intent_id, account_id)
            REFERENCES mgboost_child_user_intents(id, account_id)
            ON DELETE RESTRICT
    )
"""

_FINAL_EVENTS_OBJECTS = (
    """
    CREATE INDEX ix_mgboost_wl_usage_events_lookup
        ON mgboost_wl_usage_sample_events(child_intent_id, node_id, sample_hour)
    """,
    """
    CREATE TRIGGER trg_mgboost_wl_usage_events_no_update
        BEFORE UPDATE ON mgboost_wl_usage_sample_events
        BEGIN SELECT RAISE(ABORT, 'usage sample events are immutable'); END
    """,
    """
    CREATE TRIGGER trg_mgboost_wl_usage_events_no_delete
        BEFORE DELETE ON mgboost_wl_usage_sample_events
        BEGIN SELECT RAISE(ABORT, 'usage sample events are immutable'); END
    """,
)

SCHEMA_CHECKSUM = hashlib.sha256(
    (MIGRATION_ID + "\n" + _FINAL_EVENTS_TABLE + "\n" + "\n".join(_FINAL_EVENTS_OBJECTS)).encode("utf-8")
).hexdigest()

# Exposed so callers/tests can name the new unique-key columns without
# re-deriving them from the DDL string.
EVENT_UNIQUE_COLUMNS = ("child_intent_id", "node_id", "reset_generation", "cursor_before")


def _table_names(connection: sqlite3.Connection) -> set[str]:
    return {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _events_stats(connection: sqlite3.Connection, table: str) -> tuple[int, set]:
    # Keyed by the immutable primary key `id` (never re-used, never renumbered
    # by the rebuild) so this catches row loss/duplication precisely, unlike
    # an aggregate that could collide across different (child, node) pairs.
    rows = connection.execute(
        f"SELECT id,child_intent_id,node_id,cursor_before,cursor_after,delta_bytes,reset_detected "
        f"FROM {table} ORDER BY id"
    ).fetchall()
    return len(rows), {tuple(row) for row in rows}


def _verify_v1_source(connection: sqlite3.Connection) -> None:
    if "mgboost_wl_usage_sample_events" not in _table_names(connection):
        raise RuntimeError("PH6 ledger v1 usage-sample-events table is missing")
    columns = {row[1] for row in connection.execute("PRAGMA table_info(mgboost_wl_usage_sample_events)")}
    if columns != _V1_EVENT_COLUMNS:
        raise RuntimeError("PH6 ledger v1 usage-sample-events columns are unknown or corrupt")
    triggers = {row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='mgboost_wl_usage_sample_events'"
    )}
    if triggers != _V1_EVENT_TRIGGERS:
        raise RuntimeError("PH6 ledger v1 usage-sample-events triggers are unknown or corrupt")
    if "mgboost_wl_usage_cursors" not in _table_names(connection):
        raise RuntimeError("PH6 ledger v1 usage-cursors table is missing")
    cursor_columns = {row[1] for row in connection.execute("PRAGMA table_info(mgboost_wl_usage_cursors)")}
    if "reset_count" not in cursor_columns:
        raise RuntimeError("PH6 ledger v1 usage-cursors reset_count column is missing")
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise RuntimeError("foreign-key corruption blocks BUG-004 usage-ledger migration")


def _verify_final_schema(connection: sqlite3.Connection) -> None:
    if "mgboost_wl_usage_sample_events" not in _table_names(connection):
        raise RuntimeError("BUG-004 usage-sample-events table is missing")
    columns = {row[1] for row in connection.execute("PRAGMA table_info(mgboost_wl_usage_sample_events)")}
    expected = _V1_EVENT_COLUMNS | {"reset_generation"}
    if columns != expected:
        raise RuntimeError("BUG-004 usage-sample-events columns are incompatible")
    cursor_columns = {row[1] for row in connection.execute("PRAGMA table_info(mgboost_wl_usage_cursors)")}
    if "reset_generation" not in cursor_columns:
        raise RuntimeError("BUG-004 usage-cursors reset_generation column is missing")
    objects = {row[0]: row[1] or "" for row in connection.execute(
        "SELECT name,sql FROM sqlite_master WHERE type IN ('index','trigger')"
    )}
    required = {"ix_mgboost_wl_usage_events_lookup", *_V1_EVENT_TRIGGERS}
    if not required.issubset(objects):
        raise RuntimeError("BUG-004 usage-sample-events indexes/triggers incomplete")
    unique_indexes = [
        row for row in connection.execute("PRAGMA index_list(mgboost_wl_usage_sample_events)")
        if int(row[2]) == 1
    ]
    if len(unique_indexes) != 1:
        raise RuntimeError("BUG-004 usage-sample-events uniqueness is unknown or corrupt")
    index_columns = [row[2] for row in connection.execute(f"PRAGMA index_info({unique_indexes[0][1]})")]
    if index_columns != list(EVENT_UNIQUE_COLUMNS):
        raise RuntimeError("BUG-004 usage-sample-events key is unknown or corrupt")
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise RuntimeError("foreign-key corruption blocks BUG-004 usage-ledger startup")


def apply_wl_usage_ledger_schema_v3(connection: sqlite3.Connection, *, now: int | None = None) -> bool:
    """Apply the BUG-004 reset-generation fix. Additive, idempotent, no
    destructive cleanup; every existing row and delta/traffic value is
    preserved -- only the new ``reset_generation`` column is added/backfilled,
    deterministically, from data this ledger already durably recorded."""
    connection.execute("PRAGMA foreign_keys=ON")
    timestamp = int(time.time()) if now is None else int(now)
    try:
        connection.execute("BEGIN IMMEDIATE")
        v1 = connection.execute(
            "SELECT schema_checksum FROM mgboost_schema_migrations WHERE migration_id=?",
            (V1_MIGRATION_ID,),
        ).fetchone()
        if v1 is None or v1[0] != V1_SCHEMA_CHECKSUM:
            raise RuntimeError("BUG-004 usage-ledger fix requires the exact PH6-03 v1 schema")
        existing = connection.execute(
            "SELECT schema_checksum FROM mgboost_schema_migrations WHERE migration_id=?",
            (MIGRATION_ID,),
        ).fetchone()
        if existing is not None:
            if existing[0] != SCHEMA_CHECKSUM:
                raise RuntimeError("BUG-004 usage-ledger fix schema checksum mismatch")
            _verify_final_schema(connection)
            connection.commit()
            return False

        _verify_v1_source(connection)
        before_count, before_stats = _events_stats(connection, "mgboost_wl_usage_sample_events")

        # 1) Cursor table: additive column, deterministically backfilled from
        #    the count of resets this same row already durably recorded.
        connection.execute(
            "ALTER TABLE mgboost_wl_usage_cursors "
            "ADD COLUMN reset_generation INTEGER NOT NULL DEFAULT 0 CHECK(reset_generation >= 0)"
        )
        connection.execute("UPDATE mgboost_wl_usage_cursors SET reset_generation = reset_count")

        # 2) Events table: rebuild (SQLite cannot ALTER an inline UNIQUE
        #    constraint) with the new column and the new four-column key,
        #    backfilling reset_generation per (child, node) group in strict
        #    id order from each row's own already-recorded reset_detected --
        #    exactly the same rule record_sample now applies prospectively.
        rows = connection.execute(
            "SELECT id, account_id, child_intent_id, node_id, sample_hour, cursor_before, "
            "cursor_after, delta_bytes, reset_detected, collector_id, collected_at, created_at "
            "FROM mgboost_wl_usage_sample_events ORDER BY child_intent_id, node_id, id"
        ).fetchall()
        backfilled = []
        generation_by_group: dict[tuple[int, int], int] = {}
        for row in rows:
            key = (row["child_intent_id"], row["node_id"])
            generation = generation_by_group.get(key, 0)
            backfilled.append((
                row["id"], row["account_id"], row["child_intent_id"], row["node_id"],
                row["sample_hour"], row["cursor_before"], row["cursor_after"],
                row["delta_bytes"], row["reset_detected"], generation,
                row["collector_id"], row["collected_at"], row["created_at"],
            ))
            if row["reset_detected"]:
                generation_by_group[key] = generation + 1

        for trigger in _V1_EVENT_TRIGGERS:
            connection.execute(f"DROP TRIGGER {trigger}")
        connection.execute("DROP INDEX ix_mgboost_wl_usage_events_lookup")
        connection.execute(
            "ALTER TABLE mgboost_wl_usage_sample_events RENAME TO mgboost_wl_usage_sample_events_bug004_v1"
        )
        connection.execute(_FINAL_EVENTS_TABLE)
        connection.executemany(
            f"INSERT INTO mgboost_wl_usage_sample_events ({_EVENT_COLUMNS}) "
            f"VALUES ({','.join(['?'] * 13)})",
            backfilled,
        )
        copied_count, copied_stats = _events_stats(connection, "mgboost_wl_usage_sample_events")
        if (copied_count, copied_stats) != (before_count, before_stats):
            raise RuntimeError("BUG-004 usage-ledger migration row-count or content mismatch")
        connection.execute("DROP TABLE mgboost_wl_usage_sample_events_bug004_v1")
        for statement in _FINAL_EVENTS_OBJECTS:
            connection.execute(statement)
        after_count, after_stats = _events_stats(connection, "mgboost_wl_usage_sample_events")
        if (after_count, after_stats) != (before_count, before_stats):
            raise RuntimeError("BUG-004 usage-ledger migration post-rebuild verification mismatch")

        _verify_final_schema(connection)
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
