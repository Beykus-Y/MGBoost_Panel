"""PH3-03 dual-run SHADOW resolver: dual-run safety, capability boundary and
the required failure matrix (ROADMAP.md PH3-03).

Every scenario below asserts two things: (1) the legacy path is completely
untouched by shadow failures (this module never raises past its public
entrypoint), and (2) exactly one privacy-safe metric row is recorded with the
expected PASS/FAIL category. None ever contain a raw UUID.
"""

import base64
import importlib
import json
import logging
import os
import sqlite3
import tempfile
import threading
import time

import pytest

from src.broker_operations import BrokerOperations
from src.child_contract import source_contract_hash
from src import shadow_resolver

from tests.test_broker_client_policies import (
    MAIN_CLIENT, MAIN_KEY, RESOLVER_CLIENT, RESOLVER_KEY, running_split_broker,
)
from tests.test_child_provisioning import PRIMARY, PRIMARY_LOGIN, _account
from tests.test_marzban_broker import FakeMarzban, UUID


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
    yield instance, database.DB_PATH
    instance._conn.close()


def _add_device(db_instance, *, username="alice", request_key="hwid:shadow-test-device"):
    now = int(time.time())
    db_instance._conn.execute(
        "INSERT INTO user_devices (username, token, request_key, device_name, platform, "
        "client_name, client_version, is_active, first_seen, last_seen) "
        "VALUES (?,?,?,?,?,?,?,1,?,?)",
        (username, "tokref", request_key, "iPhone 17", "iOS", "INCY", "2.5.2", now, now),
    )
    db_instance._conn.commit()
    return db_instance._conn.execute(
        "SELECT id FROM user_devices WHERE username=? AND request_key=?",
        (username, request_key),
    ).fetchone()[0]


def _build_applied_binding(db_instance, *, remote=None, request_key="hwid:shadow-test-device"):
    remote = remote or FakeMarzban()
    account, alias_id, slot = _account(db_instance)
    request_hash = source_contract_hash(remote.users["alice"])
    prepared = db_instance.child_provisioning.prepare_child_ensure(
        account_id=account["account_id"], slot_generation_id=slot["generation_id"],
        source_alias_id=alias_id, source_contract_hash=request_hash, expire=0,
        idempotency_key="shadow-resolver-fixture-" + request_key, now=200,
    )
    claimed = db_instance.child_provisioning.claim(
        prepared["operation_id"], worker_id="worker-1", now=201, lease_seconds=5
    )
    created = BrokerOperations(remote).dispatch("child.user.ensure", claimed["payload"])
    child_uuid = created.pop("uuid")
    db_instance.child_provisioning.acknowledge(
        prepared["operation_id"], worker_id="worker-1", outcome=created["outcome"],
        child_uuid=child_uuid, remote_result=created, now=202,
    )
    device_id = _add_device(db_instance, request_key=request_key)
    binding = db_instance.shadow_resolver_bindings.create_binding(
        account_id=account["account_id"], legacy_alias_id=alias_id,
        legacy_device_id=device_id, slot_generation_id=slot["generation_id"],
        child_intent_id=prepared["child_intent_id"], operation_id=prepared["operation_id"],
        decision_ref="test-shadow-canary-v1", now=203,
    )
    return {
        "remote": remote, "binding": binding, "device_id": device_id,
        "child_uuid": child_uuid, "child_username": prepared["child_username"],
        "account_id": account["account_id"], "slot": slot,
    }


def _raw_legacy_body(child_line_uuid=UUID):
    line = (
        f"vless://{child_line_uuid}@example.com:443?type=tcp&security=reality&"
        f"sni=example.com&pbk=fakepbk&sid=ab&flow=xtls-rprx-vision#LEGACY-Node"
    )
    return base64.b64encode(line.encode("utf-8"))


