"""Canonical 4-slot customer regression and retirement failure boundaries."""
import copy
import json

import pytest

from src.bootstrap_retirement import classify, preview, sweep
from src.broker_operations import BrokerOperations
from src.device_slots import CapacityReached
from src.legacy_grace_migration import migrate_bootstrapped_account, _genesis_hwid
from src.migration_lifecycle import process_migration_bridge_request
from tests.test_legacy_grace_migration import db, _capability, _bootstrap_with_entitlement, _remote_for
from tests.test_child_provisioning import HWID_KEY
from tests.test_opaque_resolver import _known_hwid_meta


@pytest.fixture
def scenario(db):
    cap = _capability(db)
    from src.legacy_grace_registration import bootstrap_grace_subject
    bootstrap_grace_subject(
        db, capability=cap, legacy_username='retirement-client', legacy_status='ACTIVE',
        legacy_expiry=100000, observed_device_count=3, observed_hwid_count=3,
        decision_ref='retirement-fixture', payment_decision_ref='owner-attestation',
        payment_attestation_note='Test evidence', payment_evidence={'source': 'fixture'},
        idempotency_key='retirement-fixture-bootstrap', now=1000)
    remote, ensure, subscription = _remote_for('retirement-client')
    account = db._conn.execute('SELECT account_id FROM mgboost_legacy_account_aliases').fetchone()[0]
    from src.legacy_paid_compat import increase_device_limit
    increase_device_limit(db, capability=cap, account_id=account, approved_extra_device_slots=1,
                          decision_ref='fixture-owner-approved-four', evidence={'source': 'fixture-owner'}, now=1500)
    migrate_args = dict(capability=cap, account_id=account, hmac_key=HWID_KEY,
                        marzban_user_snapshot=remote.users['retirement-client'], ensure_fn=ensure,
                        decision_ref='retirement-fixture', worker_id='fixture-worker', now=2000)
    migrate_bootstrapped_account(db, **migrate_args)
    genesis = db._conn.execute('SELECT id FROM mgboost_device_slot_generations WHERE account_id=?', (account,)).fetchone()[0]
    def migrate(hwid, telemetry=True):
        record = db.device_telemetry.record_observation
        if not telemetry:
            db.device_telemetry.record_observation = lambda **kwargs: None
        result = process_migration_bridge_request(
            db, 'retirement-client', _known_hwid_meta(hwid), hmac_key=HWID_KEY,
            ensure_fn=ensure, subscription_fn=subscription, worker_id='fixture-real', now=3000)
        db.device_telemetry.record_observation = record
        assert result.outcome == 'OK'
        return db._conn.execute('SELECT * FROM mgboost_device_slot_generations WHERE account_id=? ORDER BY id DESC LIMIT 1', (account,)).fetchone()
    calls = []
    def revoke(payload):
        calls.append(copy.deepcopy(payload))
        return BrokerOperations(remote).dispatch('child.user.revoke', payload)
    return dict(account=account, genesis=genesis, migrate=migrate, remote=remote,
                revoke=revoke, calls=calls, migrate_args=migrate_args)


def check(db, fx):
    return classify(db._conn, account_id=fx['account'], generation_id=fx['genesis'], hmac_key=HWID_KEY)


def run(db, fx, now=4000, revoke=None):
    return sweep(db, hmac_key=HWID_KEY, revoke_fn=revoke or fx['revoke'], worker_id='retire-test', now=now)


def active(db, fx):
    return db._conn.execute("SELECT count(*) FROM mgboost_device_slot_generations WHERE account_id=? AND status='ACTIVE'", (fx['account'],)).fetchone()[0]


def test_no_real_device(db, scenario):
    assert check(db, scenario)['eligibility'] == 'NOT_ELIGIBLE'
    assert run(db, scenario)['completed'] == 0
    assert not scenario['calls']


