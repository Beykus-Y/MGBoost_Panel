"""PH4-04 corrective fix: a normal human browser opening a valid ACTIVE
opaque URL must see the existing legacy browser landing page instead of
the uniform invalid response -- and must never claim a slot, provision a
child, or mutate any device/migration state. Invalid/unknown/revoked/
expired-parent tokens still get the exact same uniform invalid response
regardless of User-Agent."""

import importlib
import io
import os
import tempfile

import pytest

from tests.test_child_provisioning import HWID_KEY, PRIMARY, PRIMARY_LOGIN, _account
from tests.test_opaque_resolver import _issue_active_credential


@pytest.fixture
def db(monkeypatch):
    tmp = tempfile.mkdtemp()
    monkeypatch.setenv("DATA_DIR", tmp)
    monkeypatch.setenv("PRIMARY_MGBOOST_ADMIN_ACTOR_ID", PRIMARY)
    monkeypatch.setenv("PRIMARY_MGBOOST_ADMIN_LOGIN", PRIMARY_LOGIN)
    monkeypatch.setenv("OPAQUE_SUBSCRIPTION_ENABLED", "1")
    import src.config as config
    import src.database as database
    import src.routes.sub as sub_route
    import src.routes.opaque_sub as opaque_sub_route
    importlib.reload(config)
    importlib.reload(database)
    # `opaque_sub`/`sub` import config values by copy at import time -- must
    # be reloaded too so the fresh OPAQUE_SUBSCRIPTION_ENABLED/env values
    # actually take effect (same pattern test_opaque_sub_route.py uses).
    importlib.reload(sub_route)
    importlib.reload(opaque_sub_route)
    database.DB_PATH = os.path.join(tmp, "db.sqlite3")
    instance = database.Database()
    yield instance
    instance._conn.close()


class FakeHandler:
    def __init__(self, db, *, user_agent="", peer="127.0.0.1"):
        self.client_address = (peer, 12345)
        self.headers = {"User-Agent": user_agent, "Host": "sub.beykus.fun"}
        self.server = type("S", (), {"db": db})()
        self.status = None
        self.sent_headers = []
        self.wfile = io.BytesIO()

    def send_response(self, status, message=None):
        self.status = status

    def send_header(self, k, v):
        self.sent_headers.append((k, v))

    def end_headers(self):
        pass


BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0"
CLIENT_UA = "Happ/2.7.0"


def _mutation_counts(db):
    return (
        db._conn.execute("SELECT COUNT(*) FROM mgboost_device_slot_generations").fetchone()[0],
        db._conn.execute("SELECT COUNT(*) FROM mgboost_child_user_intents").fetchone()[0],
        db._conn.execute("SELECT COUNT(*) FROM mgboost_outbox").fetchone()[0],
    )


def test_valid_token_browser_gets_landing_page_not_invalid(db, monkeypatch):
    from src.routes import opaque_sub as mod
    account, _alias_id, _slot = _account(db, mapping="BROWSER_A", tg=950500001)
    token = _issue_active_credential(db, account["account_id"], idem_prefix="browser-landing-a")

    handler = FakeHandler(db, user_agent=BROWSER_UA)
    mod.handle_opaque_sub(handler, token)
    assert handler.status == 200
    body = handler.wfile.getvalue()
    assert b"Subscription not found" not in body
    assert b"\xd0\xa1\xd1\x81\xd1\x8b\xd0\xbb\xd0\xba\xd1\x83" in body or b"copy" in body.lower() or len(body) > 100


def test_browser_visit_creates_zero_slot_child_mutations(db):
    from src.routes import opaque_sub as mod
    account, _alias_id, _slot = _account(db, mapping="BROWSER_B", tg=950500002)
    token = _issue_active_credential(db, account["account_id"], idem_prefix="browser-landing-b")

    before = _mutation_counts(db)
    handler = FakeHandler(db, user_agent=BROWSER_UA)
    mod.handle_opaque_sub(handler, token)
    after = _mutation_counts(db)
    assert handler.status == 200
    assert before == after


