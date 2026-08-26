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
        elif path == "/sub-admin/assets/admin/accounts.js":
            self._send(200, (ROOT / "frontend" / "assets" / "admin" / "accounts.js").read_bytes(), "text/javascript")
        elif path == "/sub-admin/assets/admin/core.js":
            self._send(200, (ROOT / "frontend" / "assets" / "admin" / "core.js").read_bytes(), "text/javascript")
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
        elif path == "/sub-admin-api/admin/accounts":
            self._send(200, {"presentation_metadata_available": True, "technical_hidden_count": 1, "accounts": [{
                "id": 1, "status": "ACTIVE", "account_source": "DIRECT",
                "created_at": 100, "primary_alias": "client_alias", "aliases": ["client_alias"],
                "display_note": PAYLOAD, "public_id": "acct_fixture", "alias_count": 1,
                "subscription": {"status": "ACTIVE", "display_name": PAYLOAD},
                "telegram_status": "BOUND", "active_devices": 1,
                "migrated_devices": 0, "parent_ready": True,
                "grace": None, "migration_action": "WAITING_FOR_REGISTRATION",
            }]})
        elif path == "/sub-admin-api/admin/accounts/1":
            self._send(200, {
                "account": {"id": 1, "public_id": "acct_technical", "status": "ACTIVE", "account_source": "DIRECT", "created_at": 100},
                "display_identity": {"display_note": PAYLOAD, "display_note_source_alias": "client_alias", "primary_alias": "client_alias", "public_id": "acct_technical"},
                "aliases": [{"legacy_username": "client_alias", "note": PAYLOAD, "alias_role": "PRIMARY", "ownership_provenance": "EVIDENCE_PROVEN", "legacy_status": "ACTIVE"}],
                "subscription": {"status": "ACTIVE", "display_name": PAYLOAD, "current_expiry": None, "effective": {"device_limit_mode": "LIMITED", "device_limit": 3}},
                "credential": None,
                "devices": [{"slot_number": 1, "slot_kind": "BASE", "desired_state": "ACTIVE", "observed_state": "ACTIVE", "hwid_masked": "hwid_fixture_mask", "child_observed_state": "ACTIVE", "migration_state": None, "real_migration_lineage": False, "proven_genesis_bootstrap": True}],
                "telegram": {"status": "BOUND", "identities": [{"telegram_id": 12345678, "role": "OWNER", "provenance": "MIGRATION", "linked_at": 100, "revoked_at": None}]},
                "migration_grace": {"action": "WAITING_FOR_REGISTRATION", "bridge_enabled": True, "active_devices": 1, "migrated_devices": 0, "migration_state": {"MIGRATING": 0, "MIGRATED": 0, "LEGACY_REVOKE_PENDING": 0, "LEGACY_REVOKED": 0, "ERROR_RECONCILE": 0}, "grace": None},
                "technical": {"account_public_id": "acct_technical", "device_lineage": [{"slot_number": 1, "generation": 1, "generation_status": "ACTIVE", "slot_generation_id": 91, "child_intent_id": 92, "child_username": "mgc_technical_only", "hwid_verifier": "hmac-sha256:technical-only", "uuid_verifier": "sha256:technical-only", "outbox_id": 93, "operation_id": "op_technical_only", "outbox_state": "APPLIED", "child_desired_state": "ACTIVE", "child_observed_state": "ACTIVE"}]},
                "presentation_metadata_available": True,
            })
        elif path == "/sub-admin-api/admin/dashboard":
            self._send(200, {"grace_campaign": None, "health": {"error_reconcile": 0, "resolver_errors_72h": 0, "slot_state_mismatches": 0, "child_state_mismatches": 0}, "expiring": {"buckets": {"today": 0, "three_days": 0, "seven_days": 0, "thirty_days": 0}, "accounts": []}, "tickets": {"open": 0, "unanswered": 0}})
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
            page.locator('[data-page="users"]').click()
            page.wait_for_function("payload => document.querySelector('#users-tbody').innerText.includes(payload)", arg=PAYLOAD)

            assert page.evaluate("globalThis.__mgboost_xss") == 0
            assert page.locator("#users-tbody img").count() == 0
            assert PAYLOAD in page.locator("#users-tbody").inner_text()
            page.locator('[data-page="configs"]').click()
            page.wait_for_selector(".config-row", state="attached")
            page.wait_for_selector("[data-inbound-value]", state="attached")
            assert PAYLOAD in page.locator("#cfg-list").inner_text()
            assert PAYLOAD in page.locator("#inbound-extra-list").inner_text()
            page.locator('.nav-item[data-page="accounts"]').click()
            page.wait_for_function("payload => document.querySelector('#accounts-tbody').innerText.includes(payload)", arg=PAYLOAD)
            assert page.locator("#accounts-tbody img").count() == 0
            page.locator('#account-search').fill(PAYLOAD[:12])
            assert page.locator('#accounts-tbody [data-action="open-account"]').count() == 1
            page.locator('#accounts-tbody [data-action="open-account"]').click()
            page.wait_for_selector("#page-account-detail.active")
            assert PAYLOAD in page.locator("#account-tab-content").inner_text()
            assert "UNLIMITED" not in page.locator("#account-tab-content").inner_text()
            assert "mgc_technical_only" not in page.locator("#account-tab-content").inner_text()
            page.locator('[data-account-tab="technical"]').click()
            assert "mgc_technical_only" in page.locator("#account-tab-content").inner_text()
            page.set_viewport_size({"width": 480, "height": 900})
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")
            assert page.locator("#app img").count() == 0
            assert page.locator("[onclick],[onchange],[oninput],[ondragstart]").count() == 0
            assert page.evaluate("localStorage.getItem('mz_token')") is None
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
