"""Additive PH7-13 account consolidation (merge/supersession) schema.

DL-057: two already-existing, independently-provisioned parent accounts
that turn out to be the same real person are merged by adding a new,
explicit, append-only fact layered on top of both -- never by mutating,
deleting, or reassigning either account's existing rows. Neither account's
pre-existing history (aliases, subscriptions, device slots/generations,
child intents, entitlement mutations, WL usage, grace periods, evidence) is
ever touched by this schema or by anything that reads through it.

`mgboost_account_merges` records "the absorbed account's identity now
canonically resolves to the survivor, as of this decision". Reversal is a
new `mgboost_account_merge_events` row plus a CAS flip of `status`, never a
DELETE/UPDATE of the merge row's identity -- matching every other
append-only table in this codebase (closest precedent:
`mgboost_legacy_bridge_bindings`/`_binding_events`).

`mgboost_account_display_names` is unrelated to legacy-username aliasing:
it is a purely cosmetic, owner-set human label for admin/read surfaces,
modeled like `mgboost_telegram_identities` (revoke-and-reinsert, partial
unique index enforcing "at most one active row per account").
"""

from __future__ import annotations

import hashlib
import sqlite3
import time

from .account_schema import MIGRATION_ID as PARENT_MIGRATION_ID
from .account_schema import SCHEMA_CHECKSUM as PARENT_SCHEMA_CHECKSUM


MIGRATION_ID = "ph7_13_account_consolidation_v1"

