"""Exact-proof bootstrap retirement; preview is read-only, effects use PH3-05.

No remote creation and no capacity exception. Durable lifecycle rows are the
only retirement journal. Callers supply the configured device-slot HMAC key.
"""
from __future__ import annotations

import hashlib

from .child_lifecycle import ChildLifecycleConflict, process_free, process_revoke
from .legacy_grace_migration import is_genesis_hwid_verifier

REASON = 'automatic retirement of proven synthetic migration bootstrap after confirmed real-device migration'
SUCCESS_STATES = ('MIGRATED', 'LEGACY_REVOKED')


def _key(account_id, generation_id, kind):
    return f'bootstrap-retirement-v1:{account_id}:{generation_id}:{kind}'


def _owned(op, account_id, generation_id):
    kind = op['operation_kind']
    expected = hashlib.sha256((f'child-lifecycle-{kind}-v1\0' +
                              _key(account_id, generation_id, kind)).encode()).hexdigest()
    return (kind in ('REVOKE', 'FREE') and op['account_id'] == account_id
            and op['old_slot_generation_id'] == generation_id
            and op['idempotency_key_hash'] == expected and op['reason'] == REASON)


def classify(connection, *, account_id, generation_id, hmac_key):
    """Bounded public result; no verifier, UUID, names, or raw remote payloads.

    Can run inside the lifecycle prepare transaction, so evidence and enqueue
    share the database write lock. Rechecked by fences before remote effects.
    """
    result = dict(account_id=account_id, bootstrap_generation_id=generation_id,
                  bootstrap_slot_number=None, bootstrap_generation=None,
                  bootstrap_child_intent_id=None, eligibility='NOT_ELIGIBLE',
                  reason='NOT_PROVEN_BOOTSTRAP', confirmed_real_generation_id=None,
                  confirmed_real_slot_number=None, migration_proof_state='MISSING',
                  telemetry_proof_state='UNKNOWN', revoke_state=None, free_state=None)
    def refuse(reason):
        return {**result, 'reason': reason}
    g = connection.execute(
        'SELECT g.*,s.current_generation,s.desired_state,s.observed_state '
        'FROM mgboost_device_slot_generations g JOIN mgboost_device_slots s '
        'ON s.id=g.slot_id AND s.account_id=g.account_id WHERE g.id=? AND g.account_id=?',
        (generation_id, account_id)).fetchone()
    if not g or not is_genesis_hwid_verifier(account_id, g['hwid_verifier'], hmac_key):
        return result
    result.update(bootstrap_slot_number=g['slot_number'], bootstrap_generation=g['generation'])
    child = connection.execute(
        'SELECT * FROM mgboost_child_user_intents WHERE slot_generation_id=? AND account_id=?',
        (generation_id, account_id)).fetchone()
    if (not child or not child['uuid_verifier'] or child['slot_id'] != g['slot_id']
            or child['generation'] != g['generation'] or child['slot_number'] != g['slot_number']):
        return refuse('CHILD_PROOF_MISSING')
    result['bootstrap_child_intent_id'] = child['id']
    ops = connection.execute(
        'SELECT * FROM mgboost_child_lifecycle_operations WHERE slot_id=?', (g['slot_id'],)).fetchall()
    own = {}
    for op in ops:
        if _owned(op, account_id, generation_id) and op['old_child_intent_id'] == child['id']:
            own[op['operation_kind']] = op
        elif op['old_slot_generation_id'] == generation_id or op['state'] != 'APPLIED':
            return refuse('LIFECYCLE_CONFLICT')
    revoke, free = own.get('REVOKE'), own.get('FREE')
    result.update(revoke_state=revoke['state'] if revoke else None,
                  free_state=free['state'] if free else None)
    if free and free['state'] == 'APPLIED' and g['status'] == 'RELEASED':
        return {**result, 'eligibility': 'COMPLETED', 'reason': 'RETIRED'}
    if g['current_generation'] != g['generation']:
        return refuse('STALE_GENERATION')
    released_replay = (g['status'] == 'RELEASED' and g['end_reason'] == REASON
                       and g['desired_state'] == g['observed_state'] == 'FREE'
                       and revoke and revoke['state'] == 'APPLIED' and free)
    if g['status'] != 'ACTIVE' and not released_replay:
        return refuse('GENERATION_NOT_ACTIVE')
    if g['status'] == 'ACTIVE' and g['desired_state'] != 'ACTIVE':
        return refuse('CHILD_NOT_SUITABLE')
    if any(op['state'] == 'ERROR' for op in own.values()):
        return refuse('LIFECYCLE_ERROR')
    revoked = revoke and revoke['state'] == 'APPLIED'
    expected = 'REVOKED' if revoked else 'ACTIVE'
    if child['desired_state'] != expected or child['observed_state'] != expected:
        return refuse('CHILD_NOT_SUITABLE')
    account = connection.execute('SELECT status FROM mgboost_accounts WHERE id=?', (account_id,)).fetchone()
    if not account or account['status'] != 'ACTIVE':
        return refuse('ACCOUNT_NOT_ACTIVE')
    provisioning = connection.execute('SELECT state FROM mgboost_outbox WHERE child_intent_id=?', (child['id'],)).fetchone()
    if not provisioning or provisioning['state'] != 'APPLIED':
        return refuse('CHILD_NOT_SUITABLE')
    if connection.execute(
        "SELECT 1 FROM mgboost_parent_sync_operations WHERE child_intent_id=? "
        "AND state IN ('PENDING','RETRY','IN_FLIGHT')", (child['id'],)).fetchone():
        return refuse('LIFECYCLE_CONFLICT')
    bridge = connection.execute(
        'SELECT b.legacy_alias_id FROM mgboost_legacy_bridge_bindings b '
        'JOIN mgboost_legacy_account_aliases a ON a.id=b.legacy_alias_id '
        "AND a.account_id=b.account_id AND a.alias_role='PRIMARY' "
        'WHERE b.account_id=? AND b.enabled=1', (account_id,)).fetchone()
    if not bridge:
        return refuse('BRIDGE_NOT_ENABLED')
    real = connection.execute(
        'SELECT g.*,m.state AS migration_state,t.hwid_verifier AS telemetry_verifier '
        'FROM mgboost_device_slot_generations g JOIN mgboost_device_slots s '
        'ON s.id=g.slot_id AND s.account_id=g.account_id AND s.current_generation=g.generation '
        'JOIN mgboost_child_user_intents c ON c.slot_generation_id=g.id AND c.account_id=g.account_id '
        'LEFT JOIN mgboost_migration_bindings m ON m.slot_generation_id=g.id '
        'AND m.account_id=g.account_id AND m.hwid_verifier=g.hwid_verifier '
        'AND m.child_intent_id=c.id AND m.legacy_alias_id=? '
        'LEFT JOIN mgboost_device_telemetry t ON t.slot_generation_id=g.id AND t.account_id=g.account_id '
        "WHERE g.account_id=? AND g.id!=? AND g.status='ACTIVE' "
        "AND c.desired_state='ACTIVE' AND c.observed_state='ACTIVE' "
        'AND NOT EXISTS (SELECT 1 FROM mgboost_child_lifecycle_operations o '
        'WHERE o.old_slot_generation_id=g.id) ORDER BY g.id',
        (bridge['legacy_alias_id'], account_id, generation_id)).fetchall()
    reason = 'NO_SUCCESSFUL_REAL_MIGRATION'
    for device in real:
        if is_genesis_hwid_verifier(account_id, device['hwid_verifier'], hmac_key):
            continue
        if device['migration_state'] not in SUCCESS_STATES:
            continue
        result['migration_proof_state'] = device['migration_state']
        reason = 'CANONICAL_TELEMETRY_MISSING'
        if device['telemetry_verifier'] != device['hwid_verifier']:
            continue
        result.update(confirmed_real_generation_id=device['id'],
                      confirmed_real_slot_number=device['slot_number'], telemetry_proof_state='CONFIRMED')
        return {**result, 'eligibility': 'PENDING' if own else 'ELIGIBLE', 'reason': 'PROVEN'}
    return refuse(reason)


