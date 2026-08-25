"""PH3-05 durable device revoke/free/rebind lifecycle.

Every scenario proves the hard ordering guarantee: a slot is never freed
before a matching REVOKE lifecycle operation is durably `APPLIED` against a
real remote reread, generations are monotonic and immutable, retries never
duplicate a remote/local effect, and none of this ever touches Telegram
ownership or another account's state.
"""

import json
import threading
import time

import pytest

from src.broker_operations import BrokerOperations
from src.child_contract import credential_verifier, source_contract_hash
from src import child_lifecycle
from src.child_lifecycle import ChildLifecycleConflict, ChildLifecycleError
from src.device_slots import CrossAccountHWID, StaleSlotGeneration

from tests.test_child_provisioning import HWID_KEY, PRIMARY, PRIMARY_LOGIN, _account
from tests.test_marzban_broker import FakeMarzban, UUID


@pytest.fixture
def db(monkeypatch):
    import importlib
    import os
    import tempfile

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


def _build_applied_child(db, *, mapping="LIFECYCLE_TEST", tg=555001, alias="alice"):
    account, alias_id, slot = _account(db, mapping=mapping, tg=tg, alias=alias)
    remote = FakeMarzban()
    if alias != "alice":
        remote.users[alias] = remote.users.pop("alice")
        remote.users[alias]["username"] = alias
    request_hash = source_contract_hash(remote.users[alias])
    prepared = db.child_provisioning.prepare_child_ensure(
        account_id=account["account_id"], slot_generation_id=slot["generation_id"],
        source_alias_id=alias_id, source_contract_hash=request_hash, expire=0,
        idempotency_key=f"lifecycle-fixture-{mapping}", now=200,
    )
    claimed = db.child_provisioning.claim(
        prepared["operation_id"], worker_id="fixture-worker", now=201, lease_seconds=5,
    )
    created = BrokerOperations(remote).dispatch("child.user.ensure", claimed["payload"])
    child_uuid = created.pop("uuid")
    child = db.child_provisioning.acknowledge(
        prepared["operation_id"], worker_id="fixture-worker",
        outcome=created["outcome"], child_uuid=child_uuid, remote_result=created, now=202,
    )
    return {
        "account": account, "alias_id": alias_id, "slot": slot, "remote": remote,
        "prepared": prepared, "child": child, "child_intent_id": prepared["child_intent_id"],
        "child_username": prepared["child_username"], "child_uuid": child_uuid,
    }


def _revoke_fn(remote):
    return lambda payload: BrokerOperations(remote).dispatch("child.user.revoke", payload)


# --- REVOKE ------------------------------------------------------------------

def test_normal_revoke_disables_and_rotates_remote_credential(db):
    fx = _build_applied_child(db)
    prepared = db.child_lifecycle.prepare_revoke(
        account_id=fx["account"]["account_id"], old_child_intent_id=fx["child_intent_id"],
        reason="device replaced", idempotency_key="revoke-1--------", now=300,
    )
    result = child_lifecycle.process_revoke(
        db, prepared["operation_id"], worker_id="worker-one", revoke_fn=_revoke_fn(fx["remote"]), now=301,
    )
    assert result["state"] == "APPLIED"
    remote_user = fx["remote"].users[fx["child_username"]]
    assert remote_user["status"] == "disabled"
    assert remote_user["proxies"]["vless"]["id"] != fx["child_uuid"]
    intent = db._conn.execute(
        "SELECT desired_state,observed_state FROM mgboost_child_user_intents WHERE id=?",
        (fx["child_intent_id"],),
    ).fetchone()
    assert tuple(intent) == ("REVOKED", "REVOKED")


