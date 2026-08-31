"""Additive hardening for the P0 legacy-commercial transition contract."""
from __future__ import annotations

import hashlib
import sqlite3
import time

from .legacy_commercial_transition_schema import MIGRATION_ID as V1_MIGRATION_ID
from .legacy_commercial_transition_schema import SCHEMA_CHECKSUM as V1_SCHEMA_CHECKSUM

MIGRATION_ID = "p0_legacy_commercial_transition_v2"

_S = (
    "ALTER TABLE mgboost_legacy_commercial_transitions ADD COLUMN source_subscription_id INTEGER REFERENCES mgboost_subscriptions(id) ON DELETE RESTRICT",
    "ALTER TABLE mgboost_legacy_commercial_transitions ADD COLUMN source_post_confirmation_row_version INTEGER",
    "DROP TRIGGER trg_mgboost_transition_payment_cancel_propagates",
    """CREATE TRIGGER trg_mgboost_transition_payment_cancel_propagates
       AFTER UPDATE OF status ON mgboost_manual_payment_records
       WHEN OLD.status='PENDING' AND NEW.status='CANCELLED'
       BEGIN
         UPDATE mgboost_legacy_commercial_transitions
            SET state='CANCELLED',revision=revision+1,updated_at=NEW.updated_at
          WHERE payment_record_id=NEW.id AND state='PENDING_PAYMENT'
            AND payment_confirmed_at IS NULL;
         INSERT INTO mgboost_legacy_commercial_transition_events
              (transition_id,event_type,actor_ref,reason,revision,created_at)
         SELECT id,'CANCELLED',NEW.actor_ref,COALESCE(NEW.cancel_reason,'payment cancelled'),
                revision,NEW.updated_at
           FROM mgboost_legacy_commercial_transitions
          WHERE payment_record_id=NEW.id AND state='CANCELLED'
            AND payment_confirmed_at IS NULL
            AND NOT EXISTS (
              SELECT 1 FROM mgboost_legacy_commercial_transition_events e
               WHERE e.transition_id=mgboost_legacy_commercial_transitions.id
                 AND e.event_type='CANCELLED');
       END""",
    """CREATE TRIGGER trg_mgboost_legacy_transition_source_identity_frozen
       BEFORE UPDATE OF source_subscription_id,source_post_confirmation_row_version
       ON mgboost_legacy_commercial_transitions
       WHEN OLD.payment_confirmed_at IS NOT NULL AND
            (NEW.source_subscription_id IS NOT OLD.source_subscription_id OR
             NEW.source_post_confirmation_row_version IS NOT OLD.source_post_confirmation_row_version)
       BEGIN SELECT RAISE(ABORT,'confirmed source identity is immutable'); END""",
    """CREATE TRIGGER trg_mgboost_legacy_transition_confirmation_requires_source_identity
       BEFORE UPDATE OF payment_confirmed_at ON mgboost_legacy_commercial_transitions
       WHEN OLD.payment_confirmed_at IS NULL AND NEW.payment_confirmed_at IS NOT NULL AND
            (NEW.source_subscription_id IS NULL OR NEW.source_post_confirmation_row_version IS NULL)
       BEGIN SELECT RAISE(ABORT,'confirmed transition requires frozen source identity'); END""",
)

SCHEMA_CHECKSUM = hashlib.sha256("\n".join(value.strip() for value in _S).encode()).hexdigest()


def apply_legacy_commercial_transition_schema_v2(
    connection: sqlite3.Connection, *, now: int | None = None,
) -> bool:
    timestamp = int(time.time()) if now is None else int(now)
    connection.execute("PRAGMA foreign_keys=ON")
    try:
        connection.execute("BEGIN IMMEDIATE")
        dependency = connection.execute(
            "SELECT schema_checksum FROM mgboost_schema_migrations WHERE migration_id=?",
            (V1_MIGRATION_ID,),
        ).fetchone()
        if not dependency or dependency[0] != V1_SCHEMA_CHECKSUM:
            raise RuntimeError("legacy transition v2 dependency mismatch")
        row = connection.execute(
            "SELECT schema_checksum FROM mgboost_schema_migrations WHERE migration_id=?",
            (MIGRATION_ID,),
        ).fetchone()
        if row:
            if row[0] != SCHEMA_CHECKSUM:
                raise RuntimeError("legacy transition v2 schema checksum mismatch")
            connection.commit()
            return False
        for statement in _S:
            connection.execute(statement)
        connection.execute(
            "INSERT INTO mgboost_schema_migrations (migration_id,schema_checksum,applied_at) VALUES (?,?,?)",
            (MIGRATION_ID, SCHEMA_CHECKSUM, timestamp),
        )
        connection.commit()
        return True
    except Exception:
        connection.rollback()
        raise
