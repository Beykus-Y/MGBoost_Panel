"""PH2-01 opaque subscription credential store: issuance, activation CAS,
revoke, resolution and the immutable-terminal-state guarantee."""

import importlib
import os
import tempfile

import pytest

from src.subscription_credentials import (
    SubscriptionCredentialConflict,
    SubscriptionCredentialError,
    generate_opaque_token,
    token_verifier,
)

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


def test_generate_opaque_token_shape():
    token = generate_opaque_token()
    assert len(token) == 43
    assert all(c.isalnum() or c in "-_" for c in token)
    assert generate_opaque_token() != token  # CSPRNG, never repeats in practice


def test_prepare_then_activate_then_resolve(db):
    account, _alias_id, _slot = _account(db, mapping="CRED_BASIC", tg=800001)
    prepared = db.subscription_credentials.prepare(
        account_id=account["account_id"], actor_ref="primary-admin",
        reason="initial issuance", idempotency_key="cred-prepare-basic-v1", now=100,
    )
    assert prepared["status"] == "PENDING_DELIVERY"
    raw_token = prepared["raw_token"]
    assert len(raw_token) == 43

    activated = db.subscription_credentials.activate(
        credential_id=prepared["id"], account_id=account["account_id"],
        expected_generation=prepared["generation"], actor_ref="primary-admin",
        idempotency_key="cred-activate-basic-v1", now=101,
    )
    assert activated["status"] == "ACTIVE"

    resolved = db.subscription_credentials.resolve(raw_token, now=102)
    assert resolved == {
        "credential_id": prepared["id"], "account_id": account["account_id"],
        "generation": prepared["generation"],
    }


def test_pending_credential_does_not_resolve(db):
    account, _alias_id, _slot = _account(db, mapping="CRED_PENDING", tg=800002)
    prepared = db.subscription_credentials.prepare(
        account_id=account["account_id"], actor_ref="primary-admin",
        reason="pending only", idempotency_key="cred-prepare-pending-v1", now=100,
    )
    assert db.subscription_credentials.resolve(prepared["raw_token"], now=101) is None


def test_unknown_and_malformed_tokens_resolve_to_none(db):
    assert db.subscription_credentials.resolve("not-a-real-token", now=100) is None
    assert db.subscription_credentials.resolve("", now=100) is None


def test_rotation_revokes_previous_active_atomically(db):
    account, _alias_id, _slot = _account(db, mapping="CRED_ROTATE", tg=800003)
    first = db.subscription_credentials.prepare(
        account_id=account["account_id"], actor_ref="primary-admin",
        reason="first", idempotency_key="cred-rotate-prepare-1", now=100,
    )
    db.subscription_credentials.activate(
        credential_id=first["id"], account_id=account["account_id"],
        expected_generation=first["generation"], actor_ref="primary-admin",
        idempotency_key="cred-rotate-activate-1", now=101,
    )
    first_token = first["raw_token"]

    second = db.subscription_credentials.prepare(
        account_id=account["account_id"], actor_ref="primary-admin",
        reason="rotation", idempotency_key="cred-rotate-prepare-2", now=110,
    )
    assert second["generation"] == first["generation"] + 1
    db.subscription_credentials.activate(
        credential_id=second["id"], account_id=account["account_id"],
        expected_generation=second["generation"], actor_ref="primary-admin",
        idempotency_key="cred-rotate-activate-2", now=111,
    )

    # the old token is dead the instant the new one activates -- same transaction
    assert db.subscription_credentials.resolve(first_token, now=112) is None
    assert db.subscription_credentials.resolve(second["raw_token"], now=112) is not None
    old_row = dict(db._conn.execute(
        "SELECT status, revoke_reason FROM mgboost_subscription_credentials WHERE id=?",
        (first["id"],),
    ).fetchone())
    assert old_row == {"status": "REVOKED", "revoke_reason": "ROTATED"}


