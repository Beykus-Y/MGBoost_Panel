import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
PAYLOAD = "<img src=x onerror=globalThis.__mgboost_lk_xss=1>'\\\"&"
RENAMED_PAYLOAD = "renamed </span><svg onload=globalThis.__mgboost_lk_xss=2>\\'\""


class LkFixtureHandler(BaseHTTPRequestHandler):
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
        if path == "/lk/":
            body = (ROOT / "frontend" / "lk.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self'; style-src 'self'; "
                "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
                "base-uri 'self'; frame-ancestors 'none'; form-action 'self'",
            )
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/lk/assets/lk.js":
            self._send(200, (ROOT / "frontend" / "assets" / "lk.js").read_bytes(), "text/javascript")
        elif path == "/lk/assets/lk.css":
            self._send(200, (ROOT / "frontend" / "assets" / "lk.css").read_bytes(), "text/css")
        elif path == "/lk/api/info":
            self._send(200, {
                "username": PAYLOAD,
                "status": "active",
                "expire": None,
                "used_traffic": 42,
                "data_limit": None,
                "subscription_url": "https://example.test/sub/synthetic",
            })
        elif path == "/lk/api/usage":
            self._send(200, {"usages": [{
                "node_name": PAYLOAD,
                "used_traffic": 42,
                "percent": 7,
            }]})
        elif path == "/lk/api/devices":
            self._send(200, {
                "limit": 3,
                "active_count": 1,
                "devices": [{
                    "id": 7,
                    "display_name": PAYLOAD,
                    "device_name": PAYLOAD,
                    "client_name": PAYLOAD,
                    "platform": PAYLOAD,
                    "last_seen": 1,
                    "is_active": True,
                }],
            })
        else:
            self._send(404, {"error": "not found"})

    def do_PATCH(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        if self.path == "/lk/api/devices/7" and body.get("name") == RENAMED_PAYLOAD:
            self._send(200, {"ok": True})
        else:
            self._send(400, {"error": "bad request"})

    def do_DELETE(self):
        if self.path == "/lk/api/devices/7":
            self._send(200, {"ok": True})
        else:
            self._send(404, {"error": "not found"})


def test_lk_api_values_and_renamed_device_are_text_under_csp():
    playwright = pytest.importorskip("playwright.sync_api")
    server = ThreadingHTTPServer(("127.0.0.1", 0), LkFixtureHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with playwright.sync_playwright() as runner:
            browser = runner.chromium.launch(headless=True)
            page = browser.new_page()
            page.add_init_script("globalThis.__mgboost_lk_xss = 0")
            page.goto(
                f"http://127.0.0.1:{server.server_port}/lk/#token=synthetic",
                wait_until="networkidle",
            )
            page.wait_for_selector(".device-item", state="attached")

            assert page.evaluate("globalThis.__mgboost_lk_xss") == 0
            assert page.locator("#app img, #app svg").count() == 0
            assert PAYLOAD in page.locator("#devicesCard").inner_text()
            assert PAYLOAD in page.locator("#usageCard").inner_text()
            assert PAYLOAD in page.locator("#statusCard").inner_text()
            assert page.locator("[onclick],[onchange],[oninput],[onerror],[onload]").count() == 0

            page.once("dialog", lambda dialog: dialog.accept(RENAMED_PAYLOAD))
            page.get_by_title("Переименовать").click()
            page.wait_for_function(
                "value => document.querySelector('.device-item-name')?.textContent === value",
                arg=RENAMED_PAYLOAD,
            )
            assert page.evaluate("globalThis.__mgboost_lk_xss") == 0
            assert page.locator("#app img, #app svg").count() == 0

            page.once("dialog", lambda dialog: dialog.accept())
            page.get_by_title("Отключить").click()
            page.wait_for_selector(".badge-device-inactive", state="attached")
            assert page.locator(".device-item-actions").count() == 0
            assert page.evaluate("globalThis.__mgboost_lk_xss") == 0
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
