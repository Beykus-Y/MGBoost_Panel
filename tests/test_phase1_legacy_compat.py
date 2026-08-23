"""Backward-compatibility contracts for the Phase 1 security rollout.

These tests intentionally describe the legacy production model.  Phase 1
may move credentials and calls behind a broker, but it must not migrate
accounts, subscription tokens, UUIDs, configs, expiry semantics or HWID
bindings.  The parent-account/child-user model belongs to later phases.
"""

import asyncio
import base64
import io
import json


LEGACY_TOKEN = "legacy-token-unchanged"
LEGACY_UUID = "12345678-1234-4234-9234-123456789abc"


class _SubscriptionDB:
    def __init__(self):
        self.device_checks = []
        self.requests = []

    def check_device_access(self, username, token, metadata):
        self.device_checks.append((username, token, metadata))
        return False, None

    def log_request(self, token, username, user_agent, ip, metadata):
        self.requests.append((token, username, user_agent, ip, metadata))

    def get_setting(self, _key):
        return None

    def get_node_filter(self, _username):
        return None

    def get_extra_configs(self):
        return []

    def get_per_user_configs(self, _username):
        return []

    def get_hysteria_traffic(self, _token):
        return 0, 0


class _SubscriptionClient:
    def __init__(self, body, headers):
        self.body = body
        self.headers = headers
        self.calls = []

    def get_sub(self, token, extra_headers=None):
        self.calls.append(("get_sub", token, dict(extra_headers or {})))
        return self.body, dict(self.headers)

    def get_username_for_token(self, token):
        self.calls.append(("get_username_for_token", token))
        return "legacy-user"

    def get_admin_token_from_env(self):  # pragma: no cover - must stay unused
        raise AssertionError("legacy subscription resolution must not require SUDO")


class _Server:
    def __init__(self, db):
        self.db = db


class _SubscriptionHandler:
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

    def header(self, name):
        return next(
            value
            for key, value in reversed(self.response_headers)
            if key.lower() == name.lower()
        )


def _legacy_body():
    lines = [
        f"vless://{LEGACY_UUID}@vpn-one.example:443?type=tcp#One",
        f"vless://{LEGACY_UUID}@vpn-two.example:443?type=xhttp#Two",
    ]
    return base64.b64encode("\n".join(lines).encode("utf-8")), lines


def test_legacy_subscription_keeps_token_uuid_configs_expiry_and_existing_hwid(monkeypatch):
    from src.routes import sub as sub_route

    body, original_lines = _legacy_body()
    upstream_headers = {
        "Subscription-Userinfo": "upload=10; download=20; total=0; expire=1893456000",
        "Profile-Title": "legacy profile",
    }
    client = _SubscriptionClient(body, upstream_headers)
    db = _SubscriptionDB()
    handler = _SubscriptionHandler(
        db,
        {
            "User-Agent": "Happ/3.1/Android/legacy-device-001",
            "X-Forwarded-Proto": "https",
            "Host": "sub.beykus.fun",
        },
    )
    monkeypatch.setattr(sub_route, "_client", client)

    sub_route.handle_sub(handler, LEGACY_TOKEN)

    assert handler.status == 200
    assert client.calls[0][0:2] == ("get_sub", LEGACY_TOKEN)
    assert client.calls[1] == ("get_username_for_token", LEGACY_TOKEN)
    assert db.device_checks[0][0:2] == ("legacy-user", LEGACY_TOKEN)
    assert db.device_checks[0][2]["request_key"].startswith("hwid:")
    assert db.requests[0][0:2] == (LEGACY_TOKEN, "legacy-user")
    assert base64.b64decode(handler.wfile.getvalue()).decode("utf-8").splitlines() == original_lines
    assert handler.header("Subscription-Userinfo") == upstream_headers["Subscription-Userinfo"]
    assert handler.header("Profile-Title") == "legacy profile"


def test_legacy_non_hwid_client_remains_permissive_and_does_not_claim_slot(monkeypatch):
    from src.routes import sub as sub_route

    body, original_lines = _legacy_body()
    client = _SubscriptionClient(body, {})
    db = _SubscriptionDB()
    handler = _SubscriptionHandler(db, {"User-Agent": "v2rayNG/1.9/Android"})
    monkeypatch.setattr(sub_route, "_client", client)

    sub_route.handle_sub(handler, LEGACY_TOKEN)

    assert handler.status == 200
    assert db.device_checks == []
    assert db.requests[0][4]["request_key"].startswith("fp:")
    assert base64.b64decode(handler.wfile.getvalue()).decode("utf-8").splitlines() == original_lines


def test_existing_hwid_refresh_keeps_the_same_device_slot(tmp_path, monkeypatch):
    from src import database as database_module

    monkeypatch.setattr(database_module, "DB_PATH", str(tmp_path / "db.sqlite3"))
    db = database_module.Database()
    metadata = {
        "request_key": "hwid:0123456789abcdef0123456789abcdef",
        "device_name": "Existing phone",
        "client_name": "Happ",
        "client_version": "1.0",
        "platform": "Android",
    }
    try:
        assert db.check_device_access("legacy-user", "old-alias", metadata) == (False, None)
        before = db._conn.execute(
            "SELECT id, first_seen FROM user_devices WHERE username=?", ("legacy-user",)
        ).fetchone()

        refreshed = dict(metadata, client_version="1.1")
        assert db.check_device_access("legacy-user", "same-legacy-url", refreshed) == (False, None)
        rows = db._conn.execute(
            "SELECT id, token, first_seen, client_version FROM user_devices WHERE username=?",
            ("legacy-user",),
        ).fetchall()
        lock = db._conn.execute(
            "SELECT username FROM hwid_lock WHERE request_key=?", (metadata["request_key"],)
        ).fetchone()

        assert len(rows) == 1
        assert rows[0]["id"] == before["id"]
        assert rows[0]["first_seen"] == before["first_seen"]
        assert rows[0]["token"] == "same-legacy-url"
        assert rows[0]["client_version"] == "1.1"
        assert lock["username"] == "legacy-user"
    finally:
        db._conn.close()


