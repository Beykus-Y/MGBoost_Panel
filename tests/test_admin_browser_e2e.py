import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
PAYLOAD = "<img src=x onerror=globalThis.__mgboost_xss=1>'\\\"&"


class AdminFixtureHandler(BaseHTTPRequestHandler):
    def log_message(self, _format, *_args):
        pass

    def _send(self, status, body, content_type="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/sub-admin/":
            body = (ROOT / "frontend" / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
                "base-uri 'self'; frame-ancestors 'none'; form-action 'self'",
            )
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/sub-admin/assets/admin.js":
            self._send(200, (ROOT / "frontend" / "assets" / "admin.js").read_bytes(), "text/javascript")
        elif path == "/sub-admin/assets/admin.css":
            self._send(200, (ROOT / "frontend" / "assets" / "admin.css").read_bytes(), "text/css")
        elif path == "/sub-admin-api/admin/session":
            self._send(200, {"authenticated": True, "username": "admin", "csrf_token": "csrf", "expires_at": 9999999999})
        elif path == "/sub-admin-api/admin/marzban/system":
            self._send(200, {
                "total_user": 1, "users_active": 1, "users_expired": 0, "online_users": 0,
                "incoming_bandwidth": 0, "incoming_bandwidth_speed": 0,
                "outgoing_bandwidth": 0, "outgoing_bandwidth_speed": 0,
                "cpu_usage": 0, "cpu_cores": 1, "mem_used": 0, "mem_total": 1,
            })
        elif path == "/sub-admin-api/admin/marzban/nodes":
            self._send(200, [{
                "id": 7, "name": PAYLOAD, "address": PAYLOAD, "port": 443,
                "status": "connected", "xray_version": PAYLOAD,
            }])
        elif path == "/sub-admin-api/admin/marzban/nodes/usage":
            self._send(200, {"usages": []})
        elif path == "/sub-admin-api/admin/marzban/users":
            self._send(200, {"users": [{
                "username": PAYLOAD, "note": PAYLOAD, "sub_last_user_agent": PAYLOAD,
                "status": "active", "used_traffic": 0, "data_limit": None,
                "expire": None, "online_at": None,
            }]})
        elif path == "/sub-admin-api/admin/node-filters":
            self._send(200, {})
        elif path == "/sub-admin-api/admin/marzban/inbounds":
            self._send(200, {"vless": [{"tag": PAYLOAD}]})
        elif path == "/sub-admin-api/admin/configs":
            self._send(200, [{"id": 1, "name": PAYLOAD, "uri": PAYLOAD, "enabled": True}])
        elif path == "/sub-admin-api/admin/per-user-configs":
            self._send(200, {PAYLOAD: [{"name": PAYLOAD, "uri": PAYLOAD, "enabled": True}]})
        elif path == "/sub-admin-api/admin/settings":
            self._send(200, {"inbound_client_extras": {PAYLOAD: PAYLOAD}})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length:
            self.rfile.read(length)
        if self.path == "/sub-admin-api/admin/user-devices-counts":
            self._send(200, {PAYLOAD: 0})
        else:
            self._send(404, {"error": "not found"})


def test_stored_xss_payload_is_text_under_production_csp():
    playwright = pytest.importorskip("playwright.sync_api")
    server = ThreadingHTTPServer(("127.0.0.1", 0), AdminFixtureHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with playwright.sync_playwright() as runner:
            browser = runner.chromium.launch(headless=True)
            page = browser.new_page()
            page.add_init_script("globalThis.__mgboost_xss = 0")
            page.goto(f"http://127.0.0.1:{server.server_port}/sub-admin/", wait_until="networkidle")
            page.wait_for_selector("#users-tbody tr", state="attached")

            assert page.evaluate("globalThis.__mgboost_xss") == 0
            assert page.locator("#users-tbody img").count() == 0
            assert PAYLOAD in page.locator("#users-tbody").inner_text()
            page.locator('[data-page="configs"]').click()
            page.wait_for_selector(".config-row", state="attached")
            page.wait_for_selector("[data-inbound-value]", state="attached")
            assert PAYLOAD in page.locator("#cfg-list").inner_text()
            assert PAYLOAD in page.locator("#inbound-extra-list").inner_text()
            assert page.locator("#app img").count() == 0
            assert page.locator("[onclick],[onchange],[oninput],[ondragstart]").count() == 0
            assert page.evaluate("localStorage.getItem('mz_token')") is None
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
