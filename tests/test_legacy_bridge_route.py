"""PH4-01 route-level: legacy /sub is byte-identical when LEGACY_BRIDGE_ENABLED
is off (the production default), and a bridged device's response never
contains the shared legacy UUID when the flag is on and an explicit binding
exists."""

import base64
import importlib
import io
import os
import tempfile

import pytest

from src.child_contract import source_contract_hash
from src.security import AdminSessionStore
from src.service_marzban import ServiceMarzbanClient

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


class _Server:
    def __init__(self, db):
        self.db = db


class _Handler:
    def __init__(self, db, headers):
        self.headers = dict(headers)
        self.client_address = ("198.51.100.10", 12345)
        self.server = _Server(db)
        self.wfile = io.BytesIO()
        self.status = None
        self.response_headers = []

    def send_response(self, status):
        self.status = status

    def send_header(self, name, value):
        self.response_headers.append((name, value))

    def end_headers(self):
        pass


class _LegacyMarzbanClient:
    """Only what src/routes/sub.py's `_client` needs -- the legacy public
    subscription fetch, unrelated to the typed broker boundary."""

    def __init__(self, username, body, headers):
        self.username = username
        self.body = body
        self.headers = headers
        self.calls = []

    def get_sub(self, token, extra_headers=None):
        self.calls.append(("get_sub", token))
        return self.body, dict(self.headers)

    def get_username_for_token(self, token):
        self.calls.append(("get_username_for_token", token))
        return self.username


def _legacy_body(legacy_uuid):
    lines = [f"vless://{legacy_uuid}@vpn-one.example:443?type=tcp#One"]
    return base64.b64encode("\n".join(lines).encode("utf-8"))


def _capability(db):
    _raw, session = AdminSessionStore().create(PRIMARY_LOGIN, "legacy-bridge-route-test-jwt")
    return db.primary_admin_authority.authorize_session(session)


def test_flag_off_legacy_response_is_byte_identical(db, monkeypatch):
    from src.routes import sub as sub_route

    legacy_uuid = "12345678-1234-4234-9234-123456789abc"
    body = _legacy_body(legacy_uuid)
    client = _LegacyMarzbanClient("alice", body, {"Profile-Title": "legacy"})
    handler = _Handler(db, {"User-Agent": "Happ/2.7.0/Windows/route-test-device"})

    monkeypatch.setattr(sub_route, "_client", client)
    monkeypatch.setattr(sub_route, "LEGACY_BRIDGE_ENABLED", False)

    sub_route.handle_sub(handler, "legacy-token-unchanged")

    assert handler.status == 200
    sent_body = handler.wfile.getvalue()
    assert base64.b64decode(sent_body).decode() == base64.b64decode(body).decode()


def test_flag_on_no_binding_falls_through_unchanged(db, monkeypatch):
    from src.routes import sub as sub_route

    legacy_uuid = "12345678-1234-4234-9234-123456789abc"
    body = _legacy_body(legacy_uuid)
    client = _LegacyMarzbanClient("alice", body, {"Profile-Title": "legacy"})
    handler = _Handler(db, {"User-Agent": "Happ/2.7.0/Windows/route-test-device-2"})

    monkeypatch.setattr(sub_route, "_client", client)
    monkeypatch.setattr(sub_route, "LEGACY_BRIDGE_ENABLED", True)

    sub_route.handle_sub(handler, "legacy-token-unchanged")

    assert handler.status == 200
    sent_body = handler.wfile.getvalue()
    assert base64.b64decode(sent_body).decode() == base64.b64decode(body).decode()


def test_flag_on_bridged_account_response_has_no_shared_legacy_uuid(db, monkeypatch):
    from src.routes import sub as sub_route

    account, alias_id, slot = _account(db, mapping="ROUTE_BRIDGED", tg=850001, alias="alice")
    remote = FakeMarzban()
    legacy_uuid = remote.users["alice"]["proxies"]["vless"]["id"]

    original_create_user = remote.create_user

    def create_user_with_sub_url(payload, token):
        created = original_create_user(payload, token)
        remote.users[created["username"]]["subscription_url"] = f"/sub/{created['username']}-token"
        created["subscription_url"] = remote.users[created["username"]]["subscription_url"]
        return created

    remote.create_user = create_user_with_sub_url

    def _get_sub(self, token, extra_headers=None):
        self.calls.append(("get_sub", token))
        child_line = f"vless://{token}-fake-child-uuid@vpn-child.example:443?type=tcp#Child"
        return base64.b64encode(child_line.encode("utf-8")), {"profile-title": "child"}

    remote.get_sub = _get_sub.__get__(remote, FakeMarzban)

    request_hash = source_contract_hash(remote.users["alice"])
    prepared = db.child_provisioning.prepare_child_ensure(
        account_id=account["account_id"], slot_generation_id=slot["generation_id"],
        source_alias_id=alias_id, source_contract_hash=request_hash, expire=0,
        idempotency_key="route-bridged-seed", now=100,
    )
    claimed = db.child_provisioning.claim(
        prepared["operation_id"], worker_id="seed-worker", now=101, lease_seconds=5,
    )
    from src.broker_operations import BrokerOperations
    created = BrokerOperations(remote).dispatch("child.user.ensure", claimed["payload"])
    child_uuid = created.pop("uuid")
    db.child_provisioning.acknowledge(
        prepared["operation_id"], worker_id="seed-worker",
        outcome=created["outcome"], child_uuid=child_uuid, remote_result=created, now=102,
    )

    cap = _capability(db)
    db.legacy_bridge.create_binding(
        capability=cap, account_id=account["account_id"], legacy_alias_id=alias_id,
        enabled=True, decision_ref="owner-approved-route-test", now=110,
    )

    body = _legacy_body(legacy_uuid)
    legacy_client = _LegacyMarzbanClient("alice", body, {"Profile-Title": "legacy"})
    handler = _Handler(db, {"User-Agent": "Happ/2.7.0/Windows/route-bridge-device"})

    monkeypatch.setattr(sub_route, "_client", legacy_client)
    monkeypatch.setattr(sub_route, "LEGACY_BRIDGE_ENABLED", True)
    monkeypatch.setattr(sub_route, "DEVICE_SLOT_HMAC_KEY", HWID_KEY)
    monkeypatch.setattr(
        sub_route, "_bridge_client",
        ServiceMarzbanClient(mode="direct", direct_client=remote),
    )

    sub_route.handle_sub(handler, "legacy-token-unchanged")

    assert handler.status == 200
    sent_body = handler.wfile.getvalue()
    decoded = base64.b64decode(sent_body).decode("utf-8", errors="replace")
    assert legacy_uuid not in decoded
    assert decoded != base64.b64decode(body).decode()  # genuinely not the legacy body
