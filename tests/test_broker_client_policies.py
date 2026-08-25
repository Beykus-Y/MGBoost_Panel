"""PH3-03 shadow resolver broker capability boundary.

`mgboost-main` must lose `child.user.credentials.get`; only a dedicated
`mgboost-sub-resolver` identity, authenticated with its own separate shared
key, may call it. Every other legacy/child operation must remain reachable
by `mgboost-main` exactly as before this split.
"""

import threading
from contextlib import contextmanager

import pytest

from src.broker_operations import BrokerOperations
from src.broker_protocol import BROKER_OPERATIONS
from src.broker_server import BrokerApplication, build_broker_server
from src.child_contract import derive_child_username, derive_operation_id, source_contract_hash
from src.service_marzban import BrokerTransport

from tests.test_marzban_broker import FakeMarzban, UUID


MAIN_KEY = "main-client-key-with-at-least-32-bytes!"
RESOLVER_KEY = "resolver-client-key-with-at-least-32-b"
MAIN_CLIENT = "mgboost-main"
RESOLVER_CLIENT = "mgboost-sub-resolver"


def _policies():
    return {
        MAIN_CLIENT: {
            "shared_key": MAIN_KEY,
            "allowed_operations": BROKER_OPERATIONS - {"child.user.credentials.get"},
        },
        RESOLVER_CLIENT: {
            "shared_key": RESOLVER_KEY,
            "allowed_operations": {"child.user.credentials.get"},
        },
    }


@contextmanager
def running_split_broker(marzban=None):
    marzban = marzban or FakeMarzban()
    app = BrokerApplication(
        BrokerOperations(marzban, clock=lambda: 1_000),
        shared_key=MAIN_KEY,
        client_id=MAIN_CLIENT,
        allowed_skew_seconds=30,
        client_policies=_policies(),
    )
    server = build_broker_server("127.0.0.1", 0, app, max_workers=4)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, marzban
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def _credentials_request(marzban):
    child_username = derive_child_username(
        "acct_" + "a" * 32, 1, 1
    )
    operation_id = derive_operation_id(child_username)
    from src.child_contract import credential_verifier

    child_uuid = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    marzban.users[child_username] = {
        "username": child_username,
        "expire": 0,
        "status": "active",
        "proxies": {"vless": {"id": child_uuid, "flow": ""}},
        "inbounds": {"vless": ["LEGACY"]},
        "data_limit": None,
    }
    contract_hash = source_contract_hash(marzban.users[child_username])
    return {
        "operation_id": operation_id,
        "child_username": child_username,
        "source_contract_hash": contract_hash,
        "expire": 0,
        "uuid_verifier": credential_verifier(child_uuid),
    }


def test_main_client_can_no_longer_read_child_credentials():
    with running_split_broker() as (server, marzban):
        port = server.server_address[1]
        payload = _credentials_request(marzban)
        client = BrokerTransport(f"http://127.0.0.1:{port}", MAIN_KEY, client_id=MAIN_CLIENT)
        from urllib.error import HTTPError
        with pytest.raises(HTTPError) as excinfo:
            client.call("child.user.credentials.get", payload)
        assert excinfo.value.code == 403


def test_resolver_client_can_only_read_child_credentials():
    with running_split_broker() as (server, marzban):
        port = server.server_address[1]
        payload = _credentials_request(marzban)
        resolver = BrokerTransport(f"http://127.0.0.1:{port}", RESOLVER_KEY, client_id=RESOLVER_CLIENT)
        result = resolver.call("child.user.credentials.get", payload)
        assert result["credentials"]["vless_uuid"] == "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"

        from urllib.error import HTTPError
        with pytest.raises(HTTPError) as excinfo:
            resolver.call("legacy.user.get", {"username": "alice"})
        assert excinfo.value.code == 403


def test_main_client_keeps_every_other_legacy_and_child_operation():
    with running_split_broker() as (server, marzban):
        port = server.server_address[1]
        client = BrokerTransport(f"http://127.0.0.1:{port}", MAIN_KEY, client_id=MAIN_CLIENT)
        user = client.call("legacy.user.get", {"username": "alice"})
        assert user["proxies"]["vless"]["id"] == UUID


def test_unknown_client_identity_is_rejected():
    with running_split_broker() as (server, marzban):
        port = server.server_address[1]
        stranger = BrokerTransport(f"http://127.0.0.1:{port}", MAIN_KEY, client_id="mgboost-stranger")
        from urllib.error import HTTPError
        with pytest.raises(HTTPError) as excinfo:
            stranger.call("legacy.user.get", {"username": "alice"})
        assert excinfo.value.code == 401


def test_client_policies_reject_unknown_operation_at_construction():
    with pytest.raises(ValueError):
        BrokerApplication(
            BrokerOperations(FakeMarzban(), clock=lambda: 1_000),
            shared_key=MAIN_KEY,
            client_id=MAIN_CLIENT,
            client_policies={
                MAIN_CLIENT: {"shared_key": MAIN_KEY, "allowed_operations": {"not.a.real.operation"}},
            },
        )


def test_without_policies_the_single_legacy_client_is_unaffected():
    """Backward compatibility: omitting client_policies (production default
    before MARZBAN_BROKER_RESOLVER_AUTH_KEY is configured) must behave
    exactly like the pre-PH3-03 single-client broker."""
    with __import__("tests.test_marzban_broker", fromlist=["running_broker"]).running_broker() as (
        server, marzban,
    ):
        port = server.server_address[1]
        client = BrokerTransport(f"http://127.0.0.1:{port}", "broker-test-key-with-at-least-32-bytes")
        user = client.call("legacy.user.get", {"username": "alice"})
        assert user["username"] == "alice"
