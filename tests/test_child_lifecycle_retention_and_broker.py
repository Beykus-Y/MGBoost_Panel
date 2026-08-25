"""PH3-05: DL-019/038 tombstone retention eligibility, device_slots.rebind()
edge cases, and the child.user.revoke typed broker payload boundary."""

import pytest

from src.broker_operations import BrokerOperations
from src.child_contract import (
    derive_lifecycle_operation_id,
    validate_child_revoke_request,
)
from src.child_lifecycle import (
    RETENTION_DAYS,
    cleanup_eligible_at,
    is_eligible_for_physical_cleanup,
)
from src.device_slots import CrossAccountHWID, DeviceSlotError, StaleSlotGeneration

from tests.test_child_provisioning import db  # noqa: F401
from tests.test_device_slots import HWID_KEY, _account_with_plan
from tests.test_marzban_broker import FakeMarzban, UUID


# --- retention ---------------------------------------------------------------

def test_tombstone_not_eligible_before_180_days():
    revoked_at = 1_000_000
    just_before = cleanup_eligible_at(revoked_at) - 1
    assert not is_eligible_for_physical_cleanup(
        revoked_at=revoked_at, now=just_before, has_live_references=False
    )


def test_tombstone_eligible_after_exactly_180_days_with_no_live_refs():
    revoked_at = 1_000_000
    exactly_at = cleanup_eligible_at(revoked_at)
    assert is_eligible_for_physical_cleanup(
        revoked_at=revoked_at, now=exactly_at, has_live_references=False
    )
    assert RETENTION_DAYS == 180


def test_live_reference_blocks_cleanup_even_after_180_days():
    revoked_at = 1_000_000
    long_after = cleanup_eligible_at(revoked_at) + 999_999
    assert not is_eligible_for_physical_cleanup(
        revoked_at=revoked_at, now=long_after, has_live_references=True
    )


def test_lifecycle_tables_still_block_delete_at_the_schema_level(db):
    # Defense in depth: even though this module never issues a DELETE, the
    # schema itself must still refuse one -- tombstones are permanent unless
    # a separate future migration explicitly changes this.
    from src.child_contract import source_contract_hash
    from src.broker_operations import BrokerOperations
    from tests.test_child_provisioning import _account

    account, alias_id, slot = _account(db, mapping="RETENTION_TRIGGER_TEST", tg=700001, alias="alice")
    remote = FakeMarzban()
    request_hash = source_contract_hash(remote.users["alice"])
    prepared = db.child_provisioning.prepare_child_ensure(
        account_id=account["account_id"], slot_generation_id=slot["generation_id"],
        source_alias_id=alias_id, source_contract_hash=request_hash, expire=0,
        idempotency_key="retention-trigger-fixture", now=200,
    )
    claimed = db.child_provisioning.claim(prepared["operation_id"], worker_id="fixture-worker", now=201, lease_seconds=5)
    created = BrokerOperations(remote).dispatch("child.user.ensure", claimed["payload"])
    child_uuid = created.pop("uuid")
    db.child_provisioning.acknowledge(
        prepared["operation_id"], worker_id="fixture-worker", outcome=created["outcome"],
        child_uuid=child_uuid, remote_result=created, now=202,
    )
    revoke = db.child_lifecycle.prepare_revoke(
        account_id=account["account_id"], old_child_intent_id=prepared["child_intent_id"],
        reason="retention trigger test", idempotency_key="retention-trigger-key", now=300,
    )
    with pytest.raises(Exception, match="immutable"):
        db._conn.execute(
            "DELETE FROM mgboost_child_lifecycle_operations WHERE operation_id=?",
            (revoke["operation_id"],),
        )
    db._conn.rollback()


# --- device_slots.rebind() edge cases ----------------------------------------

def test_rebind_stale_generation_raises(db):
    account, _sub = _account_with_plan(db, limit=3)
    claimed = db.device_slots.claim(account["id"], "device-a", HWID_KEY, now=100)
    with pytest.raises(StaleSlotGeneration):
        db.device_slots.rebind(
            account["id"], claimed["slot_id"], claimed["generation"] + 5,
            "device-b", HWID_KEY, reason="wrong generation", now=101,
        )


def test_rebind_cross_account_hwid_raises(db):
    account_a, _ = _account_with_plan(db, limit=3, code="A")
    account_b, _ = _account_with_plan(db, limit=3, code="B")
    claimed_a = db.device_slots.claim(account_a["id"], "device-a", HWID_KEY, now=100)
    db.device_slots.claim(account_b["id"], "shared-hwid", HWID_KEY, now=100)
    with pytest.raises(CrossAccountHWID):
        db.device_slots.rebind(
            account_a["id"], claimed_a["slot_id"], claimed_a["generation"],
            "shared-hwid", HWID_KEY, reason="cross account", now=101,
        )