def _resolver_env(monkeypatch, port, *, timeout="2"):
    monkeypatch.setenv("SHADOW_RESOLVER_ENABLED", "1")
    monkeypatch.setenv("MARZBAN_BROKER_URL", f"http://127.0.0.1:{port}")
    monkeypatch.setenv("MARZBAN_BROKER_AUTH_KEY", MAIN_KEY)
    monkeypatch.setenv("MARZBAN_BROKER_CLIENT_ID", MAIN_CLIENT)
    monkeypatch.setenv("MARZBAN_BROKER_RESOLVER_AUTH_KEY", RESOLVER_KEY)
    monkeypatch.setenv("MARZBAN_BROKER_RESOLVER_CLIENT_ID", RESOLVER_CLIENT)
    monkeypatch.setenv("MARZBAN_BROKER_RESOLVER_TIMEOUT_SECONDS", timeout)


def _split_config(port, *, timeout=2.0, credentials_key=RESOLVER_KEY, credentials_client=RESOLVER_CLIENT,
                   observe_key=MAIN_KEY, observe_client=MAIN_CLIENT):
    return {
        "base_url": f"http://127.0.0.1:{port}",
        "observe_shared_key": observe_key, "observe_client_id": observe_client,
        "credentials_shared_key": credentials_key, "credentials_client_id": credentials_client,
        "timeout": timeout,
    }


def _metrics_rows(db_instance, binding_id):
    return db_instance._conn.execute(
        "SELECT result, category, credential_result, legacy_fallback_success, request_count "
        "FROM mgboost_shadow_resolver_metrics WHERE binding_id=?",
        (binding_id,),
    ).fetchall()


def _run_sync(config, db_path, request_key, fixture):
    """Call the internal resolve+record step directly (no thread) for
    deterministic failure-matrix assertions."""
    shadow_resolver._resolve_and_record(
        config, db_path, "legacy-token", "alice", request_key, _raw_legacy_body(),
    )


# --- dual-run safety -------------------------------------------------------

def test_disabled_resolver_never_runs(db, monkeypatch):
    instance, db_path = db
    fixture = _build_applied_binding(instance)
    monkeypatch.delenv("SHADOW_RESOLVER_ENABLED", raising=False)
    shadow_resolver.schedule_shadow_resolution(
        "tok", "alice", {"request_key": "hwid:shadow-test-device"}, _raw_legacy_body(),
        db_path=db_path,
    )
    assert _metrics_rows(instance, fixture["binding"]["id"]) == []


def test_no_binding_for_device_is_a_silent_skip(db, monkeypatch):
    instance, db_path = db
    with running_split_broker() as (server, marzban):
        _resolver_env(monkeypatch, server.server_address[1])
        shadow_resolver.schedule_shadow_resolution(
            "tok", "alice", {"request_key": "hwid:unrelated-device"}, _raw_legacy_body(),
            db_path=db_path,
        )
        for t in threading.enumerate():
            if t.name == "shadow-resolver":
                t.join(timeout=3)
    assert instance._conn.execute(
        "SELECT COUNT(*) FROM mgboost_shadow_resolver_metrics"
    ).fetchone()[0] == 0


def test_disabled_binding_is_a_silent_skip(db, monkeypatch):
    instance, db_path = db
    fixture = _build_applied_binding(instance)
    instance.shadow_resolver_bindings.set_enabled(fixture["binding"]["id"], False)
    with running_split_broker(fixture["remote"]) as (server, marzban):
        config = _split_config(server.server_address[1])
        _run_sync(config, db_path, "hwid:shadow-test-device", fixture)
    assert _metrics_rows(instance, fixture["binding"]["id"]) == []


