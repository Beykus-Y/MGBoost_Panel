"""PH2-05 COMPROMISE crash-boundary fault injection.

Proves, by fault injection plus a real process-restart simulation (the
SQLite connection is closed and a fresh `Database()` is opened against the
same on-disk file, not just retried in-process), that a crash between the
two durable steps of a COMPROMISE rebind can never leave the one forbidden
resting state: a new Telegram owner ACTIVE while the old (compromised)
opaque credential is still ACTIVE.

`src/ownership_rebind.py::process_rebind` deliberately rotates the PH2-01
credential *before* the identity mutation for exactly this reason (see its
own docstring). This file is the durable regression evidence for that
ordering decision -- it fails immediately if a future change reverts the
order or otherwise reopens the gap.
"""

import importlib
import os
import tempfile

import pytest

from src.ownership_rebind import process_rebind
from src.security import AdminSessionStore

from tests.test_child_provisioning import PRIMARY, PRIMARY_LOGIN, _account


@pytest.fixture
def data_dir(monkeypatch):
    tmp = tempfile.mkdtemp()
    monkeypatch.setenv("DATA_DIR", tmp)
    monkeypatch.setenv("PRIMARY_MGBOOST_ADMIN_ACTOR_ID", PRIMARY)
    monkeypatch.setenv("PRIMARY_MGBOOST_ADMIN_LOGIN", PRIMARY_LOGIN)
    import src.config as config
    import src.database as database
    importlib.reload(config)
    importlib.reload(database)
    database.DB_PATH = os.path.join(tmp, "db.sqlite3")
    yield database


def _open(database_module):
    return database_module.Database()


def _capability(db):
    _raw, session = AdminSessionStore().create(PRIMARY_LOGIN, "crash-boundary-test-jwt")
    return db.primary_admin_authority.authorize_session(session)


def _active_owner_telegram_id(db, account_id):
    row = db._conn.execute(
        "SELECT telegram_id FROM mgboost_telegram_identities "
        "WHERE account_id=? AND role='OWNER' AND revoked_at IS NULL", (account_id,),
    ).fetchone()
    return row["telegram_id"] if row else None


def _assert_forbidden_state_absent(db, account_id, new_tg, old_raw_token):
    """The one outcome that must never hold: the new owner is active while
    the old (compromised) opaque credential still resolves."""
    new_owner_active = _active_owner_telegram_id(db, account_id) == new_tg
    old_token_active = db.subscription_credentials.resolve(old_raw_token, now=10**9) is not None
    assert not (new_owner_active and old_token_active), (
        "FORBIDDEN STATE: new Telegram owner is active while the old "
        "compromised opaque token is still valid"
    )


def test_transaction_boundaries_are_separate_commits_not_one_transaction(data_dir):
    """Establishes the fact the rest of this file relies on: identity
    mutation and credential rotation are each their own committed SQLite
    transaction, not one atomic unit -- proven by making the credential
    step raise and confirming the already-committed identity mutation
    survives a fresh connection to the same file."""
    db = _open(data_dir)
    account, _alias_id, _slot = _account(db, mapping="CRASH_BOUNDARY_PROOF", tg=910001)
    account_id = account["account_id"]
    cap = _capability(db)
    prepared_cred = db.subscription_credentials.prepare(
        account_id=account_id, actor_ref="primary-admin", reason="fixture",
        idempotency_key="boundary-proof-credential-prep", now=50,
    )
    db.subscription_credentials.activate(
        credential_id=prepared_cred["id"], account_id=account_id,
        expected_generation=prepared_cred["generation"], actor_ref="primary-admin",
        idempotency_key="boundary-proof-credential-act", now=51,
    )

    # Manually run only the (now credential-first) rotation half of a
    # COMPROMISE operation, then close the connection without ever calling
    # apply_identity_mutation -- if the two steps were one transaction, the
    # credential rotation itself would not have survived a rollback either;
    # here we show it durably survives on its own.
    rebind_prepared = db.ownership_rebind.prepare(
        capability=cap, account_id=account_id, expected_old_telegram_id=910001,
        new_telegram_id=910002, mode="COMPROMISE", reason="boundary proof",
        idempotency_key="boundary-proof-rebind-v1", now=100,
    )
    db.ownership_rebind.claim(rebind_prepared["operation_id"], worker_id="worker-a", now=101)
    new_cred = db.subscription_credentials.prepare(
        account_id=account_id, actor_ref="primary-admin",
        reason="boundary proof rotation", idempotency_key="boundary-proof-rotation-prep", now=102,
    )
    activated = db.subscription_credentials.activate(
        credential_id=new_cred["id"], account_id=account_id,
        expected_generation=new_cred["generation"], actor_ref="primary-admin",
        idempotency_key="boundary-proof-rotation-act", now=103,
    )
    db._conn.close()

    fresh = _open(data_dir)
    # The credential rotation committed and survives; identity was never touched.
    assert _active_owner_telegram_id(fresh, account_id) == 910001  # old owner still active
    active_cred = dict(fresh._conn.execute(
        "SELECT generation FROM mgboost_subscription_credentials WHERE account_id=? AND status='ACTIVE'",
        (account_id,),
    ).fetchone())
    assert active_cred["generation"] == activated["generation"]
    fresh._conn.close()


