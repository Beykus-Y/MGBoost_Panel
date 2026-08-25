"""scripts/configure_ph3_03_shadow_binding.py -- the root-only tool that
creates the single approved PH3-03 SHADOW binding. Every scenario proves the
tool is fail-closed: it never silently updates an existing row, never widens
scope beyond the one fixed manifest, and never creates a second binding.
"""

import importlib
import os
import tempfile
import time

import pytest

from src.broker_operations import BrokerOperations
from src.child_contract import source_contract_hash

from scripts import configure_ph3_03_shadow_binding as tool
from scripts.build_ph3_03_staging_xray import EFFECTIVE_VLESS_TAGS
from scripts.verify_ph3_03_marzban_staging import _make_parent_and_slot
from tests.test_child_provisioning import PRIMARY, PRIMARY_LOGIN, _account
from tests.test_marzban_broker import FakeMarzban


@pytest.fixture
def db(monkeypatch):
    tmp = tempfile.mkdtemp()
    monkeypatch.setenv("DATA_DIR", tmp)
    monkeypatch.setenv("PRIMARY_MGBOOST_ADMIN_ACTOR_ID", PRIMARY)
    monkeypatch.setenv("PRIMARY_MGBOOST_ADMIN_LOGIN", PRIMARY_LOGIN)
    monkeypatch.setenv("DEVICE_SLOT_HMAC_KEY", "shadow-binding-tool-test-hmac-key-32bytes")
    import src.config as config
    import src.database as database
    importlib.reload(config)
    importlib.reload(database)
    database.DB_PATH = os.path.join(tmp, "db.sqlite3")
    instance = database.Database()
    yield instance
    instance._conn.close()


def _approved_source_user():
    return {
        "username": tool.SOURCE_USERNAME,
        "expire": 0, "status": "active",
        "proxies": {"vless": {"id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", "flow": "xtls-rprx-vision"}},
        "inbounds": {"vless": list(EFFECTIVE_VLESS_TAGS)},
        "data_limit": None,
    }


def _insert_device(db_instance, *, device_id=tool.DEVICE_ROW_ID, username=None, overrides=None):
    now = int(time.time())
    fields = dict(tool.EXPECTED_DEVICE)
    fields["username"] = username or fields["username"]
    if overrides:
        fields.update(overrides)
    db_instance._conn.execute(
        "INSERT INTO user_devices (id, username, token, request_key, device_name, platform, "
        "client_name, client_version, is_active, first_seen, last_seen) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            device_id, fields["username"], "tokref", f"hwid:approved-canary-device-{device_id}",
            fields["device_name"], fields["platform"], fields["client_name"],
            fields["client_version"], fields["is_active"], now, now,
        ),
    )
    db_instance._conn.commit()


def _insert_workflow(db_instance, outbox_id, account_id, child_intent_id, *, state="IN_SYNC"):
    now = int(time.time())
    db_instance._conn.execute(
        "INSERT INTO mgboost_child_workflow_state (outbox_id, account_id, child_intent_id, "
        "reconcile_state, next_check_at, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
        (outbox_id, account_id, child_intent_id, state, now, now, now),
    )
    db_instance._conn.commit()


def _build_manifest(db_instance, *, apply=True, insert_device=True, workflow_state="IN_SYNC"):
    """Builds the exact approved canary shape the tool expects: same mapping
    key, same alias/telegram identity, same slot 1/generation 1, and a child
    intent/outbox whose server-derived identity matches tool.EXPECTED_*."""
    account, alias_id, slot = _make_parent_and_slot(db_instance)
    assert account["public_id"] == tool.EXPECTED_ACCOUNT_PUBLIC_ID
    remote = FakeMarzban()
    remote.users = {tool.SOURCE_USERNAME: _approved_source_user()}
    request_hash = source_contract_hash(remote.users[tool.SOURCE_USERNAME])
    assert request_hash == tool.EXPECTED_SOURCE_HASH
    prepared = db_instance.child_provisioning.prepare_child_ensure(
        account_id=account["account_id"], slot_generation_id=slot["generation_id"],
        source_alias_id=alias_id, source_contract_hash=request_hash, expire=0,
        idempotency_key="shadow-binding-tool-fixture", now=200,
    )
    assert prepared["child_username"] == tool.EXPECTED_CHILD_USERNAME
    assert prepared["operation_id"] == tool.EXPECTED_OPERATION_ID
    if insert_device:
        _insert_device(db_instance)
    if not apply:
        return account, alias_id, slot, prepared
    claimed = db_instance.child_provisioning.claim(
        prepared["operation_id"], worker_id="fixture-worker", now=201, lease_seconds=5,
    )
    created = BrokerOperations(remote).dispatch("child.user.ensure", claimed["payload"])
    child_uuid = created.pop("uuid")
    db_instance.child_provisioning.acknowledge(
        prepared["operation_id"], worker_id="fixture-worker",
        outcome=created["outcome"], child_uuid=child_uuid, remote_result=created, now=202,
    )
    if workflow_state is not None:
        _insert_workflow(
            db_instance, prepared["id"], account["account_id"], prepared["child_intent_id"],
            state=workflow_state,
        )
    return account, alias_id, slot, prepared