class _BodyHandler:
    def __init__(self, payload=b""):
        self._body = payload
        self.rfile = io.BytesIO(payload)
        self.wfile = io.BytesIO()
        self.headers = {"Content-Length": str(len(payload))}
        self.server = type("Server", (), {"bot_runner": None})()
        self.status = None
        self.response_headers = []

    def send_response(self, status):
        self.status = status

    def send_header(self, name, value):
        self.response_headers.append((name, value))

    def end_headers(self):
        pass

    def json(self):
        return json.loads(self.wfile.getvalue())


class _LegacyMutationClient:
    def __init__(self):
        self.calls = []
        self.user = {
            "username": "legacy-user",
            "expire": 1_800_000_000,
            "status": "active",
            "proxies": {"vless": {"id": LEGACY_UUID, "flow": ""}},
            "inbounds": {"vless": ["LEGACY-ONE", "LEGACY-TWO"]},
            "data_limit": None,
        }

    def get_admin_token_from_env(self):
        return "service-token"

    def get_user(self, username, token):
        self.calls.append(("get", username, token))
        return dict(self.user)

    def create_user(self, payload, token):
        self.calls.append(("create", dict(payload), token))
        return dict(payload)

    def modify_user(self, username, payload, token):
        self.calls.append(("modify", username, dict(payload), token))
        self.user.update(payload)
        return dict(self.user)

    def renew_user(self, username, renewal, token):
        user = self.get_user(username, token)
        update = {}
        if "add_days" in renewal:
            update["expire"] = max(int(user.get("expire") or 0), 1_700_000_000) + renewal["add_days"] * 86400
        if "expire" in renewal:
            update["expire"] = renewal["expire"]
        if "data_limit" in renewal:
            update["data_limit"] = renewal["data_limit"] or None
        if "status" in renewal:
            update["status"] = renewal["status"]
        return self.modify_user(username, update, token)

    def delete_user(self, username, token):
        self.calls.append(("delete", username, token))
        return {}


def test_legacy_manual_renew_changes_only_expiry(monkeypatch):
    from src.routes import internal

    client = _LegacyMutationClient()
    monkeypatch.setattr(internal, "_client", client)
    monkeypatch.setattr(internal.time, "time", lambda: 1_700_000_000)
    before = json.loads(json.dumps(client.user))
    handler = _BodyHandler(json.dumps({"add_days": 30}).encode("utf-8"))

    internal.handle_internal_user_renew(handler, "legacy-user")

    assert handler.status == 200
    expected_expire = before["expire"] + 30 * 86400
    assert client.calls == [
        ("get", "legacy-user", "service-token"),
        ("modify", "legacy-user", {"expire": expected_expire}, "service-token"),
    ]
    assert client.user["expire"] == expected_expire
    for field in ("proxies", "inbounds", "data_limit", "status"):
        assert client.user[field] == before[field]


def test_legacy_create_and_delete_payload_contract_is_explicit(monkeypatch):
    from src.routes import internal

    client = _LegacyMutationClient()
    monkeypatch.setattr(internal, "_client", client)
    create_payload = {
        "username": "new-legacy-user",
        "proxies": {"vless": {}},
        "inbounds": {"vless": ["LEGACY-ONE"]},
        "expire": 1_900_000_000,
        "data_limit": 10_000,
        "data_limit_reset_strategy": "no_reset",
        "note": "external payment",
        "status": "active",
    }
    create_handler = _BodyHandler(json.dumps(create_payload).encode("utf-8"))

    internal.handle_internal_user_create(create_handler)
    delete_handler = _BodyHandler()
    internal.handle_internal_user_delete(delete_handler, "new-legacy-user")

    assert create_handler.status == 201
    assert delete_handler.status == 200
    assert client.calls == [
        ("create", create_payload, "service-token"),
        ("delete", "new-legacy-user", "service-token"),
    ]


def test_stars_apply_changes_only_expiry_and_preserves_legacy_identity(tmp_path, monkeypatch):
    from src import config
    from src import database as database_module
    from src.stars import _resolve_plan

    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(database_module, "DB_PATH", str(tmp_path / "db.sqlite3"))
    db = database_module.Database()
    try:
        invoice = db.create_stars_invoice(
            created_by_telegram_id=1,
            marzban_username="legacy-user",
            tariff_id=1,
            tariff_name="legacy 30 days",
            duration_days=30,
            stars_price=99,
        )
        db.mark_invoice_paid(
            invoice["id"], "charge-contract", None, payer_telegram_id=1, total_amount=99
        )
        db.commit_apply_plan(invoice["id"], base_expire_observed=1000, target_expire=2000)
        row = db.get_invoice(invoice["id"])
        client = _LegacyMutationClient()
        client.user["expire"] = 1000
        before = json.loads(json.dumps(client.user))

        asyncio.run(_resolve_plan(None, db, client, "service-token", row))

        assert ("modify", "legacy-user", {"expire": 2000}, "service-token") in client.calls
        assert client.user["expire"] == 2000
        for field in ("proxies", "inbounds", "data_limit", "status"):
            assert client.user[field] == before[field]
    finally:
        db._conn.close()