def test_rebind_hwid_already_active_on_a_different_slot_of_same_account_raises(db):
    account, _sub = _account_with_plan(db, limit=3)
    slot_1 = db.device_slots.claim(account["id"], "device-1", HWID_KEY, now=100)
    slot_2 = db.device_slots.claim(account["id"], "device-2", HWID_KEY, now=100)
    with pytest.raises(DeviceSlotError):
        db.device_slots.rebind(
            account["id"], slot_1["slot_id"], slot_1["generation"],
            "device-2", HWID_KEY, reason="collides with slot 2", now=101,
        )


def test_rebind_idempotent_replay_returns_existing_next_generation(db):
    account, _sub = _account_with_plan(db, limit=3)
    claimed = db.device_slots.claim(account["id"], "device-a", HWID_KEY, now=100)
    first = db.device_slots.rebind(
        account["id"], claimed["slot_id"], claimed["generation"], "device-b",
        HWID_KEY, reason="reinstall", now=101,
    )
    second = db.device_slots.rebind(
        account["id"], claimed["slot_id"], claimed["generation"], "device-b",
        HWID_KEY, reason="reinstall", now=105,
    )
    assert second["result"] == "EXISTING"
    assert second["generation"] == first["generation"]
    count = db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_device_slot_generations WHERE slot_id=?",
        (claimed["slot_id"],),
    ).fetchone()[0]
    assert count == 2


def test_rebind_replay_with_a_different_new_hwid_conflicts(db):
    account, _sub = _account_with_plan(db, limit=3)
    claimed = db.device_slots.claim(account["id"], "device-a", HWID_KEY, now=100)
    db.device_slots.rebind(
        account["id"], claimed["slot_id"], claimed["generation"], "device-b",
        HWID_KEY, reason="reinstall", now=101,
    )
    with pytest.raises(StaleSlotGeneration):
        db.device_slots.rebind(
            account["id"], claimed["slot_id"], claimed["generation"], "device-c",
            HWID_KEY, reason="reinstall", now=105,
        )


# --- typed broker payload boundary -------------------------------------------

def test_revoke_request_rejects_arbitrary_extra_fields():
    with pytest.raises(ValueError, match="invalid child revoke fields"):
        validate_child_revoke_request({
            "operation_id": "lc_" + "a" * 26,
            "child_username": "mgc_" + "a" * 26,
            "uuid_verifier": "sha256:" + "a" * 64,
            "proxies": {"vless": {"id": "attacker-supplied"}},
        })


def test_revoke_request_rejects_missing_fields():
    with pytest.raises(ValueError, match="invalid child revoke fields"):
        validate_child_revoke_request({"child_username": "mgc_" + "a" * 26})


def test_revoke_request_rejects_operation_id_not_matching_derivation():
    child_username = "mgc_" + "a" * 26
    with pytest.raises(ValueError, match="does not match child identity"):
        validate_child_revoke_request({
            "operation_id": "lc_" + "z" * 26,  # not derived from child_username
            "child_username": child_username,
            "uuid_verifier": "sha256:" + "a" * 64,
        })


def test_revoke_request_rejects_ensure_operation_id_reuse():
    """The REVOKE operation id must be distinct from the ENSURE operation id
    for the exact same child -- reusing one namespace for another operation
    kind is rejected, not silently accepted."""
    from src.child_contract import derive_operation_id

    child_username = "mgc_" + "a" * 26
    ensure_op_id = derive_operation_id(child_username)
    with pytest.raises(ValueError, match="invalid child lifecycle operation id"):
        validate_child_revoke_request({
            "operation_id": ensure_op_id,
            "child_username": child_username,
            "uuid_verifier": "sha256:" + "a" * 64,
        })


def test_broker_capability_split_resolver_identity_cannot_revoke():
    """Mirrors PH3-03's capability split: only mgboost-main-class identities
    may call child.user.revoke; the resolver-only identity must not."""
    from src.broker_protocol import BROKER_OPERATIONS
    from src.broker_server import BrokerApplication

    main_key = "lifecycle-test-main-key-at-least-32-bytes!"
    resolver_key = "lifecycle-test-resolver-key-at-least-32-by"
    app = BrokerApplication(
        BrokerOperations(FakeMarzban()), shared_key=main_key, client_id="mgboost-main",
        client_policies={
            "mgboost-main": {
                "shared_key": main_key,
                "allowed_operations": BROKER_OPERATIONS - {"child.user.credentials.get"},
            },
            "mgboost-sub-resolver": {
                "shared_key": resolver_key,
                "allowed_operations": {"child.user.credentials.get"},
            },
        },
    )
    assert app.authorize_operation("mgboost-main", "child.user.revoke") is True
    assert app.authorize_operation("mgboost-sub-resolver", "child.user.revoke") is False


def test_broker_revoke_rejects_untyped_payload_end_to_end():
    remote = FakeMarzban()
    with pytest.raises(ValueError):
        BrokerOperations(remote).dispatch("child.user.revoke", {
            "username": "alice", "status": "disabled",
        })
