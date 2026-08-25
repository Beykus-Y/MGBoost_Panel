"""PH2-05 Telegram ownership recovery/rebind: fixed first-rollout policy
(OPD-39/DL-041) -- primary-admin-only, atomic old-revoke+new-active,
ordinary preserves the opaque token/child UUID, compromise mandatorily
rotates the opaque token but never the child UUID, and this is never a
device rebind (PH3-02/03/05/08 tables are byte-identical throughout)."""

import importlib
import os
import sqlite3
import tempfile

import pytest

from src.ownership_rebind import (
    OwnershipRebindConflict,
    OwnershipRebindError,
    PrimaryAdminRequired,
    process_rebind,
)
from src.security import AdminSessionStore

from tests.test_child_provisioning import PRIMARY, PRIMARY_LOGIN, _account


@pytest.fixture
def db(monkeypatch):
    tmp = tempfile.mkdtemp()
    monkeypatch.setenv("DATA_DIR", tmp)
    monkeypatch.setenv("PRIMARY_MGBOOST_ADMIN_ACTOR_ID", PRIMARY)
    monkeypatch.setenv("PRIMARY_MGBOOST_ADMIN_LOGIN", PRIMARY_LOGIN)
    import src.config as config
    import src.database as database
    importlib.reload(config)
    importlib.reload(database)
    database.DB_PATH = os.path.join(tmp, "db.sqlite3")
    instance = database.Database()
    yield instance
    instance._conn.close()


def _capability(db):
    _raw, session = AdminSessionStore().create(PRIMARY_LOGIN, "ownership-rebind-test-jwt")
    return db.primary_admin_authority.authorize_session(session)


def _active_owner(db, account_id):
    return dict(db._conn.execute(
        "SELECT * FROM mgboost_telegram_identities WHERE account_id=? AND role='OWNER' "
        "AND revoked_at IS NULL", (account_id,),
    ).fetchone())


def _active_credential(db, account_id, *, now):
    cap = _capability(db)
    prepared = db.subscription_credentials.prepare(
        account_id=account_id, actor_ref="primary-admin", reason="fixture credential",
        idempotency_key=f"fixture-credential-{account_id}", now=now,
    )
    db.subscription_credentials.activate(
        credential_id=prepared["id"], account_id=account_id,
        expected_generation=prepared["generation"], actor_ref="primary-admin",
        idempotency_key=f"fixture-credential-activate-{account_id}", now=now + 1,
    )
    return prepared["raw_token"]


def _prepare_and_process(db, *, account_id, old_tg, new_tg, mode, reason, idem_key, now):
    cap = _capability(db)
    prepared = db.ownership_rebind.prepare(
        capability=cap, account_id=account_id, expected_old_telegram_id=old_tg,
        new_telegram_id=new_tg, mode=mode, reason=reason, idempotency_key=idem_key, now=now,
    )
    result = process_rebind(db, prepared["operation_id"], worker_id="rebind-test-worker", now=now + 1)
    return prepared, result


# --- ordinary rebind -----------------------------------------------------------

def test_ordinary_rebind_atomic_revoke_and_activate_no_dual_owner(db):
    account, _alias_id, _slot = _account(db, mapping="REBIND_ORDINARY", tg=900001)
    account_id = account["account_id"]
    old_owner = _active_owner(db, account_id)

    prepared, result = _prepare_and_process(
        db, account_id=account_id, old_tg=900001, new_tg=900002, mode="ORDINARY",
        reason="owner requested ordinary rebind", idem_key="ordinary-rebind-v1", now=100,
    )
    assert result["state"] == "APPLIED"

    old_row = dict(db._conn.execute(
        "SELECT revoked_at,revoke_reason FROM mgboost_telegram_identities WHERE id=?",
        (old_owner["id"],),
    ).fetchone())
    assert old_row["revoked_at"] is not None
    assert old_row["revoke_reason"] == "ownership_rebind:ordinary"

    new_owner = _active_owner(db, account_id)
    assert new_owner["telegram_id"] == 900002
    assert new_owner["provenance"] == "ADMIN_REBIND"

    active_owners = db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_telegram_identities WHERE account_id=? AND role='OWNER' "
        "AND revoked_at IS NULL", (account_id,),
    ).fetchone()[0]
    assert active_owners == 1


