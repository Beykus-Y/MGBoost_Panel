"""PH4-04 LK presentation: opaque credential issue/status gated by the same
`_require_mgmt_session` boundary every other destructive LK device action
already requires -- never the bare legacy subscription token alone."""

import importlib
import json
import os
import tempfile

import pytest

from tests.test_child_provisioning import PRIMARY, PRIMARY_LOGIN, _account
from tests.test_lk_mgmt_routes import (
    FakeHandler,
    _get_mgmt_session_cookie,
    _mock_username_lookup,  # noqa: F401 (autouse fixture)
    _reset_lk_module_state,  # noqa: F401 (autouse fixture)
)


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
    yield instance
    instance._conn.close()


def _reviewed_account(db, mapping, username):
    account, _alias_id, _slot = _account(db, mapping=mapping, tg=940400000 + hash(mapping) % 1000, alias=username)
    return account


def test_status_requires_mgmt_session(db, monkeypatch):
    monkeypatch.setenv("OPAQUE_SUBSCRIPTION_ENABLED", "1")
    import importlib, src.config as config
    importlib.reload(config)
    import src.routes.lk as lk_mod
    importlib.reload(lk_mod)

    h = FakeHandler(db, path="/lk/api/opaque-subscription", headers={"Cookie": "token=tok-nouser"})
    lk_mod.handle_lk_opaque_subscription_status(h)
    assert h._response_code == 400


def test_status_reports_unavailable_for_account_without_review(db):
    import src.routes.lk as lk_mod
    session_cookie = _get_mgmt_session_cookie(db, 1234501, "unreviewed_user")
    h = FakeHandler(db, headers={"Cookie": f"mgmt_session={session_cookie}"})
    lk_mod.handle_lk_opaque_subscription_status(h)
    assert h._response_code == 200
    assert h.json_response()["available"] is False


def test_issue_disabled_by_default_flag(db):
    import importlib, src.config as config
    importlib.reload(config)
    import src.routes.lk as lk_mod
    importlib.reload(lk_mod)
    account = _reviewed_account(db, "LK_OPAQUE_A", "lk_opaque_a")
    session_cookie = _get_mgmt_session_cookie(db, 1234502, "lk_opaque_a")
    h = FakeHandler(db, headers={"Cookie": f"mgmt_session={session_cookie}"})
    h.command = "POST"
    lk_mod.handle_lk_opaque_subscription_issue(h)
    assert h._response_code == 404


def test_issue_requires_mgmt_session_not_bare_token(db, monkeypatch):
    monkeypatch.setenv("OPAQUE_SUBSCRIPTION_ENABLED", "1")
    import importlib, src.config as config
    importlib.reload(config)
    import src.routes.lk as lk_mod
    importlib.reload(lk_mod)
    account = _reviewed_account(db, "LK_OPAQUE_B", "lk_opaque_b")

    h = FakeHandler(db, headers={"Cookie": "token=tok-lk_opaque_b"})
    h.command = "POST"
    lk_mod.handle_lk_opaque_subscription_issue(h)
    # bare legacy token, no mgmt_session -- must not be sufficient
    assert h._response_code in (400, 401, 403)
    assert db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_subscription_credentials"
    ).fetchone()[0] == 0


def test_issue_with_valid_mgmt_session_returns_raw_token_once(db, monkeypatch):
    monkeypatch.setenv("OPAQUE_SUBSCRIPTION_ENABLED", "1")
    import importlib, src.config as config
    importlib.reload(config)
    import src.routes.lk as lk_mod
    importlib.reload(lk_mod)
    account = _reviewed_account(db, "LK_OPAQUE_C", "lk_opaque_c")
    session_cookie = _get_mgmt_session_cookie(db, 1234503, "lk_opaque_c")

    h = FakeHandler(db, headers={"Cookie": f"mgmt_session={session_cookie}"})
    h.command = "POST"
    lk_mod.handle_lk_opaque_subscription_issue(h)
    assert h._response_code == 200
    body = h.json_response()
    assert len(body["raw_token"]) == 43
    resolved = db.subscription_credentials.resolve(body["raw_token"])
    assert resolved is not None and resolved["account_id"] == account["account_id"]

    status_h = FakeHandler(db, headers={"Cookie": f"mgmt_session={session_cookie}"})
    lk_mod.handle_lk_opaque_subscription_status(status_h)
    status_body = status_h.json_response()
    assert "raw_token" not in json.dumps(status_body)


def test_reissue_without_confirm_requires_confirmation_and_does_not_rotate(db, monkeypatch):
    monkeypatch.setenv("OPAQUE_SUBSCRIPTION_ENABLED", "1")
    import importlib, src.config as config
    importlib.reload(config)
    import src.routes.lk as lk_mod
    importlib.reload(lk_mod)
    account = _reviewed_account(db, "LK_OPAQUE_D", "lk_opaque_d")
    session_cookie = _get_mgmt_session_cookie(db, 1234504, "lk_opaque_d")

    first = FakeHandler(db, headers={"Cookie": f"mgmt_session={session_cookie}"})
    first.command = "POST"
    lk_mod.handle_lk_opaque_subscription_issue(first)
    old_token = first.json_response()["raw_token"]

    lk_mod._mutation_cooldown.clear()
    second = FakeHandler(db, headers={"Cookie": f"mgmt_session={session_cookie}"})
    second.command = "POST"
    lk_mod.handle_lk_opaque_subscription_issue(second)
    assert second._response_code == 409
    assert second.json_response()["reason"] == "requires_confirmation"
    assert db.subscription_credentials.resolve(old_token) is not None


def test_reissue_with_explicit_confirm_rotates(db, monkeypatch):
    monkeypatch.setenv("OPAQUE_SUBSCRIPTION_ENABLED", "1")
    import importlib, src.config as config
    importlib.reload(config)
    import src.routes.lk as lk_mod
    importlib.reload(lk_mod)
    account = _reviewed_account(db, "LK_OPAQUE_E", "lk_opaque_e")
    session_cookie = _get_mgmt_session_cookie(db, 1234505, "lk_opaque_e")

    first = FakeHandler(db, headers={"Cookie": f"mgmt_session={session_cookie}"})
    first.command = "POST"
    lk_mod.handle_lk_opaque_subscription_issue(first)
    old_token = first.json_response()["raw_token"]

    lk_mod._mutation_cooldown.clear()
    second = FakeHandler(
        db, headers={"Cookie": f"mgmt_session={session_cookie}"},
        body=json.dumps({"confirm": True}).encode(),
    )
    second.command = "POST"
    lk_mod.handle_lk_opaque_subscription_issue(second)
    assert second._response_code == 200
    new_token = second.json_response()["raw_token"]

    assert old_token != new_token
    assert db.subscription_credentials.resolve(old_token) is None
    assert db.subscription_credentials.resolve(new_token) is not None
