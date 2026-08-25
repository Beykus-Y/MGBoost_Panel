"""PH2-01 opaque resolver: typed subscription-fetch broker operation and the
full engine (token -> credential -> parent state -> HWID/slot -> lazy child
-> subscription body), reusing PH3-02/03/04/08 primitives end to end."""

import importlib
import os
import tempfile

import pytest

from src.broker_operations import BrokerOperations
from src.child_contract import derive_operation_id, source_contract_hash
from src.opaque_resolver import (
    OUTCOME_DENY_CROSS_ACCOUNT_HWID,
    OUTCOME_DENY_MALFORMED_HWID,
    OUTCOME_DENY_MISSING_HWID,
    OUTCOME_DENY_SLOT_LIMIT,
    OUTCOME_DENY_UNSUPPORTED_CLIENT,
    OUTCOME_INVALID_TOKEN,
    OUTCOME_OK,
    OUTCOME_PARENT_UNAVAILABLE,
    OUTCOME_PROVISIONING_UNAVAILABLE,
    resolve_opaque_subscription,
)

from tests.test_child_provisioning import HWID_KEY, PRIMARY, PRIMARY_LOGIN, _account
from tests.test_marzban_broker import FakeMarzban


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


SUPPORTED_METADATA = {
    "client_name": "Happ", "client_version": "2.7.0", "platform": "windows",
}


def _known_hwid_meta(hwid):
    return {
        **SUPPORTED_METADATA, "hwid_candidate_present": True,
        "hwid_candidate_supported": True, "device_id": hwid,
    }


def _get_sub(self, token, extra_headers=None):
    self.calls.append(("get_sub", token))
    return b"child-config-body", {"profile-title": "child"}


def _remote_and_ensure_fn():
    remote = FakeMarzban()
    remote.get_sub = _get_sub.__get__(remote, FakeMarzban)
    remote.users["alice"]["subscription_url"] = "/sub/opaque-resolver-test-source-token"
    original_create_user = remote.create_user

    def create_user_with_sub_url(payload, token):
        created = original_create_user(payload, token)
        remote.users[created["username"]]["subscription_url"] = f"/sub/{created['username']}-token"
        created["subscription_url"] = remote.users[created["username"]]["subscription_url"]
        return created

    remote.create_user = create_user_with_sub_url

    def ensure_fn(payload):
        return BrokerOperations(remote).dispatch("child.user.ensure", payload)

    def subscription_fn(payload):
        return BrokerOperations(remote).dispatch("child.user.subscription.get", payload)

    return remote, ensure_fn, subscription_fn


def _seed_account_with_first_child(db, *, mapping, tg, alias="alice"):
    """The engine deliberately refuses to discover a brand-new source
    template on its own (see opaque_resolver.py's docstring) -- seed the
    account's first child the same way every prior PH3-0x gate does."""
    account, alias_id, slot = _account(db, mapping=mapping, tg=tg, alias=alias)
    remote, ensure_fn, subscription_fn = _remote_and_ensure_fn()
    if alias != "alice":
        remote.users[alias] = remote.users.pop("alice")
        remote.users[alias]["username"] = alias
    request_hash = source_contract_hash(remote.users[alias])
    prepared = db.child_provisioning.prepare_child_ensure(
        account_id=account["account_id"], slot_generation_id=slot["generation_id"],
        source_alias_id=alias_id, source_contract_hash=request_hash, expire=0,
        idempotency_key=f"opaque-resolver-seed-{mapping}", now=100,
    )
    claimed = db.child_provisioning.claim(
        prepared["operation_id"], worker_id="seed-worker", now=101, lease_seconds=5,
    )
    created = ensure_fn(claimed["payload"])
    child_uuid = created.pop("uuid")
    db.child_provisioning.acknowledge(
        prepared["operation_id"], worker_id="seed-worker",
        outcome=created["outcome"], child_uuid=child_uuid, remote_result=created, now=102,
    )
    return account, alias_id, slot, remote, ensure_fn, subscription_fn