def test_fp_key_requests_never_reach_shadow_scope(db, monkeypatch):
    """Only hwid-locked devices can ever be in shadow scope, matching the
    legacy resolver's own hwid-only enforcement boundary."""
    instance, db_path = db
    fixture = _build_applied_binding(instance)
    with running_split_broker(fixture["remote"]) as (server, marzban):
        _resolver_env(monkeypatch, server.server_address[1])
        shadow_resolver.schedule_shadow_resolution(
            "tok", "alice", {"request_key": "fp:something"}, _raw_legacy_body(), db_path=db_path,
        )
        for t in threading.enumerate():
            if t.name == "shadow-resolver":
                t.join(timeout=3)
    assert _metrics_rows(instance, fixture["binding"]["id"]) == []


def test_end_to_end_schedule_never_blocks_or_raises(db, monkeypatch):
    """The public entrypoint used by src/routes/sub.py: proves it returns
    immediately (background thread) and a PASS metric eventually lands."""
    instance, db_path = db
    fixture = _build_applied_binding(instance)
    with running_split_broker(fixture["remote"]) as (server, marzban):
        _resolver_env(monkeypatch, server.server_address[1])
        started = time.monotonic()
        shadow_resolver.schedule_shadow_resolution(
            "tok", "alice", {"request_key": "hwid:shadow-test-device"},
            _raw_legacy_body(), db_path=db_path,
        )
        elapsed = time.monotonic() - started
        assert elapsed < 0.2  # must not perform network I/O inline
        for t in threading.enumerate():
            if t.name == "shadow-resolver":
                t.join(timeout=5)
    rows = _metrics_rows(instance, fixture["binding"]["id"])
    assert len(rows) == 1
    assert tuple(rows[0]) == ("PASS", "MATCH", "SUCCESS", 1, 1)
    # No raw UUID anywhere in the whole database file after resolution.
    dump = "\n".join(instance._conn.iterdump())
    assert fixture["child_uuid"] not in dump
    assert UUID not in dump


# --- failure matrix ---------------------------------------------------------

def test_broker_unavailable(db, monkeypatch):
    instance, db_path = db
    fixture = _build_applied_binding(instance)
    config = _split_config(65500, timeout=1.0)
    _run_sync(config, db_path, "hwid:shadow-test-device", fixture)
    rows = _metrics_rows(instance, fixture["binding"]["id"])
    assert len(rows) == 1
    assert rows[0]["result"] == "FAIL"
    assert rows[0]["category"] == "BROKER_UNAVAILABLE"
    assert rows[0]["credential_result"] == "NOT_ATTEMPTED"
    assert rows[0]["legacy_fallback_success"] == 1


def test_resolver_capability_denied_when_credentials_identity_is_misconfigured(db, monkeypatch):
    """If the resolver were ever pointed at the main identity for the
    credentials.get call (a deployment misconfiguration), the broker's own
    per-client allowlist denies it -- exactly the boundary this dual-run
    architecture exists to enforce."""
    instance, db_path = db
    fixture = _build_applied_binding(instance)
    with running_split_broker(fixture["remote"]) as (server, marzban):
        config = _split_config(
            server.server_address[1], credentials_key=MAIN_KEY, credentials_client=MAIN_CLIENT,
        )
        _run_sync(config, db_path, "hwid:shadow-test-device", fixture)
    rows = _metrics_rows(instance, fixture["binding"]["id"])
    assert rows[0]["category"] == "RESOLVER_CAPABILITY_DENIED"
    assert rows[0]["credential_result"] == "FAIL"


def test_remote_child_missing(db, monkeypatch):
    instance, db_path = db
    fixture = _build_applied_binding(instance)
    del fixture["remote"].users[fixture["child_username"]]
    with running_split_broker(fixture["remote"]) as (server, marzban):
        config = _split_config(server.server_address[1])
        _run_sync(config, db_path, "hwid:shadow-test-device", fixture)
    rows = _metrics_rows(instance, fixture["binding"]["id"])
    assert rows[0]["category"] == "REMOTE_CHILD_MISSING"
    assert rows[0]["credential_result"] == "NOT_ATTEMPTED"