def test_duplicate_revoke_converges_without_double_rotation(db):
    fx = _build_applied_child(db)
    prepared = db.child_lifecycle.prepare_revoke(
        account_id=fx["account"]["account_id"], old_child_intent_id=fx["child_intent_id"],
        reason="device replaced", idempotency_key="revoke-dup------", now=300,
    )
    child_lifecycle.process_revoke(
        db, prepared["operation_id"], worker_id="worker-one", revoke_fn=_revoke_fn(fx["remote"]), now=301,
    )
    rotated_uuid = fx["remote"].users[fx["child_username"]]["proxies"]["vless"]["id"]
    # Prepare is idempotent: same idempotency key returns the same row, no
    # second lifecycle operation, so a "duplicate revoke" is a fresh claim
    # attempt against the exact same (already-APPLIED) operation.
    same = db.child_lifecycle.prepare_revoke(
        account_id=fx["account"]["account_id"], old_child_intent_id=fx["child_intent_id"],
        reason="device replaced", idempotency_key="revoke-dup------", now=305,
    )
    assert same["operation_id"] == prepared["operation_id"]
    assert db.child_lifecycle.claim(prepared["operation_id"], worker_id="worker-two", now=306) is None
    assert fx["remote"].users[fx["child_username"]]["proxies"]["vless"]["id"] == rotated_uuid


def test_lost_ack_after_remote_revoke_reconciles_without_second_mutation(db):
    fx = _build_applied_child(db)
    prepared = db.child_lifecycle.prepare_revoke(
        account_id=fx["account"]["account_id"], old_child_intent_id=fx["child_intent_id"],
        reason="lost ack simulation", idempotency_key="revoke-lost-ack-", now=300,
    )
    claimed = db.child_lifecycle.claim(prepared["operation_id"], worker_id="worker-one", now=301, lease_seconds=5)
    result = BrokerOperations(fx["remote"]).dispatch("child.user.revoke", claimed["payload"])
    assert result["outcome"] == "REVOKED"
    rotated_uuid = fx["remote"].users[fx["child_username"]]["proxies"]["vless"]["id"]
    # Crash before local ACK: lease expires, a second worker reclaims.
    reclaimed_result = child_lifecycle.process_revoke(
        db, prepared["operation_id"], worker_id="worker-two", revoke_fn=_revoke_fn(fx["remote"]), now=340,
    )
    assert reclaimed_result["state"] == "APPLIED"
    assert fx["remote"].users[fx["child_username"]]["proxies"]["vless"]["id"] == rotated_uuid


def test_remote_already_revoked_is_idempotent_success(db):
    fx = _build_applied_child(db)
    fx["remote"].users[fx["child_username"]]["status"] = "disabled"
    prepared = db.child_lifecycle.prepare_revoke(
        account_id=fx["account"]["account_id"], old_child_intent_id=fx["child_intent_id"],
        reason="already disabled out of band", idempotency_key="revoke-already--", now=300,
    )
    result = child_lifecycle.process_revoke(
        db, prepared["operation_id"], worker_id="worker-one", revoke_fn=_revoke_fn(fx["remote"]), now=301,
    )
    assert result["state"] == "APPLIED"


def test_remote_missing_child_is_classified_revoked(db):
    fx = _build_applied_child(db)
    del fx["remote"].users[fx["child_username"]]
    prepared = db.child_lifecycle.prepare_revoke(
        account_id=fx["account"]["account_id"], old_child_intent_id=fx["child_intent_id"],
        reason="remote already gone", idempotency_key="revoke-missing--", now=300,
    )
    result = child_lifecycle.process_revoke(
        db, prepared["operation_id"], worker_id="worker-one", revoke_fn=_revoke_fn(fx["remote"]), now=301,
    )
    assert result["state"] == "APPLIED"


