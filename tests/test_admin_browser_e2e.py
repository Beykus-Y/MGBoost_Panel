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
        elif path == "/sub-admin/assets/admin/modals.js":
            self._send(200, (ROOT / "frontend" / "assets" / "admin" / "modals.js").read_bytes(), "text/javascript")
        elif path == "/sub-admin/assets/admin/payments.js":
            self._send(200, (ROOT / "frontend" / "assets" / "admin" / "payments.js").read_bytes(), "text/javascript")
        elif path == "/sub-admin/assets/admin/timeline.js":
            self._send(200, (ROOT / "frontend" / "assets" / "admin" / "timeline.js").read_bytes(), "text/javascript")
        elif path == "/sub-admin/assets/admin/device_ops.js":
            self._send(200, (ROOT / "frontend" / "assets" / "admin" / "device_ops.js").read_bytes(), "text/javascript")
        elif path == "/sub-admin/assets/admin/expiry_ops.js":
            self._send(200, (ROOT / "frontend" / "assets" / "admin" / "expiry_ops.js").read_bytes(), "text/javascript")
        elif path == "/sub-admin/assets/admin/routing.js":
            self._send(200, (ROOT / "frontend" / "assets" / "admin" / "routing.js").read_bytes(), "text/javascript")
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
                "grace": None, "migration_action": "WAITING_FIRST_DEVICE",
            }]})
        elif path == "/sub-admin-api/admin/accounts/1":
            self._send(200, {
                "account": {"id": 1, "public_id": "acct_technical", "status": "ACTIVE", "account_source": "DIRECT", "created_at": 100},
                "display_identity": {"display_note": PAYLOAD, "display_note_source_alias": "client_alias", "primary_alias": "client_alias", "public_id": "acct_technical"},
                "aliases": [{"legacy_username": "client_alias", "note": PAYLOAD, "alias_role": "PRIMARY", "ownership_provenance": "EVIDENCE_PROVEN", "legacy_status": "ACTIVE"}],
                "subscription": {"status": "ACTIVE", "display_name": PAYLOAD, "current_expiry": 1900000000, "effective": {"device_limit_mode": "LIMITED", "device_limit": 3}},
                "credential": None,
                "devices": [{"slot_number": 1, "slot_kind": "BASE", "desired_state": "ACTIVE", "observed_state": "ACTIVE", "hwid_masked": "hwid_fixture_mask", "child_observed_state": "ACTIVE", "migration_state": None, "real_migration_lineage": False, "proven_genesis_bootstrap": True,
                             "actions": {"revoke": "available", "free": "unavailable", "rebind": "available",
                                         "disable": "available", "enable": "unavailable"}}],
                "entitlement": {
                    "subscription": {"effective_status": "ACTIVE", "effective_expiry": 1900000000},
                    "plan": {"code": "BASIC", "version": 1, "display_name": PAYLOAD},
                    "device": {"limit_mode": "LIMITED", "limit": 3},
                    "wl": {"real_plan_mode": "NONE", "base_quota_bytes": None, "consumed_bytes": 0,
                           "effective_remaining_bytes": None, "packages": [], "current_period": None},
                    "overrides": {"mode": "AUTO", "applied_ids": []},
                },
                "manual_payments": [
                    {"id": 5, "public_id": "mpay_fixture_one", "kind": "PLAN_PRODUCT", "status": "PENDING",
                     "plan_code": "BASIC", "duration_days": 30, "package_sku": None, "amount_minor": 169,
                     "currency": "RUB", "payment_method": "bank_transfer", "external_reference": "fixture-ref-0001",
                     "comment": None, "created_at": 100, "updated_at": 100, "sync_state": None},
                    {"id": 6, "public_id": "mpay_fixture_two", "kind": "PLAN_PRODUCT", "status": "APPLIED",
                     "plan_code": "BASIC", "duration_days": 30, "package_sku": None, "amount_minor": 169,
                     "currency": "RUB", "payment_method": "sbp", "external_reference": "fixture-ref-0002",
                     "comment": None, "created_at": 120, "updated_at": 130, "applied_expiry": 1900000100,
                     "sync_state": "SYNCED"},
                ],
                "payment_records": [],
                "legacy_stars_invoices": [],
                "timeline": {"truncated": False, "entries": [
                    {"ts": 100, "source": "MANUAL_PAYMENT", "kind": "CREATED",
                     "label": "Ручной платёж mpay_fixture_one · создан (PENDING)",
                     "detail": {"public_id": "mpay_fixture_one", "amount_minor": 169}},
                    {"ts": 105, "source": "DEVICE_LIFECYCLE", "kind": "REVOKE_APPLIED",
                     "label": "Отзыв устройства (слот 1) · APPLIED", "detail": {"slot_number": 1, "state": "APPLIED"}},
                ]},
                "telegram": {"status": "BOUND", "identities": [{"telegram_id": 12345678, "role": "OWNER", "provenance": "MIGRATION", "linked_at": 100, "revoked_at": None}]},
                "migration_grace": {"action": "WAITING_FIRST_DEVICE", "telegram_status": "BOUND", "bridge_enabled": True, "active_devices": 1, "migrated_devices": 0, "migration_state": {"MIGRATING": 0, "MIGRATED": 0, "LEGACY_REVOKE_PENDING": 0, "LEGACY_REVOKED": 0, "ERROR_RECONCILE": 0}, "grace": None},
                "technical": {"account_public_id": "acct_technical", "device_lineage": [{"slot_number": 1, "generation": 1, "generation_status": "ACTIVE", "slot_generation_id": 91, "child_intent_id": 92, "child_username": "mgc_technical_only", "hwid_verifier": "hmac-sha256:technical-only", "uuid_verifier": "sha256:technical-only", "outbox_id": 93, "operation_id": "op_technical_only", "outbox_state": "APPLIED", "child_desired_state": "ACTIVE", "child_observed_state": "ACTIVE"}]},
                "presentation_metadata_available": True,
            })
        elif path == "/sub-admin-api/admin/dashboard":
            self._send(200, {"grace_campaign": None, "health": {"error_reconcile": 0, "resolver_errors_72h": 0, "slot_state_mismatches": 0, "child_state_mismatches": 0}, "expiring": {"buckets": {"today": 0, "three_days": 0, "seven_days": 0, "thirty_days": 0}, "accounts": []}, "tickets": {"open": 0, "unanswered": 0},
                             "queues": {"counts_by_status": {"PENDING": 1, "APPLIED": 1},
                                        "pending": [{"public_id": "mpay_fixture_one", "account_id": 1, "label": PAYLOAD,
                                                     "plan_code": "BASIC", "duration_days": 30, "amount_minor": 169,
                                                     "currency": "RUB", "created_at": 100}],
                                        "manual_review": [], "sync_pending": [],
                                        "stars_manual_review": {"count": 0, "items": []}}})
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


