"""DL-063: additive durable contract for self-service Stars-paid
LEGACY_PAID_COMPAT_V1_* -> commercial plan switches.

Modeled on ``legacy_commercial_transition_schema.py``'s CAS/immutability
pattern, but keyed to ``stars_invoices`` rows instead of manual RUB payment
records. Every table here is additive -- nothing in ``stars_purchase_schema``
or ``legacy_commercial_transition_schema`` is altered, per DL-063's explicit
decision not to polymorphize either checksum-locked schema's FK/CHECK
contract for a second, structurally different source of records.
"""
from __future__ import annotations

import hashlib
import sqlite3
import time

from .stars_purchase_schema import MIGRATION_ID as STARS_MIGRATION_ID
from .stars_purchase_schema import SCHEMA_CHECKSUM as STARS_SCHEMA_CHECKSUM
from .legacy_commercial_transition_schema import MIGRATION_ID as LEGACY_MIGRATION_ID
from .legacy_commercial_transition_schema import SCHEMA_CHECKSUM as LEGACY_SCHEMA_CHECKSUM

MIGRATION_ID = "dl063_legacy_stars_plan_switch_v1"

_S = (
"""CREATE TABLE IF NOT EXISTS mgboost_legacy_stars_plan_switches (
 id INTEGER PRIMARY KEY AUTOINCREMENT, public_id TEXT NOT NULL UNIQUE,
 account_id INTEGER NOT NULL, invoice_id INTEGER NOT NULL UNIQUE,
 state TEXT NOT NULL CHECK(state IN ('PENDING_PAYMENT','SCHEDULED','APPLYING','APPLIED','MANUAL_REVIEW','CANCELLED')),
 revision INTEGER NOT NULL DEFAULT 1 CHECK(revision>0),
 source_plan_version_id INTEGER NOT NULL, source_subscription_id INTEGER,
 source_subscription_status TEXT, original_source_expiry INTEGER,
 aligned_source_expiry INTEGER, target_plan_version_id INTEGER NOT NULL,
 duration_days INTEGER NOT NULL CHECK(duration_days IN (30,60)), target_expiry INTEGER,
 confirmed_at INTEGER, activation_at INTEGER,
 source_post_confirmation_row_version INTEGER, device_count_at_confirm INTEGER,
 review_reason TEXT, applied_at INTEGER,
 created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
 FOREIGN KEY(account_id) REFERENCES mgboost_accounts(id) ON DELETE RESTRICT,
 FOREIGN KEY(invoice_id) REFERENCES stars_invoices(id) ON DELETE RESTRICT,
 FOREIGN KEY(source_plan_version_id) REFERENCES mgboost_plan_versions(id) ON DELETE RESTRICT,
 FOREIGN KEY(target_plan_version_id) REFERENCES mgboost_plan_versions(id) ON DELETE RESTRICT,
 CHECK((confirmed_at IS NULL AND activation_at IS NULL AND target_expiry IS NULL) OR (confirmed_at IS NOT NULL AND activation_at IS NOT NULL AND target_expiry=activation_at+duration_days*86400)),
 CHECK(aligned_source_expiry IS NULL OR aligned_source_expiry % 3600 = 0)
)""",
"""CREATE UNIQUE INDEX IF NOT EXISTS ux_mgboost_legacy_stars_switch_live_account ON mgboost_legacy_stars_plan_switches(account_id) WHERE state NOT IN ('APPLIED','CANCELLED')""",
"""CREATE INDEX IF NOT EXISTS ix_mgboost_legacy_stars_switch_due ON mgboost_legacy_stars_plan_switches(state,activation_at,id)""",
"""CREATE TABLE IF NOT EXISTS mgboost_legacy_stars_plan_switch_events (
 id INTEGER PRIMARY KEY AUTOINCREMENT, switch_id INTEGER NOT NULL,
 event_type TEXT NOT NULL CHECK(event_type IN ('CREATED','CONFIRMED','APPLIED','MANUAL_REVIEW','MANUAL_REVIEW_RETRY','CANCELLED')),
 actor_ref TEXT NOT NULL CHECK(length(actor_ref) BETWEEN 1 AND 128),
 reason TEXT NOT NULL CHECK(length(reason) BETWEEN 1 AND 300),
 revision INTEGER NOT NULL, created_at INTEGER NOT NULL,
 FOREIGN KEY(switch_id) REFERENCES mgboost_legacy_stars_plan_switches(id) ON DELETE RESTRICT
)""",
"""CREATE TABLE IF NOT EXISTS mgboost_legacy_stars_plan_switch_applications (
 id INTEGER PRIMARY KEY AUTOINCREMENT, invoice_id INTEGER NOT NULL UNIQUE,
 account_id INTEGER NOT NULL, entitlement_mutation_id INTEGER NOT NULL UNIQUE,
 applied_expiry INTEGER NOT NULL, entitlement_snapshot_json TEXT NOT NULL,
 created_at INTEGER NOT NULL,
 FOREIGN KEY(invoice_id) REFERENCES stars_invoices(id) ON DELETE RESTRICT,
 FOREIGN KEY(account_id) REFERENCES mgboost_accounts(id) ON DELETE RESTRICT,
 FOREIGN KEY(entitlement_mutation_id,account_id) REFERENCES mgboost_entitlement_mutations(id,account_id) ON DELETE RESTRICT
)""",
"""CREATE TABLE IF NOT EXISTS mgboost_legacy_stars_plan_switch_wl_baselines (
 id INTEGER PRIMARY KEY AUTOINCREMENT, switch_id INTEGER NOT NULL, account_id INTEGER NOT NULL,
 wl_period_id INTEGER NOT NULL, child_intent_id INTEGER NOT NULL, node_id INTEGER NOT NULL,
 state TEXT NOT NULL DEFAULT 'PENDING' CHECK(state IN ('PENDING','CONSUMED')),
 created_at INTEGER NOT NULL, consumed_at INTEGER,
 UNIQUE(switch_id,child_intent_id,node_id),
 FOREIGN KEY(switch_id) REFERENCES mgboost_legacy_stars_plan_switches(id) ON DELETE RESTRICT,
 FOREIGN KEY(wl_period_id) REFERENCES mgboost_wl_periods(id) ON DELETE RESTRICT,
 FOREIGN KEY(child_intent_id,account_id) REFERENCES mgboost_child_user_intents(id,account_id) ON DELETE RESTRICT
)""",
"""CREATE INDEX IF NOT EXISTS ix_mgboost_legacy_stars_switch_wl_baseline_pending ON mgboost_legacy_stars_plan_switch_wl_baselines(child_intent_id,node_id,state)""",
"""CREATE TRIGGER IF NOT EXISTS trg_mgboost_legacy_stars_switch_events_no_update BEFORE UPDATE ON mgboost_legacy_stars_plan_switch_events BEGIN SELECT RAISE(ABORT,'switch events are append-only'); END""",
"""CREATE TRIGGER IF NOT EXISTS trg_mgboost_legacy_stars_switch_events_no_delete BEFORE DELETE ON mgboost_legacy_stars_plan_switch_events BEGIN SELECT RAISE(ABORT,'switch events are never deleted'); END""",
"""CREATE TRIGGER IF NOT EXISTS trg_mgboost_legacy_stars_switch_no_delete BEFORE DELETE ON mgboost_legacy_stars_plan_switches BEGIN SELECT RAISE(ABORT,'legacy stars plan switches are never deleted'); END""",
"""CREATE TRIGGER IF NOT EXISTS trg_mgboost_legacy_stars_switch_frozen BEFORE UPDATE ON mgboost_legacy_stars_plan_switches WHEN OLD.confirmed_at IS NOT NULL AND (NEW.source_plan_version_id!=OLD.source_plan_version_id OR NEW.source_subscription_id IS NOT OLD.source_subscription_id OR NEW.source_subscription_status IS NOT OLD.source_subscription_status OR NEW.original_source_expiry IS NOT OLD.original_source_expiry OR NEW.aligned_source_expiry!=OLD.aligned_source_expiry OR NEW.invoice_id!=OLD.invoice_id OR NEW.target_plan_version_id!=OLD.target_plan_version_id OR NEW.duration_days!=OLD.duration_days OR NEW.activation_at!=OLD.activation_at OR NEW.target_expiry!=OLD.target_expiry OR NEW.confirmed_at!=OLD.confirmed_at) BEGIN SELECT RAISE(ABORT,'confirmed switch facts are immutable'); END""",
"""CREATE TRIGGER IF NOT EXISTS trg_mgboost_legacy_stars_switch_state_machine BEFORE UPDATE OF state ON mgboost_legacy_stars_plan_switches WHEN NEW.state!=OLD.state AND NOT ((OLD.state='PENDING_PAYMENT' AND NEW.state IN ('SCHEDULED','MANUAL_REVIEW','CANCELLED')) OR (OLD.state='SCHEDULED' AND NEW.state IN ('APPLYING','MANUAL_REVIEW')) OR (OLD.state='APPLYING' AND NEW.state IN ('APPLIED','MANUAL_REVIEW')) OR (OLD.state='MANUAL_REVIEW' AND NEW.state='SCHEDULED')) BEGIN SELECT RAISE(ABORT,'invalid legacy stars switch state change'); END""",
"""CREATE TRIGGER IF NOT EXISTS trg_mgboost_legacy_stars_switch_application_no_update BEFORE UPDATE ON mgboost_legacy_stars_plan_switch_applications BEGIN SELECT RAISE(ABORT,'legacy stars switch application is immutable'); END""",
"""CREATE TRIGGER IF NOT EXISTS trg_mgboost_legacy_stars_switch_application_no_delete BEFORE DELETE ON mgboost_legacy_stars_plan_switch_applications BEGIN SELECT RAISE(ABORT,'legacy stars switch application is never deleted'); END""",
"""CREATE TRIGGER IF NOT EXISTS trg_mgboost_legacy_stars_switch_wl_baseline_identity_immutable BEFORE UPDATE OF switch_id,account_id,wl_period_id,child_intent_id,node_id,created_at ON mgboost_legacy_stars_plan_switch_wl_baselines BEGIN SELECT RAISE(ABORT,'switch wl baseline identity is immutable'); END""",
"""CREATE TRIGGER IF NOT EXISTS trg_mgboost_legacy_stars_switch_wl_baseline_no_delete BEFORE DELETE ON mgboost_legacy_stars_plan_switch_wl_baselines BEGIN SELECT RAISE(ABORT,'switch wl baselines are never deleted'); END""",
# Kind-scoped freeze, separate from trg_stars_canonical_product_snapshot_immutable
# (which only fires for OLD.invoice_kind='CANONICAL_PLAN') -- does not touch
# that existing trigger's WHEN clause or guarantees.
"""CREATE TRIGGER IF NOT EXISTS trg_stars_legacy_switch_product_snapshot_immutable
    BEFORE UPDATE OF invoice_kind,account_id,plan_version_id,duration_id,
                     catalog_version_id,price_id,plan_code_snapshot,
                     plan_version_snapshot,catalog_version_snapshot,price_amount_snapshot
    ON stars_invoices
    WHEN OLD.invoice_kind='LEGACY_PLAN_SWITCH'
    BEGIN SELECT RAISE(ABORT, 'legacy switch Stars product snapshot is immutable'); END""",
)
SCHEMA_CHECKSUM = hashlib.sha256('\n'.join(x.strip() for x in _S).encode()).hexdigest()