@pytest.mark.parametrize('proof', ['migration', 'telemetry', 'both', 'pending', 'error'])
def test_missing_proof_fail_closed(db, scenario, proof):
    g = scenario['migrate']('real-a', telemetry=proof not in ('telemetry', 'both'))
    if proof in ('migration', 'both', 'pending', 'error'):
        state = {'pending': 'LEGACY_REVOKE_PENDING', 'error': 'ERROR_RECONCILE'}.get(proof, 'MIGRATING')
        db._conn.execute('UPDATE mgboost_migration_bindings SET state=? WHERE slot_generation_id=?', (state, g['id']))
    db._conn.commit()
    assert check(db, scenario)['eligibility'] == 'NOT_ELIGIBLE'
    assert run(db, scenario)['completed'] == 0
    assert active(db, scenario) == 2


def test_four_slot_customer_fixture_and_reuse(db, scenario):
    fx = scenario
    real_a = fx['migrate']('real-a')
    fx['migrate']('real-b', telemetry=False)
    fx['migrate']('real-c', telemetry=False)
    db._conn.commit()
    before = {table: [tuple(r) for r in db._conn.execute('SELECT * FROM '+table)] for table in (
        'mgboost_migration_bindings', 'mgboost_device_telemetry', 'mgboost_legacy_bridge_bindings',
        'mgboost_subscriptions', 'mgboost_legacy_account_aliases')}
    real_users = {r['child_username']: copy.deepcopy(fx['remote'].users[r['child_username']]) for r in db._conn.execute(
        'SELECT child_username FROM mgboost_child_user_intents WHERE slot_generation_id!=?', (fx['genesis'],))}
    assert check(db, fx)['eligibility'] == 'ELIGIBLE'
    assert active(db, fx) == 4
    with pytest.raises(CapacityReached):
        db.device_slots.claim(fx['account'], 'new-ipad', HWID_KEY, now=3100)
    assert run(db, fx)['completed'] == 1
    assert active(db, fx) == 3
    assert len(fx['calls']) == 1
    assert check(db, fx)['eligibility'] == 'COMPLETED'
    for table, rows in before.items():
        assert [tuple(r) for r in db._conn.execute('SELECT * FROM '+table)] == rows
    for name, user in real_users.items():
        assert fx['remote'].users[name] == user
    new = db.device_slots.claim(fx['account'], 'new-ipad', HWID_KEY, now=4100)
    assert new['slot_number'] == 1
    assert new['generation'] == 2
    assert active(db, fx) == 4
    assert classify(db._conn, account_id=fx['account'], generation_id=new['generation_id'], hmac_key=HWID_KEY)['reason'] == 'NOT_PROVEN_BOOTSTRAP'
    assert run(db, fx, now=4200)['completed'] == 0
    assert len(fx['calls']) == 1
    assert migrate_bootstrapped_account(db, **fx['migrate_args'])['already_migrated']
    assert active(db, fx) == 4


def test_wrong_account_and_wrong_key(db, scenario):
    fx = scenario
    fx['migrate']('real-a')
    assert classify(db._conn, account_id=fx['account']+1, generation_id=fx['genesis'], hmac_key=HWID_KEY)['eligibility'] == 'NOT_ELIGIBLE'
    assert classify(db._conn, account_id=fx['account'], generation_id=fx['genesis'], hmac_key='wrong-key')['eligibility'] == 'NOT_ELIGIBLE'
    encoded = json.dumps(preview(db._conn, hmac_key=HWID_KEY))
    assert 'hmac-sha256' not in encoded and 'uuid' not in encoded and _genesis_hwid(fx['account']) not in encoded


def test_remote_failure_keeps_capacity_then_retry(db, scenario):
    fx = scenario
    fx['migrate']('real-a')
    def unavailable(payload):
        raise ConnectionError('unavailable')
    assert run(db, fx, revoke=unavailable)['errors'] == 1
    assert active(db, fx) == 2
    assert db._conn.execute("SELECT count(*) FROM mgboost_child_lifecycle_operations WHERE operation_kind='FREE'").fetchone()[0] == 0
    assert run(db, fx, now=4001)['pending'] == 1
    assert run(db, fx, now=4040)['completed'] == 1
    assert active(db, fx) == 1
    assert db._conn.execute('SELECT count(*) FROM mgboost_child_lifecycle_operations').fetchone()[0] == 2