def test_remote_mismatch_fails_closed(db):
    fx = _build_applied_child(db)
    # Drift the remote UUID before revoke is attempted (simulates an
    # ambiguous/contract-mismatch remote state).
    fx["remote"].users[fx["child_username"]]["proxies"]["vless"]["id"] = (
        "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    )
    prepared = db.child_lifecycle.prepare_revoke(
        account_id=fx["account"]["account_id"], old_child_intent_id=fx["child_intent_id"],
        reason="mismatch test", idempotency_key="revoke-mismatch-", now=300,
    )
    with pytest.raises(ValueError, match="verifier mismatch"):
        child_lifecycle.process_revoke(
            db, prepared["operation_id"], worker_id="worker-one", revoke_fn=_revoke_fn(fx["remote"]), now=301,
        )
    row = db._conn.execute(
        "SELECT state FROM mgboost_child_lifecycle_operations WHERE operation_id=?",
        (prepared["operation_id"],),
    ).fetchone()
    assert row["state"] == "IN_FLIGHT"  # never silently marked APPLIED
    intent = db._conn.execute(
        "SELECT observed_state FROM mgboost_child_user_intents WHERE id=?",
        (fx["child_intent_id"],),
    ).fetchone()
    assert intent["observed_state"] == "ACTIVE"  # untouched


def test_outage_then_recovery(db):
    fx = _build_applied_child(db)
    prepared = db.child_lifecycle.prepare_revoke(
        account_id=fx["account"]["account_id"], old_child_intent_id=fx["child_intent_id"],
        reason="outage test", idempotency_key="revoke-outage---", now=300,
    )
    fx["remote"].outage = True
    with pytest.raises(Exception):
        child_lifecycle.process_revoke(
            db, prepared["operation_id"], worker_id="worker-one", revoke_fn=_revoke_fn(fx["remote"]), now=301,
        )
    db.child_lifecycle.retry_later(prepared["operation_id"], delay_seconds=5, now=302)
    fx["remote"].outage = False
    result = child_lifecycle.process_revoke(
        db, prepared["operation_id"], worker_id="worker-one", revoke_fn=_revoke_fn(fx["remote"]), now=310,
    )
    assert result["state"] == "APPLIED"


def test_retry_exhaustion_leaves_slot_occupied_not_error_state_forced(db):
    fx = _build_applied_child(db)
    prepared = db.child_lifecycle.prepare_revoke(
        account_id=fx["account"]["account_id"], old_child_intent_id=fx["child_intent_id"],
        reason="retry exhaustion", idempotency_key="revoke-exhaust--", now=300,
    )
    fx["remote"].outage = True
    clock = 300
    for attempt in range(3):
        with pytest.raises(Exception):
            child_lifecycle.process_revoke(
                db, prepared["operation_id"], worker_id="worker-one",
                revoke_fn=_revoke_fn(fx["remote"]), now=clock,
            )
        db.child_lifecycle.retry_later(prepared["operation_id"], delay_seconds=5, now=clock)
        clock += 10
    row = db._conn.execute(
        "SELECT state FROM mgboost_child_lifecycle_operations WHERE operation_id=?",
        (prepared["operation_id"],),
    ).fetchone()
    assert row["state"] == "RETRY"
    slot = db._conn.execute(
        "SELECT desired_state FROM mgboost_device_slots WHERE id=?", (fx["slot"]["slot_id"],),
    ).fetchone()
    assert slot["desired_state"] == "ACTIVE"  # never freed while revoke is unresolved


# --- FREE ----------------------------------------------------------------------

def test_free_refuses_before_revoke_confirmed(db):
    fx = _build_applied_child(db)
    free_prepared = db.child_lifecycle.prepare_free(
        account_id=fx["account"]["account_id"], old_child_intent_id=fx["child_intent_id"],
        reason="cleanup", idempotency_key="free-no-revoke--", now=300,
    )
    with pytest.raises(ChildLifecycleError, match="not APPLIED"):
        child_lifecycle.process_free(db, free_prepared["operation_id"], worker_id="worker-one", now=301)
    slot = db._conn.execute(
        "SELECT desired_state FROM mgboost_device_slots WHERE id=?", (fx["slot"]["slot_id"],),
    ).fetchone()
    assert slot["desired_state"] == "ACTIVE"


def test_free_succeeds_after_confirmed_revoke(db):
    fx = _build_applied_child(db)
    revoke = db.child_lifecycle.prepare_revoke(
        account_id=fx["account"]["account_id"], old_child_intent_id=fx["child_intent_id"],
        reason="free flow", idempotency_key="revoke-for-free-", now=300,
    )
    child_lifecycle.process_revoke(
        db, revoke["operation_id"], worker_id="worker-one", revoke_fn=_revoke_fn(fx["remote"]), now=301,
    )
    free_prepared = db.child_lifecycle.prepare_free(
        account_id=fx["account"]["account_id"], old_child_intent_id=fx["child_intent_id"],
        reason="free flow", idempotency_key="free-for-real---", now=302,
    )
    result = child_lifecycle.process_free(db, free_prepared["operation_id"], worker_id="worker-one", now=303)
    assert result["state"] == "APPLIED"
    slot = db._conn.execute(
        "SELECT desired_state FROM mgboost_device_slots WHERE id=?", (fx["slot"]["slot_id"],),
    ).fetchone()
    assert slot["desired_state"] == "FREE"


def test_duplicate_free_is_idempotent(db):
    fx = _build_applied_child(db)
    revoke = db.child_lifecycle.prepare_revoke(
        account_id=fx["account"]["account_id"], old_child_intent_id=fx["child_intent_id"],
        reason="dup free", idempotency_key="revoke-dup-free-", now=300,
    )
    child_lifecycle.process_revoke(
        db, revoke["operation_id"], worker_id="worker-one", revoke_fn=_revoke_fn(fx["remote"]), now=301,
    )
    free_prepared = db.child_lifecycle.prepare_free(
        account_id=fx["account"]["account_id"], old_child_intent_id=fx["child_intent_id"],
        reason="dup free", idempotency_key="free-dup--------", now=302,
    )
    first = child_lifecycle.process_free(db, free_prepared["operation_id"], worker_id="worker-one", now=303)
    assert first["state"] == "APPLIED"
    assert db.child_lifecycle.claim(free_prepared["operation_id"], worker_id="worker-two", now=304) is None
    same = db.child_lifecycle.prepare_free(
        account_id=fx["account"]["account_id"], old_child_intent_id=fx["child_intent_id"],
        reason="dup free", idempotency_key="free-dup--------", now=305,
    )
    assert same["operation_id"] == free_prepared["operation_id"]


def test_free_remote_failure_leaves_slot_occupied(db):
    fx = _build_applied_child(db)
    revoke = db.child_lifecycle.prepare_revoke(
        account_id=fx["account"]["account_id"], old_child_intent_id=fx["child_intent_id"],
        reason="failure test", idempotency_key="revoke-failure-free", now=300,
    )
    fx["remote"].outage = True
    with pytest.raises(Exception):
        child_lifecycle.process_revoke(
            db, revoke["operation_id"], worker_id="worker-one", revoke_fn=_revoke_fn(fx["remote"]), now=301,
        )
    with pytest.raises(ChildLifecycleError):
        free_prepared = db.child_lifecycle.prepare_free(
            account_id=fx["account"]["account_id"], old_child_intent_id=fx["child_intent_id"],
            reason="failure test", idempotency_key="free-failure----", now=302,
        )
        child_lifecycle.process_free(db, free_prepared["operation_id"], worker_id="worker-one", now=303)
    slot = db._conn.execute(
        "SELECT desired_state FROM mgboost_device_slots WHERE id=?", (fx["slot"]["slot_id"],),
    ).fetchone()
    assert slot["desired_state"] == "ACTIVE"


def test_free_does_not_create_new_generation(db):
    fx = _build_applied_child(db)
    revoke = db.child_lifecycle.prepare_revoke(
        account_id=fx["account"]["account_id"], old_child_intent_id=fx["child_intent_id"],
        reason="no auto generation", idempotency_key="revoke-no-gen---", now=300,
    )
    child_lifecycle.process_revoke(
        db, revoke["operation_id"], worker_id="worker-one", revoke_fn=_revoke_fn(fx["remote"]), now=301,
    )
    free_prepared = db.child_lifecycle.prepare_free(
        account_id=fx["account"]["account_id"], old_child_intent_id=fx["child_intent_id"],
        reason="no auto generation", idempotency_key="free-no-gen-----", now=302,
    )
    child_lifecycle.process_free(db, free_prepared["operation_id"], worker_id="worker-one", now=303)
    count = db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_device_slot_generations WHERE slot_id=?",
        (fx["slot"]["slot_id"],),
    ).fetchone()[0]
    assert count == 1  # still only the original (now RELEASED) generation


