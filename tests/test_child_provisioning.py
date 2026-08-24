import importlib
import json
import os
import tempfile
from types import SimpleNamespace

import pytest

from src.broker_operations import BrokerOperations
from src.child_contract import source_contract_hash
from tests.test_marzban_broker import FakeMarzban


PRIMARY = "owner:mgboost-primary:v1"
PRIMARY_LOGIN = "authenticated-primary-login"
HWID_KEY = "slot-test-hwid-key-that-is-at-least-32-bytes"


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


def _account(db, *, mapping="INTERNAL_OWNER_PRIMARY", tg=905302972, alias="alice"):
    capability = db.primary_admin_authority.authorize_session(
        SimpleNamespace(username=PRIMARY_LOGIN)
    )
    plan = db.internal_entitlements.create_internal_plan(
        capability=capability,
        plan_code="INTERNAL_CANARY",
        version=1,
        display_name="Internal canary",
        device_limit_mode="LIMITED",
        device_limit=10,
        wl_mode="UNLIMITED",
        now=100,
    )
    account = db.internal_entitlements.create_reviewed_account(
        capability=capability,
        plan_version_id=plan["id"],
        legacy_username=alias,
        mapping_key=mapping,
        decision_ref="owner-approved-canary-v1",
        legacy_aliases=[{
            "legacy_username": alias,
            "alias_role": "PRIMARY",
            "ownership_provenance": "OWNER_APPROVED",
            "legacy_status": "UNLIMITED",
            "legacy_expiry": None,
            "observed_device_count": 1,
            "observed_hwid_count": 1,
            "evidence": {"ref": "masked-candidate"},
        }],
        ownership_evidence="PROVEN",
        telegram_id=tg,
        legacy_status="UNLIMITED",
        legacy_expiry=None,
        device_evidence_count=1,
        hwid_evidence_count=1,
        internal_reason="Owner-approved isolated child canary fixture",
        migration_confidence="HIGH",
        evidence={"schema": 1},
        idempotency_key="account-create-" + mapping,
        now=100,
    )
    alias_row = db._conn.execute(
        "SELECT id FROM mgboost_legacy_account_aliases WHERE account_id=?",
        (account["account_id"],),
    ).fetchone()
    slot = db.device_slots.claim(
        account["account_id"], "privacy-safe-test-hwid-" + mapping, HWID_KEY, now=101
    )
    return account, alias_row["id"], slot


def test_prerequisite_schema_is_idempotent_and_dormant(db):
    from src.child_provisioning_schema import (
        MIGRATION_ID, NEW_RUNTIME_TABLES, SCHEMA_CHECKSUM,
        apply_child_provisioning_schema,
    )
    assert apply_child_provisioning_schema(db._conn, now=200) is False
    marker = db._conn.execute(
        "SELECT schema_checksum FROM mgboost_schema_migrations WHERE migration_id=?",
        (MIGRATION_ID,),
    ).fetchone()
    assert marker[0] == SCHEMA_CHECKSUM
    assert all(
        db._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
        for table in NEW_RUNTIME_TABLES
    )


def test_prepare_is_atomic_server_derived_and_idempotent(db):
    account, alias_id, slot = _account(db)
    contract_hash = "a" * 64
    first = db.child_provisioning.prepare_child_ensure(
        account_id=account["account_id"],
        slot_generation_id=slot["generation_id"],
        source_alias_id=alias_id,
        source_contract_hash=contract_hash,
        expire=0,
        idempotency_key="stable-child-create-request",
        now=102,
    )
    second = db.child_provisioning.prepare_child_ensure(
        account_id=account["account_id"],
        slot_generation_id=slot["generation_id"],
        source_alias_id=alias_id,
        source_contract_hash=contract_hash,
        expire=0,
        idempotency_key="stable-child-create-request",
        now=103,
    )
    assert first["id"] == second["id"]
    assert first["child_username"].startswith("mgc_")
    payload = json.loads(first["payload_json"])
    assert payload["child_username"] == first["child_username"]
    assert set(payload) == {
        "operation_id", "child_username", "source_username",
        "source_contract_hash", "expire",
    }
    assert db._conn.execute("SELECT COUNT(*) FROM mgboost_child_user_intents").fetchone()[0] == 1
    assert db._conn.execute("SELECT COUNT(*) FROM mgboost_outbox").fetchone()[0] == 1


def test_remote_created_local_ack_failed_retries_as_existing_and_stores_no_raw_uuid(db):
    account, alias_id, slot = _account(db)
    remote = FakeMarzban()
    request_hash = source_contract_hash(remote.users["alice"])
    prepared = db.child_provisioning.prepare_child_ensure(
        account_id=account["account_id"],
        slot_generation_id=slot["generation_id"],
        source_alias_id=alias_id,
        source_contract_hash=request_hash,
        expire=0,
        idempotency_key="crash-retry-child-operation",
        now=102,
    )
    claimed = db.child_provisioning.claim(
        prepared["operation_id"], worker_id="worker-one", now=103, lease_seconds=5
    )
    created = BrokerOperations(remote).dispatch("child.user.ensure", claimed["payload"])
    assert created["outcome"] == "CREATED"

    # Simulate process death after Marzban committed but before local ACK.
    reclaimed = db.child_provisioning.claim(
        prepared["operation_id"], worker_id="worker-two", now=109, lease_seconds=5
    )
    existing = BrokerOperations(remote).dispatch("child.user.ensure", reclaimed["payload"])
    assert existing["outcome"] == "EXISTING"
    child = db.child_provisioning.acknowledge(
        prepared["operation_id"], worker_id="worker-two",
        outcome=existing["outcome"], child_uuid=existing.pop("uuid"),
        remote_result=existing, now=110,
    )
    assert child["observed_state"] == "ACTIVE"
    assert child["uuid_verifier"].startswith("sha256:")
    assert child["uuid_masked"].startswith("uuid_")
    assert created["uuid"] not in json.dumps(dict(child))
    raw_text_values = [
        value for row in db._conn.execute(
            "SELECT uuid_verifier,uuid_masked FROM mgboost_child_user_intents"
        ) for value in row if value
    ]
    assert created["uuid"] not in raw_text_values
    events = db._conn.execute(
        "SELECT attempt_no,event_type,outcome FROM mgboost_outbox_attempt_events "
        "ORDER BY id"
    ).fetchall()
    assert [tuple(row) for row in events] == [
        (1, "STARTED", None), (2, "STARTED", None),
        (2, "RECONCILED", "EXISTING"),
    ]


def test_cross_account_alias_or_generation_cannot_prepare_child(db):
    first, first_alias, first_slot = _account(db, mapping="FIRST", tg=1001, alias="alice")
    second, second_alias, second_slot = _account(
        db, mapping="SECOND", tg=1002, alias="second-source"
    )
    with pytest.raises(Exception, match="account-owned legacy alias"):
        db.child_provisioning.prepare_child_ensure(
            account_id=first["account_id"],
            slot_generation_id=first_slot["generation_id"],
            source_alias_id=second_alias,
            source_contract_hash="b" * 64,
            expire=0,
            idempotency_key="cross-account-alias-attempt",
            now=200,
        )
    with pytest.raises(Exception, match="active slot generation"):
        db.child_provisioning.prepare_child_ensure(
            account_id=first["account_id"],
            slot_generation_id=second_slot["generation_id"],
            source_alias_id=first_alias,
            source_contract_hash="b" * 64,
            expire=0,
            idempotency_key="cross-account-generation-attempt",
            now=200,
        )