def _issue_active_credential(db, account_id, *, idem_prefix, now=200):
    prepared = db.subscription_credentials.prepare(
        account_id=account_id, actor_ref="primary-admin", reason="opaque resolver test",
        idempotency_key=f"{idem_prefix}-prepare", now=now,
    )
    db.subscription_credentials.activate(
        credential_id=prepared["id"], account_id=account_id,
        expected_generation=prepared["generation"], actor_ref="primary-admin",
        idempotency_key=f"{idem_prefix}-activate", now=now + 1,
    )
    return prepared["raw_token"]


# --- broker-level typed subscription fetch ------------------------------------

def test_broker_subscription_get_returns_body_and_headers():
    remote = FakeMarzban()
    remote.get_sub = _get_sub.__get__(remote, FakeMarzban)
    source = remote.users["alice"]
    from src.child_contract import build_child_payload, credential_verifier, validate_child_ensure_request
    username = "mgc_" + "s" * 26
    request = {
        "operation_id": derive_operation_id(username), "child_username": username,
        "source_username": "alice", "source_contract_hash": source_contract_hash(source),
        "expire": 0,
    }
    created = BrokerOperations(remote).dispatch("child.user.ensure", request)
    remote.users[username]["subscription_url"] = "/sub/opaque-child-token"

    sub_request = {
        "operation_id": derive_operation_id(username), "child_username": username,
        "source_contract_hash": source_contract_hash(source), "expire": 0,
        "uuid_verifier": credential_verifier(created["uuid"]),
    }
    result = BrokerOperations(remote).dispatch("child.user.subscription.get", sub_request)
    assert result["headers"] == {"profile-title": "child"}
    import base64
    assert base64.b64decode(result["body_b64"]) == b"child-config-body"
    assert result["body_b64"] not in str(remote.calls)  # sanity: not the raw uuid


def test_broker_subscription_get_rejects_verifier_mismatch():
    remote = FakeMarzban()
    source = remote.users["alice"]
    username = "mgc_" + "t" * 26
    request = {
        "operation_id": derive_operation_id(username), "child_username": username,
        "source_username": "alice", "source_contract_hash": source_contract_hash(source),
        "expire": 0,
    }
    BrokerOperations(remote).dispatch("child.user.ensure", request)
    remote.users[username]["subscription_url"] = "/sub/opaque-child-token"
    from src.child_contract import credential_verifier
    sub_request = {
        "operation_id": derive_operation_id(username), "child_username": username,
        "source_contract_hash": source_contract_hash(source), "expire": 0,
        "uuid_verifier": credential_verifier("00000000-0000-4000-8000-000000000000"),
    }
    with pytest.raises(ValueError, match="verifier mismatch"):
        BrokerOperations(remote).dispatch("child.user.subscription.get", sub_request)


# --- full engine ---------------------------------------------------------------

def test_invalid_token_is_uniform(db):
    _remote, ensure_fn, subscription_fn = _remote_and_ensure_fn()
    result = resolve_opaque_subscription(
        db, "not-a-real-opaque-token", _known_hwid_meta("engine-hwid-1"),
        hmac_key=HWID_KEY, ensure_fn=ensure_fn, subscription_fn=subscription_fn,
        worker_id="engine-test-worker", now=300,
    )
    assert result.outcome == OUTCOME_INVALID_TOKEN


