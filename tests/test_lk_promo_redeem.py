"""PH5-13 LK self-service promo redemption: gated by the SAME
`_require_mgmt_session` boundary as every destructive LK action, deterministic
client `request_id` idempotency, OWNER telegram identity as the actor."""

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
    from src.plan_catalog import seed_plan_catalog
    seed_plan_catalog(instance.plan_catalog, now=1)
    yield instance
    instance._conn.close()


def _wl_account_with_alias(db, *, mapping, tg, alias):
    """A reviewed account with a working alias -> mgmt-session resolution and
    a WL (LIMITED) plan identity, so a self-service promo EXTEND is eligible.
    The reviewed fixture creates the account UNLIMITED; route-level tests only
    need the WL state, so switch the plan identity in place (the promo
    eligibility logic itself is unit-tested in tests/test_promo.py)."""
    account, _alias_id, _slot = _account(db, mapping=mapping, tg=tg, alias=alias)
    wl_plan = db.plan_catalog.get_plan_version("WL")
    db._conn.execute(
        "UPDATE mgboost_subscriptions SET current_plan_version_id=?,status='ACTIVE' "
        "WHERE account_id=?", (wl_plan["id"], account["account_id"]),
    )
    db._conn.commit()
    return account, None, _admin_cap(db)


def _define_wl_promo(db, cap, code="LKWL7"):
    from tests.test_promo import _define
    return _define(db, cap, code=code, effect_kind="EXTEND_SUBSCRIPTION",
                   effect_params={"days": 7})


def _post(db, *, session, body):
    h = FakeHandler(
        db, path="/lk/api/promo/redeem",
        body=json.dumps(body).encode(),
        headers={"Cookie": f"mgmt_session={session}", "Content-Type": "application/json"},
    )
    from src.routes.lk import handle_lk_promo_redeem
    handle_lk_promo_redeem(h)
    return h


def test_redeem_requires_mgmt_session(db):
    _wl_account_with_alias(db, mapping="LK_PROMO_A", tg=940400001, alias="lk_promo_a")
    h = FakeHandler(
        db, path="/lk/api/promo/redeem",
        body=json.dumps({"code": "X", "request_id": "aaaaaaaaaaaa"}).encode(),
        headers={"Content-Type": "application/json"},
    )
    from src.routes.lk import handle_lk_promo_redeem
    handle_lk_promo_redeem(h)
    assert h._response_code == 400


def test_redeem_requires_request_id_never_mints_one(db):
    _wl_account_with_alias(db, mapping="LK_PROMO_B", tg=940400002, alias="lk_promo_b")
    _define_wl_promo(db, _admin_cap(db))
    session = _get_mgmt_session_cookie(db, 940400002, "lk_promo_b")
    h = _post(db, session=session, body={"code": "LKWL7"})
    assert h._response_code == 400
    assert h.json_response()["reason"] == "request_id_required"
    assert db._conn.execute(
        "SELECT COUNT(*) c FROM mgboost_promo_redemptions"
    ).fetchone()["c"] == 0


def _admin_cap(db):
    from src.security import AdminSessionStore
    _raw, session = AdminSessionStore().create(PRIMARY_LOGIN, "test-server-jwt")
    return db.primary_admin_authority.authorize_session(session)


def test_redeem_success_and_request_id_replay(db):
    _wl_account_with_alias(db, mapping="LK_PROMO_C", tg=940400003, alias="lk_promo_c")
    _define_wl_promo(db, _admin_cap(db))
    session = _get_mgmt_session_cookie(db, 940400003, "lk_promo_c")

    first = _post(db, session=session, body={"code": "lkwl7", "request_id": "req-11111111"})
    assert first._response_code == 200
    assert first.json_response()["status"] == "REDEEMED"

    # Same request_id retried (network retry / double click): replay, not a
    # second redemption.
    second = _post(db, session=session, body={"code": "LKWL7", "request_id": "req-11111111"})
    assert second._response_code == 200
    assert second.json_response()["already_applied"] is True
    assert db._conn.execute(
        "SELECT COUNT(*) c FROM mgboost_promo_redemptions"
    ).fetchone()["c"] == 1


def test_redeem_unknown_code_is_404(db):
    _wl_account_with_alias(db, mapping="LK_PROMO_D", tg=940400004, alias="lk_promo_d")
    session = _get_mgmt_session_cookie(db, 940400004, "lk_promo_d")
    h = _post(db, session=session, body={"code": "NOSUCHCODE", "request_id": "req-22222222"})
    assert h._response_code == 404