def test_supported_client_still_gets_working_subscription(db):
    from src.routes import opaque_sub as mod
    from tests.test_opaque_resolver import _seed_account_with_first_child

    account, _alias_id, _slot, _remote, ensure_fn, subscription_fn = _seed_account_with_first_child(
        db, mapping="BROWSER_C", tg=950500003,
    )
    token = _issue_active_credential(db, account["account_id"], idem_prefix="browser-landing-c")
    monkeypatched = mod._client
    monkeypatched_key = mod.DEVICE_SLOT_HMAC_KEY
    mod._ensure_fn = ensure_fn
    mod._subscription_fn = subscription_fn
    mod.DEVICE_SLOT_HMAC_KEY = HWID_KEY
    try:
        handler = FakeHandler(db, user_agent=CLIENT_UA)
        handler.headers["device-id"] = "browser-landing-c-device-1"
        handler.headers["x-platform"] = "windows"
        mod.handle_opaque_sub(handler, token)
        assert handler.status == 200
        body = handler.wfile.getvalue()
        assert b"Subscription not found" not in body
    finally:
        mod._client = monkeypatched
        mod.DEVICE_SLOT_HMAC_KEY = monkeypatched_key


def test_invalid_token_browser_gets_uniform_invalid_not_landing(db):
    from src.routes import opaque_sub as mod
    handler = FakeHandler(db, user_agent=BROWSER_UA)
    mod.handle_opaque_sub(handler, "A" * 43)
    assert handler.status == 404
    assert handler.wfile.getvalue() == b"Subscription not found\n"


def test_revoked_token_browser_gets_uniform_invalid(db):
    from src.routes import opaque_sub as mod
    account, _alias_id, _slot = _account(db, mapping="BROWSER_D", tg=950500004)
    token = _issue_active_credential(db, account["account_id"], idem_prefix="browser-landing-d")
    row = db._conn.execute(
        "SELECT id FROM mgboost_subscription_credentials WHERE account_id=? AND status='ACTIVE'",
        (account["account_id"],),
    ).fetchone()
    db.subscription_credentials.revoke(
        credential_id=row["id"], account_id=account["account_id"], reason_code="ADMIN_MANUAL",
        actor_ref="test", idempotency_key="browser-landing-d-revoke-0001",
    )
    handler = FakeHandler(db, user_agent=BROWSER_UA)
    mod.handle_opaque_sub(handler, token)
    assert handler.status == 404
    assert handler.wfile.getvalue() == b"Subscription not found\n"


def test_expired_parent_browser_gets_uniform_invalid_not_misleading_landing(db):
    from src.routes import opaque_sub as mod
    account, _alias_id, _slot = _account(db, mapping="BROWSER_E", tg=950500005)
    token = _issue_active_credential(db, account["account_id"], idem_prefix="browser-landing-e")
    db._conn.execute(
        "UPDATE mgboost_subscriptions SET status='EXPIRED' WHERE account_id=?",
        (account["account_id"],),
    )
    db._conn.commit()
    handler = FakeHandler(db, user_agent=BROWSER_UA)
    mod.handle_opaque_sub(handler, token)
    assert handler.status == 404
    assert handler.wfile.getvalue() == b"Subscription not found\n"


def test_browser_landing_headers_no_store_no_referrer_csp(db):
    from src.routes import opaque_sub as mod
    account, _alias_id, _slot = _account(db, mapping="BROWSER_F", tg=950500006)
    token = _issue_active_credential(db, account["account_id"], idem_prefix="browser-landing-f")
    handler = FakeHandler(db, user_agent=BROWSER_UA)
    mod.handle_opaque_sub(handler, token)
    headers = dict(handler.sent_headers)
    assert headers.get("Cache-Control") == "no-store"
    assert headers.get("Referrer-Policy") == "no-referrer"
    assert "Content-Security-Policy" in headers
    assert headers.get("X-Content-Type-Options") == "nosniff"
    assert headers.get("X-Frame-Options") == "DENY"


def test_raw_token_not_in_query_string_of_landing_url(db):
    from src.routes import opaque_sub as mod
    account, _alias_id, _slot = _account(db, mapping="BROWSER_G", tg=950500007)
    token = _issue_active_credential(db, account["account_id"], idem_prefix="browser-landing-g")
    handler = FakeHandler(db, user_agent=BROWSER_UA)
    mod.handle_opaque_sub(handler, token)
    body = handler.wfile.getvalue()
    assert f"?{token}".encode() not in body
    assert f"https://sub.beykus.fun/{token}".encode() in body
