"""PH5-13 additive v2 repair for the Stars promo-snapshot trigger.

``ph5_13_promo_codes_v1`` has already reached production.  Its immutable
trigger protected rewrites of an existing snapshot but accidentally allowed a
NULL -> non-NULL attachment.  Never rewrite that checksum-pinned migration:
this v2 migration replaces only the trigger under its own marker.
"""

from __future__ import annotations

import hashlib
import sqlite3
import time

from .promo_schema import MIGRATION_ID as V1_MIGRATION_ID
from .promo_schema import SCHEMA_CHECKSUM as V1_SCHEMA_CHECKSUM

MIGRATION_ID = "ph5_13_promo_codes_v2_snapshot_immutable"

_SCHEMA_STATEMENTS = (
    "DROP TRIGGER IF EXISTS trg_stars_invoices_promo_snapshot_immutable",
    """
    CREATE TRIGGER trg_stars_invoices_promo_snapshot_immutable
        BEFORE UPDATE OF promo_redemption_id, original_stars_price, discount_minor
        ON stars_invoices
        WHEN (
            NEW.promo_redemption_id IS NOT OLD.promo_redemption_id
            OR NEW.original_stars_price IS NOT OLD.original_stars_price
            OR NEW.discount_minor IS NOT OLD.discount_minor
        )
        BEGIN SELECT RAISE(ABORT, 'stars invoice promo discount snapshot is immutable'); END
    """,
)

SCHEMA_CHECKSUM = hashlib.sha256(
    "\n".join(statement.strip() for statement in _SCHEMA_STATEMENTS).encode("utf-8")
).hexdigest()


def _verify(connection: sqlite3.Connection) -> None:
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
        ("trg_stars_invoices_promo_snapshot_immutable",),
    ).fetchone()
    if row is None or "OLD.promo_redemption_id IS NOT NULL" in row[0]:
        raise RuntimeError("PH5-13 v2 immutable snapshot trigger is missing or obsolete")


def apply_promo_schema_v2(connection: sqlite3.Connection, *, now: int | None = None) -> bool:
    timestamp = int(time.time()) if now is None else int(now)
    try:
        connection.execute("BEGIN IMMEDIATE")
        parent = connection.execute(
            "SELECT schema_checksum FROM mgboost_schema_migrations WHERE migration_id=?",
            (V1_MIGRATION_ID,),
        ).fetchone()
        if parent is None or parent[0] != V1_SCHEMA_CHECKSUM:
            raise RuntimeError("PH5-13 v2 requires exact PH5-13 v1 schema")
        existing = connection.execute(
            "SELECT schema_checksum FROM mgboost_schema_migrations WHERE migration_id=?",
            (MIGRATION_ID,),
        ).fetchone()
        if existing is not None:
            if existing[0] != SCHEMA_CHECKSUM:
                raise RuntimeError("PH5-13 v2 schema checksum mismatch")
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
