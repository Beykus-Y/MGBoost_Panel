"""PH1-05 typed broker unit, transport, outage, restart and rollback contracts."""

import io
import json
import os
import socket
import subprocess
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import pytest

from src.broker_operations import BrokerOperations
from src.broker_protocol import (
    BROKER_CLIENT_HEADER,
    BROKER_NONCE_HEADER,
    BROKER_OPERATIONS,
    LEGACY_BROKER_OPERATIONS,
    BROKER_SIGNATURE_HEADER,
    BROKER_TIMESTAMP_HEADER,
    build_broker_signature,
    validate_loopback_host,
    validate_loopback_url,
)
from src.broker_server import BrokerApplication, build_broker_server
from src.service_marzban import BrokerTransport, ServiceMarzbanClient
from src.child_contract import (
    credential_verifier, derive_child_username, derive_operation_id,
    source_contract_hash,
)
from src.shadowsocks_retirement import retirement_snapshot


AUTH_KEY = "broker-test-key-with-at-least-32-bytes"
CLIENT_ID = "mgboost-main"
UUID = "12345678-1234-4234-9234-123456789abc"


class FakeMarzban:
    def __init__(self):
        self.calls = []
        self.fail_modify = None
        self.outage = False
        self.users = {
            "alice": {
                "username": "alice",
                "expire": 2_000,
                "status": "active",
                "proxies": {"vless": {"id": UUID}},
                "inbounds": {"vless": ["LEGACY"]},
                "data_limit": None,
            }
        }

    def _available(self):
        if self.outage:
            raise URLError("marzban down")

    def get_admin_token_from_env(self):
        self._available()
        return "sudo-token-only-inside-broker"

    def get_user(self, username, token):
        self._available()
        self.calls.append(("get_user", username, token))
        if username not in self.users:
            raise HTTPError("http://marzban/api/user", 404, "missing", {}, io.BytesIO(b'{"detail":"User not found"}'))
        return json.loads(json.dumps(self.users[username]))

    def get_user_usage(self, username, token, start="", end=""):
        self._available()
        self.calls.append(("get_user_usage", username, token, start, end))
        return {"username": username, "usages": [], "start": start, "end": end}

    def get_users(self, token, limit=100, offset=0):
        self._available()
        self.calls.append(("get_users", token, limit, offset))
        return {"users": list(self.users.values()), "total": len(self.users)}

    def get_nodes(self, token):
        self._available()
        self.calls.append(("get_nodes", token))
        return [{"id": 1, "name": "node", "status": "connected"}]

    def get_nodes_usage(self, token, start="", end=""):
        self._available()
        self.calls.append(("get_nodes_usage", token, start, end))
        return {"usages": [], "start": start, "end": end}

    def get_inbounds(self, token):
        self._available()
        self.calls.append(("get_inbounds", token))
        return {"vless": [{"tag": "LEGACY"}]}

    def create_user(self, payload, token):
        self._available()
        self.calls.append(("create_user", json.loads(json.dumps(payload)), token))
        user = json.loads(json.dumps(payload))
        user.setdefault("expire", 0)
        if "vless" in user["proxies"] and not user["proxies"]["vless"].get("id"):
            user["proxies"]["vless"]["id"] = str(
                uuid.uuid5(uuid.NAMESPACE_URL, "fake-marzban:" + user["username"])
            )
        if "shadowsocks" in user["proxies"] and not user["proxies"]["shadowsocks"].get("password"):
            user["proxies"]["shadowsocks"]["password"] = "fresh-fake-password-" + user["username"]
        self.users[user["username"]] = user
        return json.loads(json.dumps(user))

    def modify_user(self, username, payload, token):
        self._available()
        self.calls.append(("modify_user", username, dict(payload), token))
        if self.fail_modify:
            raise self.fail_modify
        self.users[username].update(payload)
        return json.loads(json.dumps(self.users[username]))

    def delete_user(self, username, token):
        self._available()
        self.calls.append(("delete_user", username, token))
        self.users.pop(username, None)
        return {}


@contextmanager
def running_broker(marzban=None, *, key=AUTH_KEY, port=0):
    marzban = marzban or FakeMarzban()
    app = BrokerApplication(
        BrokerOperations(marzban, clock=lambda: 1_000),
        shared_key=key,
        client_id=CLIENT_ID,
        allowed_skew_seconds=30,
    )
    server = build_broker_server("127.0.0.1", port, app, max_workers=4)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, marzban
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def broker_client(port, *, key=AUTH_KEY):
    return ServiceMarzbanClient(
        mode="broker",
        broker_url=f"http://127.0.0.1:{port}",
        broker_key=key,
        broker_client_id=CLIENT_ID,
        broker_timeout=1,
        direct_client=FakePublicMarzban(),
    )