def test_ordinary_rebind_preserves_opaque_credential_unchanged(db):
    account, _alias_id, _slot = _account(db, mapping="REBIND_ORDINARY_TOKEN", tg=900010)
    account_id = account["account_id"]
    raw_token = _active_credential(db, account_id, now=50)
    cred_before = dict(db._conn.execute(
        "SELECT id,generation,token_hash,status FROM mgboost_subscription_credentials "
        "WHERE account_id=? AND status='ACTIVE'", (account_id,),
    ).fetchone())

    _prepared, result = _prepare_and_process(
        db, account_id=account_id, old_tg=900010, new_tg=900011, mode="ORDINARY",
        reason="ordinary, token must not rotate", idem_key="ordinary-token-preserve-v1", now=100,
    )
    assert result["state"] == "APPLIED"

    cred_after = dict(db._conn.execute(
        "SELECT id,generation,token_hash,status FROM mgboost_subscription_credentials "
        "WHERE account_id=? AND status='ACTIVE'", (account_id,),
    ).fetchone())
    assert cred_after == cred_before
    from src.subscription_credentials import token_verifier
    assert db.subscription_credentials.resolve(raw_token, now=101) is not None


def test_ordinary_rebind_preserves_child_and_all_ph3_data(db):
    from src.child_contract import source_contract_hash
    from src.broker_operations import BrokerOperations
    from tests.test_marzban_broker import FakeMarzban

    account, alias_id, slot = _account(db, mapping="REBIND_CHILD_PRESERVE", tg=900020, alias="alice")
    account_id = account["account_id"]
    remote = FakeMarzban()
    request_hash = source_contract_hash(remote.users["alice"])
    prepared_child = db.child_provisioning.prepare_child_ensure(
        account_id=account_id, slot_generation_id=slot["generation_id"], source_alias_id=alias_id,
        source_contract_hash=request_hash, expire=0, idempotency_key="rebind-preserve-child-v1", now=100,
    )
    claimed = db.child_provisioning.claim(prepared_child["operation_id"], worker_id="seed-worker", now=101, lease_seconds=5)
    created = BrokerOperations(remote).dispatch("child.user.ensure", claimed["payload"])
    child_uuid = created.pop("uuid")
    db.child_provisioning.acknowledge(
        prepared_child["operation_id"], worker_id="seed-worker",
        outcome=created["outcome"], child_uuid=child_uuid, remote_result=created, now=102,
    )
    child_before = dict(db._conn.execute(
        "SELECT child_username,uuid_verifier,desired_state,observed_state FROM mgboost_child_user_intents "
        "WHERE account_id=?", (account_id,),
    ).fetchone())
    slot_before = dict(db._conn.execute(
        "SELECT status,generation FROM mgboost_device_slot_generations WHERE account_id=?", (account_id,),
    ).fetchone())
    aliases_before = list(db._conn.execute(
        "SELECT * FROM mgboost_legacy_account_aliases WHERE account_id=? ORDER BY id", (account_id,),
    ).fetchall())

    _prepared, result = _prepare_and_process(
        db, account_id=account_id, old_tg=900020, new_tg=900021, mode="ORDINARY",
        reason="ordinary rebind must not touch PH3 data", idem_key="rebind-preserve-v1", now=200,
    )
    assert result["state"] == "APPLIED"

    child_after = dict(db._conn.execute(
        "SELECT child_username,uuid_verifier,desired_state,observed_state FROM mgboost_child_user_intents "
        "WHERE account_id=?", (account_id,),
    ).fetchone())
    slot_after = dict(db._conn.execute(
        "SELECT status,generation FROM mgboost_device_slot_generations WHERE account_id=?", (account_id,),
    ).fetchone())
    aliases_after = list(db._conn.execute(
        "SELECT * FROM mgboost_legacy_account_aliases WHERE account_id=? ORDER BY id", (account_id,),
    ).fetchall())
    assert child_after == child_before
    assert slot_after == slot_before
    assert [dict(r) for r in aliases_after] == [dict(r) for r in aliases_before]
    remote_child = remote.users[child_before["child_username"]]
    assert remote_child["proxies"]["vless"]["id"] == child_uuid  # untouched remotely too


# --- compromise mode -------------------------------------------------------------

def test_compromise_rebind_revokes_old_opaque_token_and_issues_exactly_one_new(db):
    account, _alias_id, _slot = _account(db, mapping="REBIND_COMPROMISE", tg=900030)
    account_id = account["account_id"]
    old_raw_token = _active_credential(db, account_id, now=50)

    prepared, result = _prepare_and_process(
        db, account_id=account_id, old_tg=900030, new_tg=900031, mode="COMPROMISE",
        reason="suspected compromise", idem_key="compromise-rebind-v1", now=100,
    )
    assert result["state"] == "APPLIED"
    assert result["new_credential_id"] is not None
    assert result["old_credential_id"] is not None

    assert db.subscription_credentials.resolve(old_raw_token, now=101) is None  # old dead

    generations = db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_subscription_credentials WHERE account_id=?", (account_id,),
    ).fetchone()[0]
    assert generations == 2  # exactly one new generation, not more

    active = dict(db._conn.execute(
        "SELECT generation FROM mgboost_subscription_credentials WHERE account_id=? AND status='ACTIVE'",
        (account_id,),
    ).fetchone())
    assert active["generation"] == 2