def test_crash_after_credential_rotation_before_identity_mutation_is_safe_and_recoverable(data_dir):
    db = _open(data_dir)
    account, _alias_id, _slot = _account(db, mapping="CRASH_AFTER_CRED", tg=910010)
    account_id = account["account_id"]
    cap = _capability(db)
    old_prepared = db.subscription_credentials.prepare(
        account_id=account_id, actor_ref="primary-admin", reason="fixture",
        idempotency_key="crash-after-cred-fixture-prep", now=50,
    )
    db.subscription_credentials.activate(
        credential_id=old_prepared["id"], account_id=account_id,
        expected_generation=old_prepared["generation"], actor_ref="primary-admin",
        idempotency_key="crash-after-cred-fixture-act", now=51,
    )
    old_raw_token = old_prepared["raw_token"]

    rebind_prepared = db.ownership_rebind.prepare(
        capability=cap, account_id=account_id, expected_old_telegram_id=910010,
        new_telegram_id=910011, mode="COMPROMISE", reason="crash after credential rotation",
        idempotency_key="crash-after-cred-rebind-v1", now=100,
    )
    operation_id = rebind_prepared["operation_id"]

    # Manually replay process_rebind()'s first half (credential rotation
    # only) exactly as the real function does, then simulate a crash by
    # closing the connection before apply_identity_mutation/finish ever run.
    claimed = db.ownership_rebind.claim(operation_id, worker_id="worker-a", now=101)
    new_prepared = db.subscription_credentials.prepare(
        account_id=account_id, actor_ref=claimed["actor_ref"],
        reason=f"ownership rebind compromise: {operation_id}",
        idempotency_key=f"ownership-rebind-credential-{operation_id}", now=102,
    )
    old_active = db._conn.execute(
        "SELECT id FROM mgboost_subscription_credentials WHERE account_id=? AND status='ACTIVE'",
        (account_id,),
    ).fetchone()
    activated = db.subscription_credentials.activate(
        credential_id=new_prepared["id"], account_id=account_id,
        expected_generation=new_prepared["generation"], actor_ref=claimed["actor_ref"],
        idempotency_key=f"ownership-rebind-activate-{operation_id}", now=103,
    )
    db.ownership_rebind.record_credential_rotation(
        operation_id, worker_id="worker-a",
        old_credential_id=old_active["id"], new_credential_id=activated["id"], now=104,
    )
    # CRASH -- close the connection without ever calling
    # apply_identity_mutation() or finish(). No except/rollback path runs;
    # everything committed so far stays committed, exactly like a real kill.
    db._conn.close()

    # ---- inspect durable state with a brand-new process/connection ----
    fresh = _open(data_dir)
    old_owner_tg = _active_owner_telegram_id(fresh, account_id)
    assert old_owner_tg == 910010  # old owner is STILL active -- identity step never ran
    assert fresh.subscription_credentials.resolve(old_raw_token, now=105) is None  # old token dead
    new_active = dict(fresh._conn.execute(
        "SELECT generation FROM mgboost_subscription_credentials WHERE account_id=? AND status='ACTIVE'",
        (account_id,),
    ).fetchone())
    assert new_active["generation"] == 2
    op_row = dict(fresh._conn.execute(
        "SELECT state FROM mgboost_ownership_rebind_operations WHERE operation_id=?",
        (operation_id,),
    ).fetchone())
    assert op_row["state"] == "IN_FLIGHT"  # non-terminal: reconcile-required, not falsely successful
    _assert_forbidden_state_absent(fresh, account_id, 910011, old_raw_token)

    # ---- retry from the fresh process completes the operation ----
    result = process_rebind(fresh, operation_id, worker_id="worker-b", now=200)
    assert result["state"] == "APPLIED"
    assert _active_owner_telegram_id(fresh, account_id) == 910011
    old_owner_row = dict(fresh._conn.execute(
        "SELECT revoked_at FROM mgboost_telegram_identities WHERE account_id=? AND telegram_id=910010",
        (account_id,),
    ).fetchone())
    assert old_owner_row["revoked_at"] is not None

    # ---- idempotency: no second rotation, no N+2 generation ----
    generations = fresh._conn.execute(
        "SELECT COUNT(*) FROM mgboost_subscription_credentials WHERE account_id=?", (account_id,),
    ).fetchone()[0]
    assert generations == 2
    again = process_rebind(fresh, operation_id, worker_id="worker-c", now=201)
    assert again is None  # terminal operation refuses reclaim
    generations_after_retry = fresh._conn.execute(
        "SELECT COUNT(*) FROM mgboost_subscription_credentials WHERE account_id=?", (account_id,),
    ).fetchone()[0]
    assert generations_after_retry == 2
    fresh._conn.close()


