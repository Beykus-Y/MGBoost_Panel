"""DL-063: self-service LEGACY_PAID_COMPAT_V1_* -> commercial plan switch,
paid via Telegram Stars.

Mirrors ``legacy_commercial_transition.py``'s CAS/anchor pattern (same
``ceil_to_utc_hour`` timing rule, same APPLYING freeze immediately before the
subscription CAS update, same TRANSITION_BASELINE authoritative-lineage gate
for a LIMITED target), but:

* is keyed to a ``stars_invoices`` row instead of a manual RUB payment record;
* is self-service (``create``/``confirm``/``apply`` take no capability --
  only ``retry_manual_review`` is admin-gated, exactly like the manual
  engine splits ``manual_review`` (system-callable, no capability) from
  ``retry_manual_review`` (operator action, capability-gated));
* never selects or retires a device -- if the account's active device count
  exceeds the target plan's ``device_limit``, self-service is hard-blocked
  (``DeviceLimitExceeded``); there is no SELECTION_REQUIRED state here.

Transaction convention (matches stars_purchase.py's own ``_locked`` helpers):
``create_locked`` and ``assert_still_eligible_locked`` never open their own
transaction -- they are always called by ``stars_purchase.py`` from inside
its own already-open ``BEGIN IMMEDIATE``. Every other public method
(``cancel_unpaid_locked``, ``confirm_locked``, ``apply_locked``,
``manual_review``, ``retry_manual_review``) is a top-level operation that
owns its own ``with self._lock: BEGIN IMMEDIATE ... commit()`` block, exactly
like ``legacy_commercial_transition.py``'s own top-level methods.

``payment_confirmed_at`` in the DL-062/DL-063 timing formula is always the
durable ``stars_invoices.paid_at`` column (set atomically by
``StarsPurchaseStore.capture_paid`` at the moment money was captured) --
never a ``now`` sampled inside this module. A worker-tick delay between
capture and ``confirm_locked`` running must never shift the financial
boundary.
"""
from __future__ import annotations

import json
import secrets
import sqlite3
import time

from .subscription_renewal import _idempotency_hash, schedule_wl_period_windows
from .wl_topology import WL_NODE_IDS

_TARGET_PLAN_CODES = {'BASIC', 'BASIC_PLUS', 'BASIC_PRO', 'WL', 'EXTENDED', 'FAMILY'}

# Same allowlist as legacy_commercial_transition.py's apply_ready gate --
# deliberately duplicated rather than imported, so a future change to that
# module's allowlist does not silently change this one's semantics too.
_AUTHORITATIVE_CHILD_STATES = ('ACTIVE', 'DISABLED')


class LegacyStarsPlanSwitchError(ValueError):
    pass


class LegacyStarsPlanSwitchConflict(LegacyStarsPlanSwitchError):
    pass


class DeviceLimitExceeded(LegacyStarsPlanSwitchError):
    def __init__(self, active_count: int, target_device_limit: int):
        super().__init__(
            f"active_count={active_count} exceeds target device_limit={target_device_limit}"
        )
        self.active_count = int(active_count)
        self.target_device_limit = int(target_device_limit)


def ceil_to_utc_hour(value: int) -> int:
    value = int(value)
    return value if value % 3600 == 0 else value + (3600 - value % 3600)


