"""Additive PH5-02 immutability guard for the PH3-01 `mgboost_wl_periods` table.

PH3-01 created `mgboost_wl_periods` but never guarded it against UPDATE/
DELETE (unlike `mgboost_plan_versions`/`mgboost_subscription_terms`, which
already are). PH5-02's own Rollback contract ("immutable scheduled periods
... preserved") requires that once a period is scheduled its identity/quota
fields (account/subscription/term, sequence, start/end, quota mode/bytes)
can never be rewritten or deleted -- only its `status` may still move
forward (`PLANNED -> ACTIVE -> CLOSED`), which is Phase 6's own future
runtime concern, not built here.
"""

from __future__ import annotations

import hashlib
import sqlite3
import time

from .account_schema import MIGRATION_ID as PARENT_MIGRATION_ID
from .account_schema import SCHEMA_CHECKSUM as PARENT_SCHEMA_CHECKSUM


MIGRATION_ID = "ph5_02_wl_period_lifecycle_v1"

_SCHEMA_STATEMENTS = (
    """
    CREATE TRIGGER IF NOT EXISTS trg_mgboost_wl_periods_identity_immutable
        BEFORE UPDATE OF account_id, subscription_id, subscription_term_id,
            sequence_no, starts_at, ends_at, quota_mode, base_quota_bytes, created_at
        ON mgboost_wl_periods
        BEGIN SELECT RAISE(ABORT, 'WL period identity/quota fields are immutable'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mgboost_wl_periods_no_delete
        BEFORE DELETE ON mgboost_wl_periods
        BEGIN SELECT RAISE(ABORT, 'WL periods are never deleted'); END
    """,
)

SCHEMA_CHECKSUM = hashlib.sha256(
    "\n".join(statement.strip() for statement in _SCHEMA_STATEMENTS).encode("utf-8")
).hexdigest()


def _verify(connection: sqlite3.Connection) -> None:
    objects = {row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE name LIKE 'trg_mgboost_wl_periods_%'"
    )}
    expected = {
        "trg_mgboost_wl_periods_identity_immutable",
        "trg_mgboost_wl_periods_no_delete",
    }
    if not expected.issubset(objects):
        raise RuntimeError("PH5-02 WL period lifecycle triggers incomplete")


def apply_wl_period_lifecycle_schema(
    connection: sqlite3.Connection, *, now: int | None = None
) -> bool:
    connection.execute("PRAGMA foreign_keys=ON")
    timestamp = int(time.time()) if now is None else int(now)
    try:
        connection.execute("BEGIN IMMEDIATE")
        parent = connection.execute(
            "SELECT schema_checksum FROM mgboost_schema_migrations WHERE migration_id=?",
            (PARENT_MIGRATION_ID,),
        ).fetchone()
        if not parent or parent[0] != PARENT_SCHEMA_CHECKSUM:
            raise RuntimeError("PH5-02 requires exact PH3-01 schema")
        row = connection.execute(
            "SELECT schema_checksum FROM mgboost_schema_migrations WHERE migration_id=?",
            (MIGRATION_ID,),
        ).fetchone()
        if row:
            if row[0] != SCHEMA_CHECKSUM:
                raise RuntimeError("PH5-02 schema checksum mismatch")
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
