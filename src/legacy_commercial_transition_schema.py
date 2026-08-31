"""Additive durable contract for paid LEGACY_PAID_COMPAT -> commercial moves."""
from __future__ import annotations

import hashlib
import sqlite3
import time

from .manual_payment_schema import MIGRATION_ID as PAYMENT_MIGRATION_ID
from .manual_payment_schema import SCHEMA_CHECKSUM as PAYMENT_SCHEMA_CHECKSUM
from .wl_usage_ledger_schema_v2 import MIGRATION_ID as LEDGER_MIGRATION_ID
from .wl_usage_ledger_schema_v2 import SCHEMA_CHECKSUM as LEDGER_SCHEMA_CHECKSUM

MIGRATION_ID = "p0_legacy_commercial_transition_v1"

_S = (
"""CREATE TABLE IF NOT EXISTS mgboost_legacy_commercial_transitions (
 id INTEGER PRIMARY KEY AUTOINCREMENT, public_id TEXT NOT NULL UNIQUE,
 account_id INTEGER NOT NULL, payment_record_id INTEGER NOT NULL UNIQUE,
 state TEXT NOT NULL CHECK(state IN ('PENDING_PAYMENT','PAYMENT_CONFIRMED','SCHEDULED','SELECTION_REQUIRED','SELECTION_RECORDED','RETIREMENT_IN_PROGRESS','READY_TO_APPLY','APPLYING','APPLIED','MANUAL_REVIEW','CANCELLED')),
 revision INTEGER NOT NULL DEFAULT 1 CHECK(revision>0),
 lease_owner TEXT, lease_expires_at INTEGER,
 source_plan_version_id INTEGER NOT NULL, source_subscription_status TEXT NOT NULL,
 original_source_expiry INTEGER,
 aligned_source_expiry INTEGER NOT NULL, payment_confirmed_at INTEGER,
 activation_at INTEGER, target_plan_version_id INTEGER NOT NULL,
 duration_days INTEGER NOT NULL CHECK(duration_days IN (30,60)), target_expiry INTEGER,
 catalog_version_id INTEGER NOT NULL, plan_price_id INTEGER NOT NULL,
 expected_amount_minor INTEGER NOT NULL CHECK(expected_amount_minor>0),
 actor_ref TEXT NOT NULL CHECK(length(actor_ref) BETWEEN 1 AND 128),
 reason TEXT NOT NULL CHECK(length(reason) BETWEEN 3 AND 300),
 review_reason TEXT, applied_at INTEGER, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
 FOREIGN KEY(account_id) REFERENCES mgboost_accounts(id) ON DELETE RESTRICT,
 FOREIGN KEY(payment_record_id) REFERENCES mgboost_manual_payment_records(id) ON DELETE RESTRICT,
 FOREIGN KEY(source_plan_version_id) REFERENCES mgboost_plan_versions(id) ON DELETE RESTRICT,
 FOREIGN KEY(target_plan_version_id) REFERENCES mgboost_plan_versions(id) ON DELETE RESTRICT,
 FOREIGN KEY(catalog_version_id) REFERENCES mgboost_price_catalog_versions(id) ON DELETE RESTRICT,
 FOREIGN KEY(plan_price_id) REFERENCES mgboost_plan_prices(id) ON DELETE RESTRICT,
 CHECK((payment_confirmed_at IS NULL AND activation_at IS NULL AND target_expiry IS NULL) OR (payment_confirmed_at IS NOT NULL AND activation_at IS NOT NULL AND target_expiry=activation_at+duration_days*86400)),
 CHECK(aligned_source_expiry % 3600 = 0)
)""",
"""CREATE UNIQUE INDEX IF NOT EXISTS ux_mgboost_legacy_transition_live_account ON mgboost_legacy_commercial_transitions(account_id) WHERE state NOT IN ('APPLIED','CANCELLED')""",
"""CREATE INDEX IF NOT EXISTS ix_mgboost_legacy_transition_due ON mgboost_legacy_commercial_transitions(state,activation_at,id)""",
"""CREATE TABLE IF NOT EXISTS mgboost_legacy_commercial_transition_events (
 id INTEGER PRIMARY KEY AUTOINCREMENT, transition_id INTEGER NOT NULL, event_type TEXT NOT NULL CHECK(event_type IN ('CREATED','PAYMENT_CONFIRMED','ALIGNMENT_GRACE','SELECTION_RECORDED','RETIREMENT_STARTED','READY','APPLIED','MANUAL_REVIEW','MANUAL_REVIEW_RETRY','CANCELLED')), actor_ref TEXT NOT NULL CHECK(length(actor_ref) BETWEEN 1 AND 128), reason TEXT NOT NULL CHECK(length(reason) BETWEEN 1 AND 300), revision INTEGER NOT NULL, created_at INTEGER NOT NULL,
 FOREIGN KEY(transition_id) REFERENCES mgboost_legacy_commercial_transitions(id) ON DELETE RESTRICT
)""",
"""CREATE TABLE IF NOT EXISTS mgboost_legacy_commercial_transition_selections (
 id INTEGER PRIMARY KEY AUTOINCREMENT, transition_id INTEGER NOT NULL, slot_generation_id INTEGER NOT NULL, actor_ref TEXT NOT NULL, created_at INTEGER NOT NULL, UNIQUE(transition_id,slot_generation_id), FOREIGN KEY(transition_id) REFERENCES mgboost_legacy_commercial_transitions(id) ON DELETE RESTRICT, FOREIGN KEY(slot_generation_id) REFERENCES mgboost_device_slot_generations(id) ON DELETE RESTRICT
)""",
"""CREATE TRIGGER IF NOT EXISTS trg_mgboost_legacy_transition_events_no_update BEFORE UPDATE ON mgboost_legacy_commercial_transition_events BEGIN SELECT RAISE(ABORT,'transition events are append-only'); END""",
"""CREATE TRIGGER IF NOT EXISTS trg_mgboost_legacy_transition_events_no_delete BEFORE DELETE ON mgboost_legacy_commercial_transition_events BEGIN SELECT RAISE(ABORT,'transition events are never deleted'); END""",
"""CREATE TRIGGER IF NOT EXISTS trg_mgboost_legacy_transition_selection_no_update BEFORE UPDATE ON mgboost_legacy_commercial_transition_selections BEGIN SELECT RAISE(ABORT,'transition selections are append-only'); END""",
"""CREATE TRIGGER IF NOT EXISTS trg_mgboost_legacy_transition_selection_no_delete BEFORE DELETE ON mgboost_legacy_commercial_transition_selections BEGIN SELECT RAISE(ABORT,'transition selections are never deleted'); END""",
"""CREATE TRIGGER IF NOT EXISTS trg_mgboost_legacy_transition_frozen BEFORE UPDATE ON mgboost_legacy_commercial_transitions WHEN OLD.payment_confirmed_at IS NOT NULL AND (NEW.source_plan_version_id!=OLD.source_plan_version_id OR NEW.source_subscription_status!=OLD.source_subscription_status OR NEW.original_source_expiry IS NOT OLD.original_source_expiry OR NEW.aligned_source_expiry!=OLD.aligned_source_expiry OR NEW.payment_record_id!=OLD.payment_record_id OR NEW.target_plan_version_id!=OLD.target_plan_version_id OR NEW.duration_days!=OLD.duration_days OR NEW.catalog_version_id!=OLD.catalog_version_id OR NEW.plan_price_id!=OLD.plan_price_id OR NEW.expected_amount_minor!=OLD.expected_amount_minor OR NEW.activation_at!=OLD.activation_at OR NEW.target_expiry!=OLD.target_expiry) BEGIN SELECT RAISE(ABORT,'confirmed transition facts are immutable'); END""",
"""CREATE TRIGGER IF NOT EXISTS trg_mgboost_legacy_transition_no_delete BEFORE DELETE ON mgboost_legacy_commercial_transitions BEGIN SELECT RAISE(ABORT,'legacy transitions are never deleted'); END""",
"""CREATE TRIGGER IF NOT EXISTS trg_mgboost_legacy_transition_state_machine BEFORE UPDATE OF state ON mgboost_legacy_commercial_transitions WHEN NEW.state!=OLD.state AND NOT ((OLD.state='PENDING_PAYMENT' AND NEW.state IN ('SCHEDULED','SELECTION_REQUIRED','CANCELLED')) OR (OLD.state='SCHEDULED' AND NEW.state IN ('SELECTION_REQUIRED','RETIREMENT_IN_PROGRESS','MANUAL_REVIEW')) OR (OLD.state='SELECTION_REQUIRED' AND NEW.state IN ('SELECTION_RECORDED','MANUAL_REVIEW')) OR (OLD.state='SELECTION_RECORDED' AND NEW.state IN ('RETIREMENT_IN_PROGRESS','MANUAL_REVIEW')) OR (OLD.state='RETIREMENT_IN_PROGRESS' AND NEW.state IN ('READY_TO_APPLY','MANUAL_REVIEW')) OR (OLD.state='READY_TO_APPLY' AND NEW.state IN ('APPLYING','MANUAL_REVIEW')) OR (OLD.state='APPLYING' AND NEW.state IN ('APPLIED','MANUAL_REVIEW')) OR (OLD.state='MANUAL_REVIEW' AND NEW.state IN ('SCHEDULED','SELECTION_REQUIRED','SELECTION_RECORDED'))) BEGIN SELECT RAISE(ABORT,'invalid legacy transition state change'); END""",
"""CREATE TRIGGER IF NOT EXISTS trg_mgboost_transition_payment_locked BEFORE UPDATE ON mgboost_manual_payment_records WHEN EXISTS (SELECT 1 FROM mgboost_legacy_commercial_transitions t WHERE t.payment_record_id=OLD.id AND t.payment_confirmed_at IS NOT NULL AND t.state!='APPLYING') BEGIN SELECT RAISE(ABORT,'confirmed legacy transition payment is orchestrator-locked'); END""",
"""CREATE TRIGGER IF NOT EXISTS trg_mgboost_transition_payment_facts_frozen BEFORE UPDATE OF account_id,plan_version_id,duration_id,catalog_version_id,catalog_version_snapshot,plan_price_id,plan_code_snapshot,plan_version_snapshot,duration_days_snapshot,expected_amount_minor,recorded_amount_minor,currency,payment_method,external_reference,comment ON mgboost_manual_payment_records WHEN EXISTS (SELECT 1 FROM mgboost_legacy_commercial_transitions t WHERE t.payment_record_id=OLD.id AND t.payment_confirmed_at IS NOT NULL) BEGIN SELECT RAISE(ABORT,'confirmed legacy transition payment facts are immutable'); END""",
"""CREATE TRIGGER IF NOT EXISTS trg_mgboost_transition_payment_apply_shape BEFORE UPDATE ON mgboost_manual_payment_records WHEN EXISTS (SELECT 1 FROM mgboost_legacy_commercial_transitions t WHERE t.payment_record_id=OLD.id AND t.payment_confirmed_at IS NOT NULL AND t.state='APPLYING') AND NOT (OLD.status='PENDING' AND NEW.status='APPLIED' AND NEW.applied_at IS NOT NULL AND NEW.entitlement_mutation_id IS NOT NULL AND NEW.applied_operation='RENEW' AND NEW.applied_expiry IS NOT NULL AND NEW.cancelled_at IS OLD.cancelled_at AND NEW.cancel_reason IS OLD.cancel_reason AND NEW.review_reason IS OLD.review_reason AND NEW.review_at IS OLD.review_at) BEGIN SELECT RAISE(ABORT,'legacy transition payment update is not an apply'); END""",
"""CREATE TRIGGER IF NOT EXISTS trg_mgboost_transition_payment_cancel_propagates AFTER UPDATE OF status ON mgboost_manual_payment_records WHEN OLD.status='PENDING' AND NEW.status='CANCELLED' BEGIN UPDATE mgboost_legacy_commercial_transitions SET state='CANCELLED',revision=revision+1,updated_at=NEW.updated_at WHERE payment_record_id=NEW.id AND state='PENDING_PAYMENT' AND payment_confirmed_at IS NULL; END""",
"""CREATE TABLE IF NOT EXISTS mgboost_wl_transition_baselines (
 id INTEGER PRIMARY KEY AUTOINCREMENT, transition_id INTEGER NOT NULL, account_id INTEGER NOT NULL, wl_period_id INTEGER NOT NULL, child_intent_id INTEGER NOT NULL, node_id INTEGER NOT NULL, state TEXT NOT NULL DEFAULT 'PENDING' CHECK(state IN ('PENDING','CONSUMED')), created_at INTEGER NOT NULL, consumed_at INTEGER, UNIQUE(transition_id,child_intent_id,node_id), FOREIGN KEY(transition_id) REFERENCES mgboost_legacy_commercial_transitions(id) ON DELETE RESTRICT, FOREIGN KEY(wl_period_id) REFERENCES mgboost_wl_periods(id) ON DELETE RESTRICT, FOREIGN KEY(child_intent_id,account_id) REFERENCES mgboost_child_user_intents(id,account_id) ON DELETE RESTRICT
)""",
"""CREATE INDEX IF NOT EXISTS ix_mgboost_wl_transition_baseline_pending ON mgboost_wl_transition_baselines(child_intent_id,node_id,state)""",
"""CREATE TRIGGER IF NOT EXISTS trg_mgboost_wl_transition_baseline_identity_immutable BEFORE UPDATE OF transition_id,account_id,wl_period_id,child_intent_id,node_id,created_at ON mgboost_wl_transition_baselines BEGIN SELECT RAISE(ABORT,'transition baseline identity is immutable'); END""",
"""CREATE TRIGGER IF NOT EXISTS trg_mgboost_wl_transition_baseline_no_delete BEFORE DELETE ON mgboost_wl_transition_baselines BEGIN SELECT RAISE(ABORT,'transition baselines are never deleted'); END""",
)
SCHEMA_CHECKSUM=hashlib.sha256('\n'.join(x.strip() for x in _S).encode()).hexdigest()
def apply_legacy_commercial_transition_schema(connection:sqlite3.Connection,*,now:int|None=None)->bool:
    timestamp=int(time.time()) if now is None else int(now); connection.execute('PRAGMA foreign_keys=ON')
    try:
      connection.execute('BEGIN IMMEDIATE')
      for mid,check in ((PAYMENT_MIGRATION_ID,PAYMENT_SCHEMA_CHECKSUM),(LEDGER_MIGRATION_ID,LEDGER_SCHEMA_CHECKSUM)):
       row=connection.execute('SELECT schema_checksum FROM mgboost_schema_migrations WHERE migration_id=?',(mid,)).fetchone()
       if not row or row[0]!=check: raise RuntimeError('legacy transition schema dependency mismatch')
      row=connection.execute('SELECT schema_checksum FROM mgboost_schema_migrations WHERE migration_id=?',(MIGRATION_ID,)).fetchone()
      if row:
       if row[0]!=SCHEMA_CHECKSUM: raise RuntimeError('legacy transition schema checksum mismatch')
       connection.commit(); return False
      for statement in _S: connection.execute(statement)
      connection.execute('INSERT INTO mgboost_schema_migrations (migration_id,schema_checksum,applied_at) VALUES (?,?,?)',(MIGRATION_ID,SCHEMA_CHECKSUM,timestamp)); connection.commit(); return True
    except Exception:
      connection.rollback(); raise