# --- REBIND ----------------------------------------------------------------------

def test_rebind_generation_increments_and_old_revoked_first(db):
    fx = _build_applied_child(db)
    prepared = db.child_lifecycle.prepare_rebind(
        account_id=fx["account"]["account_id"], old_child_intent_id=fx["child_intent_id"],
        reason="reinstall", idempotency_key="rebind-1--------", now=300,
    )
    result = child_lifecycle.process_rebind(
        db, prepared["operation_id"], worker_id="worker-one", revoke_fn=_revoke_fn(fx["remote"]),
        new_raw_hwid="new-device-hwid", hmac_key=HWID_KEY, now=301,
    )
    assert result["state"] == "APPLIED"
    slot = db._conn.execute(
        "SELECT current_generation FROM mgboost_device_slots WHERE id=?", (fx["slot"]["slot_id"],),
    ).fetchone()
    assert slot["current_generation"] == fx["slot"]["generation"] + 1
    old_intent = db._conn.execute(
        "SELECT desired_state,observed_state FROM mgboost_child_user_intents WHERE id=?",
        (fx["child_intent_id"],),
    ).fetchone()
    assert tuple(old_intent) == ("REVOKED", "REVOKED")
    assert fx["remote"].users[fx["child_username"]]["status"] == "disabled"