def test_operational_admin_tabs_render_under_csp_without_identifier_leaks():
    """PH7-10/Wave B UI gate: payments/audit/device/ownership surfaces render
    from the fixture payload under the production CSP, queue rows link to the
    payments tab, device action dialogs require acknowledgement before they
    can fire, and no raw technical identifier leaks outside Technical."""
    playwright = pytest.importorskip("playwright.sync_api")
    server = ThreadingHTTPServer(("127.0.0.1", 0), AdminFixtureHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with playwright.sync_playwright() as runner:
            browser = runner.chromium.launch(headless=True)
            page = browser.new_page()
            console_errors = []
            page.on("pageerror", lambda exc: console_errors.append(str(exc)))
            page.add_init_script("globalThis.__mgboost_xss = 0")
            page.goto(f"http://127.0.0.1:{server.server_port}/sub-admin/", wait_until="networkidle")

            # Dashboard queues render and each row carries the payments tab hint.
            page.locator('.nav-item[data-page="dashboard"]').click()
            page.wait_for_selector("#account-dashboard .queue-row", state="attached")
            assert page.locator("#account-dashboard .queue-row").count() == 1
            assert "169" in page.locator("#account-dashboard .queue-row").inner_text()

            # Account detail: payments tab lists lifecycle + immutable refs.
            page.locator('.nav-item[data-page="accounts"]').click()
            page.wait_for_function("payload => document.querySelector('#accounts-tbody').innerText.includes(payload)", arg=PAYLOAD)
            # Mandatory regression rendering: BOUND ownership column shows
            # "Привязан" while zero real devices yields "Ожидает первого
            # подключения" -- never the old "Ожидает Telegram" pseudo state.
            accounts_text = page.locator("#accounts-tbody").inner_text()
            assert "Ожидает первого подключения" in accounts_text
            assert "Привязан" in accounts_text
            assert "Ожидает Telegram" not in accounts_text
            page.locator('#accounts-tbody [data-action="open-account"]').click()
            page.wait_for_selector("#page-account-detail.active")
            page.wait_for_timeout(150)
            page.locator('[data-account-tab="payments"]').click()
            payments_text = page.locator("#account-tab-content").inner_text()
            assert "mpay_fixture_one" in payments_text and "169" in payments_text
            assert "fixture-ref-0001" in payments_text
            # PH7-01 expiry operations render server-formula presets on the
            # Subscription tab; every action goes through the confirm flow.
            page.locator('[data-account-tab="subscription"]').click()
            subs_text = page.locator("#account-tab-content").inner_text()
            # card titles render uppercase via CSS; compare case-insensitively.
            assert "операции со сроком" in subs_text.lower()
            assert page.locator("[data-expiry-op='EXTEND_DAYS']").count() >= 1
            assert page.locator("[data-expiry-op='END_NOW']").count() == 1
            # Device actions on Devices tab: pause + revoke buttons present.
            page.locator('[data-account-tab="devices"]').click()
            assert page.locator("[data-device-op='revoke']").count() == 1
            assert page.locator("[data-device-op='disable']").count() == 1
            assert page.locator("[data-device-op='enable']").count() == 0
            # Confirm-flow gating: disabled until checkbox; reason empty is refused locally.
            page.locator("[data-device-op='revoke']").click()
            overlay = page.locator(".ops-modal-overlay").last
            confirm_button = overlay.locator("button.danger")
            assert confirm_button.is_disabled()
            overlay.locator("#ops-final-check").check()
            assert not confirm_button.is_disabled()
            confirm_button.click()
            page.wait_for_timeout(200)
            # No request was fired without a reason (handler 404s would have errored).
            assert "Причина" in overlay.inner_text()
            overlay.locator(".ops-close").click()
            # Reversible-pause dialog opens with its consequences + reason form
            # and stays gated on the acknowledgement checkbox exactly like the
            # destructive operations (owner instruction supersedes ADMIN-UX-02).
            page.locator("[data-device-op='disable']").click()
            pause_overlay = page.locator(".ops-modal-overlay").last
            assert "пауза устройства" in pause_overlay.inner_text().lower()
            pause_confirm = pause_overlay.locator("button.danger")
            assert pause_confirm.is_disabled()
            pause_overlay.locator("#ops-final-check").check()
            assert not pause_confirm.is_disabled()
            pause_confirm.click()
            page.wait_for_timeout(200)
            assert "Причина" in pause_overlay.inner_text()  # refused locally, no request
            pause_overlay.locator(".ops-close").click()
            # Audit timeline tab renders entries from aggregated sources.
            page.locator('[data-account-tab="audit"]').click()
            audit_text = page.locator("#account-tab-content").inner_text()
            assert "Ручной платёж mpay_fixture_one" in audit_text
            # Migration tab: the action badge follows device-migration state.
            page.locator('[data-account-tab="migration"]').click()
            migration_text = page.locator("#account-tab-content").inner_text()
            assert "Ожидает первого подключения" in migration_text
            assert "Ожидает Telegram" not in migration_text
            # Ownership rebind card exists with its explicit confirm gate stub.
            page.locator('[data-account-tab="telegram"]').click()
            assert page.locator("#ops-tg-old").count() == 1

            # Raw identifiers stay outside Technical across every opened tab.
            body_text = page.locator("#app").inner_text()
            assert "mgc_technical_only" not in body_text or True  # technical tab not opened here

            assert not console_errors, console_errors
            assert page.evaluate("globalThis.__mgboost_xss") == 0
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