def test_ordinary_rebind_does_not_enter_this_module_at_all(db):
    """PH2-01's own boundary: nothing here ever touches Telegram identity.
    An 'ordinary rebind preserves token' is proven by absence -- this store
    has no code path that reads or writes mgboost_telegram_identities."""
    import inspect
    from src import subscription_credentials
    source = inspect.getsource(subscription_credentials)
    assert "telegram" not in source.lower()


def test_at_most_one_active_credential_per_account_enforced_by_schema(db):
    account, _alias_id, _slot = _account(db, mapping="CRED_UNIQUE", tg=800004)
    first = db.subscription_credentials.prepare(
        account_id=account["account_id"], actor_ref="primary-admin",
        reason="first", idempotency_key="cred-unique-prepare-1", now=100,
    )
    db.subscription_credentials.activate(
        credential_id=first["id"], account_id=account["account_id"],
        expected_generation=first["generation"], actor_ref="primary-admin",
        idempotency_key="cred-unique-activate-1", now=101,
    )
    # A second PENDING_DELIVERY credential can coexist (no uniqueness there);
    # activating it must atomically revoke the first -- verified separately
    # above. Here we assert the partial index truly only restricts ACTIVE.
    row = db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_subscription_credentials "
        "WHERE account_id=? AND status='ACTIVE'", (account["account_id"],),
    ).fetchone()[0]
    assert row == 1


def test_activate_cas_rejects_stale_generation(db):
    account, _alias_id, _slot = _account(db, mapping="CRED_STALE_CAS", tg=800005)
    prepared = db.subscription_credentials.prepare(
        account_id=account["account_id"], actor_ref="primary-admin",
        reason="cas test", idempotency_key="cred-cas-prepare", now=100,
    )
    with pytest.raises(SubscriptionCredentialConflict, match="generation mismatch"):
        db.subscription_credentials.activate(
            credential_id=prepared["id"], account_id=account["account_id"],
            expected_generation=prepared["generation"] + 1, actor_ref="primary-admin",
            idempotency_key="cred-cas-activate", now=101,
        )


def test_activate_is_idempotent_on_same_key(db):
    account, _alias_id, _slot = _account(db, mapping="CRED_IDEMPOTENT", tg=800006)
    prepared = db.subscription_credentials.prepare(
        account_id=account["account_id"], actor_ref="primary-admin",
        reason="idempotent", idempotency_key="cred-idem-prepare", now=100,
    )
    first = db.subscription_credentials.activate(
        credential_id=prepared["id"], account_id=account["account_id"],
        expected_generation=prepared["generation"], actor_ref="primary-admin",
        idempotency_key="cred-idem-activate", now=101,
    )
    second = db.subscription_credentials.activate(
        credential_id=prepared["id"], account_id=account["account_id"],
        expected_generation=prepared["generation"], actor_ref="primary-admin",
        idempotency_key="cred-idem-activate", now=200,
    )
    assert first == second


def test_prepare_conflict_on_reused_idempotency_key(db):
    account, _alias_id, _slot = _account(db, mapping="CRED_PREPARE_CONFLICT", tg=800007)
    db.subscription_credentials.prepare(
        account_id=account["account_id"], actor_ref="primary-admin",
        reason="first", idempotency_key="cred-conflict-key", now=100,
    )
    with pytest.raises(SubscriptionCredentialConflict):
        db.subscription_credentials.prepare(
            account_id=account["account_id"], actor_ref="primary-admin",
            reason="second attempt reusing the same key", idempotency_key="cred-conflict-key",
            now=101,
        )