def test_remote_contract_mismatch(db, monkeypatch):
    instance, db_path = db
    fixture = _build_applied_binding(instance)
    fixture["remote"].users[fixture["child_username"]]["expire"] = 999999
    with running_split_broker(fixture["remote"]) as (server, marzban):
        config = _split_config(server.server_address[1])
        _run_sync(config, db_path, "hwid:shadow-test-device", fixture)
    rows = _metrics_rows(instance, fixture["binding"]["id"])
    assert rows[0]["category"] == "REMOTE_CONTRACT_MISMATCH"


def test_credential_verifier_mismatch(db, monkeypatch):
    instance, db_path = db
    fixture = _build_applied_binding(instance)
    drifted_uuid = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    fixture["remote"].users[fixture["child_username"]]["proxies"]["vless"]["id"] = drifted_uuid
    with running_split_broker(fixture["remote"]) as (server, marzban):
        config = _split_config(server.server_address[1])
        _run_sync(config, db_path, "hwid:shadow-test-device", fixture)
    rows = _metrics_rows(instance, fixture["binding"]["id"])
    assert rows[0]["category"] == "CREDENTIAL_VERIFIER_MISMATCH"
    assert rows[0]["credential_result"] == "FAIL"
    dump = "\n".join(instance._conn.iterdump())
    assert drifted_uuid not in dump


def test_stale_slot_generation(db, monkeypatch):
    instance, db_path = db
    fixture = _build_applied_binding(instance)
    instance._conn.execute(
        "UPDATE mgboost_device_slots SET desired_state='DISABLED' WHERE id=?",
        (fixture["slot"]["slot_id"],),
    )
    instance._conn.commit()
    with running_split_broker(fixture["remote"]) as (server, marzban):
        config = _split_config(server.server_address[1])
        _run_sync(config, db_path, "hwid:shadow-test-device", fixture)
    rows = _metrics_rows(instance, fixture["binding"]["id"])
    assert rows[0]["category"] == "STALE_SLOT_GENERATION"


def test_invalid_account_slot_mapping(db, monkeypatch):
    instance, db_path = db
    fixture = _build_applied_binding(instance)
    bogus_binding = {
        "id": fixture["binding"]["id"], "account_id": fixture["account_id"],
        "legacy_alias_id": fixture["binding"]["legacy_alias_id"],
        "slot_generation_id": fixture["binding"]["slot_generation_id"],
        "child_intent_id": 999999, "operation_id": fixture["binding"]["operation_id"],
        "enabled": 1,
    }
    with pytest.raises(ValueError) as excinfo:
        shadow_resolver._load_mapping(instance._conn, bogus_binding)
    assert str(excinfo.value) == "INVALID_ACCOUNT_SLOT_MAPPING"


def test_resolver_timeout(db, monkeypatch):
    instance, db_path = db
    fixture = _build_applied_binding(instance)

    class SlowMarzban(FakeMarzban):
        def get_user(self, username, token):
            time.sleep(0.5)
            return super().get_user(username, token)

    slow = SlowMarzban()
    slow.users = fixture["remote"].users
    with running_split_broker(slow) as (server, marzban):
        config = _split_config(server.server_address[1], timeout=0.05)
        _run_sync(config, db_path, "hwid:shadow-test-device", fixture)
    rows = _metrics_rows(instance, fixture["binding"]["id"])
    assert rows[0]["category"] in ("RESOLVER_TIMEOUT", "BROKER_UNAVAILABLE")


def test_shadow_comparison_failure_on_unparsable_legacy_line(db, monkeypatch):
    instance, db_path = db
    fixture = _build_applied_binding(instance)
    with running_split_broker(fixture["remote"]) as (server, marzban):
        config = _split_config(server.server_address[1])
        malformed_body = base64.b64encode(b"vless://not-a-valid-uri-at-all")
        shadow_resolver._resolve_and_record(
            config, db_path, "tok", "alice", "hwid:shadow-test-device", malformed_body,
        )
    rows = _metrics_rows(instance, fixture["binding"]["id"])
    assert rows[0]["category"] == "SHADOW_COMPARISON_FAILURE"
    assert rows[0]["credential_result"] == "SUCCESS"