def apply_legacy_stars_plan_switch_schema(connection: sqlite3.Connection, *, now: int | None = None) -> bool:
    timestamp = int(time.time()) if now is None else int(now)
    connection.execute('PRAGMA foreign_keys=ON')
    try:
        connection.execute('BEGIN IMMEDIATE')
        for mid, check in ((STARS_MIGRATION_ID, STARS_SCHEMA_CHECKSUM), (LEGACY_MIGRATION_ID, LEGACY_SCHEMA_CHECKSUM)):
            row = connection.execute(
                'SELECT schema_checksum FROM mgboost_schema_migrations WHERE migration_id=?', (mid,)
            ).fetchone()
            if not row or row[0] != check:
                raise RuntimeError('legacy stars plan switch schema dependency mismatch')
        row = connection.execute(
            'SELECT schema_checksum FROM mgboost_schema_migrations WHERE migration_id=?', (MIGRATION_ID,)
        ).fetchone()
        if row:
            if row[0] != SCHEMA_CHECKSUM:
                raise RuntimeError('legacy stars plan switch schema checksum mismatch')
            connection.commit()
            return False
        for statement in _S:
            connection.execute(statement)
        connection.execute(
            'INSERT INTO mgboost_schema_migrations (migration_id,schema_checksum,applied_at) VALUES (?,?,?)',
            (MIGRATION_ID, SCHEMA_CHECKSUM, timestamp),
        )
        connection.commit()
        return True
    except Exception:
        connection.rollback()
        raise
