"""PH4-04 admin route tests: auth/CSRF/IDOR boundaries, one-time raw token
delivery, non-primary-admin rejection, and audit trail without raw tokens."""

import importlib
import io
import json
import os
import tempfile

import pytest

from src import security
from src.routes import subscription_credentials_admin as route_module

from tests.test_child_provisioning import PRIMARY, PRIMARY_LOGIN, _account


class FakeHandler:
    def __init__(self, *, method="GET", body=b"", headers=None):
        self.command = method
        self.rfile = io.BytesIO(body)
        self.wfile = io.BytesIO()
        self.headers = {"Content-Length": str(len(body)), **(headers or {})}
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


@pytest.fixture
def db(monkeypatch):
    tmp = tempfile.mkdtemp()
    monkeypatch.setenv("DATA_DIR", tmp)
    monkeypatch.setenv("PRIMARY_MGBOOST_ADMIN_ACTOR_ID", PRIMARY)
    monkeypatch.setenv("PRIMARY_MGBOOST_ADMIN_LOGIN", PRIMARY_LOGIN)
    import src.config as config
    import src.database as database
    importlib.reload(config)
    importlib.reload(database)
    database.DB_PATH = os.path.join(tmp, "db.sqlite3")
    instance = database.Database()
    instance.server = type("S", (), {"db": instance})()  # not used, placeholder
    yield instance
    instance._conn.close()


def _authed_handler(db, *, method="GET", body=b"", primary=True):
    username = PRIMARY_LOGIN if primary else "some-other-admin"
    raw_session_id, session = security.create_admin_session(username, "test-jwt")
    headers = {"Cookie": f"{security.ADMIN_SESSION_COOKIE}={raw_session_id}"}
    if method != "GET":
        headers["X-CSRF-Token"] = session.csrf_token
    handler = FakeHandler(method=method, body=body, headers=headers)
    handler.server = type("S", (), {"db": db})()
    return handler


def test_status_requires_admin_auth(db):
    handler = FakeHandler(method="GET")
    handler.server = type("S", (), {"db": db})()
    route_module.handle_subscription_credential_status(handler, "1")
    assert handler.status == 401


def test_status_unknown_account_is_404(db):
    handler = _authed_handler(db)
    route_module.handle_subscription_credential_status(handler, "999999")
    assert handler.status == 404


def test_status_shows_no_credential_initially(db):
    account, _alias_id, _slot = _account(db, mapping="ADMIN_ROUTE_A", tg=930300001)
    handler = _authed_handler(db)
    route_module.handle_subscription_credential_status(handler, str(account["account_id"]))
    assert handler.status == 200
    assert handler.json()["credential"] is None


def test_issue_requires_admin_auth(db):
    account, _alias_id, _slot = _account(db, mapping="ADMIN_ROUTE_B", tg=930300002)
    handler = FakeHandler(method="POST", body=json.dumps({"reason": "test issue"}).encode())
    handler.server = type("S", (), {"db": db})()
    route_module.handle_subscription_credential_issue(handler, str(account["account_id"]))
    assert handler.status == 401


def test_issue_rejects_csrf_missing(db):
    account, _alias_id, _slot = _account(db, mapping="ADMIN_ROUTE_C", tg=930300003)
    raw_session_id, _session = security.create_admin_session(PRIMARY_LOGIN, "test-jwt")
    handler = FakeHandler(
        method="POST", body=json.dumps({"reason": "test issue"}).encode(),
        headers={"Cookie": f"{security.ADMIN_SESSION_COOKIE}={raw_session_id}"},
    )
    handler.server = type("S", (), {"db": db})()
    route_module.handle_subscription_credential_issue(handler, str(account["account_id"]))
    assert handler.status == 403


def test_issue_rejects_non_primary_admin(db):
    account, _alias_id, _slot = _account(db, mapping="ADMIN_ROUTE_D", tg=930300004)
    handler = _authed_handler(
        db, method="POST", body=json.dumps({"reason": "test issue"}).encode(), primary=False,
    )
    route_module.handle_subscription_credential_issue(handler, str(account["account_id"]))
    assert handler.status == 403


