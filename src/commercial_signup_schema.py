"""PH5-11 additive schema for the first commercial STANDARD signup flow.

Two concerns, both crash-safe and additive:

1. ``mgboost_provisioning_templates`` -- the per-account SYSTEM-OWNED
   provisioning anchor. A brand-new commercial customer has no legacy
   account, no legacy subscription URL and no real legacy Marzban user, so
   first-device bootstrap needs an infrastructure-owned source user whose
   exact VLESS contract (flow + inbound membership == the STANDARD delivery
   profile) is pinned here as a hash. The template is infrastructure, never
   a customer identity: the customer never receives the template's UUID or
   subscription URL, and every occupied device slot still gets its own child
   user with its own Marzban-minted UUID (validated by the existing
   ``child_contract.validate_created_child``).

2. ``mgboost_signup_template_jobs`` -- the durable retry job that drives
   remote template provisioning after a confirmed payment. Failure here
   never loses the paid entitlement: account, subscription and credential
   are already durable; the job retries until the template exists, and a
   first device simply fail-closes (existing uniform response) until then.

The Stars signup invoice kind (``CANONICAL_SIGNUP``) reuses the existing
``stars_invoices`` row: its nullable ``account_id`` becomes the fill-once
account anchor, enforced by trigger exactly like the direct-enrollment
intent's fill-once pattern.
"""

from __future__ import annotations

import hashlib
import sqlite3
import time

from .account_schema import MIGRATION_ID as ACCOUNT_MIGRATION_ID
from .account_schema import SCHEMA_CHECKSUM as ACCOUNT_SCHEMA_CHECKSUM
from .stars_purchase_schema import MIGRATION_ID as STARS_MIGRATION_ID
from .stars_purchase_schema import SCHEMA_CHECKSUM as STARS_SCHEMA_CHECKSUM


MIGRATION_ID = "ph5_11_commercial_signup_v1"

_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS mgboost_provisioning_templates (
        account_id INTEGER PRIMARY KEY,
        template_username TEXT NOT NULL UNIQUE,
        source_contract_hash TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'ACTIVE' CHECK(state IN ('ACTIVE','REVOKED')),
        pinned_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        FOREIGN KEY(account_id) REFERENCES mgboost_accounts(id) ON DELETE RESTRICT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS mgboost_signup_template_jobs (
        account_id INTEGER PRIMARY KEY,
        invoice_id INTEGER NOT NULL,
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
    CREATE INDEX IF NOT EXISTS ix_mgboost_signup_template_jobs_pending
        ON mgboost_signup_template_jobs(state, updated_at, account_id)
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_stars_signup_account_fill_once
        BEFORE UPDATE OF account_id ON stars_invoices
        WHEN OLD.invoice_kind='CANONICAL_SIGNUP'
             AND OLD.account_id IS NOT NULL
             AND NEW.account_id IS NOT NULL
             AND NEW.account_id != OLD.account_id
        BEGIN SELECT RAISE(ABORT, 'signup invoice account binding is fill-once'); END
    """,
)

SCHEMA_CHECKSUM = hashlib.sha256(
    "\n".join(statement.strip() for statement in _SCHEMA_STATEMENTS).encode("utf-8")
).hexdigest()

_REQUIRED_COLUMNS = {
    "mgboost_provisioning_templates": {
        "account_id", "template_username", "source_contract_hash", "state",
        "pinned_at", "updated_at",
    },
    "mgboost_signup_template_jobs": {
        "account_id", "invoice_id", "state", "attempts", "last_error_class",
        "created_at", "updated_at",
    },
}


def _verify(connection: sqlite3.Connection) -> None:
    for table, required in _REQUIRED_COLUMNS.items():
        actual = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        if required - actual:
            raise RuntimeError(f"PH5-11 incompatible table {table}")
    triggers = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
    if "trg_stars_signup_account_fill_once" not in triggers:
        raise RuntimeError("PH5-11 signup fill-once trigger missing")


def apply_commercial_signup_schema(connection: sqlite3.Connection, *, now: int | None = None) -> bool:
    connection.execute("PRAGMA foreign_keys=ON")
    timestamp = int(time.time()) if now is None else int(now)
    try:
        connection.execute("BEGIN IMMEDIATE")
        for migration_id, checksum, label in (
            (ACCOUNT_MIGRATION_ID, ACCOUNT_SCHEMA_CHECKSUM, "PH3-01"),
            (STARS_MIGRATION_ID, STARS_SCHEMA_CHECKSUM, "PH5-05"),
        ):
            row = connection.execute(
                "SELECT schema_checksum FROM mgboost_schema_migrations WHERE migration_id=?",
                (migration_id,),
            ).fetchone()
            if not row or row[0] != checksum:
                raise RuntimeError(f"PH5-11 requires exact {label} schema")
        row = connection.execute(
            "SELECT schema_checksum FROM mgboost_schema_migrations WHERE migration_id=?",
            (MIGRATION_ID,),
        ).fetchone()
        if row:
            if row[0] != SCHEMA_CHECKSUM:
                raise RuntimeError("PH5-11 schema checksum mismatch")
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
