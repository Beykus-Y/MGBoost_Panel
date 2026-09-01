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
        elif path.startswith("/sub-admin/assets/"):
            # Serve any real file under frontend/assets/ by its actual
            # relative path instead of a hand-maintained per-file allowlist.
            # PH7-16 Wave 0B's real-browser pre-deploy check caught that the
            # old allowlist here had silently missed admin_grant_ops.js
            # (added in PH7-14) and promo_ops.js (added in PH5-13) -- both
            # already dynamically import()-ed by accounts.js/admin.js in
            # production, just never exercised by this fixture because
            # Playwright wasn't available to actually run these tests until
            # now. A generic handler can't drift out of sync with the real
            # module graph the way an enumerated list did.
            rel = path[len("/sub-admin/assets/"):]
            asset_path = (ROOT / "frontend" / "assets" / rel).resolve()
            assets_root = (ROOT / "frontend" / "assets").resolve()
            if assets_root not in asset_path.parents or not asset_path.is_file():
                self._send(404, {"error": "not found"})
                return
            content_type = "text/javascript" if asset_path.suffix == ".js" else (
                "text/css" if asset_path.suffix == ".css" else "application/octet-stream"
            )
            self._send(200, asset_path.read_bytes(), content_type)
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
        elif path == "/sub-admin-api/admin/stars-settings":
            self._send(200, {"enabled": True})
        elif path == "/sub-admin-api/admin/stars-tariffs":
            self._send(200, [])
        elif path == "/sub-admin-api/admin/stars-payments":
            self._send(200, [{
                "id": 77, "marzban_username": "canonical-user", "tariff_name": "Basic",
                "duration_days": 30, "stars_price": 100, "status": "canonical_applied",
                "created_by_telegram_id": 1001, "payer_telegram_id": 1001,
                "base_expire_observed": None, "target_expire": None,
                "applied_expire": 1900000000, "manual_review_reason": None,
            }])
        elif path == "/sub-admin-api/admin/stars-orphan-payments":
            self._send(200, [])
        elif path == "/sub-admin-api/admin/legacy-transitions":
            self._send(200, {"total": 2, "truncated": False, "has_more": False, "transitions": [
                {"id": 501, "public_id": "lct_pending", "account_id": 1, "state": "PENDING_PAYMENT",
                 "review_reason": None, "activation_at": None, "target_expiry": None,
                 "expected_amount_minor": 169, "updated_at": 100, "source_plan_code": "LEGACY_PAID_COMPAT_V1_D3",
                 "target_plan_code": "BASIC", "target_display_name": "Basic", "label": "Pending fixture"},
                {"id": 502, "public_id": "lct_review", "account_id": 1, "state": "MANUAL_REVIEW",
                 "review_reason": "fixture review", "activation_at": 200, "target_expiry": 300,
                 "expected_amount_minor": 169, "updated_at": 110, "source_plan_code": "LEGACY_PAID_COMPAT_V1_D3",
                 "target_plan_code": "BASIC", "target_display_name": "Basic", "label": "Review fixture"},
            ]})
        elif path in {"/sub-admin-api/admin/legacy-transitions/501", "/sub-admin-api/admin/legacy-transitions/502"}:
            transition_id = int(path.rsplit("/", 1)[1])
            state = "PENDING_PAYMENT" if transition_id == 501 else "MANUAL_REVIEW"
            self._send(200, {"transition": {
                "id": transition_id, "account_id": 1, "state": state,
                "source_plan_code": "LEGACY_PAID_COMPAT_V1_D3", "target_plan_code": "BASIC",
                "expected_amount_minor": 169, "duration_days": 30,
                "original_source_expiry": 100, "activation_at": 200, "target_expiry": 300,
                "active_device_count": 1, "target_device_limit": 3, "capacity_excess": 0,
                "review_reason": "fixture review" if state == "MANUAL_REVIEW" else None,
                "devices": [], "events": [],
            }})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        if hasattr(self.server, "mutation_paths"):
            self.server.mutation_paths.append(self.path)
        length = int(self.headers.get("Content-Length", "0"))
        if length:
            self.rfile.read(length)
        if self.path == "/sub-admin-api/admin/user-devices-counts":
            self._send(200, {PAYLOAD: 0})
        else:
            self._send(404, {"error": "not found"})


