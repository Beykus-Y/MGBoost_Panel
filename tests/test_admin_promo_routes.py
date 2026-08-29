"""PH5-13 admin promo routes: definitions create/list/disable behind
`require_admin_auth` + primary capability, and the read-only redemptions
inspection. Route-layer tests only -- store semantics live in
tests/test_promo.py."""

import pytest

from tests._ops_helpers import (  # noqa: F401
    PRIMARY,
    PRIMARY_LOGIN,
    db,
    make_handler,
)


def test_create_requires_auth_and_primary(db):
    from src.routes import admin_promo as AP

    payload = {"code": "TESTCODE", "effect_kind": "EXTEND_SUBSCRIPTION",
               "effect_params": {"days": 7}, "reason": "route test definition",
               "idempotency_key": "promo-route-key-00000001"}
    h = make_handler(db, payload=payload, authenticated=False)
    AP.handle_admin_promo_create(h)
    assert h.status == 401

    h = make_handler(db, payload=payload, primary=False)
    AP.handle_admin_promo_create(h)
    assert h.status == 403
    assert db._conn.execute(
        "SELECT COUNT(*) c FROM mgboost_promo_definitions").fetchone()["c"] == 0


def test_create_list_and_reject_duplicate(db):
    from src.routes import admin_promo as AP

    payload = {"code": "ROUTECODE", "effect_kind": "EXTEND_SUBSCRIPTION",
               "effect_params": {"days": 7}, "per_user_limit": 1,
               "reason": "route test definition",
               "idempotency_key": "promo-route-key-00000002"}
    h = make_handler(db, payload=payload)
    AP.handle_admin_promo_create(h)
    assert h.status == 200
    assert h.json()["code"] == "ROUTECODE"

    duplicate = make_handler(db, payload=dict(payload, idempotency_key="promo-route-key-00000003"))
    AP.handle_admin_promo_create(duplicate)
    assert duplicate.status == 409

    listing = make_handler(db, command="GET")
    AP.handle_admin_promo_list(listing)
    assert listing.status == 200
    codes = [d["code"] for d in listing.json()["definitions"]]
    assert codes == ["ROUTECODE"]


def test_create_rejects_bad_code_and_effect_kind(db):
    from src.routes import admin_promo as AP

    h = make_handler(db, payload={"code": "lower-case", "effect_kind": "EXTEND_SUBSCRIPTION",
                                  "effect_params": {"days": 7}, "reason": "route test definition",
                                  "idempotency_key": "promo-route-key-00000004"})
    AP.handle_admin_promo_create(h)
    assert h.status == 400

    h = make_handler(db, payload={"code": "GOODCODE", "effect_kind": "NOT_A_KIND",
                                  "effect_params": {"days": 7}, "reason": "route test definition",
                                  "idempotency_key": "promo-route-key-00000005"})
    AP.handle_admin_promo_create(h)
    assert h.status == 400


def test_disable_route(db):
    from src.routes import admin_promo as AP

    h = make_handler(db, payload={"code": "DISME", "effect_kind": "EXTEND_SUBSCRIPTION",
                                  "effect_params": {"days": 7}, "reason": "route test definition",
                                  "idempotency_key": "promo-route-key-00000006"})
    AP.handle_admin_promo_create(h)
    assert h.status == 200

    h = make_handler(db, payload={"reason": "promo campaign ended"})
    AP.handle_admin_promo_disable(h, "DISME")
    assert h.status == 200
    assert h.json()["status"] == "DISABLED"

    missing = make_handler(db, payload={"reason": "promo campaign ended"})
    AP.handle_admin_promo_disable(missing, "NEVEREXISTED")
    assert missing.status == 404


def test_redemptions_list_is_read_only_and_empty_by_default(db):
    from src.routes import admin_promo as AP

    h = make_handler(db, command="GET")
    AP.handle_admin_promo_redemptions(h)
    assert h.status == 200
    assert h.json()["redemptions"] == []