@pytest.mark.parametrize('stage', ['before_remote', 'after_remote', 'after_ack', 'after_release'])
def test_crash_recovery(db, scenario, monkeypatch, stage):
    fx = scenario
    fx['migrate']('real-a')
    if stage == 'before_remote':
        original = fx['revoke']
        def crash(payload):
            raise RuntimeError('crash')
        fx['revoke'] = crash
    elif stage == 'after_remote':
        original = db.child_lifecycle.acknowledge_revoke
        monkeypatch.setattr(db.child_lifecycle, 'acknowledge_revoke', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('crash')))
    elif stage == 'after_ack':
        original = db.child_lifecycle._prepare
        def prepare(**kwargs):
            if kwargs['operation_kind'] == 'FREE':
                raise RuntimeError('crash')
            return original(**kwargs)
        monkeypatch.setattr(db.child_lifecycle, '_prepare', prepare)
    else:
        original = db.child_lifecycle.finish_free
        monkeypatch.setattr(db.child_lifecycle, 'finish_free', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('crash')))
    assert run(db, fx)['errors'] == 1
    assert active(db, fx) == (1 if stage == 'after_release' else 2)
    remote_after = copy.deepcopy(fx['remote'].users)
    if stage == 'before_remote':
        fx['revoke'] = original
    else:
        monkeypatch.setattr(db.child_lifecycle, {'after_remote': 'acknowledge_revoke', 'after_ack': '_prepare', 'after_release': 'finish_free'}[stage], original)
    assert run(db, fx, now=4040)['completed'] == 1
    assert active(db, fx) == 1
    if stage != 'before_remote':
        assert fx['remote'].users == remote_after
    assert run(db, fx, now=4080)['completed'] == 0


def test_replaced_generation_blocks_stale_free(db, scenario, monkeypatch):
    fx = scenario
    fx['migrate']('real-a')
    original = db.child_lifecycle.finish_free
    monkeypatch.setattr(db.child_lifecycle, 'finish_free', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('crash')))
    run(db, fx)
    new = db.device_slots.claim(fx['account'], 'replacement', HWID_KEY, now=4010)
    monkeypatch.setattr(db.child_lifecycle, 'finish_free', original)
    assert check(db, fx)['reason'] == 'STALE_GENERATION'
    run(db, fx, now=4040)
    assert db._conn.execute('SELECT status FROM mgboost_device_slot_generations WHERE id=?', (new['generation_id'],)).fetchone()[0] == 'ACTIVE'
    assert len(fx['calls']) == 1


def test_disabled_bridge_and_terminal_recreation_guard(db, scenario):
    from src.legacy_grace_migration import PrerequisiteMissing
    fx = scenario
    fx['migrate']('real-a')
    run(db, fx)
    db._conn.execute('UPDATE mgboost_legacy_bridge_bindings SET enabled=0')
    db._conn.commit()
    with pytest.raises(PrerequisiteMissing, match='bootstrap must not be recreated'):
        migrate_bootstrapped_account(db, **fx['migrate_args'])
    assert active(db, fx) == 1


def test_conflicting_lifecycle_refused(db, scenario):
    fx = scenario
    fx['migrate']('real-a')
    child_id = check(db, fx)['bootstrap_child_intent_id']
    db.child_lifecycle.prepare_rebind(account_id=fx['account'], old_child_intent_id=child_id,
        reason='operator replacement', idempotency_key='operator-replacement-test', now=3500)
    assert check(db, fx)['reason'] == 'LIFECYCLE_CONFLICT'
    assert run(db, fx)['completed'] == 0
    assert not fx['calls']


def test_non_one_slot_uses_exact_verifier(db, scenario):
    fx = scenario
    fx['migrate']('real-a')
    run(db, fx)
    db.device_slots.claim(fx['account'], 'real-slot-one', HWID_KEY, now=4100)
    # Construct an independent historical-identity classifier input using the
    # store, not a slot-number mutation (slot identities are immutable).
    synthetic = db.device_slots.claim(fx['account'], _genesis_hwid(fx['account']), HWID_KEY, now=4200)
    assert synthetic['slot_number'] != 1
    result = classify(db._conn, account_id=fx['account'], generation_id=synthetic['generation_id'], hmac_key=HWID_KEY)
    assert result['bootstrap_slot_number'] == synthetic['slot_number']
    assert result['reason'] == 'CHILD_PROOF_MISSING'


