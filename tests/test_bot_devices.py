from src.bot_devices import list_devices, release_device, sweep_pending
from tests.test_child_lifecycle import _build_applied_child, _revoke_fn, db

import asyncio


def test_one_request_revokes_remote_then_frees_slot(db):
    fx = _build_applied_child(db)
    account_id = fx["account"]["account_id"]
    slot_number = fx["slot"]["slot_number"]
    generation_id = fx["slot"]["generation_id"]
    assert any(d["generation_id"] == generation_id for d in list_devices(db, account_id))

    result = release_device(
        db, account_id=account_id, slot_number=slot_number,
        generation_id=generation_id, revoke_fn=_revoke_fn(fx["remote"]), now=300,
    )
    assert result == "done"
    assert fx["remote"].users[fx["child_username"]]["status"] == "disabled"
    assert not list_devices(db, account_id)
    assert release_device(
        db, account_id=account_id, slot_number=slot_number,
        generation_id=generation_id, revoke_fn=_revoke_fn(fx["remote"]), now=301,
    ) == "done"


def test_device_list_shows_client_only_for_proven_generation(db):
    fx = _build_applied_child(db)
    account_id = fx["account"]["account_id"]
    generation_id = fx["slot"]["generation_id"]
    verifier = db._conn.execute(
        "SELECT hwid_verifier FROM mgboost_device_slot_generations WHERE id=?",
        (generation_id,),
    ).fetchone()[0]
    db.device_telemetry.record_observation(
        account_id=account_id, slot_generation_id=generation_id,
        hwid_verifier=verifier, model="Phone", client_name="Hiddify",
        client_version="2.0", now=300,
    )
    devices = list_devices(db, account_id)
    assert devices[0]["model"] == "Phone"
    assert devices[0]["client_name"] == "Hiddify"
    assert devices[0]["client_version"] == "2.0"


def test_failed_remote_revoke_keeps_slot_until_background_retry(db):
    fx = _build_applied_child(db)
    account_id = fx["account"]["account_id"]
    slot_number = fx["slot"]["slot_number"]
    generation_id = fx["slot"]["generation_id"]

    def unavailable(_payload):
        raise OSError("broker unavailable")

    assert release_device(
        db, account_id=account_id, slot_number=slot_number,
        generation_id=generation_id, revoke_fn=unavailable, now=300,
    ) == "pending"
    assert list_devices(db, account_id)
    sweep_pending(db, revoke_fn=_revoke_fn(fx["remote"]), now=421)
    assert not list_devices(db, account_id)


def test_bot_button_is_scoped_to_telegram_owner(db, monkeypatch):
    from aiogram import Dispatcher
    from src.bot_support import setup_support_handlers
    from tests.test_bot_legacy_stars_switch_review_fixes import FakeCall, _get_handler
    from src.routes import admin_devices

    fx = _build_applied_child(db)
    monkeypatch.setattr(admin_devices, "_revoke_fn", _revoke_fn(fx["remote"]))
    dp = Dispatcher()
    setup_support_handlers(dp, db, marzban=None)
    callback = _get_handler(dp.callback_query, "cb_device_release")
    data = f"dev_release:{fx['slot']['slot_number']}:{fx['slot']['generation_id']}"

    stranger = FakeCall(777777, data)
    asyncio.run(callback(stranger))
    assert list_devices(db, fx["account"]["account_id"])

    owner = FakeCall(555001, data)
    asyncio.run(callback(owner))
    assert "слот свободен" in owner.message.answers[-1][0]
    assert not list_devices(db, fx["account"]["account_id"])
