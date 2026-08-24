import io
import json
import threading
import time
import urllib.error
import urllib.request

import pytest


SECURITY_HEADERS = {
    "cache-control",
    "referrer-policy",
    "x-content-type-options",
    "x-frame-options",
    "strict-transport-security",
    "permissions-policy",
    "content-security-policy",
}


class _DummyDB:
    pass


@pytest.fixture
def app_server():
    from src.server import _Handler, _ServerWithDB

    server = _ServerWithDB(("127.0.0.1", 0), _Handler, _DummyDB())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def _open(url, *, method="GET"):
    request = urllib.request.Request(url, method=method)
    try:
        response = urllib.request.urlopen(request, timeout=5)
    except urllib.error.HTTPError as exc:
        response = exc
    body = response.read()
    return response.status, {k.lower(): v for k, v in response.headers.items()}, body


@pytest.mark.parametrize(
    "path,expected_status",
    [
        ("/lk/", 200),
        ("/sub-admin/", 200),
        ("/admin/session", 401),
        ("/docs", 404),
        ("/openapi.json", 404),
        ("/version", 404),
    ],
)
def test_every_dynamic_response_gets_security_baseline_and_hides_runtime_version(
    app_server, path, expected_status,
):
    status, headers, _ = _open(app_server + path)
    assert status == expected_status
    assert SECURITY_HEADERS <= set(headers)
    assert headers["cache-control"] == "no-store"
    assert headers["referrer-policy"] == "no-referrer"
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in headers["content-security-policy"]
    assert headers["server"] == "MGBoost"
    assert "Python" not in headers["server"]
    assert "/" not in headers["server"]


def test_static_assets_keep_cache_contract_but_receive_other_security_headers(app_server):
    status, headers, body = _open(app_server + "/assets/lk.js?v=security-test")
    assert status == 200
    assert body
    assert headers["cache-control"] == "public, max-age=3600"
    assert SECURITY_HEADERS - {"cache-control"} <= set(headers)


def test_unknown_method_has_uniform_json_error_and_security_headers(app_server):
    status, headers, body = _open(app_server + "/not-an-endpoint", method="POST")
    assert status == 404
    assert json.loads(body) == {"error": "Not found"}
    assert SECURITY_HEADERS <= set(headers)


class _SubHandler:
    def __init__(self, *, user_agent="Happ/1", host="sub.example"):
        self.headers = {"User-Agent": user_agent, "Host": host, "X-Forwarded-Proto": "https"}
        self.client_address = ("198.51.100.10", 12345)
        self.wfile = io.BytesIO()
        self.status = None
        self.response_headers = []
        self.server = type("Server", (), {"db": _DummyDB()})()

    def send_response(self, status):
        self.status = status

    def send_header(self, name, value):
        self.response_headers.append((name, value))

    def end_headers(self):
        pass

    def header(self, name):
        return next(
            value for key, value in reversed(self.response_headers)
            if key.lower() == name.lower()
        )


class _FailingSubClient:
    def __init__(self, status):
        self.status = status

    def get_sub(self, token, extra_headers=None):
        if self.status == "network":
            raise urllib.error.URLError("upstream detail must not be returned")
        raise urllib.error.HTTPError(
            f"http://upstream.invalid/sub/{token}", self.status, "upstream detail", {},
            io.BytesIO(b'{"detail":"sensitive upstream detail"}'),
        )


@pytest.mark.parametrize("upstream_status", [401, 403, 404])
def test_invalid_subscription_responses_are_uniform_and_timing_floored(
    monkeypatch, upstream_status,
):
    from src.routes import sub as sub_route

    monkeypatch.setattr(sub_route, "_client", _FailingSubClient(upstream_status))
    monkeypatch.setattr(sub_route, "_INVALID_RESPONSE_FLOOR_SECONDS", 0.01)
    handler = _SubHandler()
    started = time.monotonic()
    sub_route.handle_sub(handler, "invalid-legacy-token")
    elapsed = time.monotonic() - started
    assert handler.status == 404
    assert handler.wfile.getvalue() == b"Subscription not found\n"
    assert elapsed >= 0.008
    assert handler.header("Cache-Control") == "no-store"
    assert handler.header("Referrer-Policy") == "no-referrer"
    assert handler.header("X-Frame-Options") == "DENY"


def test_subscription_outage_is_generic_and_does_not_return_upstream_detail(monkeypatch):
    from src.routes import sub as sub_route

    monkeypatch.setattr(sub_route, "_client", _FailingSubClient("network"))
    handler = _SubHandler()
    sub_route.handle_sub(handler, "valid-shape-token")
    assert handler.status == 502
    assert handler.wfile.getvalue() == b"Subscription service unavailable\n"
    assert b"upstream" not in handler.wfile.getvalue()


def test_browser_subscription_csp_report_only_then_enforce(monkeypatch):
    from src.routes import sub as sub_route

    report_only = _SubHandler(user_agent="Mozilla/5.0")
    monkeypatch.setattr(sub_route, "SUB_BROWSER_CSP_ENFORCE", False)
    sub_route.handle_sub(report_only, "legacy-browser-token")
    assert report_only.status == 200
    assert report_only.header("Content-Security-Policy") == sub_route._BROWSER_CSP_BASELINE
    assert report_only.header("Content-Security-Policy-Report-Only") == sub_route._BROWSER_CSP_STRICT

    enforced = _SubHandler(user_agent="Mozilla/5.0")
    monkeypatch.setattr(sub_route, "SUB_BROWSER_CSP_ENFORCE", True)
    sub_route.handle_sub(enforced, "legacy-browser-token")
    assert enforced.status == 200
    assert enforced.header("Content-Security-Policy") == sub_route._BROWSER_CSP_STRICT
    assert not any(
        name.lower() == "content-security-policy-report-only"
        for name, _ in enforced.response_headers
    )


def test_oversized_subscription_token_is_rejected_before_browser_reflection(monkeypatch):
    from src.routes import sub as sub_route

    monkeypatch.setattr(sub_route, "_INVALID_RESPONSE_FLOOR_SECONDS", 0)
    handler = _SubHandler(user_agent="Mozilla/5.0")
    sub_route.handle_sub(handler, "x" * (sub_route._MAX_LEGACY_TOKEN_LENGTH + 1))
    assert handler.status == 404
    assert handler.wfile.getvalue() == b"Subscription not found\n"