def test_rebind_creates_exactly_one_new_child_intent_and_outbox(db):
    fx = _build_applied_child(db)
    prepared = db.child_lifecycle.prepare_rebind(
        account_id=fx["account"]["account_id"], old_child_intent_id=fx["child_intent_id"],
        reason="reinstall", idempotency_key="rebind-2--------", now=300,
    )
    result = child_lifecycle.process_rebind(
        db, prepared["operation_id"], worker_id="worker-one", revoke_fn=_revoke_fn(fx["remote"]),
        new_raw_hwid="new-device-hwid-2", hmac_key=HWID_KEY, now=301,
    )
    intents = db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_child_user_intents WHERE account_id=?",
        (fx["account"]["account_id"],),
    ).fetchone()[0]
    outboxes = db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_outbox WHERE account_id=?",
        (fx["account"]["account_id"],),
    ).fetchone()[0]
    assert intents == 2  # old + new
    assert outboxes == 2
    assert result["new_child_intent_id"] is not None
    assert result["new_child_intent_id"] != fx["child_intent_id"]


def test_repeated_rebind_request_is_idempotent_exactly_one_x_plus_1(db):
    fx = _build_applied_child(db)
    prepared = db.child_lifecycle.prepare_rebind(
        account_id=fx["account"]["account_id"], old_child_intent_id=fx["child_intent_id"],
        reason="reinstall", idempotency_key="rebind-3--------", now=300,
    )
    first = child_lifecycle.process_rebind(
        db, prepared["operation_id"], worker_id="worker-one", revoke_fn=_revoke_fn(fx["remote"]),
        new_raw_hwid="new-device-hwid-3", hmac_key=HWID_KEY, now=301,
    )
    same = db.child_lifecycle.prepare_rebind(
        account_id=fx["account"]["account_id"], old_child_intent_id=fx["child_intent_id"],
        reason="reinstall", idempotency_key="rebind-3--------", now=305,
    )
    assert same["operation_id"] == prepared["operation_id"]
    assert db.child_lifecycle.claim(prepared["operation_id"], worker_id="worker-two", now=306) is None
    generations = db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_device_slot_generations WHERE slot_id=?",
        (fx["slot"]["slot_id"],),
    ).fetchone()[0]
    assert generations == 2  # old (RELEASED) + new (ACTIVE), never a third