class BrokenStarsModuleHandler(AdminFixtureHandler):
    def do_GET(self):
        if self.path.split("?", 1)[0].endswith("/admin/payments/stars_legacy.js"):
            self._send(404, {"error": "intentionally broken module"})
            return
        super().do_GET()


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
            page.locator('[data-page="stars"]').click()
            page.wait_for_selector('#stars-payments-tbody [data-payment-action="refund"]', state="attached")
            assert "Применён к аккаунту" in page.locator("#stars-payments-tbody").inner_text()
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
            technical_text = page.locator("#account-tab-content").inner_text()
            assert "hmac-sha256:technical-only" not in technical_text
            assert "sha256:technical-only" not in technical_text
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

            # Dashboard attention queue renders and each row carries the payments tab hint.
            page.locator('.nav-item[data-page="dashboard"]').click()
            page.wait_for_selector("#account-dashboard .attention-item", state="attached")
            assert page.locator("#account-dashboard .attention-item").count() == 1
            assert "169" in page.locator("#account-dashboard .attention-item").inner_text()
            assert page.locator("#account-dashboard .attention-item").get_attribute("data-open-tab") == "payments"

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
            assert "mgc_technical_only" not in body_text
            assert "hmac-sha256:technical-only" not in body_text
            assert "sha256:technical-only" not in body_text

            assert not console_errors, console_errors
            assert page.evaluate("globalThis.__mgboost_xss") == 0
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_broken_domain_module_shows_controlled_visible_error_without_pageerror():
    playwright = pytest.importorskip("playwright.sync_api")
    server = ThreadingHTTPServer(("127.0.0.1", 0), BrokenStarsModuleHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with playwright.sync_playwright() as runner:
            browser = runner.chromium.launch(headless=True)
            page = browser.new_page()
            page_errors = []
            page.on("pageerror", lambda exc: page_errors.append(str(exc)))
            page.goto(f"http://127.0.0.1:{server.server_port}/sub-admin/", wait_until="networkidle")
            page.locator('[data-page="stars"]').click()
            page.wait_for_selector("#page-stars .module-unavailable")
            assert "Модуль недоступен: Telegram Stars" in page.locator("#page-stars").inner_text()
            assert not page_errors, page_errors
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_transition_prompt_cancel_and_invalid_reason_send_no_mutation():
    playwright = pytest.importorskip("playwright.sync_api")
    server = ThreadingHTTPServer(("127.0.0.1", 0), AdminFixtureHandler)
    server.mutation_paths = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with playwright.sync_playwright() as runner:
            browser = runner.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(f"http://127.0.0.1:{server.server_port}/sub-admin/", wait_until="networkidle")
            page.locator('.nav-item[data-page="legacy-transitions"]').click()
            page.wait_for_selector('[data-transition-id="501"]')

            page.locator('[data-transition-id="501"]').click()
            page.wait_for_selector("#lct-cancel")
            page.once("dialog", lambda dialog: dialog.dismiss())
            page.locator("#lct-cancel").click()
            page.wait_for_timeout(100)
            assert not any("/cancel" in path for path in server.mutation_paths)

            page.once("dialog", lambda dialog: dialog.accept("short"))
            page.locator("#lct-cancel").click()
            page.wait_for_timeout(100)
            assert not any("/cancel" in path for path in server.mutation_paths)

            page.locator(".ops-close").last.click()
            page.locator('[data-transition-id="502"]').click()
            page.wait_for_selector("#lct-retry")
            page.once("dialog", lambda dialog: dialog.dismiss())
            page.locator("#lct-retry").click()
            page.wait_for_timeout(100)
            assert not any("/retry-review" in path for path in server.mutation_paths)
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