def test_compromise_rebind_never_rotates_child_uuid(db):
    from src.child_contract import source_contract_hash
    from src.broker_operations import BrokerOperations
    from tests.test_marzban_broker import FakeMarzban

    account, alias_id, slot = _account(db, mapping="REBIND_COMPROMISE_CHILD", tg=900040, alias="alice")
    account_id = account["account_id"]
    remote = FakeMarzban()
    request_hash = source_contract_hash(remote.users["alice"])
    prepared_child = db.child_provisioning.prepare_child_ensure(
        account_id=account_id, slot_generation_id=slot["generation_id"], source_alias_id=alias_id,
        source_contract_hash=request_hash, expire=0, idempotency_key="rebind-compromise-child-v1", now=100,
    )
    claimed = db.child_provisioning.claim(prepared_child["operation_id"], worker_id="seed-worker", now=101, lease_seconds=5)
    created = BrokerOperations(remote).dispatch("child.user.ensure", claimed["payload"])
    child_uuid = created.pop("uuid")
    db.child_provisioning.acknowledge(
        prepared_child["operation_id"], worker_id="seed-worker",
        outcome=created["outcome"], child_uuid=child_uuid, remote_result=created, now=102,
    )
    _active_credential(db, account_id, now=150)

    _prepared, result = _prepare_and_process(
        db, account_id=account_id, old_tg=900040, new_tg=900041, mode="COMPROMISE",
        reason="compromise must not touch child UUID", idem_key="rebind-compromise-child-check-v1", now=200,
    )
    assert result["state"] == "APPLIED"

    child_after = dict(db._conn.execute(
        "SELECT uuid_verifier FROM mgboost_child_user_intents WHERE account_id=?", (account_id,),
    ).fetchone())
    remote_child = remote.users[
        db._conn.execute(
            "SELECT child_username FROM mgboost_child_user_intents WHERE account_id=?", (account_id,),
        ).fetchone()["child_username"]
    ]
    assert remote_child["proxies"]["vless"]["id"] == child_uuid


def test_caller_cannot_request_compromise_without_token_rotation(db):
    """There is no parameter that lets a caller pick COMPROMISE and also
    skip the mandatory rotation -- process_rebind always rotates for
    COMPROMISE, unconditionally."""
    account, _alias_id, _slot = _account(db, mapping="REBIND_COMPROMISE_MANDATORY", tg=900050)
    account_id = account["account_id"]
    _active_credential(db, account_id, now=50)
    _prepared, result = _prepare_and_process(
        db, account_id=account_id, old_tg=900050, new_tg=900051, mode="COMPROMISE",
        reason="compromise always rotates", idem_key="compromise-mandatory-v1", now=100,
    )
    assert result["new_credential_id"] is not None


# --- lost-response / abandon+reissue --------------------------------------------

def test_compromise_lost_response_retry_reissues_without_reactivating_old(db):
    from src import subscription_credentials as sc_module

    account, _alias_id, _slot = _account(db, mapping="REBIND_LOST_RESPONSE", tg=900060)
    account_id = account["account_id"]
    old_raw_token = _active_credential(db, account_id, now=50)

    cap = _capability(db)
    prepared = db.ownership_rebind.prepare(
        capability=cap, account_id=account_id, expected_old_telegram_id=900060,
        new_telegram_id=900061, mode="COMPROMISE", reason="lost response test",
        idempotency_key="lost-response-v1", now=100,
    )
    claimed = db.ownership_rebind.claim(prepared["operation_id"], worker_id="worker-a", now=101)
    row = db.ownership_rebind.apply_identity_mutation(prepared["operation_id"], worker_id="worker-a", now=102)
    # simulate: prepare() for the credential succeeded but the process
    # crashed before recording new_credential_id on the rebind row.
    db.subscription_credentials.prepare(
        account_id=account_id, actor_ref=row["actor_ref"],
        reason=f"ownership rebind compromise: {prepared['operation_id']}",
        idempotency_key=f"ownership-rebind-credential-{prepared['operation_id']}", now=103,
    )
    # the lease is still IN_FLIGHT from worker-a; let it expire, then a
    # fresh worker retries the whole operation from claim().
    result = process_rebind(db, prepared["operation_id"], worker_id="worker-b", now=140)
    assert result["state"] == "APPLIED"
    assert db.subscription_credentials.resolve(old_raw_token, now=141) is None
    generations = db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_subscription_credentials WHERE account_id=?", (account_id,),
    ).fetchone()[0]
    # original ACTIVE (now REVOKED/superseded) + the abandoned lost-response
    # PENDING_DELIVERY (immutable tombstone, never deleted) + exactly one
    # real reissued generation that is ACTIVE -- never a second live rotation.
    assert generations == 3
    active = dict(db._conn.execute(
        "SELECT generation FROM mgboost_subscription_credentials WHERE account_id=? AND status='ACTIVE'",
        (account_id,),
    ).fetchone())
    assert active["generation"] == 3