def test_rebind_lost_ack_between_generation_swap_and_provisioning_handoff(db):
    fx = _build_applied_child(db)
    prepared = db.child_lifecycle.prepare_rebind(
        account_id=fx["account"]["account_id"], old_child_intent_id=fx["child_intent_id"],
        reason="reinstall crash test", idempotency_key="rebind-crash----", now=300,
    )
    claimed = db.child_lifecycle.claim(prepared["operation_id"], worker_id="worker-one", now=301, lease_seconds=5)
    # Simulate the exact steps process_rebind performs, but "crash" (never
    # call record_rebind_generation) after the remote revoke + local swap.
    from src.child_contract import derive_lifecycle_operation_id
    revoke_payload = {
        "operation_id": derive_lifecycle_operation_id(fx["child_username"], "REVOKE"),
        "child_username": fx["child_username"],
        "uuid_verifier": fx["child"]["uuid_verifier"],
    }
    BrokerOperations(fx["remote"]).dispatch("child.user.revoke", revoke_payload)
    db._conn.execute(
        "UPDATE mgboost_child_user_intents SET desired_state='REVOKED',observed_state='REVOKED' "
        "WHERE id=?", (fx["child_intent_id"],),
    )
    db._conn.commit()
    old_generation = db._conn.execute(
        "SELECT generation FROM mgboost_device_slot_generations WHERE id=?",
        (claimed["old_slot_generation_id"],),
    ).fetchone()
    db.device_slots.rebind(
        fx["account"]["account_id"], fx["slot"]["slot_id"], old_generation["generation"],
        "new-device-hwid-crash", HWID_KEY, reason="reinstall crash test", now=302,
    )
    # No record_rebind_generation call -- lease will expire, simulating a crash.
    result = child_lifecycle.process_rebind(
        db, prepared["operation_id"], worker_id="worker-two", revoke_fn=_revoke_fn(fx["remote"]),
        new_raw_hwid="new-device-hwid-crash", hmac_key=HWID_KEY, now=340,
    )
    assert result["state"] == "APPLIED"
    generations = db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_device_slot_generations WHERE slot_id=?",
        (fx["slot"]["slot_id"],),
    ).fetchone()[0]
    assert generations == 2  # convergence, not a duplicate X+2


def test_old_credential_never_reactivated_after_rebind(db):
    fx = _build_applied_child(db)
    prepared = db.child_lifecycle.prepare_rebind(
        account_id=fx["account"]["account_id"], old_child_intent_id=fx["child_intent_id"],
        reason="reinstall", idempotency_key="rebind-4--------", now=300,
    )
    child_lifecycle.process_rebind(
        db, prepared["operation_id"], worker_id="worker-one", revoke_fn=_revoke_fn(fx["remote"]),
        new_raw_hwid="new-device-hwid-4", hmac_key=HWID_KEY, now=301,
    )
    assert fx["remote"].users[fx["child_username"]]["status"] == "disabled"
    old_generation = db._conn.execute(
        "SELECT status FROM mgboost_device_slot_generations WHERE id=?",
        (fx["slot"]["generation_id"],),
    ).fetchone()
    assert old_generation["status"] == "RELEASED"


def test_stale_x_cannot_be_reactivated_by_a_second_rebind_attempt(db):
    fx = _build_applied_child(db)
    prepared = db.child_lifecycle.prepare_rebind(
        account_id=fx["account"]["account_id"], old_child_intent_id=fx["child_intent_id"],
        reason="reinstall", idempotency_key="rebind-5--------", now=300,
    )
    child_lifecycle.process_rebind(
        db, prepared["operation_id"], worker_id="worker-one", revoke_fn=_revoke_fn(fx["remote"]),
        new_raw_hwid="new-device-hwid-5", hmac_key=HWID_KEY, now=301,
    )
    # A completely separate attempt to rebind using the OLD (stale) generation
    # number directly against device_slots must fail closed.
    with pytest.raises(StaleSlotGeneration):
        db.device_slots.rebind(
            fx["account"]["account_id"], fx["slot"]["slot_id"], fx["slot"]["generation"],
            "yet-another-hwid", HWID_KEY, reason="stale retry", now=310,
        )


# --- security: cross-account / caller-supplied slot -----------------------------