def test_worker_preview_then_active(db, scenario, monkeypatch):
    import child_worker_main
    import src.config as config
    from types import SimpleNamespace
    fx = scenario
    fx['migrate']('real-a')
    monkeypatch.setattr(config, 'DEVICE_SLOT_HMAC_KEY', HWID_KEY)
    remote = SimpleNamespace(revoke_child_user=fx['revoke'])
    monkeypatch.delenv('BOOTSTRAP_RETIREMENT_MODE', raising=False)
    before = db._conn.total_changes
    result = child_worker_main.run_bootstrap_retirement_tick(db, remote, worker_id='worker-retire', now=4000)
    assert result['mode'] == 'preview'
    assert result['candidates'][0]['eligibility'] == 'ELIGIBLE'
    assert db._conn.total_changes == before
    assert not fx['calls']
    monkeypatch.setenv('BOOTSTRAP_RETIREMENT_MODE', 'active')
    assert child_worker_main.run_bootstrap_retirement_tick(db, remote, worker_id='worker-retire', now=4010)['completed'] == 1
    assert child_worker_main.run_bootstrap_retirement_tick(db, remote, worker_id='worker-retire', now=4050)['completed'] == 0
    assert len(fx['calls']) == 1


def test_admin_bootstrap_states(db, scenario):
    from src.admin_read_models import _device_summaries
    fx = scenario
    def device():
        return _device_summaries(db._conn, fx['account'], device_slot_hmac_key=HWID_KEY)[0]
    assert device()['bootstrap_retirement']['reason'] == 'NO_SUCCESSFUL_REAL_MIGRATION'
    fx['migrate']('real-a')
    assert device()['bootstrap_retirement']['eligibility'] == 'ELIGIBLE'
    run(db, fx, revoke=lambda payload: (_ for _ in ()).throw(ConnectionError()))
    assert device()['bootstrap_retirement']['revoke_state'] == 'IN_FLIGHT'
    run(db, fx, now=4040)
    assert not device()['proven_genesis_bootstrap']
    assert device()['generation_status'] is None
    assert device()['desired_state'] == 'FREE'


def test_prepare_rechecks_evidence_inside_transaction(db, scenario, monkeypatch):
    fx = scenario
    fx['migrate']('real-a')
    original = db.child_lifecycle._prepare
    def changed(**kwargs):
        db._conn.execute('UPDATE mgboost_legacy_bridge_bindings SET enabled=0')
        db._conn.commit()
        return original(**kwargs)
    monkeypatch.setattr(db.child_lifecycle, '_prepare', changed)
    assert run(db, fx)['errors'] == 1
    assert db._conn.execute('SELECT count(*) FROM mgboost_child_lifecycle_operations').fetchone()[0] == 0
    assert not fx['calls']


def test_exact_telemetry_mismatch_rejected_without_mutating_history(db, scenario):
    fx = scenario
    fx['migrate']('real-a')
    # A read adapter models corrupted imported telemetry; immutable production
    # records and their protective triggers remain untouched by the fixture.
    class MismatchedTelemetry:
        def execute(self, sql, args=()):
            rows = db._conn.execute(sql, args)
            if 'AS telemetry_verifier' in sql:
                class Rows:
                    def fetchall(self):
                        return [{**dict(row), 'telemetry_verifier': 'hmac-sha256:' + '0'*64} for row in rows]
                return Rows()
            return rows
    result = classify(MismatchedTelemetry(), account_id=fx['account'], generation_id=fx['genesis'], hmac_key=HWID_KEY)
    assert result['eligibility'] == 'NOT_ELIGIBLE'
    assert result['reason'] == 'CANONICAL_TELEMETRY_MISSING'