class FakePublicMarzban:
    def __init__(self):
        self.public_calls = []

    def get_sub(self, token, extra_headers=None):
        self.public_calls.append(("sub", token, dict(extra_headers or {})))
        return b"legacy-config", {"profile-title": "legacy"}

    def get_username_for_token(self, token):
        self.public_calls.append(("info", token))
        return "alice"


def test_all_ten_operations_are_explicit_and_preserve_legacy_payload_semantics():
    marzban = FakeMarzban()
    operations = BrokerOperations(marzban, clock=lambda: 1_000)
    seen = set()

    user = operations.dispatch("legacy.user.get", {"username": "alice"})
    seen.add("legacy.user.get")
    assert user["proxies"]["vless"]["id"] == UUID

    usage = operations.dispatch(
        "legacy.user.usage", {"username": "alice", "start": "a", "end": "b"}
    )
    seen.add("legacy.user.usage")
    assert usage["start"] == "a" and usage["end"] == "b"

    assert operations.dispatch("legacy.users.list", {"limit": 10, "offset": 0})["total"] == 1
    seen.add("legacy.users.list")
    assert operations.dispatch("legacy.nodes.list", {})[0]["id"] == 1
    seen.add("legacy.nodes.list")
    assert operations.dispatch("legacy.nodes.usage", {"start": "", "end": ""})["usages"] == []
    seen.add("legacy.nodes.usage")
    assert "vless" in operations.dispatch("legacy.inbounds.list", {})
    seen.add("legacy.inbounds.list")

    create_payload = {
        "username": "bob",
        "proxies": {"vless": {}},
        "inbounds": {"vless": ["LEGACY"]},
        "expire": 3_000,
        "data_limit": 123,
        "data_limit_reset_strategy": "no_reset",
        "note": "manual",
        "status": "active",
    }
    created = operations.dispatch("legacy.user.create", {"user": create_payload})
    seen.add("legacy.user.create")
    assert created["expire"] == 3_000 and created["data_limit"] == 123

    renewed = operations.dispatch(
        "legacy.user.renew", {"username": "alice", "renewal": {"add_days": 7}}
    )
    seen.add("legacy.user.renew")
    assert renewed["expire"] == 2_000 + 7 * 86400
    for field in ("proxies", "inbounds", "data_limit", "status"):
        assert renewed[field] == user[field]

    set_expire = operations.dispatch(
        "legacy.user.set_expire", {"username": "alice", "expire": 9_999}
    )
    seen.add("legacy.user.set_expire")
    assert set_expire["expire"] == 9_999
    assert marzban.calls[-1][0:3] == ("modify_user", "alice", {"expire": 9_999})

    assert operations.dispatch("legacy.user.delete", {"username": "bob"}) == {}
    seen.add("legacy.user.delete")
    assert seen == LEGACY_BROKER_OPERATIONS
    assert BROKER_OPERATIONS == LEGACY_BROKER_OPERATIONS | {
        "child.user.ensure", "child.user.credentials.get",
        "child.user.observe", "child.user.revoke",
        "maintenance.user.retire_shadowsocks",
    }


def test_child_observe_is_read_only_and_classifies_absent_match_and_mismatch():
    marzban = FakeMarzban()
    operations = BrokerOperations(marzban)
    child_username = derive_child_username("acct_test-observe", 1, 1)
    request = {
        "operation_id": derive_operation_id(child_username),
        "child_username": child_username,
        "source_username": "alice",
        "source_contract_hash": source_contract_hash(marzban.users["alice"]),
        "expire": 0,
    }
    assert operations.dispatch("child.user.observe", request) == {"presence": "ABSENT"}
    created = operations.dispatch("child.user.ensure", request)
    assert created["outcome"] == "CREATED"
    call_count = len(marzban.calls)
    observed = operations.dispatch("child.user.observe", request)
    assert observed["presence"] == "MATCH"
    assert observed["uuid"] == created["uuid"]
    assert not any(call[0] in {"create_user", "modify_user", "delete_user"}
                   for call in marzban.calls[call_count:])
    marzban.users[child_username]["expire"] = 123
    mismatch = operations.dispatch("child.user.observe", request)
    assert mismatch == {
        "presence": "MISMATCH", "mismatch_code": "REMOTE_CONTRACT_MISMATCH"
    }