def test_known_hwid_resolves_existing_slot_and_returns_child_config(db):
    account, alias_id, slot, remote, ensure_fn, subscription_fn = _seed_account_with_first_child(
        db, mapping="ENGINE_KNOWN", tg=810001,
    )
    token = _issue_active_credential(db, account["account_id"], idem_prefix="engine-known")
    result = resolve_opaque_subscription(
        db, token, _known_hwid_meta(slot["hwid_masked"]) | {"device_id": "engine-known-hwid"},
        hmac_key=HWID_KEY, ensure_fn=ensure_fn, subscription_fn=subscription_fn,
        worker_id="engine-test-worker", now=300,
    )
    # First call: this HWID hasn't claimed a slot yet under this fresh
    # account fixture's own HWID space (the seed used a *different* HWID
    # internally via _account's own slot claim), so it assigns a fresh slot.
    assert result.outcome == OUTCOME_OK
    assert result.child_username is not None
    assert result.body_b64 is not None

    # Retry with the exact same HWID: same slot/generation/child, idempotent.
    second = resolve_opaque_subscription(
        db, token, _known_hwid_meta("engine-known-hwid"),
        hmac_key=HWID_KEY, ensure_fn=ensure_fn, subscription_fn=subscription_fn,
        worker_id="engine-test-worker", now=301,
    )
    assert second.outcome == OUTCOME_OK
    assert second.child_username == result.child_username
    assert second.slot_number == result.slot_number
    assert second.generation == result.generation


def test_missing_hwid_denied(db):
    account, _alias_id, _slot, _r, ensure_fn, subscription_fn = _seed_account_with_first_child(
        db, mapping="ENGINE_MISSING_HWID", tg=810002,
    )
    token = _issue_active_credential(db, account["account_id"], idem_prefix="engine-missing")
    meta = {**SUPPORTED_METADATA, "hwid_candidate_present": False, "hwid_candidate_supported": False, "device_id": None}
    result = resolve_opaque_subscription(
        db, token, meta, hmac_key=HWID_KEY, ensure_fn=ensure_fn, subscription_fn=subscription_fn,
        worker_id="engine-test-worker", now=300,
    )
    assert result.outcome == OUTCOME_DENY_MISSING_HWID


def test_malformed_hwid_denied(db):
    account, _alias_id, _slot, _r, ensure_fn, subscription_fn = _seed_account_with_first_child(
        db, mapping="ENGINE_MALFORMED_HWID", tg=810003,
    )
    token = _issue_active_credential(db, account["account_id"], idem_prefix="engine-malformed")
    meta = {**SUPPORTED_METADATA, "hwid_candidate_present": True, "hwid_candidate_supported": False, "device_id": "!!!"}
    result = resolve_opaque_subscription(
        db, token, meta, hmac_key=HWID_KEY, ensure_fn=ensure_fn, subscription_fn=subscription_fn,
        worker_id="engine-test-worker", now=300,
    )
    assert result.outcome == OUTCOME_DENY_MALFORMED_HWID


def test_unsupported_client_denied(db):
    account, _alias_id, _slot, _r, ensure_fn, subscription_fn = _seed_account_with_first_child(
        db, mapping="ENGINE_UNSUPPORTED_CLIENT", tg=810004,
    )
    token = _issue_active_credential(db, account["account_id"], idem_prefix="engine-unsupported")
    meta = _known_hwid_meta("engine-unsupported-hwid") | {"client_name": "TotallyUnknownClient"}
    result = resolve_opaque_subscription(
        db, token, meta, hmac_key=HWID_KEY, ensure_fn=ensure_fn, subscription_fn=subscription_fn,
        worker_id="engine-test-worker", now=300,
    )
    assert result.outcome == OUTCOME_DENY_UNSUPPORTED_CLIENT


def test_full_slots_denied_with_clear_refusal_no_eviction(db):
    account, alias_id, slot, remote, ensure_fn, subscription_fn = _seed_account_with_first_child(
        db, mapping="ENGINE_FULL", tg=810005,
    )
    # this internal-plan account defaults to device_limit=10 (see _account());
    # fill remaining capacity with 9 more distinct HWIDs via the real allocator.
    for i in range(9):
        db.device_slots.claim(account["account_id"], f"engine-full-filler-{i}", HWID_KEY, now=150 + i)
    token = _issue_active_credential(db, account["account_id"], idem_prefix="engine-full")
    result = resolve_opaque_subscription(
        db, token, _known_hwid_meta("engine-full-overflow-hwid"),
        hmac_key=HWID_KEY, ensure_fn=ensure_fn, subscription_fn=subscription_fn,
        worker_id="engine-test-worker", now=300,
    )
    assert result.outcome == OUTCOME_DENY_SLOT_LIMIT


