"""Additive PH7-14 schema: ADMIN_GRANT-origin template-provisioning job
queue -- the ADMIN_GRANT counterpart to PH5-11's
``mgboost_signup_template_jobs``.

Kept as its own table rather than reusing PH5-11's job table because that
table's ``invoice_id`` is ``NOT NULL`` by design (it is the payment-origin
job queue -- see ``commercial_signup_schema.py``'s own docstring: "the
durable retry job that drives remote template provisioning after a
confirmed payment"). An ADMIN_GRANT account has no invoice and must never
be given a fabricated one just to satisfy a NOT NULL column -- that would
misrepresent a non-financial grant as payment-originated in the exact
table whose whole purpose is payment provenance.

``commercial_signup.CommercialSignupStore.ensure_template_for_account`` is
reused UNCHANGED for actually provisioning the remote template (it already
falls back to the account's own current subscription's ``wl_mode`` when no
``mgboost_signup_template_jobs`` row exists for the account -- exactly the
ADMIN_GRANT case). Only the job QUEUE differs; the template-provisioning
engine itself is not duplicated.
"""

from __future__ import annotations

import hashlib
import sqlite3
import time

from .account_schema import MIGRATION_ID as ACCOUNT_MIGRATION_ID
from .account_schema import SCHEMA_CHECKSUM as ACCOUNT_SCHEMA_CHECKSUM

MIGRATION_ID = "ph7_14_admin_grant_template_jobs_v1"

_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS mgboost_admin_grant_template_jobs (
        account_id INTEGER PRIMARY KEY,
        decision_ref TEXT NOT NULL CHECK(length(decision_ref) BETWEEN 3 AND 300),
        state TEXT NOT NULL DEFAULT 'PENDING'
            CHECK(state IN ('PENDING','READY','MANUAL_REVIEW')),
        attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
        last_error_class TEXT,
        last_attempt_at INTEGER,
        ready_at INTEGER,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        FOREIGN KEY(account_id) REFERENCES mgboost_accounts(id) ON DELETE RESTRICT
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_mgboost_admin_grant_template_jobs_pending
        ON mgboost_admin_grant_template_jobs(state, updated_at, account_id)
    """,
)

SCHEMA_CHECKSUM = hashlib.sha256(
    "\n".join(statement.strip() for statement in _SCHEMA_STATEMENTS).encode("utf-8")
).hexdigest()

_REQUIRED_COLUMNS = {
    "mgboost_admin_grant_template_jobs": {
        "account_id", "decision_ref", "state", "attempts", "last_error_class",
        "created_at", "updated_at",
    },
}


def _verify(connection: sqlite3.Connection) -> None:
    for table, required in _REQUIRED_COLUMNS.items():
        actual = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        if required - actual:
            raise RuntimeError(f"PH7-14 incompatible table {table}")


def apply_admin_grant_schema(connection: sqlite3.Connection, *, now: int | None = None) -> bool:
    connection.execute("PRAGMA foreign_keys=ON")
    timestamp = int(time.time()) if now is None else int(now)
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT schema_checksum FROM mgboost_schema_migrations WHERE migration_id=?",
            (ACCOUNT_MIGRATION_ID,),
        ).fetchone()
        if not row or row[0] != ACCOUNT_SCHEMA_CHECKSUM:
            raise RuntimeError("PH7-14 requires exact PH3-01 schema")
        row = connection.execute(
            "SELECT schema_checksum FROM mgboost_schema_migrations WHERE migration_id=?",
            (MIGRATION_ID,),
        ).fetchone()
        if row:
            if row[0] != SCHEMA_CHECKSUM:
                raise RuntimeError("PH7-14 schema checksum mismatch")
            _verify(connection)
            connection.commit()
            return False
        for statement in _SCHEMA_STATEMENTS:
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