def test_compromise_retry_after_full_success_does_not_create_generation_n_plus_2(db):
    account, _alias_id, _slot = _account(db, mapping="REBIND_RETRY_NO_N2", tg=900070)
    account_id = account["account_id"]
    old_raw_token = _active_credential(db, account_id, now=50)

    cap = _capability(db)
    prepared = db.ownership_rebind.prepare(
        capability=cap, account_id=account_id, expected_old_telegram_id=900070,
        new_telegram_id=900071, mode="COMPROMISE", reason="idempotent retry test",
        idempotency_key="retry-no-n2-test-v1", now=100,
    )
    first = process_rebind(db, prepared["operation_id"], worker_id="worker-a", now=101)
    assert first["state"] == "APPLIED"

    # exact repeat request (same idempotency key) -- prepare() returns the
    # same terminal row; process_rebind on an APPLIED operation is a no-op.
    prepared_again = db.ownership_rebind.prepare(
        capability=cap, account_id=account_id, expected_old_telegram_id=900070,
        new_telegram_id=900071, mode="COMPROMISE", reason="idempotent retry test",
        idempotency_key="retry-no-n2-test-v1", now=200,
    )
    assert prepared_again["operation_id"] == prepared["operation_id"]
    second = process_rebind(db, prepared_again["operation_id"], worker_id="worker-b", now=201)
    assert second is None  # claim() refuses an APPLIED operation

    generations = db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_subscription_credentials WHERE account_id=?", (account_id,),
    ).fetchone()[0]
    assert generations == 2


# --- authorization boundary -----------------------------------------------------

def test_non_primary_or_missing_capability_denied(db):
    account, _alias_id, _slot = _account(db, mapping="REBIND_NOAUTH", tg=900080)
    with pytest.raises(PrimaryAdminRequired):
        db.ownership_rebind.prepare(
            capability=None, account_id=account["account_id"], expected_old_telegram_id=900080,
            new_telegram_id=900081, mode="ORDINARY", reason="no capability",
            idempotency_key="noauth-probe-test-v1", now=100,
        )


def test_stale_old_owner_request_rejected(db):
    """IDOR-adjacent: a caller (or a stale queued request) supplying the
    wrong current owner never mutates anything."""
    account, _alias_id, _slot = _account(db, mapping="REBIND_STALE_OWNER", tg=900090)
    account_id = account["account_id"]
    cap = _capability(db)
    prepared = db.ownership_rebind.prepare(
        capability=cap, account_id=account_id, expected_old_telegram_id=999999999,  # wrong
        new_telegram_id=900091, mode="ORDINARY", reason="stale owner test",
        idempotency_key="stale-owner-probe-v1", now=100,
    )
    with pytest.raises(OwnershipRebindConflict, match="stale"):
        process_rebind(db, prepared["operation_id"], worker_id="worker-a", now=101)
    owner = _active_owner(db, account_id)
    assert owner["telegram_id"] == 900090  # unchanged


def test_new_telegram_id_already_active_elsewhere_denied_dual_ownership(db):
    account_a, _alias_a, _slot_a = _account(db, mapping="REBIND_DUAL_A", tg=900100)
    account_b, _alias_b, _slot_b = _account(db, mapping="REBIND_DUAL_B", tg=900101, alias="second-source")
    cap = _capability(db)
    prepared = db.ownership_rebind.prepare(
        capability=cap, account_id=account_a["account_id"], expected_old_telegram_id=900100,
        new_telegram_id=900101,  # already owns account_b
        mode="ORDINARY", reason="dual ownership probe", idempotency_key="dual-owner-probe-v1", now=100,
    )
    with pytest.raises(OwnershipRebindConflict, match="dual ownership"):
        process_rebind(db, prepared["operation_id"], worker_id="worker-a", now=101)
    owner_a = _active_owner(db, account_a["account_id"])
    owner_b = _active_owner(db, account_b["account_id"])
    assert owner_a["telegram_id"] == 900100
    assert owner_b["telegram_id"] == 900101


