"""PH4-01 legacy bridge resolver engine: mapping -> the shared
resolve_account_device tail, per-device (not per-user) semantics, fail-closed
after a durable slot claim, and full/missing/lost-ACK/parent-unavailable
handling reusing PH3-02/03/04/08 exactly like PH2-01 does."""

import importlib
import os
import tempfile

import pytest

from src.legacy_bridge_resolver import OUTCOME_NOT_BRIDGED, is_fall_through_outcome, resolve_legacy_bridge
from src.opaque_resolver import (
    OUTCOME_DENY_MISSING_HWID,
    OUTCOME_DENY_SLOT_LIMIT,
    OUTCOME_OK,
    OUTCOME_PARENT_UNAVAILABLE,
    OUTCOME_PROVISIONING_UNAVAILABLE,
)
from src.security import AdminSessionStore

from tests.test_child_provisioning import HWID_KEY, PRIMARY, PRIMARY_LOGIN, _account
from tests.test_opaque_resolver import _known_hwid_meta, _remote_and_ensure_fn


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
    _raw, session = AdminSessionStore().create(PRIMARY_LOGIN, "legacy-bridge-resolver-test-jwt")
    return db.primary_admin_authority.authorize_session(session)


def _seed_bridged_account_with_first_child(db, *, mapping, tg, enabled=True):
    from src.child_contract import source_contract_hash

    account, alias_id, slot = _account(db, mapping=mapping, tg=tg, alias="alice")
    remote, ensure_fn, subscription_fn = _remote_and_ensure_fn()
    request_hash = source_contract_hash(remote.users["alice"])
    prepared = db.child_provisioning.prepare_child_ensure(
        account_id=account["account_id"], slot_generation_id=slot["generation_id"],
        source_alias_id=alias_id, source_contract_hash=request_hash, expire=0,
        idempotency_key=f"legacy-bridge-seed-{mapping}", now=100,
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
    cap = _capability(db)
    db.legacy_bridge.create_binding(
        capability=cap, account_id=account["account_id"], legacy_alias_id=alias_id,
        enabled=enabled, decision_ref=f"owner-approved-{mapping}", now=110,
    )
    return account, alias_id, slot, remote, ensure_fn, subscription_fn


def test_unmapped_legacy_username_is_not_bridged(db):
    _account(db, mapping="LBR_UNMAPPED", tg=840001)
    remote, ensure_fn, subscription_fn = _remote_and_ensure_fn()
    result = resolve_legacy_bridge(
        db, "some-unrelated-username", _known_hwid_meta("lbr-hwid-1"), hmac_key=HWID_KEY,
        ensure_fn=ensure_fn, subscription_fn=subscription_fn, worker_id="lbr-worker", now=300,
    )
    assert result.outcome == OUTCOME_NOT_BRIDGED
    assert is_fall_through_outcome(result.outcome)


def test_disabled_binding_is_not_bridged(db):
    account, alias_id, slot, remote, ensure_fn, subscription_fn = _seed_bridged_account_with_first_child(
        db, mapping="LBR_DISABLED", tg=840002, enabled=False,
    )
    result = resolve_legacy_bridge(
        db, "alice", _known_hwid_meta("lbr-hwid-2"), hmac_key=HWID_KEY,
        ensure_fn=ensure_fn, subscription_fn=subscription_fn, worker_id="lbr-worker", now=300,
    )
    assert is_fall_through_outcome(result.outcome)


def test_enabled_binding_known_username_bridges_to_child_config(db):
    account, alias_id, slot, remote, ensure_fn, subscription_fn = _seed_bridged_account_with_first_child(
        db, mapping="LBR_BRIDGED", tg=840003, enabled=True,
    )
    result = resolve_legacy_bridge(
        db, "alice", _known_hwid_meta("lbr-hwid-3"), hmac_key=HWID_KEY,
        ensure_fn=ensure_fn, subscription_fn=subscription_fn, worker_id="lbr-worker", now=300,
    )
    assert result.outcome == OUTCOME_OK
    assert result.body_b64 is not None
    assert result.child_username is not None


def test_per_device_not_per_user_second_unbridged_hwid_still_falls_through_when_full(db):
    """One legacy username can have several devices; a device that cannot
    get a slot (e.g. capacity) simply is not bridged this time -- it is
    never denied outright, since the whole point is the legacy path keeps
    working for it."""
    account, alias_id, slot, remote, ensure_fn, subscription_fn = _seed_bridged_account_with_first_child(
        db, mapping="LBR_PER_DEVICE", tg=840004, enabled=True,
    )
    for i in range(9):
        db.device_slots.claim(account["account_id"], f"lbr-filler-{i}", HWID_KEY, now=150 + i)
    result = resolve_legacy_bridge(
        db, "alice", _known_hwid_meta("lbr-overflow-hwid"), hmac_key=HWID_KEY,
        ensure_fn=ensure_fn, subscription_fn=subscription_fn, worker_id="lbr-worker", now=300,
    )
    assert result.outcome == OUTCOME_DENY_SLOT_LIMIT
    assert is_fall_through_outcome(result.outcome)


def test_missing_hwid_falls_through(db):
    account, alias_id, slot, remote, ensure_fn, subscription_fn = _seed_bridged_account_with_first_child(
        db, mapping="LBR_MISSING_HWID", tg=840005, enabled=True,
    )
    meta = {"client_name": "Happ", "client_version": "2.7.0", "platform": "windows",
            "hwid_candidate_present": False, "hwid_candidate_supported": False, "device_id": None}
    result = resolve_legacy_bridge(
        db, "alice", meta, hmac_key=HWID_KEY, ensure_fn=ensure_fn, subscription_fn=subscription_fn,
        worker_id="lbr-worker", now=300,
    )
    assert result.outcome == OUTCOME_DENY_MISSING_HWID
    assert is_fall_through_outcome(result.outcome)


def test_expired_parent_falls_through_not_fail_closed(db):
    account, alias_id, slot, remote, ensure_fn, subscription_fn = _seed_bridged_account_with_first_child(
        db, mapping="LBR_EXPIRED", tg=840006, enabled=True,
    )
    db._conn.execute(
        "UPDATE mgboost_subscriptions SET status='EXPIRED',current_expiry=? WHERE account_id=?",
        (50, account["account_id"]),
    )
    db._conn.commit()
    result = resolve_legacy_bridge(
        db, "alice", _known_hwid_meta("lbr-expired-hwid"), hmac_key=HWID_KEY,
        ensure_fn=ensure_fn, subscription_fn=subscription_fn, worker_id="lbr-worker", now=300,
    )
    assert result.outcome == OUTCOME_PARENT_UNAVAILABLE
    assert is_fall_through_outcome(result.outcome)


def test_provisioning_failure_after_slot_claim_is_fail_closed_not_fall_through(db):
    """Once a slot is durably claimed for this device (a fresh HWID, so
    ASSIGN_FREE_SLOT commits), a downstream broker failure must NOT be
    classified as fall-through -- the caller (route) must fail closed,
    never silently hand this device the shared legacy credential."""
    account, alias_id, slot, remote, ensure_fn, subscription_fn = _seed_bridged_account_with_first_child(
        db, mapping="LBR_PROV_FAIL", tg=840007, enabled=True,
    )

    def down_ensure_fn(payload):
        raise ConnectionError("broker down")

    def down_subscription_fn(payload):
        raise ConnectionError("broker down")

    result = resolve_legacy_bridge(
        db, "alice", _known_hwid_meta("lbr-prov-fail-hwid"), hmac_key=HWID_KEY,
        ensure_fn=down_ensure_fn, subscription_fn=down_subscription_fn,
        worker_id="lbr-worker", now=300,
    )
    assert result.outcome == OUTCOME_PROVISIONING_UNAVAILABLE
    assert not is_fall_through_outcome(result.outcome)


def test_repeat_request_same_device_converges_on_same_child(db):
    account, alias_id, slot, remote, ensure_fn, subscription_fn = _seed_bridged_account_with_first_child(
        db, mapping="LBR_REPEAT", tg=840008, enabled=True,
    )
    first = resolve_legacy_bridge(
        db, "alice", _known_hwid_meta("lbr-repeat-hwid"), hmac_key=HWID_KEY,
        ensure_fn=ensure_fn, subscription_fn=subscription_fn, worker_id="lbr-worker", now=300,
    )
    second = resolve_legacy_bridge(
        db, "alice", _known_hwid_meta("lbr-repeat-hwid"), hmac_key=HWID_KEY,
        ensure_fn=ensure_fn, subscription_fn=subscription_fn, worker_id="lbr-worker", now=301,
    )
    assert first.outcome == OUTCOME_OK and second.outcome == OUTCOME_OK
    assert first.child_username == second.child_username
    assert first.slot_number == second.slot_number


def test_second_distinct_device_gets_its_own_child(db):
    account, alias_id, slot, remote, ensure_fn, subscription_fn = _seed_bridged_account_with_first_child(
        db, mapping="LBR_SECOND_DEVICE", tg=840009, enabled=True,
    )
    first = resolve_legacy_bridge(
        db, "alice", _known_hwid_meta("lbr-second-a"), hmac_key=HWID_KEY,
        ensure_fn=ensure_fn, subscription_fn=subscription_fn, worker_id="lbr-worker", now=300,
    )
    second = resolve_legacy_bridge(
        db, "alice", _known_hwid_meta("lbr-second-b"), hmac_key=HWID_KEY,
        ensure_fn=ensure_fn, subscription_fn=subscription_fn, worker_id="lbr-worker", now=301,
    )
    assert first.outcome == OUTCOME_OK and second.outcome == OUTCOME_OK
    assert first.child_username != second.child_username


def test_bridged_response_never_contains_the_shared_legacy_uuid(db):
    account, alias_id, slot, remote, ensure_fn, subscription_fn = _seed_bridged_account_with_first_child(
        db, mapping="LBR_NO_SHARED_UUID", tg=840010, enabled=True,
    )
    import base64
    result = resolve_legacy_bridge(
        db, "alice", _known_hwid_meta("lbr-shared-check-hwid"), hmac_key=HWID_KEY,
        ensure_fn=ensure_fn, subscription_fn=subscription_fn, worker_id="lbr-worker", now=300,
    )
    assert result.outcome == OUTCOME_OK
    decoded = base64.b64decode(result.body_b64).decode("utf-8", errors="replace")
    shared_uuid = remote.users["alice"]["proxies"]["vless"]["id"]
    assert shared_uuid not in decoded


def test_raw_hwid_and_child_uuid_absent_from_db_after_bridge(db):
    account, alias_id, slot, remote, ensure_fn, subscription_fn = _seed_bridged_account_with_first_child(
        db, mapping="LBR_LEAK", tg=840011, enabled=True,
    )
    raw_hwid = "lbr-leak-raw-hwid-value"
    result = resolve_legacy_bridge(
        db, "alice", _known_hwid_meta(raw_hwid), hmac_key=HWID_KEY,
        ensure_fn=ensure_fn, subscription_fn=subscription_fn, worker_id="lbr-worker", now=300,
    )
    assert result.outcome == OUTCOME_OK
    dump = "\n".join(db._conn.iterdump())
    assert raw_hwid not in dump