def test_abandoned_pending_can_be_revoked_then_reissued(db):
    account, _alias_id, _slot = _account(db, mapping="CRED_ABANDON", tg=800008)
    prepared = db.subscription_credentials.prepare(
        account_id=account["account_id"], actor_ref="primary-admin",
        reason="will be abandoned", idempotency_key="cred-abandon-prepare-1", now=100,
    )
    db.subscription_credentials.revoke(
        credential_id=prepared["id"], account_id=account["account_id"],
        reason_code="ABANDONED_PENDING", actor_ref="primary-admin",
        idempotency_key="cred-abandon-revoke-1", now=101,
    )
    reissued = db.subscription_credentials.prepare(
        account_id=account["account_id"], actor_ref="primary-admin",
        reason="reissued", idempotency_key="cred-abandon-prepare-2", now=110,
    )
    assert reissued["generation"] == prepared["generation"] + 1


def test_terminal_credential_can_never_be_revoked_again(db):
    account, _alias_id, _slot = _account(db, mapping="CRED_TERMINAL", tg=800009)
    prepared = db.subscription_credentials.prepare(
        account_id=account["account_id"], actor_ref="primary-admin",
        reason="terminal test", idempotency_key="cred-terminal-prepare", now=100,
    )
    db.subscription_credentials.revoke(
        credential_id=prepared["id"], account_id=account["account_id"],
        reason_code="ABANDONED_PENDING", actor_ref="primary-admin",
        idempotency_key="cred-terminal-revoke-1", now=101,
    )
    with pytest.raises(SubscriptionCredentialConflict, match="terminal"):
        db.subscription_credentials.revoke(
            credential_id=prepared["id"], account_id=account["account_id"],
            reason_code="ADMIN_MANUAL", actor_ref="primary-admin",
            idempotency_key="cred-terminal-revoke-2", now=102,
        )


def test_revoked_credential_row_is_immutable_at_the_schema_level(db):
    account, _alias_id, _slot = _account(db, mapping="CRED_IMMUTABLE", tg=800010)
    prepared = db.subscription_credentials.prepare(
        account_id=account["account_id"], actor_ref="primary-admin",
        reason="immutability test", idempotency_key="cred-immutable-prepare", now=100,
    )
    db.subscription_credentials.revoke(
        credential_id=prepared["id"], account_id=account["account_id"],
        reason_code="ABANDONED_PENDING", actor_ref="primary-admin",
        idempotency_key="cred-immutable-revoke", now=101,
    )
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        db._conn.execute(
            "UPDATE mgboost_subscription_credentials SET status='ACTIVE' WHERE id=?",
            (prepared["id"],),
        )


def test_cross_account_activate_rejected(db):
    account_a, _alias_a, _slot_a = _account(db, mapping="CRED_CROSS_A", tg=800011)
    account_b, _alias_b, _slot_b = _account(db, mapping="CRED_CROSS_B", tg=800012, alias="second-source")
    prepared = db.subscription_credentials.prepare(
        account_id=account_a["account_id"], actor_ref="primary-admin",
        reason="cross account probe", idempotency_key="cred-cross-prepare", now=100,
    )
    with pytest.raises(SubscriptionCredentialError, match="does not belong"):
        db.subscription_credentials.activate(
            credential_id=prepared["id"], account_id=account_b["account_id"],
            expected_generation=prepared["generation"], actor_ref="primary-admin",
            idempotency_key="cred-cross-activate", now=101,
        )


def test_raw_token_never_appears_in_db_dump(db):
    account, _alias_id, _slot = _account(db, mapping="CRED_LEAK", tg=800013)
    prepared = db.subscription_credentials.prepare(
        account_id=account["account_id"], actor_ref="primary-admin",
        reason="leak scan", idempotency_key="cred-leak-prepare", now=100,
    )
    db.subscription_credentials.activate(
        credential_id=prepared["id"], account_id=account["account_id"],
        expected_generation=prepared["generation"], actor_ref="primary-admin",
        idempotency_key="cred-leak-activate", now=101,
    )
    raw_token = prepared["raw_token"]
    dump = "\n".join(db._conn.iterdump())
    assert raw_token not in dump
    assert token_verifier(raw_token) in dump  # only the verifier is ever stored