class LegacyStarsPlanSwitchStore:
    def __init__(self, connection: sqlite3.Connection, lock, authority=None):
        self._conn, self._lock, self._authority = connection, lock, authority

    # -- internal helpers, always run inside an already-open transaction ---

    def _event(self, switch_id: int, event: str, actor: str, reason: str, revision: int, now: int) -> None:
        self._conn.execute(
            "INSERT INTO mgboost_legacy_stars_plan_switch_events "
            "(switch_id,event_type,actor_ref,reason,revision,created_at) VALUES (?,?,?,?,?,?)",
            (switch_id, event, actor, reason, revision, now),
        )

    def _source_locked(self, account_id: int):
        """Same eligibility predicate as legacy_commercial_transition.py's
        ``create`` (verified verbatim against that file): joins the
        account's latest subscription to its plan version and account row,
        requires a LEGACY_PAID_COMPAT_V1_* unlimited commercial source and
        an ACTIVE account -- deliberately does NOT require the subscription's
        own ``status`` column to be ACTIVE. ``EXPIRED`` there is a derived,
        on-the-fly status (entitlement_engine._effective_subscription_status)
        that does not change current_plan_version_id; the same canonical
        legacy source subscription remains eligible after its paid window
        lapses, exactly as the manual-RUB engine already treats it.
        """
        return self._conn.execute(
            "SELECT s.*,p.plan_kind,p.plan_code,p.billing_required,p.wl_mode,a.status AS account_status "
            "FROM mgboost_subscriptions s "
            "JOIN mgboost_plan_versions p ON p.id=s.current_plan_version_id "
            "JOIN mgboost_accounts a ON a.id=s.account_id WHERE s.account_id=? ORDER BY s.id DESC LIMIT 1",
            (int(account_id),),
        ).fetchone()

    def _device_count_locked(self, account_id: int) -> int:
        return int(self._conn.execute(
            "SELECT COUNT(*) FROM mgboost_device_slot_generations WHERE account_id=? AND status='ACTIVE'",
            (int(account_id),),
        ).fetchone()[0])

    def _manual_review_inline(self, switch_id: int, *, reason: str, now: int) -> None:
        """Runs inside the CALLER's already-open transaction -- used by
        confirm_locked/apply_locked when they detect a conflict mid-flight
        and must record MANUAL_REVIEW atomically with everything else they
        already read/wrote this transaction, rather than nesting a second
        BEGIN IMMEDIATE.

        Also flips the bound stars_invoices row to status='manual_review'
        (mirroring StarsPurchaseStore._mark_manual_locked's exact shape,
        duplicated here rather than imported to avoid a cross-module
        dependency for two lines of SQL) whenever it is still 'paid' -- a
        switch stuck in MANUAL_REVIEW here always means money already moved
        but the entitlement was never applied. Without this, the invoice
        stays 'paid' forever, which src/routes/admin.py's refund gate does
        not accept (only 'manual_review'/'applied'/'canonical_applied'/
        'apply_retry_exhausted'/'apply_failed_user_missing' are refundable),
        making the payment a dead end for the existing audited Stars refund
        tool. If the invoice already reached 'canonical_applied' (a real
        APPLIED switch racing a manual_review call from a stale/duplicate
        caller -- should not happen in practice since apply_locked only
        calls this before its own success path, but guarded here too), this
        intentionally leaves invoice status alone: an already-applied
        entitlement must never look refund-eligible-as-if-unapplied via this
        side channel; the existing canonical_applied refund path (money-only,
        no automatic entitlement rollback) still applies unchanged.
        """
        row = self._conn.execute('SELECT * FROM mgboost_legacy_stars_plan_switches WHERE id=?', (int(switch_id),)).fetchone()
        if not row or row['state'] == 'MANUAL_REVIEW':
            return
        timestamp = int(now)
        clipped_reason = str(reason or 'manual review')[:300]
        self._conn.execute(
            "UPDATE mgboost_legacy_stars_plan_switches SET state='MANUAL_REVIEW',review_reason=?,revision=revision+1,updated_at=? WHERE id=?",
            (clipped_reason, timestamp, row['id']),
        )
        self._conn.execute(
            "UPDATE stars_invoices SET status='manual_review',manual_review_reason=?,manual_review_at=? "
            "WHERE id=? AND status='paid'",
            (clipped_reason, timestamp, row['invoice_id']),
        )
        fresh = self._conn.execute('SELECT * FROM mgboost_legacy_stars_plan_switches WHERE id=?', (row['id'],)).fetchone()
        self._event(switch_id, 'MANUAL_REVIEW', 'SYSTEM', clipped_reason, fresh['revision'], timestamp)

    # -- nested helpers: caller already holds self._lock + BEGIN IMMEDIATE --

    def create_locked(self, *, account_id: int, invoice_id: int, target_plan_version_id: int,
                       duration_days: int, now: int) -> dict:
        """Called from inside stars_purchase.py's own invoice-creation
        transaction (same BEGIN IMMEDIATE) -- atomic invoice<->switch
        binding. Raises without inserting anything on any ineligibility."""
        timestamp = int(now)
        source = self._source_locked(account_id)
        if (not source or not str(source['plan_code']).startswith('LEGACY_PAID_COMPAT_V1_')
                or source['plan_kind'] != 'COMMERCIAL' or source['billing_required']
                or source['wl_mode'] != 'UNLIMITED' or source['account_status'] != 'ACTIVE'):
            raise LegacyStarsPlanSwitchError('only a LEGACY_PAID_COMPAT source is eligible')
        target = self._conn.execute(
            'SELECT * FROM mgboost_plan_versions WHERE id=?', (int(target_plan_version_id),)
        ).fetchone()
        if (not target or target['plan_code'] not in _TARGET_PLAN_CODES
                or target['plan_kind'] != 'COMMERCIAL' or not target['billing_required']):
            raise LegacyStarsPlanSwitchError('target must be a billable commercial plan')
        active_count = self._device_count_locked(account_id)
        if active_count > int(target['device_limit']):
            raise DeviceLimitExceeded(active_count, int(target['device_limit']))
        try:
            cursor = self._conn.execute(
                "INSERT INTO mgboost_legacy_stars_plan_switches "
                "(public_id,account_id,invoice_id,state,source_plan_version_id,source_subscription_id,"
                "source_subscription_status,original_source_expiry,target_plan_version_id,duration_days,"
                "created_at,updated_at) VALUES (?,?,?,'PENDING_PAYMENT',?,?,?,?,?,?,?,?)",
                ('lsw_' + secrets.token_urlsafe(18), int(account_id), int(invoice_id),
                 source['current_plan_version_id'], source['id'], source['status'],
                 source['current_expiry'], target['id'], int(duration_days), timestamp, timestamp),
            )
        except sqlite3.IntegrityError as exc:
            raise LegacyStarsPlanSwitchConflict('another live switch or invoice binding exists on this account') from exc
        switch_id = cursor.lastrowid
        self._event(switch_id, 'CREATED', 'TELEGRAM', 'self-service legacy stars plan switch requested', 1, timestamp)
        return self._get_inline(switch_id)

    def assert_still_eligible_locked(self, account_id: int, target_plan_version_id: int) -> None:
        """Re-check source/device-limit eligibility without mutating
        anything -- used by validate_invoice_for_checkout and capture_paid
        (both already inside their own open transaction) to catch drift
        between invoice creation and the actual charge."""
        source = self._source_locked(account_id)
        if (not source or not str(source['plan_code']).startswith('LEGACY_PAID_COMPAT_V1_')
                or source['plan_kind'] != 'COMMERCIAL' or source['billing_required']
                or source['wl_mode'] != 'UNLIMITED' or source['account_status'] != 'ACTIVE'):
            raise LegacyStarsPlanSwitchError('source is no longer an eligible LEGACY_PAID_COMPAT subscription')
        target = self._conn.execute(
            'SELECT device_limit FROM mgboost_plan_versions WHERE id=?', (int(target_plan_version_id),)
        ).fetchone()
        active_count = self._device_count_locked(account_id)
        if not target or active_count > int(target['device_limit']):
            raise DeviceLimitExceeded(active_count, int(target['device_limit']) if target else 0)

    def _get_inline(self, switch_id: int) -> dict:
        row = self._conn.execute('SELECT * FROM mgboost_legacy_stars_plan_switches WHERE id=?', (int(switch_id),)).fetchone()
        return dict(row)

    # -- top-level operations: each owns its own lock + transaction --------

    def cancel_unpaid_locked(self, switch_id: int, *, now: int) -> dict:
        """PENDING_PAYMENT -> CANCELLED. Only while the bound invoice has no
        paid_at yet -- once payment evidence exists, DL-062/DL-063 freeze
        applies and cancellation is impossible (CAS'ed against a live
        re-read of stars_invoices.paid_at, not a cached value)."""
        timestamp = int(now)
        with self._lock:
            try:
                self._conn.execute('BEGIN IMMEDIATE')
                row = self._conn.execute(
                    'SELECT * FROM mgboost_legacy_stars_plan_switches WHERE id=?', (int(switch_id),)
                ).fetchone()
                if not row:
                    raise LegacyStarsPlanSwitchError('switch not found')
                updated = self._conn.execute(
                    "UPDATE mgboost_legacy_stars_plan_switches SET state='CANCELLED',revision=revision+1,updated_at=? "
                    "WHERE id=? AND state='PENDING_PAYMENT' AND NOT EXISTS "
                    "(SELECT 1 FROM stars_invoices WHERE id=mgboost_legacy_stars_plan_switches.invoice_id AND paid_at IS NOT NULL)",
                    (timestamp, int(switch_id)),
                )
                if updated.rowcount != 1:
                    raise LegacyStarsPlanSwitchConflict('switch is already paid or no longer pending')
                fresh = self._get_inline(switch_id)
                self._event(switch_id, 'CANCELLED', 'TELEGRAM', 'cancelled before payment', fresh['revision'], timestamp)
                self._conn.commit()
                return fresh
            except Exception:
                self._conn.rollback()
                raise

    def confirm_locked(self, switch_id: int, *, now: int) -> dict:
        """Called once the bound invoice is 'paid'. Idempotent: if already
        past PENDING_PAYMENT, returns the current row unchanged (the one
        LEGACY_COMMERCIAL_ALIGNMENT_GRACE-equivalent computation this module
        performs, per DL-062, is never repeated)."""
        timestamp = int(now)
        with self._lock:
            try:
                self._conn.execute('BEGIN IMMEDIATE')
                row = self._conn.execute(
                    'SELECT * FROM mgboost_legacy_stars_plan_switches WHERE id=?', (int(switch_id),)
                ).fetchone()
                if not row:
                    raise LegacyStarsPlanSwitchError('switch not found')
                if row['state'] != 'PENDING_PAYMENT':
                    self._conn.commit()
                    return dict(row)
                invoice = self._conn.execute('SELECT * FROM stars_invoices WHERE id=?', (row['invoice_id'],)).fetchone()
                if not invoice or invoice['paid_at'] is None:
                    raise LegacyStarsPlanSwitchConflict('bound invoice is not paid yet')
                source = self._conn.execute(
                    "SELECT id,current_plan_version_id,current_expiry,status,row_version FROM mgboost_subscriptions "
                    "WHERE id=?", (row['source_subscription_id'],),
                ).fetchone()
                if (not source or source['current_plan_version_id'] != row['source_plan_version_id']
                        or source['current_expiry'] != row['original_source_expiry']
                        or source['status'] != row['source_subscription_status']):
                    self._manual_review_inline(switch_id, reason='source_diverged_before_confirmation', now=timestamp)
                    self._conn.commit()
                    raise LegacyStarsPlanSwitchConflict('source entitlement diverged before confirmation')
                target = self._conn.execute(
                    'SELECT device_limit FROM mgboost_plan_versions WHERE id=?', (row['target_plan_version_id'],)
                ).fetchone()
                active_count = self._device_count_locked(row['account_id'])
                if active_count > int(target['device_limit']):
                    # Money has already moved (invoice is paid) -- this
                    # drift must never silently reject or silently apply an
                    # over-limit switch.
                    self._manual_review_inline(switch_id, reason='device_limit_exceeded_at_confirm', now=timestamp)
                    self._conn.commit()
                    return self._get_inline(switch_id)
                # DL-062/DL-063 timing formula, verbatim: payment_confirmed_at
                # is the durable stars_invoices.paid_at fact, never `now` --
                # a worker-tick delay between capture and this call must not
                # shift the boundary.
                payment_confirmed_at = int(invoice['paid_at'])
                base_boundary = max(int(row['original_source_expiry'] or 0), payment_confirmed_at)
                activation_at = ceil_to_utc_hour(base_boundary)
                target_expiry = activation_at + int(row['duration_days']) * 86400

                # LEGACY_COMMERCIAL_ALIGNMENT_GRACE (DL-062/DL-063), mirrored
                # verbatim from legacy_commercial_transition.py's own
                # confirm_payment: unconditionally CAS the live subscription's
                # current_expiry forward to the aligned activation_at boundary
                # right now, not at apply time. Without this, a source that
                # already expired (or expires) at/before confirmation leaves a
                # real access gap of up to 3599s (or more) until apply_locked
                # runs at activation_at. Runs exactly once per switch: this
                # whole branch is only reachable from state=='PENDING_PAYMENT',
                # and the schema's state-machine trigger forbids ever
                # re-entering PENDING_PAYMENT once past it, so a manual_review
                # retry can never repeat this step (see retry_manual_review).
                grace_updated = self._conn.execute(
                    "UPDATE mgboost_subscriptions SET current_expiry=?,status='ACTIVE',updated_at=?,row_version=row_version+1 "
                    "WHERE id=? AND row_version=? AND current_plan_version_id=? AND current_expiry IS ? AND status IS ?",
                    (activation_at, timestamp, source['id'], source['row_version'],
                     row['source_plan_version_id'], row['original_source_expiry'], row['source_subscription_status']),
                )
                if grace_updated.rowcount != 1:
                    raise LegacyStarsPlanSwitchConflict('alignment grace CAS failed')
                post_grace_row_version = int(source['row_version']) + 1
                self._conn.execute(
                    "INSERT INTO mgboost_entitlement_mutations "
                    "(account_id,subscription_id,operation,payment_channel,mutation_source,actor_type,actor_ref,reason,before_json,after_json,created_at) "
                    "VALUES (?,?,'LEGACY_COMMERCIAL_ALIGNMENT_GRACE','TELEGRAM_STARS','DIRECT_PURCHASE','TELEGRAM',?,?,?,?,?)",
                    (row['account_id'], source['id'], str(invoice['payer_telegram_id']),
                     'self-service Stars legacy plan switch UTC-hour alignment',
                     json.dumps({'status': row['source_subscription_status'], 'current_expiry': row['original_source_expiry']}, sort_keys=True),
                     json.dumps({'status': 'ACTIVE', 'current_expiry': activation_at}, sort_keys=True), timestamp),
                )

                # source_post_confirmation_row_version records the row_version
                # AFTER the grace CAS above, not before -- apply_locked's own
                # re-validation (below) must match the grace-extended live
                # row, exactly like legacy_commercial_transition.py's
                # apply_ready compares against aligned_source_expiry (not
                # original_source_expiry) for this same reason.
                updated = self._conn.execute(
                    "UPDATE mgboost_legacy_stars_plan_switches SET state='SCHEDULED',confirmed_at=?,"
                    "aligned_source_expiry=?,activation_at=?,target_expiry=?,source_post_confirmation_row_version=?,"
                    "device_count_at_confirm=?,revision=revision+1,updated_at=? WHERE id=? AND revision=? AND state='PENDING_PAYMENT'",
                    (payment_confirmed_at, activation_at, activation_at, target_expiry, post_grace_row_version,
                     active_count, timestamp, switch_id, row['revision']),
                )
                if updated.rowcount != 1:
                    raise LegacyStarsPlanSwitchConflict('switch CAS failed during confirmation')
                fresh = self._get_inline(switch_id)
                self._event(switch_id, 'CONFIRMED', 'TELEGRAM', 'payment confirmed, activation scheduled', fresh['revision'], timestamp)
                self._conn.commit()
                # LEGACY_COMMERCIAL_ALIGNMENT_GRACE just extended the live
                # subscription's current_expiry, but nothing has told the
                # account's actual children yet -- mirrored verbatim from
                # legacy_commercial_transition.py::confirm_payment's own
                # post-commit block: ParentSyncStore exposes no caller-owned
                # transaction boundary, so this is called immediately AFTER
                # the money-confirming commit (never inside it -- no
                # network/remote I/O belongs in the entitlement transaction),
                # and the standalone sync worker/reconciliation sweep
                # repeats this idempotently, closing any crash between these
                # two commits.
                from .parent_sync import ParentSyncStore
                parent_sync = ParentSyncStore(self._conn, self._lock)
                parent_sync.refresh_desired_state(row['account_id'], now=timestamp)
                parent_sync.enqueue_current_children(row['account_id'], now=timestamp)
                return fresh
            except Exception:
                self._conn.rollback()
                raise

    def ready_due(self, *, now: int, limit: int = 50) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM mgboost_legacy_stars_plan_switches "
            "WHERE state='SCHEDULED' AND activation_at<=? ORDER BY activation_at,id LIMIT ?",
            (int(now), max(1, min(int(limit), 200))),
        ).fetchall()
        return [dict(row) for row in rows]

    def apply_locked(self, switch_id: int, *, now: int) -> dict:
        """Atomic local entitlement apply at activation_at. Mirrors
        legacy_commercial_transition.apply_ready's freeze/CAS/lineage/outbox
        shape exactly, minus any device retirement (none exists here)."""
        timestamp = int(now)
        with self._lock:
            try:
                self._conn.execute('BEGIN IMMEDIATE')
                row = self._conn.execute(
                    'SELECT * FROM mgboost_legacy_stars_plan_switches WHERE id=?', (int(switch_id),)
                ).fetchone()
                if not row:
                    raise LegacyStarsPlanSwitchError('switch not found')

                # Idempotency/replay check MUST happen before the state
                # gate below: a crash-and-retry (or a worker re-running
                # ready_due after this switch already reached APPLIED) must
                # short-circuit to the historical mutation, not be rejected
                # as "not ready" just because state is no longer SCHEDULED.
                idem_hash = _idempotency_hash(f"dl063-legacy-stars-switch-{switch_id:020d}")
                existing_mutation = self._conn.execute(
                    "SELECT * FROM mgboost_entitlement_mutations WHERE idempotency_key_hash=?", (idem_hash,)
                ).fetchone()
                if existing_mutation is not None:
                    already = self._get_inline(switch_id)
                    self._conn.commit()
                    return {**already, 'mutation_id': existing_mutation['id']}

                if row['state'] != 'SCHEDULED' or row['activation_at'] > timestamp:
                    raise LegacyStarsPlanSwitchConflict('switch is not ready for apply')

                sub = self._conn.execute('SELECT * FROM mgboost_subscriptions WHERE id=?', (row['source_subscription_id'],)).fetchone()
                latest = self._conn.execute(
                    'SELECT id FROM mgboost_subscriptions WHERE account_id=? ORDER BY id DESC LIMIT 1', (row['account_id'],)
                ).fetchone()
                # Matches against the GRACE-EXTENDED live row (aligned_
                # source_expiry, status=='ACTIVE'), not the pre-grace
                # original_source_expiry/source_subscription_status snapshot
                # -- confirm_locked's LEGACY_COMMERCIAL_ALIGNMENT_GRACE step
                # already moved the live subscription forward, exactly like
                # legacy_commercial_transition.py's own apply_ready checks
                # aligned_source_expiry, not original_source_expiry, here.
                if (not sub or not latest or latest['id'] != row['source_subscription_id']
                        or sub['row_version'] != row['source_post_confirmation_row_version']
                        or sub['current_plan_version_id'] != row['source_plan_version_id']
                        or sub['current_expiry'] != row['aligned_source_expiry'] or sub['status'] != 'ACTIVE'):
                    self._manual_review_inline(switch_id, reason='source_diverged_after_scheduling', now=timestamp)
                    self._conn.commit()
                    raise LegacyStarsPlanSwitchConflict('source entitlement diverged after scheduling')
                account = self._conn.execute('SELECT status FROM mgboost_accounts WHERE id=?', (row['account_id'],)).fetchone()
                invoice = self._conn.execute('SELECT * FROM stars_invoices WHERE id=?', (row['invoice_id'],)).fetchone()
                if not account or account['status'] != 'ACTIVE' or not invoice or invoice['status'] not in ('paid', 'canonical_applied'):
                    self._manual_review_inline(switch_id, reason='account_or_invoice_no_longer_applicable', now=timestamp)
                    self._conn.commit()
                    raise LegacyStarsPlanSwitchConflict('account or invoice is no longer applicable')
                target = self._conn.execute('SELECT * FROM mgboost_plan_versions WHERE id=?', (row['target_plan_version_id'],)).fetchone()
                active_count = self._device_count_locked(row['account_id'])
                if active_count > int(target['device_limit']):
                    self._manual_review_inline(switch_id, reason='device_limit_exceeded_at_apply', now=timestamp)
                    self._conn.commit()
                    raise LegacyStarsPlanSwitchConflict('device limit exceeded target at apply time')

                surviving_children = []
                periods: list[int] = []
                if target['wl_mode'] == 'LIMITED':
                    generations = self._conn.execute(
                        "SELECT g.id AS generation_id,g.account_id,s.current_generation,g.generation,"
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
                                or generation['desired_state'] not in _AUTHORITATIVE_CHILD_STATES
                                or generation['observed_state'] not in _AUTHORITATIVE_CHILD_STATES):
                            self._manual_review_inline(switch_id, reason='surviving_child_lineage_not_authoritative', now=timestamp)
                            self._conn.commit()
                            raise LegacyStarsPlanSwitchConflict('surviving child lineage is not authoritative')
                        surviving_children.append(int(generation['child_id']))

                self._conn.execute(
                    "UPDATE mgboost_legacy_stars_plan_switches SET state='APPLYING',revision=revision+1,updated_at=? WHERE id=?",
                    (timestamp, switch_id),
                )
                updated = self._conn.execute(
                    "UPDATE mgboost_subscriptions SET current_plan_version_id=?,status='ACTIVE',current_expiry=?,"
                    "updated_at=?,row_version=row_version+1 WHERE id=? AND row_version=?",
                    (row['target_plan_version_id'], row['target_expiry'], timestamp, sub['id'], sub['row_version']),
                )
                if updated.rowcount != 1:
                    raise LegacyStarsPlanSwitchConflict('subscription CAS failed')

                mutation_id = self._conn.execute(
                    "INSERT INTO mgboost_entitlement_mutations (account_id,subscription_id,operation,payment_channel,"
                    "mutation_source,actor_type,actor_ref,reason,external_reference,idempotency_key_hash,before_json,after_json,created_at) "
                    "VALUES (?,?,'LEGACY_COMMERCIAL_STARS_SWITCH','TELEGRAM_STARS','DIRECT_PURCHASE','TELEGRAM',?,?,?,?,?,?,?)",
                    (row['account_id'], sub['id'], str(invoice['payer_telegram_id']),
                     f"self-service Stars legacy plan switch {switch_id}", invoice['telegram_payment_charge_id'], idem_hash,
                     json.dumps({'plan_version_id': row['source_plan_version_id'], 'current_expiry': row['original_source_expiry']}, sort_keys=True),
                     json.dumps({'plan_version_id': row['target_plan_version_id'], 'new_expiry': row['target_expiry']}, sort_keys=True),
                     timestamp),
                ).lastrowid

                duration = self._conn.execute(
                    'SELECT * FROM mgboost_plan_durations WHERE plan_version_id=? AND duration_days=? ORDER BY duration_version DESC LIMIT 1',
                    (row['target_plan_version_id'], row['duration_days']),
                ).fetchone()
                term_id = self._conn.execute(
                    "INSERT INTO mgboost_subscription_terms (account_id,subscription_id,sequence_no,plan_version_id,duration_id,"
                    "duration_days,starts_at,ends_at,billing_required_snapshot,device_limit_mode_snapshot,device_limit_snapshot,"
                    "wl_mode_snapshot,wl_quota_bytes_snapshot,wl_period_days_snapshot,plan_snapshot_json,mutation_id,created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,1,?,?,?,?,?,?,?,?)",
                    (row['account_id'], sub['id'],
                     self._conn.execute('SELECT COALESCE(MAX(sequence_no),0)+1 FROM mgboost_subscription_terms WHERE subscription_id=?', (sub['id'],)).fetchone()[0],
                     target['id'], duration['id'] if duration else None, row['duration_days'], row['activation_at'], row['target_expiry'],
                     target['device_limit_mode'], target['device_limit'], target['wl_mode'], target['wl_quota_bytes'],
                     target['wl_period_days'], json.dumps({'plan_code': target['plan_code'], 'duration_days': row['duration_days']}, sort_keys=True),
                     mutation_id, timestamp),
                ).lastrowid

                if target['wl_mode'] == 'LIMITED':
                    base_seq = self._conn.execute(
                        'SELECT COALESCE(MAX(sequence_no),0) FROM mgboost_wl_periods WHERE subscription_id=?', (sub['id'],)
                    ).fetchone()[0]
                    for index, (start, end) in enumerate(
                        schedule_wl_period_windows(anchor=row['activation_at'], duration_days=row['duration_days'],
                                                    wl_period_days=target['wl_period_days']), 1):
                        period_id = self._conn.execute(
                            "INSERT INTO mgboost_wl_periods (account_id,subscription_id,subscription_term_id,sequence_no,"
                            "starts_at,ends_at,quota_mode,base_quota_bytes,status,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                            (row['account_id'], sub['id'], term_id, base_seq + index, start, end, 'LIMITED',
                             target['wl_quota_bytes'], 'PLANNED', timestamp),
                        ).lastrowid
                        periods.append(period_id)
                    first_period_id = periods[0]
                    for child_id in surviving_children:
                        for node_id in sorted(WL_NODE_IDS):
                            self._conn.execute(
                                "INSERT INTO mgboost_legacy_stars_plan_switch_wl_baselines "
                                "(switch_id,account_id,wl_period_id,child_intent_id,node_id,state,created_at) "
                                "VALUES (?,?,?,?,?,'PENDING',?)",
                                (switch_id, row['account_id'], first_period_id, child_id, node_id, timestamp),
                            )

                self._conn.execute(
                    "INSERT INTO mgboost_legacy_stars_plan_switch_applications "
                    "(invoice_id,account_id,entitlement_mutation_id,applied_expiry,entitlement_snapshot_json,created_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (row['invoice_id'], row['account_id'], mutation_id, row['target_expiry'],
                     json.dumps({'switch_id': switch_id, 'term_id': term_id}, sort_keys=True), timestamp),
                )
                # Same-transaction outbox insert, mirroring apply_ready/
                # apply_paid_invoice: "commit landed but sync never
                # enqueued" is structurally impossible here.
                self._conn.execute(
                    "INSERT OR IGNORE INTO mgboost_stars_purchase_sync_jobs "
                    "(invoice_id,account_id,entitlement_mutation_id,created_at,updated_at) VALUES (?,?,?,?,?)",
                    (row['invoice_id'], row['account_id'], mutation_id, timestamp, timestamp),
                )
                self._conn.execute(
                    "UPDATE stars_invoices SET status='canonical_applied',applied_expire=?,canonical_applied_at=?,"
                    "entitlement_mutation_id=? WHERE id=? AND status IN ('paid','canonical_applied')",
                    (row['target_expiry'], timestamp, mutation_id, row['invoice_id']),
                )
                self._conn.execute(
                    "UPDATE mgboost_legacy_stars_plan_switches SET state='APPLIED',applied_at=?,revision=revision+1,updated_at=? WHERE id=?",
                    (timestamp, timestamp, switch_id),
                )
                fresh = self._get_inline(switch_id)
                self._event(switch_id, 'APPLIED', 'SYSTEM', 'atomic local apply', fresh['revision'], timestamp)
                self._conn.commit()
                return {**fresh, 'mutation_id': mutation_id, 'wl_period_ids': periods}
            except Exception:
                self._conn.rollback()
                raise

    def manual_review(self, switch_id: int, *, reason: str, now: int) -> None:
        timestamp = int(now)
        with self._lock:
            try:
                self._conn.execute('BEGIN IMMEDIATE')
                row = self._conn.execute('SELECT * FROM mgboost_legacy_stars_plan_switches WHERE id=?', (int(switch_id),)).fetchone()
                if not row:
                    raise LegacyStarsPlanSwitchError('switch not found')
                self._manual_review_inline(switch_id, reason=reason, now=timestamp)
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def retry_manual_review(self, capability, switch_id: int, *, reason: str, now: int | None = None) -> dict:
        """Explicit audited operator retry -- the only capability-gated
        method in this store, mirroring legacy_commercial_transition.py's
        own manual_review (system, no capability) / retry_manual_review
        (operator, capability-gated) split."""
        timestamp = int(time.time()) if now is None else int(now)
        if self._authority is None:
            raise LegacyStarsPlanSwitchError('operator retry requires an authority')
        actor = self._authority.require(capability)
        reason = str(reason or '').strip()
        if not 8 <= len(reason) <= 300:
            raise LegacyStarsPlanSwitchError('bounded retry reason is required')
        with self._lock:
            try:
                self._conn.execute('BEGIN IMMEDIATE')
                row = self._conn.execute('SELECT * FROM mgboost_legacy_stars_plan_switches WHERE id=?', (int(switch_id),)).fetchone()
                if not row or row['state'] != 'MANUAL_REVIEW':
                    raise LegacyStarsPlanSwitchConflict('switch is not in manual review')
                # A switch that hit MANUAL_REVIEW before ever completing
                # confirmation (e.g. source_diverged_before_confirmation,
                # device_limit_exceeded_at_confirm) has activation_at/
                # confirmed_at still NULL. ready_due() only ever selects
                # SCHEDULED rows with activation_at<=now -- NULL never
                # satisfies that, so blindly restoring to SCHEDULED here
                # would create a row no worker can ever pick up again.
                # Restore to PENDING_PAYMENT instead so the next confirm
                # pass (pending_legacy_switch_invoices/confirm_locked) can
                # actually re-attempt confirmation. Only a switch that
                # already has a real activation_at (manual_review happened
                # AFTER confirmation, e.g. apply_locked's lineage/CAS-drift
                # checks) goes back to SCHEDULED.
                next_state = 'SCHEDULED' if row['confirmed_at'] is not None and row['activation_at'] is not None else 'PENDING_PAYMENT'
                self._conn.execute(
                    "UPDATE mgboost_legacy_stars_plan_switches SET state=?,review_reason=NULL,revision=revision+1,updated_at=? WHERE id=?",
                    (next_state, timestamp, row['id']),
                )
                # Additional bug found while wiring finding 7 together with
                # finding 3: _manual_review_inline (finding 3's fix) flips
                # the bound invoice to status='manual_review' whenever money
                # already moved. Without restoring it here, the retried
                # switch is unreachable again: pending_legacy_switch_
                # invoices() only selects invoice status='paid' (so a
                # PENDING_PAYMENT retry would never be picked back up for
                # confirmation), and apply_locked itself requires invoice
                # status IN ('paid','canonical_applied') (so a SCHEDULED
                # retry would immediately bounce back to MANUAL_REVIEW on
                # its very next apply attempt). Restore 'paid' only when the
                # invoice is still exactly 'manual_review' -- never touch
                # any other status (e.g. an already-refunded invoice, or one
                # this store never flipped in the first place).
                self._conn.execute(
                    "UPDATE stars_invoices SET status='paid',manual_review_reason=NULL,manual_review_at=NULL "
                    "WHERE id=? AND status='manual_review'",
                    (row['invoice_id'],),
                )
                fresh = self._get_inline(switch_id)
                self._event(switch_id, 'MANUAL_REVIEW_RETRY', actor, reason, fresh['revision'], timestamp)
                self._conn.commit()
                return fresh
            except Exception:
                self._conn.rollback()
                raise

    def _mark_refunded_inline(self, *, invoice_id: int, now: int) -> None:
        """Runs inside the CALLER's already-open transaction (see
        Database.mark_invoice_refunded, which folds this into the SAME
        BEGIN IMMEDIATE as the stars_invoices.status='refunded' CAS -- the
        two writes are money-already-refunded bookkeeping with no external
        call in between, exactly like capture_paid's own already-moved-money
        bookkeeping, so there is no real crash window between them to
        reconcile after the fact). Money-only, exactly like every other
        invoice kind's existing refund path: never touches subscriptions/
        entitlements. An already-APPLIED switch's entitlement is left
        completely untouched here (fail-closed) -- only a switch that never
        reached APPLIED (money moved but nothing was ever granted) is moved
        to the terminal REFUNDED state, which falls outside the live-switch
        UNIQUE index so a future attempt is possible. If invoice_id has no
        bound switch at all (an ordinary CANONICAL_PLAN/SIGNUP refund), this
        is a no-op. Idempotent no-op if the switch is already terminal."""
        timestamp = int(now)
        row = self._conn.execute(
            'SELECT * FROM mgboost_legacy_stars_plan_switches WHERE invoice_id=?', (int(invoice_id),)
        ).fetchone()
        if not row or row['state'] in ('APPLIED', 'CANCELLED', 'REFUNDED'):
            return
        self._conn.execute(
            "UPDATE mgboost_legacy_stars_plan_switches SET state='REFUNDED',revision=revision+1,updated_at=? WHERE id=?",
            (timestamp, row['id']),
        )
        fresh = self._get_inline(row['id'])
        self._event(row['id'], 'REFUNDED', 'SYSTEM', 'Stars payment refunded before application', fresh['revision'], timestamp)

    def mark_refunded(self, *, invoice_id: int, now: int) -> None:
        """Top-level, self-transacting wrapper around ``_mark_refunded_
        inline`` for any standalone/reconciliation caller that does not
        already hold an open transaction on this connection. The real admin
        refund path (Database.mark_invoice_refunded) does NOT call this --
        it calls ``_mark_refunded_inline`` directly, inside its own
        transaction, so the invoice and switch flips are atomic."""
        with self._lock:
            try:
                self._conn.execute('BEGIN IMMEDIATE')
                self._mark_refunded_inline(invoice_id=invoice_id, now=now)
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def active_device_count(self, account_id: int) -> int:
        """Read-only helper for the bot's live precheck before showing the
        payment confirmation screen -- no lock needed for a single SELECT."""
        return self._device_count_locked(account_id)

    def get(self, switch_id: int) -> dict | None:
        row = self._conn.execute('SELECT * FROM mgboost_legacy_stars_plan_switches WHERE id=?', (int(switch_id),)).fetchone()
        return dict(row) if row else None

    def for_account(self, account_id: int) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM mgboost_legacy_stars_plan_switches WHERE account_id=? AND state NOT IN ('APPLIED','CANCELLED','REFUNDED') "
            "ORDER BY id DESC LIMIT 1", (int(account_id),),
        ).fetchone()
        return dict(row) if row else None

    def sweep_expired_unpaid(self, *, now: int, limit: int = 50) -> int:
        """System-actor cleanup for a PENDING_PAYMENT switch whose bound
        invoice expired unpaid -- there is no existing proactive sweep for
        stale stars_invoices (expires_at is only checked reactively at
        pre_checkout), so this store provides its own, released into the
        same worker tick that already drives confirm/apply."""
        timestamp = int(now)
        with self._lock:
            rows = self._conn.execute(
                "SELECT sw.id FROM mgboost_legacy_stars_plan_switches sw JOIN stars_invoices inv ON inv.id=sw.invoice_id "
                "WHERE sw.state='PENDING_PAYMENT' AND inv.status='created' AND inv.expires_at<? "
                "ORDER BY sw.id LIMIT ?", (timestamp, max(1, min(int(limit), 200))),
            ).fetchall()
        released = 0
        for row in rows:
            try:
                self.cancel_unpaid_locked(row['id'], now=timestamp)
                released += 1
            except LegacyStarsPlanSwitchConflict:
                # Payment landed concurrently with this sweep -- leave it
                # alone, capture_paid/confirm_locked own it from here.
                pass
        return released
