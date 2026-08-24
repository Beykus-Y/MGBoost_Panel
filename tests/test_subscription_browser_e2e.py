import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from src.routes.sub import _BROWSER_CSP_STRICT, _browser_page


ROOT = Path(__file__).resolve().parents[1]


class BrowserSubscriptionFixture(BaseHTTPRequestHandler):
    def log_message(self, _format, *_args):
        pass

    def _send(self, body, content_type, *, csp=False):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        if csp:
            self.send_header("Content-Security-Policy", _BROWSER_CSP_STRICT)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/sub/synthetic-token":
            self._send(
                _browser_page(
                    "https://example.test/sub/synthetic-token?<tag>&quote='\\\""
                ),
                "text/html; charset=utf-8",
                csp=True,
            )
        elif path == "/assets/browser_page.js":
            self._send(
                (ROOT / "frontend" / "assets" / "browser_page.js").read_bytes(),
                "text/javascript",
            )
        elif path == "/assets/browser_page.css":
            self._send(
                (ROOT / "frontend" / "assets" / "browser_page.css").read_bytes(),
                "text/css",
            )
        else:
            self.send_response(404)
            self.end_headers()


def test_browser_subscription_copy_page_runs_under_enforced_strict_csp():
    playwright = pytest.importorskip("playwright.sync_api")
    server = ThreadingHTTPServer(("127.0.0.1", 0), BrowserSubscriptionFixture)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with playwright.sync_playwright() as runner:
            browser = runner.chromium.launch(headless=True)
            page = browser.new_page()
            page.add_init_script(
                """
                globalThis.__cspViolations = [];
                document.addEventListener('securitypolicyviolation', event => {
                  globalThis.__cspViolations.push(event.violatedDirective);
                });
                """
            )
            page.goto(
                f"http://127.0.0.1:{server.server_port}/sub/synthetic-token",
                wait_until="networkidle",
            )
            assert page.locator("[onclick],[onerror],[onload]").count() == 0
            assert "<tag>" in page.locator("#url-display").inner_text()
            page.locator("#copy-btn").click()
            page.wait_for_function(
                "document.querySelector('#copy-btn')?.textContent.includes('Скопировано')"
            )
            assert page.evaluate("globalThis.__cspViolations") == []
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