def test_crash_before_any_step_is_trivially_safe_and_recoverable(data_dir):
    """Crash immediately after claim(), before either durable step runs."""
    db = _open(data_dir)
    account, _alias_id, _slot = _account(db, mapping="CRASH_BEFORE_ANY", tg=910020)
    account_id = account["account_id"]
    cap = _capability(db)
    old_prepared = db.subscription_credentials.prepare(
        account_id=account_id, actor_ref="primary-admin", reason="fixture",
        idempotency_key="crash-before-any-fixture-prep", now=50,
    )
    db.subscription_credentials.activate(
        credential_id=old_prepared["id"], account_id=account_id,
        expected_generation=old_prepared["generation"], actor_ref="primary-admin",
        idempotency_key="crash-before-any-fixture-act", now=51,
    )
    old_raw_token = old_prepared["raw_token"]

    rebind_prepared = db.ownership_rebind.prepare(
        capability=cap, account_id=account_id, expected_old_telegram_id=910020,
        new_telegram_id=910021, mode="COMPROMISE", reason="crash before any durable step",
        idempotency_key="crash-before-any-rebind-v1", now=100,
    )
    db.ownership_rebind.claim(rebind_prepared["operation_id"], worker_id="worker-a", now=101)
    db._conn.close()  # crash: neither credential rotation nor identity mutation ran

    fresh = _open(data_dir)
    assert _active_owner_telegram_id(fresh, account_id) == 910020
    assert fresh.subscription_credentials.resolve(old_raw_token, now=102) is not None  # untouched
    _assert_forbidden_state_absent(fresh, account_id, 910021, old_raw_token)

    result = process_rebind(fresh, rebind_prepared["operation_id"], worker_id="worker-b", now=200)
    assert result["state"] == "APPLIED"
    assert _active_owner_telegram_id(fresh, account_id) == 910021
    assert fresh.subscription_credentials.resolve(old_raw_token, now=201) is None
    generations = fresh._conn.execute(
        "SELECT COUNT(*) FROM mgboost_subscription_credentials WHERE account_id=?", (account_id,),
    ).fetchone()[0]
    assert generations == 2
    fresh._conn.close()


def test_crash_after_full_success_before_finish_recorded_is_idempotent_on_retry(data_dir):
    """Both durable steps committed but `finish()` (the terminal marker)
    never ran -- the lease will simply expire and a retry must converge
    without any further mutation."""
    db = _open(data_dir)
    account, _alias_id, _slot = _account(db, mapping="CRASH_AFTER_BOTH", tg=910030)
    account_id = account["account_id"]
    cap = _capability(db)
    old_prepared = db.subscription_credentials.prepare(
        account_id=account_id, actor_ref="primary-admin", reason="fixture",
        idempotency_key="crash-after-both-fixture-prep", now=50,
    )
    db.subscription_credentials.activate(
        credential_id=old_prepared["id"], account_id=account_id,
        expected_generation=old_prepared["generation"], actor_ref="primary-admin",
        idempotency_key="crash-after-both-fixture-act", now=51,
    )
    old_raw_token = old_prepared["raw_token"]

    rebind_prepared = db.ownership_rebind.prepare(
        capability=cap, account_id=account_id, expected_old_telegram_id=910030,
        new_telegram_id=910031, mode="COMPROMISE", reason="crash after both steps",
        idempotency_key="crash-after-both-rebind-v1", now=100,
    )
    operation_id = rebind_prepared["operation_id"]
    claimed = db.ownership_rebind.claim(operation_id, worker_id="worker-a", now=101)
    new_prepared = db.subscription_credentials.prepare(
        account_id=account_id, actor_ref=claimed["actor_ref"],
        reason=f"ownership rebind compromise: {operation_id}",
        idempotency_key=f"ownership-rebind-credential-{operation_id}", now=102,
    )
    old_active = db._conn.execute(
        "SELECT id FROM mgboost_subscription_credentials WHERE account_id=? AND status='ACTIVE'",
        (account_id,),
    ).fetchone()
    activated = db.subscription_credentials.activate(
        credential_id=new_prepared["id"], account_id=account_id,
        expected_generation=new_prepared["generation"], actor_ref=claimed["actor_ref"],
        idempotency_key=f"ownership-rebind-activate-{operation_id}", now=103,
    )
    db.ownership_rebind.record_credential_rotation(
        operation_id, worker_id="worker-a",
        old_credential_id=old_active["id"], new_credential_id=activated["id"], now=104,
    )
    db.ownership_rebind.apply_identity_mutation(operation_id, worker_id="worker-a", now=105)
    db._conn.close()  # crash before finish()

    fresh = _open(data_dir)
    op_row = dict(fresh._conn.execute(
        "SELECT state FROM mgboost_ownership_rebind_operations WHERE operation_id=?",
        (operation_id,),
    ).fetchone())
    assert op_row["state"] == "IN_FLIGHT"
    assert _active_owner_telegram_id(fresh, account_id) == 910031
    _assert_forbidden_state_absent(fresh, account_id, 910031, old_raw_token)

    result = process_rebind(fresh, operation_id, worker_id="worker-b", now=200)
    assert result["state"] == "APPLIED"
    generations = fresh._conn.execute(
        "SELECT COUNT(*) FROM mgboost_subscription_credentials WHERE account_id=?", (account_id,),
    ).fetchone()[0]
    assert generations == 2  # retry did not rotate again
    fresh._conn.close()