def _legacy_shadowsocks_user():
    return {
        "username": "alice",
        "expire": 2_000,
        "status": "active",
        "proxies": {
            "vless": {"id": UUID, "flow": "xtls-rprx-vision"},
            "shadowsocks": {"method": "aes-128-gcm", "password": "retired-secret"},
        },
        "inbounds": {"vless": ["LEGACY"], "shadowsocks": []},
        "data_limit": None,
        "data_limit_reset_strategy": "no_reset",
        "subscription_url": "/sub/synthetic-test-bearer",
        "links": [f"vless://{UUID}@vpn.invalid:443?sid=random#LEGACY"],
    }


def test_typed_shadowsocks_retirement_is_narrow_and_idempotent():
    marzban = FakeMarzban()
    marzban.users["alice"] = _legacy_shadowsocks_user()
    operations = BrokerOperations(marzban)
    before = json.loads(json.dumps(marzban.users["alice"]))
    request = {
        "username": "alice",
        "expected_state_digest": retirement_snapshot(before)["state_digest"],
    }

    removed = operations.dispatch("maintenance.user.retire_shadowsocks", request)
    assert removed["outcome"] == "REMOVED"
    assert removed["proxy_types"] == ["vless"]
    assert UUID not in json.dumps(removed)
    modify = [call for call in marzban.calls if call[0] == "modify_user"]
    assert modify == [(
        "modify_user", "alice",
        {"proxies": {"vless": {"id": UUID, "flow": "xtls-rprx-vision"}}},
        "sudo-token-only-inside-broker",
    )]
    after = marzban.users["alice"]
    for field in ("expire", "status", "inbounds", "data_limit", "subscription_url", "links"):
        assert after[field] == before[field]
    assert after["proxies"] == {"vless": before["proxies"]["vless"]}

    retry = {
        "username": "alice",
        "expected_state_digest": retirement_snapshot(after)["state_digest"],
    }
    assert operations.dispatch(
        "maintenance.user.retire_shadowsocks", retry
    )["outcome"] == "UNCHANGED"
    assert len([call for call in marzban.calls if call[0] == "modify_user"]) == 1


def test_shadowsocks_retirement_rejects_topology_stale_inventory_and_payload_injection():
    marzban = FakeMarzban()
    marzban.users["alice"] = _legacy_shadowsocks_user()
    operations = BrokerOperations(marzban)
    digest = retirement_snapshot(marzban.users["alice"])["state_digest"]
    with pytest.raises(ValueError, match="request"):
        operations.dispatch("maintenance.user.retire_shadowsocks", {
            "username": "alice", "expected_state_digest": digest,
            "proxies": {"vless": {}},
        })
    with pytest.raises(ValueError, match="changed after"):
        operations.dispatch("maintenance.user.retire_shadowsocks", {
            "username": "alice", "expected_state_digest": "0" * 64,
        })
    marzban.get_inbounds = lambda _token: {
        "vless": [{"tag": "LEGACY"}], "shadowsocks": [{"tag": "SS"}],
    }
    with pytest.raises(ValueError, match="topology"):
        operations.dispatch("maintenance.user.retire_shadowsocks", {
            "username": "alice", "expected_state_digest": digest,
        })
    assert not [call for call in marzban.calls if call[0] == "modify_user"]


def test_shadowsocks_retirement_repairs_unexpected_functional_drift_and_stops():
    class DriftingMarzban(FakeMarzban):
        def __init__(self):
            super().__init__()
            self.first = True

        def modify_user(self, username, payload, token):
            result = super().modify_user(username, payload, token)
            if self.first:
                self.first = False
                self.users[username]["expire"] = 9_999
            return result

    marzban = DriftingMarzban()
    marzban.users["alice"] = _legacy_shadowsocks_user()
    before = json.loads(json.dumps(marzban.users["alice"]))
    request = {
        "username": "alice",
        "expected_state_digest": retirement_snapshot(before)["state_digest"],
    }
    with pytest.raises(ValueError, match="repaired; rollout stopped"):
        BrokerOperations(marzban).dispatch(
            "maintenance.user.retire_shadowsocks", request
        )
    assert marzban.users["alice"]["expire"] == before["expire"]
    assert marzban.users["alice"]["proxies"] == {
        "vless": before["proxies"]["vless"]
    }