def test_provisioning_worker_preserves_retired_child(db, scenario):
    from src.child_worker import ChildProvisioningWorker
    fx = scenario
    fx['migrate']('real-a')
    run(db, fx)
    op = db._conn.execute('SELECT o.operation_id FROM mgboost_outbox o JOIN mgboost_child_user_intents c ON c.id=o.child_intent_id WHERE c.slot_generation_id=?', (fx['genesis'],)).fetchone()[0]
    class NoRemote:
        def __getattr__(self, name):
            raise AssertionError('retired child must never reach remote provisioning')
    worker = ChildProvisioningWorker(db, NoRemote(), worker_id='retired-provisioner',
                                     allowed_operation_ids=[op], mode='active', clock=lambda: 4100)
    assert worker.run_once()['skipped'] == 1
    assert check(db, fx)['eligibility'] == 'COMPLETED'
    assert db._conn.execute('SELECT observed_state FROM mgboost_child_user_intents WHERE slot_generation_id=?', (fx['genesis'],)).fetchone()[0] == 'REVOKED'


def test_free_cannot_release_bootstrap_without_applied_revoke(db, scenario):
    from src.child_lifecycle import process_free, ChildLifecycleError
    fx = scenario
    fx['migrate']('real-a')
    child_id = check(db, fx)['bootstrap_child_intent_id']
    op = db.child_lifecycle.prepare_free(account_id=fx['account'], old_child_intent_id=child_id,
        reason='test ordering proof', idempotency_key='test-free-without-revoke', now=3500)
    with pytest.raises(ChildLifecycleError, match='not APPLIED'):
        process_free(db, op['operation_id'], worker_id='ordering-test', now=3501, strict_generation=True)
    assert active(db, fx) == 2
    assert not fx['calls']


def test_released_bootstrap_without_retirement_proof_refused(db, scenario):
    fx = scenario
    g = db._conn.execute('SELECT * FROM mgboost_device_slot_generations WHERE id=?', (fx['genesis'],)).fetchone()
    # Model historical external local release; the sweep must not infer a
    # successful remote revoke from this bare flag.
    db.device_slots.release(fx['account'], g['slot_id'], g['generation'], reason='fixture historical release', now=3000)
    assert check(db, fx)['reason'] == 'GENERATION_NOT_ACTIVE'
    assert run(db, fx)['completed'] == 0
    assert not fx['calls']


def test_legacy_revoked_is_final_success_proof(db, scenario):
    fx = scenario
    fx['migrate']('real-a')
    # The store's terminal trigger protects this final-success state.
    db._conn.execute("UPDATE mgboost_migration_bindings SET state='LEGACY_REVOKED'")
    db._conn.commit()
    assert check(db, fx)['eligibility'] == 'ELIGIBLE'
    assert check(db, fx)['migration_proof_state'] == 'LEGACY_REVOKED'
    assert run(db, fx)['completed'] == 1


def test_bridge_disabled_is_not_eligible(db, scenario):
    fx = scenario
    fx['migrate']('real-a')
    db._conn.execute('UPDATE mgboost_legacy_bridge_bindings SET enabled=0')
    db._conn.commit()
    assert check(db, fx)['reason'] == 'BRIDGE_NOT_ENABLED'
    assert run(db, fx)['completed'] == 0
    assert not fx['calls']


@pytest.mark.parametrize('state', ['IN_SYNC', 'MANUAL_REVIEW', 'terminal'])
def test_stale_provisioning_ack_cannot_overwrite_revoked(db, scenario, state):
    fx = scenario
    fx['migrate']('real-a')
    op_id = db._conn.execute('SELECT o.operation_id FROM mgboost_outbox o JOIN mgboost_child_user_intents c ON c.id=o.child_intent_id WHERE c.slot_generation_id=?', (fx['genesis'],)).fetchone()[0]
    op = db.child_workflow.get_operation(op_id)
    db.child_workflow.ensure_tracking(op, now=3500)
    db.child_workflow.claim_reconciliation(op, worker_id='stale-observer', now=3500, lease_seconds=30)
    assert run(db, fx)['completed'] == 1
    if state == 'terminal':
        db.child_workflow.terminal_provisioning_error(op, safe_reason='STALE_OR_INACTIVE_GENERATION', now=4001)
    else:
        db.child_workflow.finish_reconciliation(op, worker_id='stale-observer', state=state,
                                                now=4001, next_check_at=4100, safe_reason='STALE_OBSERVATION')
    assert db._conn.execute('SELECT observed_state FROM mgboost_child_user_intents WHERE slot_generation_id=?', (fx['genesis'],)).fetchone()[0] == 'REVOKED'
