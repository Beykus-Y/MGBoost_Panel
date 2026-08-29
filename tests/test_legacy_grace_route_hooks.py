"""PH4-05 route-level fail-open grace-activity observation hooks:
`routes/sub.py`/`routes/opaque_sub.py` must never change status/body when
the observer fails, and must never gate/deny/mutate anything -- purely
additive telemetry, exactly like the existing PH3-07 hook it mirrors."""

import base64
import io

import pytest


TOKEN = "raw-legacy-token-canary-never-store"
OPAQUE_TOKEN = "o" * 43


class _Handler:
    def __init__(self, db, headers):
        self.headers = headers
        self.client_address = ("198.51.100.20", 1)
        self.server = type("Server", (), {"db": db})()
        self.wfile = io.BytesIO()
        self.status = None
        self.response_headers = []

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.response_headers.append((key, value))

    def end_headers(self):
        pass


class _LegacyBridge:
    def __init__(self, account_id):
        self.account_id = account_id
        self.calls = []

    def resolve_account_for_legacy_username(self, username):
        self.calls.append(username)
        return self.account_id


class _SubDB:
    def __init__(self, *, account_id=42, grace_error=None):
        self.legacy_bridge = _LegacyBridge(account_id)
        self.grace_calls = []
        self.grace_error = grace_error

    def observe_hwid_compatibility(self, token, device_metadata):
        return None

    def observe_legacy_grace_activity(self, account_id, channel):
        self.grace_calls.append((account_id, channel))
        if self.grace_error:
            raise self.grace_error

    def check_device_access(self, username, token, device_metadata):
        return False, None

    def log_request(self, *args):
        pass

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


class _Client:
    def __init__(self, body):
        self.body = body

    def get_sub(self, token, extra_headers=None):
        return self.body, {"Profile-Title": "same"}

    def get_username_for_token(self, token):
        return "legacy-user"


def test_legacy_route_observes_grace_activity_for_resolved_account(monkeypatch):
    from src.routes import sub as route

    original = base64.b64encode(b"vless://uuid@vpn.example:443?type=tcp#One")
    db = _SubDB(account_id=42)
    monkeypatch.setattr(route, "_client", _Client(original))
    handler = _Handler(db, {"User-Agent": "Happ/3.1/Android"})
    route.handle_sub(handler, TOKEN)
    assert handler.status == 200
    assert handler.wfile.getvalue() == original
    assert db.grace_calls == [(42, "LEGACY")]


def test_legacy_route_grace_observer_failure_is_fail_open(monkeypatch, caplog):
    from src.routes import sub as route

    original = base64.b64encode(b"vless://uuid@vpn.example:443?type=tcp#One")
    db = _SubDB(account_id=42, grace_error=RuntimeError(f"boom-{TOKEN}"))
    monkeypatch.setattr(route, "_client", _Client(original))
    handler = _Handler(db, {"User-Agent": "Happ/3.1/Android"})
    route.handle_sub(handler, TOKEN)
    assert handler.status == 200
    assert handler.wfile.getvalue() == original
    assert TOKEN not in caplog.text


def test_legacy_route_no_account_resolution_skips_observation(monkeypatch):
    from src.routes import sub as route

    original = base64.b64encode(b"vless://uuid@vpn.example:443?type=tcp#One")
    db = _SubDB(account_id=None)
    monkeypatch.setattr(route, "_client", _Client(original))
    handler = _Handler(db, {"User-Agent": "Happ/3.1/Android"})
    route.handle_sub(handler, TOKEN)
    assert handler.status == 200
    assert db.grace_calls == []


class _OpaqueDB:
    def __init__(self, *, account_id=7, grace_error=None):
        self.grace_calls = []
        self.grace_error = grace_error
        self.compat_calls = []
        self._account_id = account_id
        self.resolve_calls = []

    class _Credentials:
        def __init__(self, outer):
            self._outer = outer

        def resolve(self, token):
            self._outer.resolve_calls.append(token)
            return {"credential_id": 1, "account_id": self._outer._account_id, "generation": 1}

    def __getattr__(self, name):
        if name == "subscription_credentials":
            return self._Credentials(self)
        raise AttributeError(name)

    def observe_legacy_grace_activity(self, account_id, channel):
        self.grace_calls.append((account_id, channel))
        if self.grace_error:
            raise self.grace_error

    def observe_hwid_compatibility(self, token, device_metadata):
        self.compat_calls.append((token, device_metadata))


def _patch_opaque_resolver(monkeypatch, route):
    from src.opaque_resolver import OUTCOME_OK, OpaqueResolveResult

    result = OpaqueResolveResult(
        outcome=OUTCOME_OK, child_username="child1", slot_number=1, generation=1,
        body_b64=base64.b64encode(b"vless://uuid@vpn.example:443?type=tcp#One").decode(),
        headers={},
    )
    monkeypatch.setattr(route, "resolve_opaque_subscription", lambda *a, **k: result)
    monkeypatch.setattr(route, "process_subscription", lambda body, headers, token, uname, db: (body, {}))


def test_opaque_route_observes_grace_activity_on_real_fetch(monkeypatch):
    from src.config import OPAQUE_SUBSCRIPTION_ENABLED  # noqa: F401 (documents the flag exists)
    from src.routes import opaque_sub as route

    monkeypatch.setattr(route, "OPAQUE_SUBSCRIPTION_ENABLED", True)
    _patch_opaque_resolver(monkeypatch, route)
    db = _OpaqueDB(account_id=7)
    handler = _Handler(db, {"User-Agent": "Happ/3.1/Android"})
    route.handle_opaque_sub(handler, OPAQUE_TOKEN)
    assert handler.status == 200
    assert db.grace_calls == [(7, "OPAQUE")]
    assert len(db.compat_calls) == 1
    assert db.compat_calls[0][0] == OPAQUE_TOKEN


def test_opaque_compat_observer_failure_is_fail_open(monkeypatch):
    from src.routes import opaque_sub as route

    monkeypatch.setattr(route, "OPAQUE_SUBSCRIPTION_ENABLED", True)
    _patch_opaque_resolver(monkeypatch, route)
    db = _OpaqueDB(account_id=7)
    db.observe_hwid_compatibility = lambda *_args: (_ for _ in ()).throw(RuntimeError("telemetry unavailable"))
    handler = _Handler(db, {"User-Agent": "Happ/3.1/Android"})
    route.handle_opaque_sub(handler, OPAQUE_TOKEN)
    assert handler.status == 200


def test_opaque_route_grace_observer_failure_is_fail_open(monkeypatch):
    from src.routes import opaque_sub as route

    monkeypatch.setattr(route, "OPAQUE_SUBSCRIPTION_ENABLED", True)
    _patch_opaque_resolver(monkeypatch, route)
    db = _OpaqueDB(account_id=7, grace_error=RuntimeError("boom"))
    handler = _Handler(db, {"User-Agent": "Happ/3.1/Android"})
    route.handle_opaque_sub(handler, OPAQUE_TOKEN)
    assert handler.status == 200
    assert handler.wfile.getvalue() == b"vless://uuid@vpn.example:443?type=tcp#One"