def _child_request(source, *, account="acct_example", slot=1, generation=1, expire=0):
    username = derive_child_username(account, slot, generation)
    return {
        "operation_id": derive_operation_id(username),
        "child_username": username,
        "source_username": source["username"],
        "source_contract_hash": source_contract_hash(source),
        "expire": expire,
    }


def test_child_ensure_creates_fresh_uuid_then_converges_to_existing():
    marzban = FakeMarzban()
    operations = BrokerOperations(marzban)
    source = marzban.users["alice"]
    request = _child_request(source, expire=7_777)

    created = operations.dispatch("child.user.ensure", request)
    existing = operations.dispatch("child.user.ensure", request)

    assert created["outcome"] == "CREATED"
    assert existing["outcome"] == "EXISTING"
    assert created["uuid"] == existing["uuid"]
    assert created["uuid"] != UUID
    remote = marzban.users[request["child_username"]]
    assert remote["expire"] == 7_777
    assert remote["inbounds"] == source["inbounds"]
    assert remote["data_limit"] is None
    assert remote["proxies"]["vless"]["id"] == created["uuid"]
    creates = [call for call in marzban.calls if call[0] == "create_user"]
    assert len(creates) == 1


def test_child_ensure_is_vless_only_and_typed_reread_validates_uuid():
    marzban = FakeMarzban()
    operations = BrokerOperations(marzban)
    marzban.users["alice"]["proxies"]["vless"]["flow"] = "xtls-rprx-vision"
    source = json.loads(json.dumps(marzban.users["alice"]))
    request = _child_request(source)

    result = operations.dispatch("child.user.ensure", request)

    remote = marzban.users[request["child_username"]]
    assert result["protocols"] == ["vless"]
    assert remote["proxies"]["vless"]["flow"] == "xtls-rprx-vision"
    assert remote["proxies"]["vless"]["id"] != source["proxies"]["vless"]["id"]
    assert set(remote["proxies"]) == {"vless"}

    reread_request = {
        "operation_id": request["operation_id"],
        "child_username": request["child_username"],
        "source_contract_hash": request["source_contract_hash"],
        "expire": 0,
        "uuid_verifier": credential_verifier(result["uuid"]),
    }
    reread = operations.dispatch("child.user.credentials.get", reread_request)
    assert reread["credentials"] == {"vless_uuid": result["uuid"]}
    with pytest.raises(ValueError, match="verifier mismatch"):
        operations.dispatch(
            "child.user.credentials.get",
            {**reread_request, "uuid_verifier": "sha256:" + "0" * 64},
        )
    marzban.users[request["child_username"]]["expire"] = 9
    with pytest.raises(ValueError, match="expiry drift"):
        operations.dispatch("child.user.credentials.get", reread_request)


def test_child_ensure_rejects_retired_shadowsocks_source_metadata():
    marzban = FakeMarzban()
    marzban.users["alice"] = _legacy_shadowsocks_user()
    with pytest.raises(ValueError, match="VLESS-only"):
        _child_request(marzban.users["alice"])


def test_child_ensure_rejects_contract_drift_arbitrary_fields_and_uuid_reuse():
    marzban = FakeMarzban()
    operations = BrokerOperations(marzban)
    request = _child_request(marzban.users["alice"])
    with pytest.raises(ValueError, match="fields"):
        operations.dispatch("child.user.ensure", {**request, "proxies": {}})
    with pytest.raises(ValueError, match="contract changed"):
        operations.dispatch(
            "child.user.ensure", {**request, "source_contract_hash": "0" * 64}
        )
    marzban.users[request["child_username"]] = {
        "username": request["child_username"], "expire": 0, "status": "active",
        "proxies": {"vless": {"id": UUID}},
        "inbounds": {"vless": ["LEGACY"]}, "data_limit": None,
    }
    with pytest.raises(ValueError, match="must differ"):
        operations.dispatch("child.user.ensure", request)


def test_child_ensure_typed_http_and_direct_comparison():
    source = FakeMarzban().users["alice"]
    request = _child_request(source, account="acct_compare", expire=8_888)
    with running_broker() as (server, broker_remote):
        broker_result = broker_client(server.server_address[1]).ensure_child_user(request)
    direct_remote = FakeMarzban()
    direct_result = ServiceMarzbanClient(
        mode="direct", direct_client=direct_remote
    ).ensure_child_user(request)
    assert broker_result == direct_result
    assert broker_remote.users[request["child_username"]] == direct_remote.users[
        request["child_username"]
    ]