def test_cross_account_revoke_denied(db):
    fx_a = _build_applied_child(db, mapping="LC_A", tg=600001, alias="alice")
    fx_b = _account(db, mapping="LC_B", tg=600002, alias="second-source")
    account_b = fx_b[0]
    with pytest.raises(ChildLifecycleError, match="does not belong"):
        db.child_lifecycle.prepare_revoke(
            account_id=account_b["account_id"], old_child_intent_id=fx_a["child_intent_id"],
            reason="cross account attempt", idempotency_key="cross-revoke----", now=300,
        )


def test_cross_account_hwid_rebind_denied(db):
    fx_a = _build_applied_child(db, mapping="LC_CROSS_A", tg=600101, alias="alice")
    fx_b = _build_applied_child(db, mapping="LC_CROSS_B", tg=600102, alias="second-source")
    revoke_a = db.child_lifecycle.prepare_revoke(
        account_id=fx_a["account"]["account_id"], old_child_intent_id=fx_a["child_intent_id"],
        reason="cross hwid test", idempotency_key="revoke-cross-a--", now=300,
    )
    child_lifecycle.process_revoke(
        db, revoke_a["operation_id"], worker_id="worker-one", revoke_fn=_revoke_fn(fx_a["remote"]), now=301,
    )
    revoke_b = db.child_lifecycle.prepare_revoke(
        account_id=fx_b["account"]["account_id"], old_child_intent_id=fx_b["child_intent_id"],
        reason="cross hwid test", idempotency_key="revoke-cross-b--", now=302,
    )
    child_lifecycle.process_revoke(
        db, revoke_b["operation_id"], worker_id="worker-one", revoke_fn=_revoke_fn(fx_b["remote"]), now=303,
    )
    rebind_a = db.child_lifecycle.prepare_rebind(
        account_id=fx_a["account"]["account_id"], old_child_intent_id=fx_a["child_intent_id"],
        reason="cross hwid test", idempotency_key="rebind-cross-a--", now=304,
    )
    child_lifecycle.process_rebind(
        db, rebind_a["operation_id"], worker_id="worker-one", revoke_fn=_revoke_fn(fx_a["remote"]),
        new_raw_hwid="shared-copied-hwid", hmac_key=HWID_KEY, now=305,
    )
    rebind_b = db.child_lifecycle.prepare_rebind(
        account_id=fx_b["account"]["account_id"], old_child_intent_id=fx_b["child_intent_id"],
        reason="cross hwid test", idempotency_key="rebind-cross-b--", now=306,
    )
    with pytest.raises(CrossAccountHWID):
        child_lifecycle.process_rebind(
            db, rebind_b["operation_id"], worker_id="worker-one", revoke_fn=_revoke_fn(fx_b["remote"]),
            new_raw_hwid="shared-copied-hwid", hmac_key=HWID_KEY, now=307,
        )


def test_no_caller_suppliable_slot_generation_uuid_in_prepare_signatures():
    import inspect
    from src.child_lifecycle import ChildLifecycleStore

    for name in ("prepare_revoke", "prepare_free", "prepare_rebind"):
        params = set(inspect.signature(getattr(ChildLifecycleStore, name)).parameters)
        assert "slot_id" not in params
        assert "generation" not in params
        assert "child_username" not in params
        assert "child_uuid" not in params
        assert "new_uuid" not in params
        assert "telegram_id" not in params


def test_stale_operation_from_conflicting_idempotency_key_denied(db):
    fx_a = _build_applied_child(db, mapping="LC_CONFLICT_A", tg=600201, alias="alice")
    fx_b = _build_applied_child(db, mapping="LC_CONFLICT_B", tg=600202, alias="second-source")
    db.child_lifecycle.prepare_revoke(
        account_id=fx_a["account"]["account_id"], old_child_intent_id=fx_a["child_intent_id"],
        reason="first reason", idempotency_key="stable-key-1----", now=300,
    )
    # Same idempotency key, but a genuinely different target device -> the
    # canonical request hash differs, so this must fail closed as a conflict
    # rather than silently reusing the first device's operation.
    with pytest.raises(ChildLifecycleConflict):
        db.child_lifecycle.prepare_revoke(
            account_id=fx_b["account"]["account_id"], old_child_intent_id=fx_b["child_intent_id"],
            reason="first reason", idempotency_key="stable-key-1----",
            now=301,
        )