def test_issue_rejects_unknown_account_idor(db):
    handler = _authed_handler(
        db, method="POST", body=json.dumps({"reason": "test issue"}).encode(),
    )
    route_module.handle_subscription_credential_issue(handler, "999999")
    assert handler.status == 404


def test_issue_returns_raw_token_exactly_once_and_status_never_does(db):
    account, _alias_id, _slot = _account(db, mapping="ADMIN_ROUTE_E", tg=930300005)
    handler = _authed_handler(
        db, method="POST", body=json.dumps({"reason": "canary issuance"}).encode(),
    )
    route_module.handle_subscription_credential_issue(handler, str(account["account_id"]))
    assert handler.status == 200
    body = handler.json()
    assert len(body["raw_token"]) == 43
    assert body["canonical_url"] == f"https://sub.beykus.fun/{body['raw_token']}"
    assert body["credential"]["status"] == "ACTIVE"

    status_handler = _authed_handler(db)
    route_module.handle_subscription_credential_status(status_handler, str(account["account_id"]))
    status_body = status_handler.json()
    assert "raw_token" not in status_body
    assert json.dumps(status_body).find(body["raw_token"]) == -1


def test_reissue_without_confirm_requires_confirmation_and_does_not_rotate(db):
    account, _alias_id, _slot = _account(db, mapping="ADMIN_ROUTE_F", tg=930300006)
    first_handler = _authed_handler(
        db, method="POST", body=json.dumps({"reason": "first issuance"}).encode(),
    )
    route_module.handle_subscription_credential_issue(first_handler, str(account["account_id"]))
    old_token = first_handler.json()["raw_token"]

    second_handler = _authed_handler(
        db, method="POST", body=json.dumps({"reason": "rotate"}).encode(),
    )
    route_module.handle_subscription_credential_issue(second_handler, str(account["account_id"]))
    assert second_handler.status == 409
    assert second_handler.json()["requires_confirmation"] is True
    assert db.subscription_credentials.resolve(old_token) is not None


def test_reissue_with_explicit_confirm_rotates_and_old_token_no_longer_resolves(db):
    account, _alias_id, _slot = _account(db, mapping="ADMIN_ROUTE_F2", tg=930300106)
    first_handler = _authed_handler(
        db, method="POST", body=json.dumps({"reason": "first issuance"}).encode(),
    )
    route_module.handle_subscription_credential_issue(first_handler, str(account["account_id"]))
    old_token = first_handler.json()["raw_token"]

    second_handler = _authed_handler(
        db, method="POST", body=json.dumps({"reason": "rotate", "confirm": True}).encode(),
    )
    route_module.handle_subscription_credential_issue(second_handler, str(account["account_id"]))
    new_token = second_handler.json()["raw_token"]

    assert old_token != new_token
    assert db.subscription_credentials.resolve(old_token) is None
    assert db.subscription_credentials.resolve(new_token) is not None


def test_issue_requires_a_bounded_reason(db):
    account, _alias_id, _slot = _account(db, mapping="ADMIN_ROUTE_G", tg=930300007)
    handler = _authed_handler(db, method="POST", body=json.dumps({"reason": ""}).encode())
    route_module.handle_subscription_credential_issue(handler, str(account["account_id"]))
    assert handler.status == 400


def test_audit_event_has_no_raw_token(db):
    account, _alias_id, _slot = _account(db, mapping="ADMIN_ROUTE_H", tg=930300008)
    handler = _authed_handler(
        db, method="POST", body=json.dumps({"reason": "audit trail check"}).encode(),
    )
    route_module.handle_subscription_credential_issue(handler, str(account["account_id"]))
    raw_token = handler.json()["raw_token"]
    events = db._conn.execute(
        "SELECT reason, actor_ref FROM mgboost_subscription_credential_events "
        "WHERE account_id=?", (account["account_id"],),
    ).fetchall()
    assert len(events) >= 1
    for event in events:
        assert raw_token not in (event["reason"] or "")
        assert raw_token not in (event["actor_ref"] or "")