def test_cross_account_hwid_denied(db):
    account_a, _alias_a, _slot_a, _r, ensure_a, sub_a = _seed_account_with_first_child(
        db, mapping="ENGINE_CROSS_A", tg=810006,
    )
    account_b, _alias_b, _slot_b, _r2, _ensure_b, _sub_b = _seed_account_with_first_child(
        db, mapping="ENGINE_CROSS_B", tg=810007, alias="second-source",
    )
    db.device_slots.claim(account_a["account_id"], "engine-cross-shared-hwid", HWID_KEY, now=150)
    token_b = _issue_active_credential(db, account_b["account_id"], idem_prefix="engine-cross-b")
    result = resolve_opaque_subscription(
        db, token_b, _known_hwid_meta("engine-cross-shared-hwid"),
        hmac_key=HWID_KEY, ensure_fn=ensure_a, subscription_fn=sub_a,
        worker_id="engine-test-worker", now=300,
    )
    assert result.outcome == OUTCOME_DENY_CROSS_ACCOUNT_HWID


def test_expired_parent_denies_even_with_valid_token_and_hwid(db):
    account, _alias_id, _slot, _r, ensure_fn, subscription_fn = _seed_account_with_first_child(
        db, mapping="ENGINE_EXPIRED_PARENT", tg=810008,
    )
    db._conn.execute(
        "UPDATE mgboost_subscriptions SET status='EXPIRED',current_expiry=? WHERE account_id=?",
        (50, account["account_id"]),
    )
    db._conn.commit()
    token = _issue_active_credential(db, account["account_id"], idem_prefix="engine-expired")
    result = resolve_opaque_subscription(
        db, token, _known_hwid_meta("engine-expired-hwid"),
        hmac_key=HWID_KEY, ensure_fn=ensure_fn, subscription_fn=subscription_fn,
        worker_id="engine-test-worker", now=300,
    )
    assert result.outcome == OUTCOME_PARENT_UNAVAILABLE


def test_no_prior_child_returns_provisioning_unavailable_not_a_silent_create(db):
    account, _alias_id, _slot = _account(db, mapping="ENGINE_NO_PRIOR_CHILD", tg=810009)
    _remote, ensure_fn, subscription_fn = _remote_and_ensure_fn()
    token = _issue_active_credential(db, account["account_id"], idem_prefix="engine-no-prior")
    result = resolve_opaque_subscription(
        db, token, _known_hwid_meta("engine-no-prior-hwid"),
        hmac_key=HWID_KEY, ensure_fn=ensure_fn, subscription_fn=subscription_fn,
        worker_id="engine-test-worker", now=300,
    )
    assert result.outcome == OUTCOME_PROVISIONING_UNAVAILABLE


def test_second_new_hwid_gets_its_own_second_child(db):
    account, alias_id, slot1, remote, ensure_fn, subscription_fn = _seed_account_with_first_child(
        db, mapping="ENGINE_SECOND_DEVICE", tg=810010,
    )
    token = _issue_active_credential(db, account["account_id"], idem_prefix="engine-second-device")

    first = resolve_opaque_subscription(
        db, token, _known_hwid_meta("engine-second-device-hwid-a"),
        hmac_key=HWID_KEY, ensure_fn=ensure_fn, subscription_fn=subscription_fn,
        worker_id="engine-test-worker", now=300,
    )
    second = resolve_opaque_subscription(
        db, token, _known_hwid_meta("engine-second-device-hwid-b"),
        hmac_key=HWID_KEY, ensure_fn=ensure_fn, subscription_fn=subscription_fn,
        worker_id="engine-test-worker", now=301,
    )
    assert first.outcome == OUTCOME_OK
    assert second.outcome == OUTCOME_OK
    assert first.child_username != second.child_username
    assert first.slot_number != second.slot_number