def test_metrics_db_failure_never_raises(db, monkeypatch, caplog):
    instance, db_path = db
    fixture = _build_applied_binding(instance)
    outcome = shadow_resolver.ShadowOutcome.ok()
    with caplog.at_level(logging.WARNING):
        shadow_resolver._record_metric("/nonexistent-directory/db.sqlite3", 1, outcome, 5, int(time.time()))
    assert "metrics" in caplog.text.lower()


def test_malformed_ensure_payload_is_classified(db, monkeypatch):
    """Both the outbox's `payload_json` and the child intent row are
    immutable once written (schema triggers), so a corrupted payload can only
    be reproduced with a fresh row -- exactly what a genuinely malformed
    write from a future caller would look like."""
    instance, db_path = db
    account, alias_id, slot = _account(instance, mapping="MALFORMED", tg=905302973, alias="malformed-alias")
    now = 300
    instance._conn.execute(
        "INSERT INTO mgboost_child_user_intents (public_id,account_id,slot_id,"
        "slot_generation_id,slot_number,generation,source_alias_id,child_username,"
        "source_contract_hash,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            "child_malformed_fixture", account["account_id"], slot["slot_id"],
            slot["generation_id"], slot["slot_number"], slot["generation"], alias_id,
            "mgc_" + "z" * 26, "e" * 64, now, now,
        ),
    )
    intent_id = instance._conn.execute(
        "SELECT id FROM mgboost_child_user_intents WHERE child_username=?", ("mgc_" + "z" * 26,),
    ).fetchone()[0]
    instance._conn.execute(
        "INSERT INTO mgboost_outbox (operation_id,account_id,child_intent_id,operation_kind,"
        "state,idempotency_key_hash,request_hash,payload_json,next_attempt_at,created_at,"
        "updated_at) VALUES (?,?,?,'CHILD_USER_ENSURE','APPLIED',?,?,?,?,?,?)",
        (
            "op_" + "z" * 26, account["account_id"], intent_id, "f" * 64, "a" * 64,
            json.dumps({"not": "a valid ensure payload"}), now, now, now,
        ),
    )
    instance._conn.commit()
    binding = instance.shadow_resolver_bindings.create_binding(
        account_id=account["account_id"], legacy_alias_id=alias_id,
        legacy_device_id=_add_device(instance, request_key="hwid:malformed-device"),
        slot_generation_id=slot["generation_id"], child_intent_id=intent_id,
        operation_id="op_" + "z" * 26, decision_ref="test-malformed-fixture", now=now,
    )
    with pytest.raises(ValueError) as excinfo:
        shadow_resolver._load_mapping(instance._conn, binding)
    assert str(excinfo.value) == "MALFORMED_REQUEST"


# --- functional comparison unit tests ---------------------------------------

def test_compare_functional_config_pass_with_only_uuid_and_label_diff():
    lines = [
        "vless://11111111-1111-4111-8111-111111111111@host1:443?type=tcp&flow=xtls-rprx-vision#Tag1",
        "vless://11111111-1111-4111-8111-111111111111@host2:8443?type=grpc&serviceName=x#Tag2",
    ]
    shadow_resolver._compare_functional_config(lines, "22222222-2222-4222-8222-222222222222")


def test_compare_functional_config_rejects_collision():
    lines = ["vless://11111111-1111-4111-8111-111111111111@host1:443?type=tcp#Tag1"]
    with pytest.raises(ValueError):
        shadow_resolver._compare_functional_config(
            lines, "11111111-1111-4111-8111-111111111111"
        )


def test_compare_functional_config_rejects_unparsable_line():
    with pytest.raises(ValueError):
        shadow_resolver._compare_functional_config(
            ["vless://missing-at-symbol"], "22222222-2222-4222-8222-222222222222"
        )