def test_fault_injected_inside_real_process_rebind_never_reaches_forbidden_state(data_dir, monkeypatch):
    """Exercises the REAL `process_rebind()` (not a hand-replayed sequence):
    monkeypatch `apply_identity_mutation` to raise the instant it is called,
    simulating a crash exactly at the boundary between the two durable
    steps. This directly proves process_rebind's own ordering -- credential
    rotation before identity mutation -- not just a manually reconstructed
    scenario."""
    from src import ownership_rebind as ownership_rebind_module

    db = _open(data_dir)
    account, _alias_id, _slot = _account(db, mapping="FAULT_INJECT_REAL", tg=910040)
    account_id = account["account_id"]
    cap = _capability(db)
    old_prepared = db.subscription_credentials.prepare(
        account_id=account_id, actor_ref="primary-admin", reason="fixture",
        idempotency_key="fault-inject-real-fixture-prep", now=50,
    )
    db.subscription_credentials.activate(
        credential_id=old_prepared["id"], account_id=account_id,
        expected_generation=old_prepared["generation"], actor_ref="primary-admin",
        idempotency_key="fault-inject-real-fixture-act", now=51,
    )
    old_raw_token = old_prepared["raw_token"]

    rebind_prepared = db.ownership_rebind.prepare(
        capability=cap, account_id=account_id, expected_old_telegram_id=910040,
        new_telegram_id=910041, mode="COMPROMISE", reason="fault injected inside real process_rebind",
        idempotency_key="fault-inject-real-rebind-v1", now=100,
    )
    operation_id = rebind_prepared["operation_id"]

    class InjectedCrash(RuntimeError):
        pass

    original_apply = db.ownership_rebind.apply_identity_mutation

    def exploding_apply_identity_mutation(*args, **kwargs):
        raise InjectedCrash("simulated crash at the identity-mutation boundary")

    monkeypatch.setattr(db.ownership_rebind, "apply_identity_mutation", exploding_apply_identity_mutation)
    with pytest.raises(InjectedCrash):
        process_rebind(db, operation_id, worker_id="worker-a", now=101)

    # The credential rotation committed (it ran first); identity mutation
    # never happened because it raised immediately. record_error() in
    # process_rebind's except block ran too (that part is real, in-process
    # exception handling, not a hard crash) -- so the operation is ERROR,
    # not IN_FLIGHT, but the invariant under test is unaffected either way.
    assert _active_owner_telegram_id(db, account_id) == 910040  # untouched
    assert db.subscription_credentials.resolve(old_raw_token, now=102) is None  # already dead
    _assert_forbidden_state_absent(db, account_id, 910041, old_raw_token)

    op_row = dict(db._conn.execute(
        "SELECT state FROM mgboost_ownership_rebind_operations WHERE operation_id=?",
        (operation_id,),
    ).fetchone())
    assert op_row["state"] == "ERROR"

    # Restore the real method and retry -- claim() must accept an ERROR
    # row? No: ERROR is terminal by this store's own claim() rule, matching
    # every other PH3-0x store's convention (a permanent failure needs an
    # explicit new operation, not an automatic reclaim). Confirm that
    # documented behavior rather than assuming it.
    monkeypatch.setattr(db.ownership_rebind, "apply_identity_mutation", original_apply)
    retry = process_rebind(db, operation_id, worker_id="worker-b", now=200)
    assert retry is None  # ERROR is terminal -- claim() refuses it, matching PH3-0x convention
    assert _active_owner_telegram_id(db, account_id) == 910040  # still safely untouched
    db._conn.close()