# --- happy path / idempotency ----------------------------------------------

def test_create_succeeds_for_the_exact_approved_manifest(db):
    _build_manifest(db)
    result = tool.create(db)
    assert result["outcome"] == "CREATED"
    assert result["enabled"] is False
    row = db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_shadow_resolver_bindings"
    ).fetchone()[0]
    assert row == 1


def test_duplicate_identical_call_is_a_safe_no_op(db):
    _build_manifest(db)
    first = tool.create(db)
    second = tool.create(db)
    assert second == {"outcome": "EXISTING", "binding_id": first["binding_id"], "enabled": False}
    assert db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_shadow_resolver_bindings"
    ).fetchone()[0] == 1


def test_enable_then_disable_round_trip_is_idempotent(db):
    _build_manifest(db)
    tool.create(db)
    enabled = tool._set_enabled(db, True)
    assert enabled["outcome"] == "UPDATED" and enabled["enabled"] is True
    no_op = tool._set_enabled(db, True)
    assert no_op["outcome"] == "NO_OP"
    disabled = tool._set_enabled(db, False)
    assert disabled["outcome"] == "UPDATED" and disabled["enabled"] is False
    status = tool.status(db)
    assert status == {
        "total_bindings": 1, "enabled_bindings": 0,
        "approved_canary_binding_id": disabled["binding_id"],
        "approved_canary_enabled": False,
    }


def test_enable_without_existing_binding_fails_closed(db):
    _build_manifest(db)
    with pytest.raises(tool.ShadowBindingToolError, match="run --action create first"):
        tool._set_enabled(db, True)


# --- fail-closed integrity checks -------------------------------------------

def test_conflicting_duplicate_for_same_device_is_rejected(db, monkeypatch):
    """The schema itself makes every binding column immutable once written
    (BEFORE UPDATE trigger), so a conflicting row can only arise if the
    manifest resolution ever disagreed with an already-created binding. This
    proves `create()` detects that and fails closed instead of treating it
    as EXISTING or silently updating anything."""
    _build_manifest(db)
    first = tool.create(db)
    assert first["outcome"] == "CREATED"

    real_resolve = tool._resolve_approved_manifest

    def _forged_resolve(conn):
        manifest = dict(real_resolve(conn))
        manifest["operation_id"] = "op_" + "z" * 26
        return manifest

    monkeypatch.setattr(tool, "_resolve_approved_manifest", _forged_resolve)
    with pytest.raises(tool.ShadowBindingToolError, match="conflicting identity"):
        tool.create(db)
    assert db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_shadow_resolver_bindings"
    ).fetchone()[0] == 1


def test_wrong_account_missing_approved_mapping_fails_closed(db):
    # No INTERNAL_OWNER_PRIMARY group exists at all in this DB.
    _account(db, mapping="SOME_OTHER_MAPPING", tg=1, alias="unrelated-alias")
    with pytest.raises(tool.ShadowBindingToolError, match="no alias group"):
        tool.create(db)
    assert db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_shadow_resolver_bindings"
    ).fetchone()[0] == 0