_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS mgboost_account_merges (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        absorbed_account_id INTEGER NOT NULL UNIQUE,
        survivor_account_id INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'ACTIVE' CHECK(status IN ('ACTIVE','REVERSED')),
        decision_ref TEXT NOT NULL CHECK(length(decision_ref) BETWEEN 3 AND 128),
        created_by_actor TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        row_version INTEGER NOT NULL DEFAULT 1 CHECK(row_version > 0),
        CHECK(absorbed_account_id != survivor_account_id),
        FOREIGN KEY(absorbed_account_id) REFERENCES mgboost_accounts(id) ON DELETE RESTRICT,
        FOREIGN KEY(survivor_account_id) REFERENCES mgboost_accounts(id) ON DELETE RESTRICT
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_mgboost_account_merges_survivor
        ON mgboost_account_merges(survivor_account_id)
    """,
    """
    CREATE TABLE IF NOT EXISTS mgboost_account_merge_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        merge_id INTEGER NOT NULL,
        event_type TEXT NOT NULL CHECK(event_type IN ('CREATED','REVERSED')),
        actor_ref TEXT NOT NULL,
        reason TEXT NOT NULL CHECK(length(reason) BETWEEN 3 AND 500),
        created_at INTEGER NOT NULL,
        FOREIGN KEY(merge_id) REFERENCES mgboost_account_merges(id) ON DELETE RESTRICT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS mgboost_account_display_names (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        account_id INTEGER NOT NULL,
        display_name TEXT NOT NULL CHECK(length(display_name) BETWEEN 1 AND 64),
        set_at INTEGER NOT NULL,
        revoked_at INTEGER,
        set_by_actor TEXT NOT NULL,
        decision_ref TEXT NOT NULL CHECK(length(decision_ref) BETWEEN 3 AND 128),
        CHECK(revoked_at IS NULL OR revoked_at >= set_at),
        FOREIGN KEY(account_id) REFERENCES mgboost_accounts(id) ON DELETE RESTRICT
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS ux_mgboost_account_active_display_name
        ON mgboost_account_display_names(account_id) WHERE revoked_at IS NULL
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_mgboost_display_names_account_history
        ON mgboost_account_display_names(account_id, set_at)
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mgboost_account_merges_identity_immutable
        BEFORE UPDATE OF absorbed_account_id,survivor_account_id,decision_ref,
                         created_by_actor,created_at
        ON mgboost_account_merges
        BEGIN SELECT RAISE(ABORT, 'account merge identity is immutable'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mgboost_account_merges_no_delete
        BEFORE DELETE ON mgboost_account_merges
        BEGIN SELECT RAISE(ABORT, 'account merge history is immutable'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mgboost_account_merge_events_no_update
        BEFORE UPDATE ON mgboost_account_merge_events
        BEGIN SELECT RAISE(ABORT, 'account merge events are immutable'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mgboost_account_merge_events_no_delete
        BEFORE DELETE ON mgboost_account_merge_events
        BEGIN SELECT RAISE(ABORT, 'account merge events are immutable'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mgboost_display_names_identity_immutable
        BEFORE UPDATE OF account_id,display_name,set_at,set_by_actor,decision_ref
        ON mgboost_account_display_names
        BEGIN SELECT RAISE(ABORT, 'display name identity is immutable'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mgboost_display_names_no_delete
        BEFORE DELETE ON mgboost_account_display_names
        BEGIN SELECT RAISE(ABORT, 'display name history is immutable'); END
    """,
)

SCHEMA_CHECKSUM = hashlib.sha256(
    "\n".join(statement.strip() for statement in _SCHEMA_STATEMENTS).encode("utf-8")
).hexdigest()

NEW_RUNTIME_TABLES = (
    "mgboost_account_merges",
    "mgboost_account_merge_events",
    "mgboost_account_display_names",
)

_REQUIRED_COLUMNS = {
    "mgboost_account_merges": {
        "id", "absorbed_account_id", "survivor_account_id", "status",
        "decision_ref", "created_by_actor", "created_at", "updated_at", "row_version",
    },
    "mgboost_account_merge_events": {
        "id", "merge_id", "event_type", "actor_ref", "reason", "created_at",
    },
    "mgboost_account_display_names": {
        "id", "account_id", "display_name", "set_at", "revoked_at",
        "set_by_actor", "decision_ref",
    },
}

_REQUIRED_OBJECTS = {
    "ix_mgboost_account_merges_survivor",
    "ux_mgboost_account_active_display_name",
    "ix_mgboost_display_names_account_history",
    "trg_mgboost_account_merges_identity_immutable",
    "trg_mgboost_account_merges_no_delete",
    "trg_mgboost_account_merge_events_no_update",
    "trg_mgboost_account_merge_events_no_delete",
    "trg_mgboost_display_names_identity_immutable",
    "trg_mgboost_display_names_no_delete",
}


def _verify(connection: sqlite3.Connection) -> None:
    for table, required in _REQUIRED_COLUMNS.items():
        actual = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        if required - actual:
            raise RuntimeError(f"PH7-13 account consolidation table {table} is incompatible")
    objects = {
        row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE name LIKE 'ix_mgboost_account_merges_%' "
            "OR name LIKE 'ux_mgboost_account_active_display_name' "
            "OR name LIKE 'ix_mgboost_display_names_%' "
            "OR name LIKE 'trg_mgboost_account_merge%' "
            "OR name LIKE 'trg_mgboost_display_names_%'"
        )
    }
    if not _REQUIRED_OBJECTS.issubset(objects):
        raise RuntimeError("PH7-13 account consolidation schema objects incomplete")


def apply_account_consolidation_schema(
    connection: sqlite3.Connection, *, now: int | None = None
) -> bool:
    """Apply PH7-13 transactionally after the exact PH3-01 foundation."""
    connection.execute("PRAGMA foreign_keys=ON")
    applied_at = int(time.time()) if now is None else int(now)
    try:
        connection.execute("BEGIN IMMEDIATE")
        parent = connection.execute(
            "SELECT schema_checksum FROM mgboost_schema_migrations WHERE migration_id=?",
            (PARENT_MIGRATION_ID,),
        ).fetchone()
        if parent is None or parent[0] != PARENT_SCHEMA_CHECKSUM:
            raise RuntimeError("PH7-13 requires exact PH3-01 schema")
        existing = connection.execute(
            "SELECT schema_checksum FROM mgboost_schema_migrations WHERE migration_id=?",
            (MIGRATION_ID,),
        ).fetchone()
        if existing is not None:
            if existing[0] != SCHEMA_CHECKSUM:
                raise RuntimeError("PH7-13 schema checksum mismatch")
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
