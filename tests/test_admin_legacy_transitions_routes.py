"""Dedicated HTTP authority/input/state/privacy contract for P0 transitions."""
import json

from tests._ops_helpers import db, make_handler  # noqa: F401
from tests.test_legacy_commercial_transition import _legacy, _payment


def _transition(db, *, suffix="route"):
    account_id, cap = _legacy(
        db, expiry=3600, username=f"lct-route-{suffix}", tg=880000+len(suffix),
    )
    payment = _payment(db, cap, account_id, tag=suffix)
    transition = db.legacy_commercial_transitions.create(
        cap, payment_record_id=payment["id"], reason="route-level paid transition", now=1000,
    )
    return account_id, cap, payment, transition


def test_get_routes_require_auth_and_primary_and_prevent_enumeration(db):
    from src.routes import admin_legacy_transitions as routes
    account_id, _cap, _payment_row, transition = _transition(db, suffix="get-auth")
    for call, identifier in (
        (routes.handle_transition_detail, transition["id"]),
        (routes.handle_account_transition, account_id),
    ):
        unauth = make_handler(db, command="GET", authenticated=False)
        call(unauth, identifier)
        assert unauth.status == 401
        secondary = make_handler(db, command="GET", primary=False)
        call(secondary, identifier)
        assert secondary.status == 403
        primary = make_handler(db, command="GET")
        call(primary, identifier)
        assert primary.status == 200


def test_all_post_routes_require_auth_primary_and_csrf(db):
    from src.routes import admin_legacy_transitions as routes
    account_id, _cap, payment, transition = _transition(db, suffix="post-auth")
    calls = [
        (routes.handle_transition_create, payment["id"], {"reason": "duplicate create route"}),
        (routes.handle_transition_confirm, transition["id"], None),
        (routes.handle_transition_cancel, transition["id"], {"reason": "cancel route evidence"}),
        (routes.handle_transition_select, transition["id"], {"slot_generation_ids": [], "reason": "selection route"}),
        (routes.handle_transition_retry_review, transition["id"], {"reason": "review retry route evidence"}),
    ]
    for call, identifier, payload in calls:
        unauth = make_handler(db, payload=payload, authenticated=False)
        call(unauth, identifier)
        assert unauth.status == 401
        secondary = make_handler(db, payload=payload, primary=False)
        call(secondary, identifier)
        assert secondary.status == 403
        missing = make_handler(db, payload=payload, with_csrf=False)
        call(missing, identifier)
        assert missing.status == 403
        invalid = make_handler(db, payload=payload)
        invalid.headers["X-CSRF-Token"] = "invalid"
        call(invalid, identifier)
        assert invalid.status == 403


def test_create_confirm_replay_cancel_and_wrong_state_are_fail_closed(db):
    from src.routes import admin_legacy_transitions as routes
    _account_id, _cap, payment, transition = _transition(db, suffix="state")
    duplicate = make_handler(db, payload={"reason": "duplicate transition create"})
    routes.handle_transition_create(duplicate, payment["id"])
    assert duplicate.status == 409
    confirm = make_handler(db, payload={})
    routes.handle_transition_confirm(confirm, transition["id"])
    assert confirm.status == 200
    replay = make_handler(db, payload={})
    routes.handle_transition_confirm(replay, transition["id"])
    assert replay.status == 200
    cancel = make_handler(db, payload={"reason": "post confirmation cancellation"})
    routes.handle_transition_cancel(cancel, transition["id"])
    assert cancel.status == 409
    retry = make_handler(db, payload={"reason": "retry in the wrong transition state"})
    routes.handle_transition_retry_review(retry, transition["id"])
    assert retry.status == 409


def test_selection_rejects_malformed_duplicate_and_foreign_generation(db):
    from src.routes import admin_legacy_transitions as routes
    account_id, cap, payment, transition = _transition(db, suffix="select")
    transition = db.legacy_commercial_transitions.confirm_payment(cap, transition["id"], now=1000)
    for values in ("not-an-array", [True], [1, 1], ["1"]):
        handler = make_handler(
            db, payload={"slot_generation_ids": values, "reason": "malformed selection"},
        )
        routes.handle_transition_select(handler, transition["id"])
        assert handler.status in {400, 409}
    foreign_account, _foreign_cap = _legacy(
        db, expiry=3600, username="lct-route-foreign", tg=889991,
    )
    from tests.test_child_provisioning import HWID_KEY
    foreign_generation = db.device_slots.claim(
        foreign_account, "route-foreign-hwid", HWID_KEY, now=1200,
    )["generation_id"]
    handler = make_handler(
        db, payload={"slot_generation_ids": [foreign_generation], "reason": "foreign selection"},
    )
    routes.handle_transition_select(handler, transition["id"])
    assert handler.status == 409


def test_malformed_body_invalid_ids_and_privacy_projection(db):
    from src.routes import admin_legacy_transitions as routes
    _account_id, _cap, _payment_row, transition = _transition(db, suffix="privacy")
    malformed = make_handler(db, payload={"reason": "placeholder"})
    malformed.rfile.seek(0)
    malformed.rfile.truncate(0)
    malformed.rfile.write(b"{")
    malformed.rfile.seek(0)
    malformed.headers["Content-Length"] = "1"
    routes.handle_transition_cancel(malformed, transition["id"])
    assert malformed.status == 400
    missing = make_handler(db, command="GET")
    routes.handle_transition_detail(missing, 999999999)
    assert missing.status == 404
    detail = make_handler(db, command="GET")
    routes.handle_transition_detail(detail, transition["id"])
    encoded = json.dumps(detail.json(), sort_keys=True).lower()
    for forbidden in ("uuid", "hwid", "bearer", "password", "secret", "subscription_url"):
        assert forbidden not in encoded


def test_route_cannot_spoof_plan_duration_amount_or_version(db):
    from src.routes import admin_legacy_transitions as routes
    from src.routes import admin_payments
    from src.plan_catalog import RUB_PRICES
    account_id, _cap = _legacy(
        db, expiry=3600, username="lct-route-spoof", tg=889992,
    )
    base = {
        "plan_code": "BASIC", "duration_days": 30,
        "recorded_amount_minor": RUB_PRICES[("BASIC", 30)],
        "payment_method": "bank_transfer", "external_reference": "route-spoof-ref",
        "idempotency_key": "route-spoof-payment-key-0001",
    }
    wrong_amount = make_handler(db, payload={**base, "recorded_amount_minor": 1})
    admin_payments.handle_manual_payment_create(wrong_amount, str(account_id))
    assert wrong_amount.status == 400
    wrong_duration = make_handler(
        db, payload={**base, "duration_days": 31,
                     "external_reference": "route-spoof-duration",
                     "idempotency_key": "route-spoof-payment-key-0002"},
    )
    admin_payments.handle_manual_payment_create(wrong_duration, str(account_id))
    assert wrong_duration.status == 400
    valid = make_handler(db, payload=base)
    admin_payments.handle_manual_payment_create(valid, str(account_id))
    assert valid.status == 200
    payment_id = valid.json()["payment"]["id"]
    create = make_handler(db, payload={
        "reason": "canonical payment controls transition",
        "plan_version_id": 999999, "duration_days": 60, "amount_minor": 1,
    })
    routes.handle_transition_create(create, payment_id)
    assert create.status == 201
    view = create.json()["transition"]
    assert view["target_plan_code"] == "BASIC"
    assert view["duration_days"] == 30
    assert view["expected_amount_minor"] == RUB_PRICES[("BASIC", 30)]