def test_wrong_device_username_fails_closed(db):
    account, alias_id, slot = _make_parent_and_slot(db)
    remote = FakeMarzban()
    remote.users = {tool.SOURCE_USERNAME: _approved_source_user()}
    request_hash = source_contract_hash(remote.users[tool.SOURCE_USERNAME])
    prepared = db.child_provisioning.prepare_child_ensure(
        account_id=account["account_id"], slot_generation_id=slot["generation_id"],
        source_alias_id=alias_id, source_contract_hash=request_hash, expire=0,
        idempotency_key="wrong-device-fixture", now=200,
    )
    _insert_device(db, username="not-beykusios")
    claimed = db.child_provisioning.claim(
        prepared["operation_id"], worker_id="fixture-worker", now=201, lease_seconds=5,
    )
    created = BrokerOperations(remote).dispatch("child.user.ensure", claimed["payload"])
    child_uuid = created.pop("uuid")
    db.child_provisioning.acknowledge(
        prepared["operation_id"], worker_id="fixture-worker",
        outcome=created["outcome"], child_uuid=child_uuid, remote_result=created, now=202,
    )
    _insert_workflow(db, prepared["id"], account["account_id"], prepared["child_intent_id"])
    with pytest.raises(tool.ShadowBindingToolError, match="does not belong to the approved alias"):
        tool.create(db)
    assert db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_shadow_resolver_bindings"
    ).fetchone()[0] == 0


def test_device_contract_mismatch_fails_closed(db):
    _make_parent_and_slot(db)
    _insert_device(db, overrides={"client_version": "9.9.9"})
    with pytest.raises(tool.ShadowBindingToolError, match="approved device contract"):
        tool.create(db)


def test_stale_slot_generation_fails_closed(db):
    account, alias_id, slot = _build_manifest(db)[:3]
    db._conn.execute(
        "UPDATE mgboost_device_slots SET desired_state='DISABLED' WHERE id=?",
        (slot["slot_id"],),
    )
    db._conn.commit()
    with pytest.raises(tool.ShadowBindingToolError, match="not the current active generation"):
        tool.create(db)
    assert db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_shadow_resolver_bindings"
    ).fetchone()[0] == 0


def test_outbox_not_applied_fails_closed(db):
    _build_manifest(db, apply=False)
    with pytest.raises(tool.ShadowBindingToolError, match="is not APPLIED"):
        tool.create(db)
    assert db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_shadow_resolver_bindings"
    ).fetchone()[0] == 0


def test_child_reconciliation_not_in_sync_fails_closed(db):
    _build_manifest(db, workflow_state="REMOTE_MISMATCH")
    with pytest.raises(tool.ShadowBindingToolError, match="not IN_SYNC"):
        tool.create(db)
    assert db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_shadow_resolver_bindings"
    ).fetchone()[0] == 0


def test_missing_reconciliation_row_fails_closed(db):
    _build_manifest(db, workflow_state=None)
    with pytest.raises(tool.ShadowBindingToolError, match="not IN_SYNC"):
        tool.create(db)


def test_unexpected_pre_existing_binding_cardinality_fails_closed(db):
    """The tool refuses to add a binding for the approved device if any
    unrelated binding already exists -- this scope is exactly one canary."""
    other_account, other_alias_id, other_slot = _account(
        db, mapping="UNRELATED_OTHER_ACCOUNT", tg=42, alias="unrelated-legacy-user",
    )
    other_remote = FakeMarzban()
    other_remote.users["unrelated-legacy-user"] = other_remote.users.pop("alice")
    other_remote.users["unrelated-legacy-user"]["username"] = "unrelated-legacy-user"
    prepared = db.child_provisioning.prepare_child_ensure(
        account_id=other_account["account_id"], slot_generation_id=other_slot["generation_id"],
        source_alias_id=other_alias_id,
        source_contract_hash=source_contract_hash(other_remote.users["unrelated-legacy-user"]),
        expire=0, idempotency_key="unrelated-other-binding", now=300,
    )
    claimed = db.child_provisioning.claim(
        prepared["operation_id"], worker_id="other-worker", now=301, lease_seconds=5,
    )
    created = BrokerOperations(other_remote).dispatch("child.user.ensure", claimed["payload"])
    other_uuid = created.pop("uuid")
    db.child_provisioning.acknowledge(
        prepared["operation_id"], worker_id="other-worker",
        outcome=created["outcome"], child_uuid=other_uuid, remote_result=created, now=302,
    )
    _insert_device(db, device_id=999, username="alice")
    db.shadow_resolver_bindings.create_binding(
        account_id=other_account["account_id"], legacy_alias_id=other_alias_id,
        legacy_device_id=999, slot_generation_id=other_slot["generation_id"],
        child_intent_id=prepared["child_intent_id"], operation_id=prepared["operation_id"],
        decision_ref="unrelated-pre-existing-binding", enabled=False, now=303,
    )

    _build_manifest(db)
    with pytest.raises(tool.ShadowBindingToolError, match="unexpected pre-existing shadow binding cardinality"):
        tool.create(db)
    assert db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_shadow_resolver_bindings"
    ).fetchone()[0] == 1