# --- concurrency -----------------------------------------------------------------

def test_two_simultaneous_revoke_of_same_device_converge(db):
    fx = _build_applied_child(db)
    prepared = db.child_lifecycle.prepare_revoke(
        account_id=fx["account"]["account_id"], old_child_intent_id=fx["child_intent_id"],
        reason="race", idempotency_key="revoke-race-----", now=300,
    )
    results = []
    errors = []
    barrier = threading.Barrier(2)

    def worker(name):
        barrier.wait(5)
        try:
            results.append(child_lifecycle.process_revoke(
                db, prepared["operation_id"], worker_id=name,
                revoke_fn=_revoke_fn(fx["remote"]), now=301,
            ))
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(f"racer-{i}",)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    applied = [r for r in results if r and r["state"] == "APPLIED"]
    assert len(applied) == 1
    intent = db._conn.execute(
        "SELECT observed_state FROM mgboost_child_user_intents WHERE id=?",
        (fx["child_intent_id"],),
    ).fetchone()
    assert intent["observed_state"] == "REVOKED"


def test_two_simultaneous_rebind_of_same_device_yield_exactly_one_new_generation(db):
    fx = _build_applied_child(db)
    prepared = db.child_lifecycle.prepare_rebind(
        account_id=fx["account"]["account_id"], old_child_intent_id=fx["child_intent_id"],
        reason="race rebind", idempotency_key="rebind-race-----", now=300,
    )
    results = []
    barrier = threading.Barrier(2)

    def worker(name):
        barrier.wait(5)
        try:
            results.append(child_lifecycle.process_rebind(
                db, prepared["operation_id"], worker_id=name,
                revoke_fn=_revoke_fn(fx["remote"]), new_raw_hwid="race-new-hwid",
                hmac_key=HWID_KEY, now=301,
            ))
        except Exception:
            pass

    threads = [threading.Thread(target=worker, args=(f"racer-{i}",)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    generations = db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_device_slot_generations WHERE slot_id=?",
        (fx["slot"]["slot_id"],),
    ).fetchone()[0]
    assert generations == 2  # exactly one new generation, never more


def test_stale_lease_can_be_reclaimed_by_another_worker(db):
    fx = _build_applied_child(db)
    prepared = db.child_lifecycle.prepare_revoke(
        account_id=fx["account"]["account_id"], old_child_intent_id=fx["child_intent_id"],
        reason="stale lease", idempotency_key="revoke-stale-lease", now=300,
    )
    first_claim = db.child_lifecycle.claim(
        prepared["operation_id"], worker_id="worker-one", now=300, lease_seconds=5,
    )
    assert first_claim is not None
    # Lease not yet expired: a second claim must fail.
    assert db.child_lifecycle.claim(prepared["operation_id"], worker_id="worker-two", now=302) is None
    # After expiry, a new worker may take over.
    second_claim = db.child_lifecycle.claim(prepared["operation_id"], worker_id="worker-two", now=310)
    assert second_claim is not None


# --- privacy -----------------------------------------------------------------------

def test_no_raw_uuid_in_db_after_revoke(db):
    fx = _build_applied_child(db)
    prepared = db.child_lifecycle.prepare_revoke(
        account_id=fx["account"]["account_id"], old_child_intent_id=fx["child_intent_id"],
        reason="privacy check", idempotency_key="revoke-privacy--", now=300,
    )
    child_lifecycle.process_revoke(
        db, prepared["operation_id"], worker_id="worker-one", revoke_fn=_revoke_fn(fx["remote"]), now=301,
    )
    dump = "\n".join(db._conn.iterdump())
    assert fx["child_uuid"] not in dump
    rotated = fx["remote"].users[fx["child_username"]]["proxies"]["vless"]["id"]
    assert rotated not in dump