@pytest.mark.parametrize(
    "operation,payload",
    [
        ("legacy.user.get", {"username": "alice", "url": "http://evil"}),
        ("legacy.nodes.list", {"path": "/api/admins"}),
        ("legacy.user.create", {"user": {"username": "x", "is_sudo": True}}),
        ("legacy.user.renew", {"username": "alice", "renewal": {"add_days": 1, "proxies": {}}}),
        ("legacy.user.set_expire", {"username": "alice", "expire": 5, "inbounds": {}}),
    ],
)
def test_typed_operations_reject_unknown_or_privileged_fields(operation, payload):
    with pytest.raises(ValueError):
        BrokerOperations(FakeMarzban()).dispatch(operation, payload)


@pytest.mark.parametrize("value", ["0.0.0.0", "192.168.1.2", "localhost", "example.com"])
def test_broker_listener_rejects_non_literal_or_non_loopback_hosts(value):
    with pytest.raises(ValueError):
        validate_loopback_host(value)


@pytest.mark.parametrize(
    "value",
    [
        "http://localhost:8002",
        "http://0.0.0.0:8002",
        "https://127.0.0.1:8002",
        "http://127.0.0.1:8002/path",
        "http://user:pass@127.0.0.1:8002",
    ],
)
def test_main_client_rejects_non_loopback_or_ambiguous_broker_urls(value):
    with pytest.raises(ValueError):
        validate_loopback_url(value)


def test_authenticated_http_transport_and_replay_protection():
    with running_broker() as (server, _marzban):
        port = server.server_address[1]
        transport = BrokerTransport(
            f"http://127.0.0.1:{port}", AUTH_KEY, client_id=CLIENT_ID, timeout=1
        )
        assert transport.call("legacy.user.get", {"username": "alice"})["username"] == "alice"

        path = "/v1/operations/legacy.nodes.list"
        body = b"{}"
        timestamp = str(int(time.time()))
        nonce = "fixed_nonce_123456789"
        signature = build_broker_signature(
            AUTH_KEY, "POST", path, timestamp, nonce, CLIENT_ID, body
        )
        headers = {
            "Content-Type": "application/json",
            BROKER_CLIENT_HEADER: CLIENT_ID,
            BROKER_TIMESTAMP_HEADER: timestamp,
            BROKER_NONCE_HEADER: nonce,
            BROKER_SIGNATURE_HEADER: signature,
        }
        first = urlopen(Request(f"http://127.0.0.1:{port}{path}", data=body, headers=headers), timeout=1)
        assert first.status == 200
        with pytest.raises(HTTPError) as replay:
            urlopen(Request(f"http://127.0.0.1:{port}{path}", data=body, headers=headers), timeout=1)
        assert replay.value.code == 409


def test_local_http_without_valid_hmac_cannot_use_sudo_or_generic_proxy():
    with running_broker() as (server, marzban):
        port = server.server_address[1]
        wrong = BrokerTransport(
            f"http://127.0.0.1:{port}", "wrong-key-that-is-still-at-least-32-bytes",
            client_id=CLIENT_ID, timeout=1,
        )
        with pytest.raises(HTTPError) as unauthorized:
            wrong.call("legacy.user.get", {"username": "alice"})
        assert unauthorized.value.code == 403
        assert marzban.calls == []

        valid = BrokerTransport(
            f"http://127.0.0.1:{port}", AUTH_KEY, client_id=CLIENT_ID, timeout=1
        )
        with pytest.raises(HTTPError) as generic:
            valid.call("legacy.arbitrary.proxy", {"path": "/api/admins"})
        assert generic.value.code == 404


def _unused_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def test_broker_outage_does_not_break_public_legacy_subscription_path():
    direct = FakePublicMarzban()
    client = ServiceMarzbanClient(
        mode="broker",
        broker_url=f"http://127.0.0.1:{_unused_port()}",
        broker_key=AUTH_KEY,
        broker_timeout=0.2,
        direct_client=direct,
    )
    assert client.get_sub("same-token", {"User-Agent": "Happ"}) == (
        b"legacy-config", {"profile-title": "legacy"}
    )
    assert client.get_username_for_token("same-token") == "alice"
    with pytest.raises(URLError):
        client.get_user("alice", client.get_admin_token_from_env())
    assert direct.public_calls == [
        ("sub", "same-token", {"User-Agent": "Happ"}),
        ("info", "same-token"),
    ]