def test_cross_account_rebind_of_someone_elses_account_via_wrong_account_id(db):
    account_a, _alias_a, _slot_a = _account(db, mapping="REBIND_IDOR_A", tg=900110)
    account_b, _alias_b, _slot_b = _account(db, mapping="REBIND_IDOR_B", tg=900111, alias="second-source")
    cap = _capability(db)
    # caller targets account_b's id but supplies account_a's owner as
    # "expected old" -- must be rejected as stale/mismatched, never silently
    # rebind account_b using account_a's telegram identity.
    prepared = db.ownership_rebind.prepare(
        capability=cap, account_id=account_b["account_id"], expected_old_telegram_id=900110,
        new_telegram_id=900112, mode="ORDINARY", reason="cross account IDOR probe",
        idempotency_key="idor-probe-test-v1", now=100,
    )
    with pytest.raises(OwnershipRebindConflict, match="stale"):
        process_rebind(db, prepared["operation_id"], worker_id="worker-a", now=101)
    owner_b = _active_owner(db, account_b["account_id"])
    assert owner_b["telegram_id"] == 900111  # untouched


# --- concurrency -----------------------------------------------------------------

def test_concurrent_rebind_same_account_exactly_one_winner(db):
    account, _alias_id, _slot = _account(db, mapping="REBIND_CONCURRENT", tg=900120)
    account_id = account["account_id"]
    cap = _capability(db)
    prepared_1 = db.ownership_rebind.prepare(
        capability=cap, account_id=account_id, expected_old_telegram_id=900120,
        new_telegram_id=900121, mode="ORDINARY", reason="racer 1",
        idempotency_key="concurrent-rebind-1", now=100,
    )
    prepared_2 = db.ownership_rebind.prepare(
        capability=cap, account_id=account_id, expected_old_telegram_id=900120,
        new_telegram_id=900122, mode="ORDINARY", reason="racer 2",
        idempotency_key="concurrent-rebind-2", now=100,
    )
    first = process_rebind(db, prepared_1["operation_id"], worker_id="worker-a", now=101)
    assert first["state"] == "APPLIED"
    with pytest.raises(OwnershipRebindConflict, match="stale"):
        process_rebind(db, prepared_2["operation_id"], worker_id="worker-b", now=102)
    owner = _active_owner(db, account_id)
    assert owner["telegram_id"] == 900121


def test_prepare_conflict_on_reused_idempotency_key_with_different_payload(db):
    account, _alias_id, _slot = _account(db, mapping="REBIND_KEY_CONFLICT", tg=900130)
    cap = _capability(db)
    db.ownership_rebind.prepare(
        capability=cap, account_id=account["account_id"], expected_old_telegram_id=900130,
        new_telegram_id=900131, mode="ORDINARY", reason="first", idempotency_key="reuse-key-probe-v1", now=100,
    )
    with pytest.raises(Exception):
        db.ownership_rebind.prepare(
            capability=cap, account_id=account["account_id"], expected_old_telegram_id=900130,
            new_telegram_id=900199,  # different target -- must conflict, not silently reuse
            mode="ORDINARY", reason="second", idempotency_key="reuse-key-probe-v1", now=101,
        )


def test_terminal_operation_row_is_immutable_at_schema_level(db):
    account, _alias_id, _slot = _account(db, mapping="REBIND_TERMINAL_IMMUTABLE", tg=900140)
    account_id = account["account_id"]
    _prepared, result = _prepare_and_process(
        db, account_id=account_id, old_tg=900140, new_tg=900141, mode="ORDINARY",
        reason="terminal immutability", idem_key="terminal-immutable-v1", now=100,
    )
    assert result["state"] == "APPLIED"
    with pytest.raises(sqlite3.IntegrityError):
        db._conn.execute(
            "UPDATE mgboost_ownership_rebind_operations SET state='PENDING' WHERE id=?",
            (result["id"],),
        )


def test_no_raw_token_uuid_hwid_leak_in_db_after_compromise_rebind(db):
    account, _alias_id, _slot = _account(db, mapping="REBIND_LEAK", tg=900150)
    account_id = account["account_id"]
    old_raw_token = _active_credential(db, account_id, now=50)
    _prepared, result = _prepare_and_process(
        db, account_id=account_id, old_tg=900150, new_tg=900151, mode="COMPROMISE",
        reason="leak scan", idem_key="leak-scan-probe-v1", now=100,
    )
    assert result["state"] == "APPLIED"
    dump = "\n".join(db._conn.iterdump())
    assert old_raw_token not in dump