def preview(connection, *, hmac_key, account_ids=None):
    """Read-only detector usable with a SQLite mode=ro connection (no Database init)."""
    scope = None if account_ids is None else set(account_ids)
    results = []
    for g in connection.execute('SELECT id,account_id,hwid_verifier FROM mgboost_device_slot_generations ORDER BY id'):
        if scope is not None and g['account_id'] not in scope:
            continue
        if is_genesis_hwid_verifier(g['account_id'], g['hwid_verifier'], hmac_key):
            results.append(classify(connection, account_id=g['account_id'], generation_id=g['id'], hmac_key=hmac_key))
    return results


def sweep(db, *, hmac_key, revoke_fn, worker_id, now, account_ids=None):
    summary = dict(completed=0, pending=0, refused=0, errors=0)
    for candidate in preview(db._conn, hmac_key=hmac_key, account_ids=account_ids):
        if candidate['eligibility'] not in ('ELIGIBLE', 'PENDING'):
            summary['refused'] += candidate['eligibility'] != 'COMPLETED'
            continue
        account_id = candidate['account_id']
        generation_id = candidate['bootstrap_generation_id']
        def fence():
            current = classify(db._conn, account_id=account_id, generation_id=generation_id, hmac_key=hmac_key)
            if current['eligibility'] not in ('ELIGIBLE', 'PENDING'):
                raise ChildLifecycleConflict('bootstrap retirement evidence changed')
        try:
            for kind in ('REVOKE', 'FREE'):
                fence()
                op = db.child_lifecycle._prepare(
                    account_id=account_id, old_child_intent_id=candidate['bootstrap_child_intent_id'],
                    operation_kind=kind, reason=REASON, idempotency_key=_key(account_id, generation_id, kind),
                    now=now, guard_fn=fence)
                if op['state'] != 'APPLIED':
                    if kind == 'REVOKE':
                        process_revoke(db, op['operation_id'], worker_id=worker_id,
                                       revoke_fn=revoke_fn, now=now, fence_fn=fence)
                    else:
                        process_free(db, op['operation_id'], worker_id=worker_id, now=now,
                                     strict_generation=True, fence_fn=fence)
                state = db._conn.execute('SELECT state FROM mgboost_child_lifecycle_operations WHERE id=?', (op['id'],)).fetchone()[0]
                if state != 'APPLIED':
                    summary['pending'] += 1
                    break
            else:
                summary['completed'] += 1
        except Exception:
            # Existing lease expiry retries lost remote acknowledgements. Never
            # log exception text (broker errors can contain sensitive payloads).
            summary['errors'] += 1
    return summary
