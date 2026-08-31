"""Durable paid LEGACY_PAID_COMPAT -> commercial transition lifecycle."""
from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import time


class LegacyCommercialTransitionError(ValueError):
    pass


class LegacyCommercialTransitionConflict(LegacyCommercialTransitionError):
    pass


class LegacyCommercialTransitionLeaseLost(LegacyCommercialTransitionConflict):
    pass


def ceil_to_utc_hour(value: int) -> int:
    value = int(value)
    return value if value % 3600 == 0 else value + (3600 - value % 3600)


class LegacyCommercialTransitionStore:
    def __init__(self, connection: sqlite3.Connection, lock, authority):
        self._conn, self._lock, self._authority = connection, lock, authority

    def _event(self, tid: int, event: str, actor: str, reason: str, revision: int, now: int):
        self._conn.execute(
            "INSERT INTO mgboost_legacy_commercial_transition_events "
            "(transition_id,event_type,actor_ref,reason,revision,created_at) VALUES (?,?,?,?,?,?)",
            (tid, event, actor, reason, revision, now),
        )

    def create(self, capability, *, payment_record_id: int, reason: str, now: int | None = None) -> dict:
        timestamp = int(time.time()) if now is None else int(now)
        actor = self._authority.require(capability)
        reason = str(reason or '').strip()
        if not 3 <= len(reason) <= 300:
            raise LegacyCommercialTransitionError('bounded reason is required')
        with self._lock:
            try:
                self._conn.execute('BEGIN IMMEDIATE')
                payment = self._conn.execute(
                    "SELECT * FROM mgboost_manual_payment_records WHERE id=?", (int(payment_record_id),)
                ).fetchone()
                if not payment or payment['kind'] != 'PLAN_PRODUCT' or payment['status'] != 'PENDING':
                    raise LegacyCommercialTransitionError('payment must be a pending PLAN_PRODUCT record')
                if payment['currency'] != 'RUB' or payment['expected_amount_minor'] != payment['recorded_amount_minor']:
                    raise LegacyCommercialTransitionError('payment is not canonical MANUAL_RUB evidence')
                source = self._conn.execute(
                    "SELECT s.*,p.plan_kind,p.plan_code,p.billing_required,p.wl_mode,a.status AS account_status FROM mgboost_subscriptions s "
                    "JOIN mgboost_plan_versions p ON p.id=s.current_plan_version_id "
                    "JOIN mgboost_accounts a ON a.id=s.account_id WHERE s.account_id=? ORDER BY s.id DESC LIMIT 1",
                    (payment['account_id'],),
                ).fetchone()
                target = self._conn.execute('SELECT * FROM mgboost_plan_versions WHERE id=?', (payment['plan_version_id'],)).fetchone()
                if (not source or not str(source['plan_code']).startswith('LEGACY_PAID_COMPAT_V1_')
                        or source['plan_kind'] != 'COMMERCIAL' or source['billing_required']
                        or source['wl_mode'] != 'UNLIMITED' or source['account_status'] != 'ACTIVE'):
                    raise LegacyCommercialTransitionError('only active LEGACY_PAID_COMPAT source is eligible')
                if (not target or target['plan_code'] not in
                        {'BASIC','BASIC_PLUS','BASIC_PRO','WL','EXTENDED','FAMILY'}
                        or target['plan_kind'] != 'COMMERCIAL' or not target['billing_required']):
                    raise LegacyCommercialTransitionError('target must be a billable commercial plan')
                cursor = self._conn.execute(
                    "INSERT INTO mgboost_legacy_commercial_transitions "
                    "(public_id,account_id,payment_record_id,state,source_plan_version_id,source_subscription_status,original_source_expiry,aligned_source_expiry,target_plan_version_id,duration_days,catalog_version_id,plan_price_id,expected_amount_minor,actor_ref,reason,created_at,updated_at,source_subscription_id) "
                    "VALUES (?,?,?,'PENDING_PAYMENT',?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    ('lct_' + secrets.token_urlsafe(18), payment['account_id'], payment['id'], source['current_plan_version_id'],
                     source['status'], source['current_expiry'], ceil_to_utc_hour(max(int(source['current_expiry'] or 0), timestamp)),
                     target['id'], payment['duration_days_snapshot'], payment['catalog_version_id'], payment['plan_price_id'],
                     payment['expected_amount_minor'], actor, reason, timestamp, timestamp, source['id']),
                )
                tid = cursor.lastrowid
                self._event(tid, 'CREATED', actor, reason, 1, timestamp)
                self._conn.commit()
                return self.get(tid)
            except sqlite3.IntegrityError as exc:
                self._conn.rollback()
                raise LegacyCommercialTransitionConflict('another live transition or payment binding exists') from exc
            except Exception:
                self._conn.rollback(); raise

    def confirm_payment(self, capability, transition_id: int, *, now: int | None = None) -> dict:
        timestamp = int(time.time()) if now is None else int(now)
        actor = self._authority.require(capability)
        with self._lock:
            try:
                self._conn.execute('BEGIN IMMEDIATE')
                row = self._conn.execute('SELECT * FROM mgboost_legacy_commercial_transitions WHERE id=?', (int(transition_id),)).fetchone()
                if not row: raise LegacyCommercialTransitionError('transition not found')
                if row['state'] != 'PENDING_PAYMENT':
                    if row['payment_confirmed_at'] is not None:
                        self._conn.commit(); return dict(row)
                    raise LegacyCommercialTransitionConflict('transition is not confirmable')
                payment=self._conn.execute(
                    "SELECT * FROM mgboost_manual_payment_records WHERE id=? AND account_id=?",
                    (row['payment_record_id'],row['account_id']),
                ).fetchone()
                target=self._conn.execute(
                    "SELECT * FROM mgboost_plan_versions WHERE id=?",
                    (payment['plan_version_id'] if payment else -1,),
                ).fetchone()
                if (not payment or payment['status']!='PENDING' or payment['kind']!='PLAN_PRODUCT'
                        or payment['currency']!='RUB'
                        or payment['recorded_amount_minor']!=payment['expected_amount_minor']
                        or not target or target['plan_code'] not in
                        {'BASIC','BASIC_PLUS','BASIC_PRO','WL','EXTENDED','FAMILY'}
                        or not target['billing_required']):
                    raise LegacyCommercialTransitionConflict('payment/catalog facts are not confirmable')
                base = max(int(row['original_source_expiry'] or 0), timestamp)
                activation = ceil_to_utc_hour(base)
                target_expiry = activation + int(payment['duration_days_snapshot']) * 86400
                # One-time technical grace only.  It retains the old legacy
                # plan and terms until the canonical hour boundary; a CAS on
                # the frozen source expiry prevents a later goodwill/admin
                # change from being overwritten.
                source = self._conn.execute(
                    "SELECT id,current_plan_version_id,current_expiry,status,row_version FROM mgboost_subscriptions "
                    "WHERE account_id=? ORDER BY id DESC LIMIT 1", (row['account_id'],)
                ).fetchone()
                if not source or (row['source_subscription_id'] is not None and source['id'] != row['source_subscription_id']) \
                        or source['current_plan_version_id'] != row['source_plan_version_id'] \
                        or source['current_expiry'] != row['original_source_expiry'] \
                        or source['status'] != row['source_subscription_status']:
                    raise LegacyCommercialTransitionConflict('source entitlement diverged before confirmation')
                changed = self._conn.execute(
                    "UPDATE mgboost_subscriptions SET current_expiry=?,status='ACTIVE',updated_at=?,row_version=row_version+1 "
                    "WHERE id=? AND row_version=? AND current_plan_version_id=? AND current_expiry IS ? AND status=?",
                    (activation, timestamp, source['id'], source['row_version'],
                     row['source_plan_version_id'], row['original_source_expiry'],row['source_subscription_status']),
                )
                if changed.rowcount != 1:
                    raise LegacyCommercialTransitionConflict('alignment grace CAS failed')
                active_count=self._conn.execute(
                    "SELECT COUNT(*) FROM mgboost_device_slot_generations "
                    "WHERE account_id=? AND status='ACTIVE'", (row['account_id'],),
                ).fetchone()[0]
                next_state=('SELECTION_REQUIRED' if int(active_count)>int(target['device_limit'])
                            else 'SCHEDULED')
                self._conn.execute(
                    "INSERT INTO mgboost_entitlement_mutations "
                    "(account_id,subscription_id,operation,payment_channel,mutation_source,actor_type,actor_ref,reason,before_json,after_json,created_at) "
                    "VALUES (?,?,'LEGACY_COMMERCIAL_ALIGNMENT_GRACE','EXTERNAL_PAYMENT','MANUAL_PAYMENT','PRIMARY_ADMIN',?,?,?,?,?)",
                    (row['account_id'], source['id'], actor, 'legacy-commercial UTC-hour alignment',
                     json.dumps({'status':row['source_subscription_status'],'current_expiry':row['original_source_expiry']},sort_keys=True),
                     json.dumps({'status':'ACTIVE','current_expiry':activation},sort_keys=True), timestamp),
                )
                updated = self._conn.execute(
                    "UPDATE mgboost_legacy_commercial_transitions SET state=?,"
                    "payment_confirmed_at=?,aligned_source_expiry=?,activation_at=?,target_expiry=?,"
                    "target_plan_version_id=?,duration_days=?,catalog_version_id=?,plan_price_id=?,"
                    "expected_amount_minor=?,source_subscription_id=?,source_post_confirmation_row_version=?,"
                    "revision=revision+1,updated_at=? WHERE id=? AND revision=?",
                    (next_state,timestamp, activation, activation, target_expiry,payment['plan_version_id'],
                     payment['duration_days_snapshot'],payment['catalog_version_id'],payment['plan_price_id'],
                     payment['expected_amount_minor'],source['id'],int(source['row_version'])+1,
                     timestamp, row['id'], row['revision']),
                )
                if updated.rowcount != 1: raise LegacyCommercialTransitionConflict('transition CAS failed')
                fresh = self._conn.execute('SELECT * FROM mgboost_legacy_commercial_transitions WHERE id=?', (row['id'],)).fetchone()
                self._event(row['id'], 'PAYMENT_CONFIRMED', actor, 'MANUAL_RUB confirmed', fresh['revision'], timestamp)
                self._event(row['id'], 'ALIGNMENT_GRACE', actor, 'one-time UTC-hour alignment', fresh['revision'], timestamp)
                self._conn.commit()
                # The existing parent-sync store does not expose an
                # caller-owned transaction boundary.  Register its durable
                # revision-stamped outbox immediately after the atomic money
                # confirmation; the standalone worker repeats this
                # idempotently, closing a crash between these commits.
                from .parent_sync import ParentSyncStore
                parent_sync = ParentSyncStore(self._conn, self._lock)
                parent_sync.refresh_desired_state(row['account_id'], now=timestamp)
                parent_sync.enqueue_current_children(row['account_id'], now=timestamp)
                return dict(fresh)
            except Exception:
                self._conn.rollback(); raise

    def cancel(self, capability, transition_id: int, *, reason: str, now: int | None = None) -> dict:
        timestamp=int(time.time()) if now is None else int(now); actor=self._authority.require(capability)
        reason=str(reason or '').strip()
        if not 8<=len(reason)<=300: raise LegacyCommercialTransitionError('bounded cancellation reason is required')
        with self._lock:
            try:
                self._conn.execute('BEGIN IMMEDIATE')
                row=self._conn.execute('SELECT * FROM mgboost_legacy_commercial_transitions WHERE id=?',(int(transition_id),)).fetchone()
                if not row or row['state']!='PENDING_PAYMENT' or row['payment_confirmed_at'] is not None:
                    raise LegacyCommercialTransitionConflict('only an unconfirmed transition may be cancelled')
                self._conn.execute("UPDATE mgboost_manual_payment_records SET status='CANCELLED',cancelled_at=?,cancel_reason=?,updated_at=? WHERE id=? AND status='PENDING'",(timestamp,reason,timestamp,row['payment_record_id']))
                fresh=self._conn.execute('SELECT * FROM mgboost_legacy_commercial_transitions WHERE id=?',(row['id'],)).fetchone()
                if fresh['state'] != 'CANCELLED':
                    raise LegacyCommercialTransitionConflict('payment cancellation did not cancel transition')
                self._conn.commit(); return dict(fresh)
            except Exception:
                self._conn.rollback(); raise

    def record_selection(self, capability, transition_id: int, *, generation_ids: list[int], reason: str, now: int | None = None) -> dict:
        timestamp = int(time.time()) if now is None else int(now); actor = self._authority.require(capability)
        reason=str(reason or '').strip()
        if not 3<=len(reason)<=300:
            raise LegacyCommercialTransitionError('bounded selection reason is required')
        if not generation_ids or len(set(generation_ids)) != len(generation_ids):
            raise LegacyCommercialTransitionError('explicit unique generation selection is required')
        with self._lock:
            try:
                self._conn.execute('BEGIN IMMEDIATE')
                row = self._conn.execute('SELECT * FROM mgboost_legacy_commercial_transitions WHERE id=?', (int(transition_id),)).fetchone()
                if not row or row['state'] not in {'SCHEDULED','SELECTION_REQUIRED'}:
                    raise LegacyCommercialTransitionConflict('transition is not awaiting selection')
                target=self._conn.execute('SELECT device_limit FROM mgboost_plan_versions WHERE id=?',(row['target_plan_version_id'],)).fetchone()
                active=self._conn.execute("SELECT COUNT(*) FROM mgboost_device_slot_generations WHERE account_id=? AND status='ACTIVE'",(row['account_id'],)).fetchone()[0]
                required=max(0,int(active)-int(target['device_limit']))
                if len(generation_ids)!=required:
                    raise LegacyCommercialTransitionConflict('selection must contain exactly the capacity excess')
                for generation_id in generation_ids:
                    valid = self._conn.execute(
                        "SELECT g.id FROM mgboost_device_slot_generations g JOIN mgboost_device_slots s ON s.id=g.slot_id "
                        "JOIN mgboost_child_user_intents c ON c.slot_generation_id=g.id AND c.account_id=g.account_id "
                        "WHERE g.id=? AND g.account_id=? AND g.status='ACTIVE' AND s.current_generation=g.generation "
                        "AND c.observed_state='ACTIVE' AND c.desired_state!='REVOKED'",
                        (int(generation_id), row['account_id']),
                    ).fetchone()
                    if not valid: raise LegacyCommercialTransitionConflict('selected generation is stale or foreign')
                    self._conn.execute("INSERT INTO mgboost_legacy_commercial_transition_selections (transition_id,slot_generation_id,actor_ref,created_at) VALUES (?,?,?,?)", (row['id'], int(generation_id), actor, timestamp))
                self._conn.execute("UPDATE mgboost_legacy_commercial_transitions SET state='SELECTION_RECORDED',revision=revision+1,updated_at=? WHERE id=?", (timestamp,row['id']))
                fresh=self._conn.execute('SELECT * FROM mgboost_legacy_commercial_transitions WHERE id=?',(row['id'],)).fetchone(); self._event(row['id'],'SELECTION_RECORDED',actor,reason,fresh['revision'],timestamp)
                self._conn.commit(); return dict(fresh)
            except Exception:
                self._conn.rollback(); raise

    def get(self, transition_id: int) -> dict | None:
        row=self._conn.execute('SELECT * FROM mgboost_legacy_commercial_transitions WHERE id=?',(int(transition_id),)).fetchone()
        return dict(row) if row else None

    def claim_due(self, *, worker_id: str, now: int, lease_seconds: int = 120,
                  limit: int = 50) -> list[dict]:
        worker_id=str(worker_id or '').strip()
        if not 3<=len(worker_id)<=128: raise LegacyCommercialTransitionError('invalid worker identity')
        with self._lock:
            try:
                self._conn.execute('BEGIN IMMEDIATE')
                rows=self._conn.execute(
                    "SELECT * FROM mgboost_legacy_commercial_transitions WHERE activation_at<=? AND "
                    "(state IN ('SCHEDULED','SELECTION_RECORDED') OR "
                    "(state='RETIREMENT_IN_PROGRESS' AND lease_expires_at<=?)) "
                    "ORDER BY activation_at,id LIMIT ?",(int(now),int(now),max(1,min(int(limit),200))),
                ).fetchall()
                claimed=[]
                for row in rows:
                    updated=self._conn.execute(
                        "UPDATE mgboost_legacy_commercial_transitions SET state='RETIREMENT_IN_PROGRESS',"
                        "lease_owner=?,lease_expires_at=?,revision=revision+1,updated_at=? "
                        "WHERE id=? AND revision=?",
                        (worker_id,int(now)+max(30,int(lease_seconds)),int(now),row['id'],row['revision']),
                    )
                    if updated.rowcount==1:
                        fresh=self._conn.execute('SELECT * FROM mgboost_legacy_commercial_transitions WHERE id=?',(row['id'],)).fetchone()
                        self._event(row['id'],'RETIREMENT_STARTED',worker_id,'activation boundary reached',fresh['revision'],int(now))
                        claimed.append(dict(fresh))
                self._conn.commit(); return claimed
            except Exception:
                self._conn.rollback(); raise

    def ready_due(self, *, now: int, limit: int = 50) -> list[dict]:
        rows=self._conn.execute(
            "SELECT * FROM mgboost_legacy_commercial_transitions "
            "WHERE state='READY_TO_APPLY' AND activation_at<=? ORDER BY activation_at,id LIMIT ?",
            (int(now),max(1,min(int(limit),200))),
        ).fetchall()
        return [dict(row) for row in rows]

    def validate_due_source(self, transition_id: int) -> dict:
        """Authoritative pre-remote-mutation guard.

        ``apply_ready`` repeats the same CAS after retirement.  This first
        reread prevents a known stale source from causing any device revoke.
        """
        row = self._conn.execute(
            "SELECT t.*,s.current_plan_version_id,s.current_expiry,s.status AS current_subscription_status,"
            "s.row_version AS current_source_row_version,a.status AS account_status,"
            "p.status AS payment_status FROM mgboost_legacy_commercial_transitions t "
            "JOIN mgboost_subscriptions s ON s.id=t.source_subscription_id "
            "JOIN mgboost_accounts a ON a.id=t.account_id "
            "JOIN mgboost_manual_payment_records p ON p.id=t.payment_record_id "
            "WHERE t.id=? ORDER BY s.id DESC LIMIT 1", (int(transition_id),),
        ).fetchone()
        if (not row or row["state"] != "RETIREMENT_IN_PROGRESS"
                or row["current_plan_version_id"] != row["source_plan_version_id"]
                or row["current_expiry"] != row["aligned_source_expiry"]
                or row["current_subscription_status"] != "ACTIVE"
                or row["current_source_row_version"] != row["source_post_confirmation_row_version"]
                or row["account_status"] != "ACTIVE" or row["payment_status"] != "PENDING"):
            raise LegacyCommercialTransitionConflict("source entitlement diverged before retirement")
        return dict(row)

    def assert_lease(self, transition_id: int, *, worker_id: str,
                     expected_revision: int, now: int) -> dict:
        """Fencing check used immediately around every retirement side effect."""
        row = self._conn.execute(
            "SELECT * FROM mgboost_legacy_commercial_transitions WHERE id=?",
            (int(transition_id),),
        ).fetchone()
        if (not row or row['state'] != 'RETIREMENT_IN_PROGRESS'
                or row['lease_owner'] != str(worker_id)
                or row['lease_expires_at'] is None or int(row['lease_expires_at']) <= int(now)
                or int(row['revision']) != int(expected_revision)):
            raise LegacyCommercialTransitionLeaseLost('transition lease was superseded or expired')
        return dict(row)

    def validate_retirement_topology(self, transition_id: int, *, worker_id: str,
                                     expected_revision: int, now: int) -> list[dict]:
        """Fail before the first revoke unless the selected set is the exact excess."""
        with self._lock:
            self._conn.execute('BEGIN IMMEDIATE')
            try:
                row = self.assert_lease(
                    transition_id, worker_id=worker_id,
                    expected_revision=expected_revision, now=now,
                )
                target = self._conn.execute(
                    'SELECT device_limit FROM mgboost_plan_versions WHERE id=?',
                    (row['target_plan_version_id'],),
                ).fetchone()
                active = self._conn.execute(
                    "SELECT g.id AS slot_generation_id,g.account_id,g.status,g.generation,"
                    "s.current_generation,c.id AS child_id,c.account_id AS child_account_id,"
                    "c.slot_generation_id AS child_generation_id,c.desired_state,c.observed_state "
                    "FROM mgboost_device_slot_generations g "
                    "JOIN mgboost_device_slots s ON s.id=g.slot_id "
                    "LEFT JOIN mgboost_child_user_intents c ON c.slot_generation_id=g.id "
                    "WHERE g.account_id=? AND g.status='ACTIVE' AND s.current_generation=g.generation "
                    "ORDER BY g.id", (row['account_id'],),
                ).fetchall()
                selections = self._conn.execute(
                    "SELECT x.slot_generation_id,c.id AS selected_child_id,c.account_id AS child_account_id,"
                    "c.slot_generation_id AS child_generation_id,g.account_id AS generation_account_id "
                    "FROM mgboost_legacy_commercial_transition_selections x "
                    "JOIN mgboost_device_slot_generations g ON g.id=x.slot_generation_id "
                    "LEFT JOIN mgboost_child_user_intents c ON c.slot_generation_id=g.id "
                    "WHERE x.transition_id=? ORDER BY x.slot_generation_id", (row['id'],),
                ).fetchall()
                by_id = {int(value['slot_generation_id']): value for value in active}
                result = []
                remaining = 0
                for selection in selections:
                    generation_id = int(selection['slot_generation_id'])
                    if (selection['generation_account_id'] != row['account_id']
                            or selection['selected_child_id'] is None
                            or selection['child_account_id'] != row['account_id']
                            or selection['child_generation_id'] != generation_id):
                        raise LegacyCommercialTransitionConflict('selected generation binding diverged')
                    value = by_id.get(generation_id)
                    revoke = self._conn.execute(
                        "SELECT state,old_child_intent_id,old_slot_generation_id FROM "
                        "mgboost_child_lifecycle_operations WHERE account_id=? "
                        "AND old_child_intent_id=? AND old_slot_generation_id=? "
                        "AND operation_kind='REVOKE'",
                        (row['account_id'], selection['selected_child_id'], generation_id),
                    ).fetchone()
                    if value is not None:
                        remaining += 1
                        if (value['child_id'] != selection['selected_child_id']
                                or (value['observed_state'] != 'ACTIVE'
                                    and not revoke)
                                or (value['desired_state'] == 'REVOKED'
                                    and not revoke)):
                            raise LegacyCommercialTransitionConflict('selected generation binding diverged')
                    else:
                        free = self._conn.execute(
                            "SELECT state,old_child_intent_id,old_slot_generation_id FROM "
                            "mgboost_child_lifecycle_operations WHERE account_id=? "
                            "AND old_child_intent_id=? AND old_slot_generation_id=? "
                            "AND operation_kind='FREE'",
                            (row['account_id'], selection['selected_child_id'], generation_id),
                        ).fetchone()
                        if (not revoke or revoke['state'] != 'APPLIED' or not free
                                or free['state'] not in {'IN_FLIGHT','APPLIED'}):
                            raise LegacyCommercialTransitionConflict('selected generation is no longer current')
                    item = dict(selection)
                    item['revoke_state'] = revoke['state'] if revoke else None
                    result.append(item)
                excess = max(0, len(active) - int(target['device_limit']))
                if remaining != excess:
                    raise LegacyCommercialTransitionConflict(
                        'selected generation set no longer matches authoritative capacity excess')
                self._conn.commit()
                return result
            except Exception:
                self._conn.rollback()
                raise

    def assess_capacity(self, transition_id: int, *, now: int, worker_id: str | None = None,
                        expected_revision: int | None = None) -> dict:
        """Durably request explicit retirement; never selects a device."""
        with self._lock:
            try:
                self._conn.execute('BEGIN IMMEDIATE')
                row=self._conn.execute('SELECT * FROM mgboost_legacy_commercial_transitions WHERE id=?',(int(transition_id),)).fetchone()
                if not row or row['state'] != 'RETIREMENT_IN_PROGRESS':
                    raise LegacyCommercialTransitionConflict('transition capacity state is not actionable')
                if worker_id is not None:
                    self.assert_lease(
                        row['id'], worker_id=worker_id,
                        expected_revision=row['revision'] if expected_revision is None else expected_revision,
                        now=now,
                    )
                target=self._conn.execute('SELECT device_limit FROM mgboost_plan_versions WHERE id=?',(row['target_plan_version_id'],)).fetchone()
                active=self._conn.execute("SELECT COUNT(*) FROM mgboost_device_slot_generations WHERE account_id=? AND status='ACTIVE'",(row['account_id'],)).fetchone()[0]
                required=max(0, int(active)-int(target['device_limit']))
                selected=self._conn.execute("SELECT COUNT(*) FROM mgboost_legacy_commercial_transition_selections WHERE transition_id=?",(row['id'],)).fetchone()[0]
                if required != 0:
                    raise LegacyCommercialTransitionConflict('capacity remains above target after retirement')
                state='READY_TO_APPLY'
                self._conn.execute("UPDATE mgboost_legacy_commercial_transitions SET state=?,lease_owner=NULL,lease_expires_at=NULL,revision=revision+1,updated_at=? WHERE id=?",(state,int(now),row['id']))
                fresh=self._conn.execute('SELECT * FROM mgboost_legacy_commercial_transitions WHERE id=?',(row['id'],)).fetchone()
                self._event(row['id'],'READY','SYSTEM','capacity and selected retirement verified',fresh['revision'],int(now))
                self._conn.commit(); return {**dict(fresh),'active_devices':int(active),'target_device_limit':int(target['device_limit']),'retirement_required':required,'selected_count':int(selected)}
            except Exception:
                self._conn.rollback(); raise

    def manual_review(self, transition_id: int, *, reason: str, now: int | None = None) -> None:
        timestamp=int(time.time()) if now is None else int(now); reason=str(reason or '').strip()[:300]
        with self._lock:
            self._conn.execute('BEGIN IMMEDIATE')
            row=self._conn.execute('SELECT * FROM mgboost_legacy_commercial_transitions WHERE id=?',(int(transition_id),)).fetchone()
            if not row: self._conn.rollback(); raise LegacyCommercialTransitionError('transition not found')
            self._conn.execute("UPDATE mgboost_legacy_commercial_transitions SET state='MANUAL_REVIEW',review_reason=?,revision=revision+1,updated_at=? WHERE id=?",(reason or 'manual review',timestamp,row['id']))
            fresh=self._conn.execute('SELECT * FROM mgboost_legacy_commercial_transitions WHERE id=?',(row['id'],)).fetchone(); self._event(row['id'],'MANUAL_REVIEW','SYSTEM',reason or 'manual review',fresh['revision'],timestamp); self._conn.commit()

    def retry_manual_review(self, capability, transition_id: int, *, reason: str,
                            now: int | None = None) -> dict:
        """Explicit audited retry only; never changes frozen facts or grace."""
        timestamp=int(time.time()) if now is None else int(now)
        actor=self._authority.require(capability); reason=str(reason or '').strip()
        if not 8<=len(reason)<=300:
            raise LegacyCommercialTransitionError('bounded retry reason is required')
        with self._lock:
            try:
                self._conn.execute('BEGIN IMMEDIATE')
                row=self._conn.execute('SELECT * FROM mgboost_legacy_commercial_transitions WHERE id=?',(int(transition_id),)).fetchone()
                if not row or row['state']!='MANUAL_REVIEW':
                    raise LegacyCommercialTransitionConflict('transition is not in manual review')
                source=self._conn.execute(
                    "SELECT s.id AS subscription_id,s.current_plan_version_id,s.current_expiry,s.status AS subscription_status,s.row_version,"
                    "a.status AS account_status "
                    "FROM mgboost_subscriptions s JOIN mgboost_accounts a ON a.id=s.account_id "
                    "WHERE s.id=?",(row['source_subscription_id'] or -1,),
                ).fetchone()
                payment=self._conn.execute('SELECT status FROM mgboost_manual_payment_records WHERE id=?',(row['payment_record_id'],)).fetchone()
                if (not source or source['subscription_id']!=row['source_subscription_id']
                        or source['row_version']!=row['source_post_confirmation_row_version']
                        or source['current_plan_version_id']!=row['source_plan_version_id']
                        or source['current_expiry']!=row['aligned_source_expiry']
                        or source['subscription_status']!='ACTIVE'
                        or source['account_status']!='ACTIVE' or not payment or payment['status']!='PENDING'):
                    raise LegacyCommercialTransitionConflict('manual review facts remain divergent')
                target=self._conn.execute('SELECT device_limit FROM mgboost_plan_versions WHERE id=?',(row['target_plan_version_id'],)).fetchone()
                active=self._conn.execute("SELECT COUNT(*) FROM mgboost_device_slot_generations WHERE account_id=? AND status='ACTIVE'",(row['account_id'],)).fetchone()[0]
                selected=self._conn.execute('SELECT COUNT(*) FROM mgboost_legacy_commercial_transition_selections WHERE transition_id=?',(row['id'],)).fetchone()[0]
                excess=max(0,int(active)-int(target['device_limit']))
                if excess:
                    next_state='SELECTION_RECORDED' if selected==excess else 'SELECTION_REQUIRED'
                else:
                    next_state='SELECTION_RECORDED' if selected else 'SCHEDULED'
                self._conn.execute("UPDATE mgboost_legacy_commercial_transitions SET state=?,review_reason=NULL,revision=revision+1,updated_at=? WHERE id=?",(next_state,timestamp,row['id']))
                fresh=self._conn.execute('SELECT * FROM mgboost_legacy_commercial_transitions WHERE id=?',(row['id'],)).fetchone()
                self._event(row['id'],'MANUAL_REVIEW_RETRY',actor,reason,fresh['revision'],timestamp)
                self._conn.commit(); return dict(fresh)
            except Exception:
                self._conn.rollback(); raise

    def apply_ready(self, transition_id: int, *, now: int | None = None) -> dict:
        """Atomic local entitlement/payment apply after worker-proven retirement.

        Remote child convergence deliberately happens after this commit via
        parent-sync; no remote failure can erase an already paid local term.
        """
        timestamp=int(time.time()) if now is None else int(now)
        with self._lock:
            try:
                self._conn.execute('BEGIN IMMEDIATE')
                row=self._conn.execute('SELECT * FROM mgboost_legacy_commercial_transitions WHERE id=?',(int(transition_id),)).fetchone()
                if not row or row['state'] != 'READY_TO_APPLY' or row['activation_at'] > timestamp:
                    raise LegacyCommercialTransitionConflict('transition is not ready for apply')
                sub=self._conn.execute('SELECT * FROM mgboost_subscriptions WHERE id=?',(row['source_subscription_id'] or -1,)).fetchone()
                latest=self._conn.execute('SELECT id FROM mgboost_subscriptions WHERE account_id=? ORDER BY id DESC LIMIT 1',(row['account_id'],)).fetchone()
                if (not sub or not latest or latest['id'] != row['source_subscription_id']
                        or sub['row_version'] != row['source_post_confirmation_row_version']
                        or sub['current_plan_version_id'] != row['source_plan_version_id']
                        or sub['current_expiry'] != row['aligned_source_expiry'] or sub['status']!='ACTIVE'):
                    raise LegacyCommercialTransitionConflict('source entitlement diverged after scheduling')
                account=self._conn.execute('SELECT status FROM mgboost_accounts WHERE id=?',(row['account_id'],)).fetchone()
                payment=self._conn.execute('SELECT * FROM mgboost_manual_payment_records WHERE id=?',(row['payment_record_id'],)).fetchone()
                if not account or account['status'] != 'ACTIVE' or not payment or payment['status'] != 'PENDING':
                    raise LegacyCommercialTransitionConflict('account or payment is no longer applicable')
                self._conn.execute("UPDATE mgboost_legacy_commercial_transitions SET state='APPLYING',revision=revision+1,updated_at=? WHERE id=?",(timestamp,row['id']))
                updated=self._conn.execute("UPDATE mgboost_subscriptions SET current_plan_version_id=?,status='ACTIVE',current_expiry=?,updated_at=?,row_version=row_version+1 WHERE id=? AND row_version=?",(row['target_plan_version_id'],row['target_expiry'],timestamp,sub['id'],sub['row_version']))
                if updated.rowcount != 1: raise LegacyCommercialTransitionConflict('subscription CAS failed')
                target=self._conn.execute('SELECT * FROM mgboost_plan_versions WHERE id=?',(row['target_plan_version_id'],)).fetchone()
                surviving_children = []
                generations = self._conn.execute(
                    "SELECT g.id AS generation_id,g.account_id,g.generation,s.current_generation,"
                    "c.id AS child_id,c.account_id AS child_account_id,c.slot_generation_id,"
                    "c.desired_state,c.observed_state "
                    "FROM mgboost_device_slot_generations g JOIN mgboost_device_slots s ON s.id=g.slot_id "
                    "LEFT JOIN mgboost_child_user_intents c ON c.slot_generation_id=g.id "
                    "WHERE g.account_id=? AND g.status='ACTIVE' AND s.current_generation=g.generation",
                    (row['account_id'],),
                ).fetchall()
                for generation in generations:
                    if (generation['child_id'] is None
                            or generation['child_account_id'] != row['account_id']
                            or generation['slot_generation_id'] != generation['generation_id']
                            or generation['desired_state'] == 'REVOKED'
                            or generation['observed_state'] != 'ACTIVE'):
                        raise LegacyCommercialTransitionConflict('surviving child lineage is not authoritative')
                    surviving_children.append(int(generation['child_id']))
                mutation=self._conn.execute("INSERT INTO mgboost_entitlement_mutations (account_id,subscription_id,operation,payment_channel,mutation_source,actor_type,actor_ref,reason,external_reference,before_json,after_json,created_at) VALUES (?,?,'LEGACY_COMMERCIAL_TRANSITION','EXTERNAL_PAYMENT','MANUAL_PAYMENT','PRIMARY_ADMIN',?,?,?,?,?,?)",(row['account_id'],sub['id'],row['actor_ref'],row['reason'],payment['external_reference'],json.dumps({'plan_version_id':row['source_plan_version_id'],'current_expiry':row['aligned_source_expiry']},sort_keys=True),json.dumps({'plan_version_id':row['target_plan_version_id'],'new_expiry':row['target_expiry']},sort_keys=True),timestamp)).lastrowid
                duration=self._conn.execute('SELECT * FROM mgboost_plan_durations WHERE id=?',(payment['duration_id'],)).fetchone()
                term=self._conn.execute("INSERT INTO mgboost_subscription_terms (account_id,subscription_id,sequence_no,plan_version_id,duration_id,duration_days,starts_at,ends_at,billing_required_snapshot,device_limit_mode_snapshot,device_limit_snapshot,wl_mode_snapshot,wl_quota_bytes_snapshot,wl_period_days_snapshot,plan_snapshot_json,mutation_id,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(row['account_id'],sub['id'],self._conn.execute('SELECT COALESCE(MAX(sequence_no),0)+1 FROM mgboost_subscription_terms WHERE subscription_id=?',(sub['id'],)).fetchone()[0],target['id'],duration['id'],row['duration_days'],row['activation_at'],row['target_expiry'],1,target['device_limit_mode'],target['device_limit'],target['wl_mode'],target['wl_quota_bytes'],target['wl_period_days'],json.dumps({'plan_code':target['plan_code'],'duration_days':row['duration_days']},sort_keys=True),mutation,timestamp)).lastrowid
                periods=[]
                if target['wl_mode']=='LIMITED':
                    from .subscription_renewal import schedule_wl_period_windows
                    from .wl_topology import WL_NODE_IDS
                    base=self._conn.execute('SELECT COALESCE(MAX(sequence_no),0) FROM mgboost_wl_periods WHERE subscription_id=?',(sub['id'],)).fetchone()[0]
                    for index,(start,end) in enumerate(schedule_wl_period_windows(anchor=row['activation_at'],duration_days=row['duration_days'],wl_period_days=target['wl_period_days']),1):
                        pid=self._conn.execute("INSERT INTO mgboost_wl_periods (account_id,subscription_id,subscription_term_id,sequence_no,starts_at,ends_at,quota_mode,base_quota_bytes,status,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",(row['account_id'],sub['id'],term,base+index,start,end,'LIMITED',target['wl_quota_bytes'],'PLANNED',timestamp)).lastrowid; periods.append(pid)
                    first_period_id=periods[0]
                    for child_id in surviving_children:
                        for node_id in sorted(WL_NODE_IDS):
                            self._conn.execute(
                                "INSERT INTO mgboost_wl_transition_baselines "
                                "(transition_id,account_id,wl_period_id,child_intent_id,node_id,state,created_at) "
                                "VALUES (?,?,?,?,?,'PENDING',?)",
                                (row['id'],row['account_id'],first_period_id,child_id,node_id,timestamp),
                            )
                self._conn.execute("INSERT INTO mgboost_manual_payment_applications (payment_record_id,account_id,entitlement_mutation_id,applied_operation,applied_expiry,related_grant_id,entitlement_snapshot_json,created_at) VALUES (?,?,?,'RENEW',?,NULL,?,?)",(payment['id'],row['account_id'],mutation,row['target_expiry'],json.dumps({'transition_id':row['id'],'term_id':term},sort_keys=True),timestamp))
                self._conn.execute("UPDATE mgboost_manual_payment_records SET status='APPLIED',applied_at=?,entitlement_mutation_id=?,applied_operation='RENEW',applied_expiry=?,updated_at=? WHERE id=? AND status='PENDING'",(timestamp,mutation,row['target_expiry'],timestamp,payment['id']))
                self._conn.execute(
                    "INSERT INTO mgboost_manual_payment_sync_jobs "
                    "(payment_record_id,account_id,entitlement_mutation_id,created_at,updated_at) "
                    "VALUES (?,?,?,?,?)",
                    (payment['id'],row['account_id'],mutation,timestamp,timestamp),
                )
                self._conn.execute("UPDATE mgboost_legacy_commercial_transitions SET state='APPLIED',applied_at=?,revision=revision+1,updated_at=? WHERE id=?",(timestamp,timestamp,row['id']))
                fresh=self._conn.execute('SELECT * FROM mgboost_legacy_commercial_transitions WHERE id=?',(row['id'],)).fetchone(); self._event(row['id'],'APPLIED','SYSTEM','atomic local apply',fresh['revision'],timestamp)
                self._conn.commit(); return {**dict(fresh),'mutation_id':mutation,'wl_period_ids':periods}
            except Exception:
                self._conn.rollback(); raise