def test_broker_restart_recovers_without_recreating_client_or_credentials():
    marzban = FakeMarzban()
    port = _unused_port()
    client = broker_client(port)
    with running_broker(marzban, port=port):
        assert client.get_user("alice", client.get_admin_token_from_env())["expire"] == 2_000
    with pytest.raises(URLError):
        client.get_user("alice", client.get_admin_token_from_env())
    with running_broker(marzban, port=port):
        user = client.get_user("alice", client.get_admin_token_from_env())
        assert user["proxies"]["vless"]["id"] == UUID


def test_marzban_outage_and_partial_renewal_fail_without_false_success_then_retry():
    marzban = FakeMarzban()
    with running_broker(marzban) as (server, _):
        client = broker_client(server.server_address[1])
        token = client.get_admin_token_from_env()
        marzban.outage = True
        with pytest.raises(HTTPError) as outage:
            client.get_user("alice", token)
        assert outage.value.code == 503
        marzban.outage = False

        before = json.loads(json.dumps(marzban.users["alice"]))
        marzban.fail_modify = ConnectionError("write failed before effect")
        with pytest.raises(HTTPError) as partial:
            client.renew_user("alice", {"add_days": 7}, token)
        assert partial.value.code == 503
        assert marzban.users["alice"] == before

        marzban.fail_modify = None
        renewed = client.renew_user("alice", {"add_days": 7}, token)
        assert renewed["expire"] == before["expire"] + 7 * 86400
        assert renewed["proxies"] == before["proxies"]


def test_fil_in_create_renew_delete_end_to_end_through_broker(monkeypatch):
    from src.routes import internal
    from tests.test_phase1_legacy_compat import _BodyHandler

    marzban = FakeMarzban()
    with running_broker(marzban) as (server, _):
        client = broker_client(server.server_address[1])
        monkeypatch.setattr(internal, "_client", client)
        create_payload = {
            "username": "filin-user",
            "proxies": {"vless": {}},
            "inbounds": {"vless": ["LEGACY"]},
            "expire": 4_000,
            "data_limit": 100,
            "data_limit_reset_strategy": "no_reset",
            "status": "active",
        }
        create = _BodyHandler(json.dumps(create_payload).encode())
        internal.handle_internal_user_create(create)
        assert create.status == 201

        renew = _BodyHandler(json.dumps({"add_days": 30, "data_limit": 0}).encode())
        internal.handle_internal_user_renew(renew, "filin-user")
        assert renew.status == 200
        assert marzban.users["filin-user"]["expire"] == 4_000 + 30 * 86400
        assert marzban.users["filin-user"]["data_limit"] is None

        delete = _BodyHandler()
        internal.handle_internal_user_delete(delete, "filin-user")
        assert delete.status == 200
        assert "filin-user" not in marzban.users


def test_direct_mode_is_an_explicit_payload_compatible_rollback():
    direct = FakeMarzban()
    client = ServiceMarzbanClient(mode="direct", direct_client=direct)
    token = client.get_admin_token_from_env()
    before = json.loads(json.dumps(direct.users["alice"]))

    result = client.modify_user("alice", {"expire": 8_888}, token)

    assert result["expire"] == 8_888
    assert result["proxies"] == before["proxies"]
    assert result["inbounds"] == before["inbounds"]
    assert result["data_limit"] == before["data_limit"]


def test_main_broker_mode_rejects_sudo_credentials_in_its_environment(monkeypatch):
    monkeypatch.setenv("MARZBAN_ADMIN_USER", "must-not-be-here")
    monkeypatch.setenv("MARZBAN_ADMIN_PASS", "must-not-be-here")
    client = ServiceMarzbanClient(
        mode="broker", broker_url="http://127.0.0.1:8002", broker_key=AUTH_KEY
    )
    with pytest.raises(RuntimeError, match="must not be present"):
        client.assert_credential_boundary()


def test_broker_startup_config_does_not_read_main_dotenv(tmp_path):
    repository = Path(__file__).resolve().parents[1]
    dotenv = tmp_path / ".env"
    dotenv.write_text("MARZBAN_URL=http://should-not-be-read.invalid\n")
    dotenv.chmod(0)
    environment = os.environ.copy()
    environment.update({
        "PYTHONPATH": str(repository),
        "MGBOOST_SKIP_DOTENV": "1",
        "MARZBAN_URL": "http://127.0.0.1:8000",
    })

    result = subprocess.run(
        [sys.executable, "-c", "import src.config; print(src.config.MARZBAN_URL)"],
        cwd=tmp_path,
        env=environment,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "http://127.0.0.1:8000"
    unit = (repository / "mgboost-marzban-broker.service").read_text()
    assert "Environment=MGBOOST_SKIP_DOTENV=1" in unit
