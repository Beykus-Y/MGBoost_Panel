"""PH3-08 parent status/expiry -> active child generations sync.

Every scenario proves the two hard guarantees this module exists for:
suspend is reversible (never rotates a UUID, never touches PH3-05 REVOKE),
and stale parent-state races can never win over a newer transition.
"""

import importlib
import os
import tempfile

import pytest

from src.broker_operations import BrokerOperations
from src.child_contract import derive_sync_operation_id
from src import child_lifecycle, parent_sync
from src.parent_sync import ParentSyncConflict, child_target_for, compute_desired_status

from tests.test_child_provisioning import PRIMARY, PRIMARY_LOGIN, _account
from tests.test_child_lifecycle import _build_applied_child, _revoke_fn
from tests.test_marzban_broker import FakeMarzban


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


def _sync_fn(remote):
    return lambda payload: BrokerOperations(remote).dispatch("child.user.state.sync", payload)


def _set_subscription(db, account_id, *, status, current_expiry):
    db._conn.execute(
        "UPDATE mgboost_subscriptions SET status=?,current_expiry=?,updated_at=updated_at "
        "WHERE account_id=?",
        (status, current_expiry, account_id),
    )
    db._conn.commit()


def _set_account_status(db, account_id, status):
    db._conn.execute("UPDATE mgboost_accounts SET status=? WHERE id=?", (status, account_id))
    db._conn.commit()


# --- pure policy --------------------------------------------------------------

def test_compute_desired_status_active_with_future_expiry():
    assert compute_desired_status("ACTIVE", "ACTIVE", 1_000, now=500) == "ACTIVE"


def test_compute_desired_status_exact_boundary_now_equals_expiry_is_expired():
    assert compute_desired_status("ACTIVE", "ACTIVE", 1_000, now=1_000) == "EXPIRED"


def test_compute_desired_status_unlimited():
    assert compute_desired_status("ACTIVE", "UNLIMITED", None, now=500) == "UNLIMITED"


def test_compute_desired_status_disabled_account_overrides_active_subscription():
    assert compute_desired_status("DISABLED", "ACTIVE", 9_999, now=500) == "DISABLED"


def test_compute_desired_status_pending_subscription_is_disabled():
    assert compute_desired_status("ACTIVE", "PENDING", None, now=500) == "DISABLED"


def test_child_target_for_active_finite():
    assert child_target_for("ACTIVE", 1_000) == ("active", 1_000)


def test_child_target_for_unlimited_is_expire_zero():
    assert child_target_for("UNLIMITED", None) == ("active", 0)


def test_child_target_for_expired_and_disabled_never_touch_expire():
    assert child_target_for("EXPIRED", 1_000) == ("disabled", None)
    assert child_target_for("DISABLED", 1_000) == ("disabled", None)


# --- refresh_desired_state / revision discipline ------------------------------

def test_refresh_creates_revision_one_then_does_not_bump_when_unchanged(db):
    account, _alias_id, _slot = _account(db, mapping="REV_STABLE", tg=700001)
    _set_subscription(db, account["account_id"], status="ACTIVE", current_expiry=10_000)
    first = db.parent_sync.refresh_desired_state(account["account_id"], now=100)
    assert first["revision"] == 1
    assert first["desired_status"] == "ACTIVE"
    second = db.parent_sync.refresh_desired_state(account["account_id"], now=200)
    assert second["revision"] == 1  # no-op refresh does not churn the revision


def test_refresh_bumps_revision_on_real_transition(db):
    account, _alias_id, _slot = _account(db, mapping="REV_BUMP", tg=700002)
    _set_subscription(db, account["account_id"], status="ACTIVE", current_expiry=10_000)
    db.parent_sync.refresh_desired_state(account["account_id"], now=100)
    _set_subscription(db, account["account_id"], status="EXPIRED", current_expiry=10_000)
    after = db.parent_sync.refresh_desired_state(account["account_id"], now=20_000)
    assert after["revision"] == 2
    assert after["desired_status"] == "EXPIRED"


# --- end-to-end sync cycles ----------------------------------------------------

def test_active_parent_syncs_child_active_with_correct_expiry(db):
    fx = _build_applied_child(db, mapping="SYNC_ACTIVE", tg=710001)
    _set_subscription(db, fx["account"]["account_id"], status="ACTIVE", current_expiry=50_000)
    result = parent_sync.run_account_sync_cycle(
        db, fx["account"]["account_id"], sync_fn=_sync_fn(fx["remote"]),
        worker_id="worker-one", now=100,
    )
    assert result == {"prepared": 1, "applied": 1, "superseded": 0, "errored": 0, "aggregate_state": "IN_SYNC"}
    remote_user = fx["remote"].users[fx["child_username"]]
    assert remote_user["status"] == "active"
    assert remote_user["expire"] == 50_000
    assert remote_user["proxies"]["vless"]["id"] == fx["child_uuid"]


def test_expired_parent_disables_child_without_uuid_rotation(db):
    fx = _build_applied_child(db, mapping="SYNC_EXPIRE", tg=710002)
    _set_subscription(db, fx["account"]["account_id"], status="EXPIRED", current_expiry=1_000)
    parent_sync.run_account_sync_cycle(
        db, fx["account"]["account_id"], sync_fn=_sync_fn(fx["remote"]), worker_id="worker-one", now=2_000,
    )
    remote_user = fx["remote"].users[fx["child_username"]]
    assert remote_user["status"] == "disabled"
    assert remote_user["proxies"]["vless"]["id"] == fx["child_uuid"]
    intent = dict(db._conn.execute(
        "SELECT desired_state,observed_state FROM mgboost_child_user_intents WHERE id=?",
        (fx["child_intent_id"],),
    ).fetchone())
    assert intent == {"desired_state": "DISABLED", "observed_state": "DISABLED"}


def test_renewal_reactivates_same_generation_same_uuid_no_new_provisioning(db):
    fx = _build_applied_child(db, mapping="SYNC_RENEW", tg=710003)
    account_id = fx["account"]["account_id"]
    _set_subscription(db, account_id, status="EXPIRED", current_expiry=1_000)
    parent_sync.run_account_sync_cycle(
        db, account_id, sync_fn=_sync_fn(fx["remote"]), worker_id="worker-one", now=2_000,
    )
    outbox_count_before = db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_outbox WHERE child_intent_id=?", (fx["child_intent_id"],),
    ).fetchone()[0]
    generation_before = dict(db._conn.execute(
        "SELECT id,generation FROM mgboost_device_slot_generations WHERE id=?",
        (fx["slot"]["generation_id"],),
    ).fetchone())

    # renewed: valid future expiry first, then a re-enable sync
    _set_subscription(db, account_id, status="ACTIVE", current_expiry=99_000)
    parent_sync.run_account_sync_cycle(
        db, account_id, sync_fn=_sync_fn(fx["remote"]), worker_id="worker-one", now=3_000,
    )
    remote_user = fx["remote"].users[fx["child_username"]]
    assert remote_user["status"] == "active"
    assert remote_user["expire"] == 99_000
    assert remote_user["proxies"]["vless"]["id"] == fx["child_uuid"]  # same UUID throughout
    outbox_count_after = db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_outbox WHERE child_intent_id=?", (fx["child_intent_id"],),
    ).fetchone()[0]
    assert outbox_count_after == outbox_count_before  # no new provisioning
    generation_after = dict(db._conn.execute(
        "SELECT id,generation FROM mgboost_device_slot_generations WHERE id=?",
        (fx["slot"]["generation_id"],),
    ).fetchone())
    assert generation_after == generation_before  # same generation/slot identity


def test_already_in_sync_second_cycle_is_a_pure_noop_dispatch(db):
    fx = _build_applied_child(db, mapping="SYNC_IDEMPOTENT", tg=710004)
    _set_subscription(db, fx["account"]["account_id"], status="ACTIVE", current_expiry=50_000)
    parent_sync.run_account_sync_cycle(
        db, fx["account"]["account_id"], sync_fn=_sync_fn(fx["remote"]), worker_id="worker-one", now=100,
    )
    calls_before = len(fx["remote"].calls)
    result = parent_sync.run_account_sync_cycle(
        db, fx["account"]["account_id"], sync_fn=_sync_fn(fx["remote"]), worker_id="worker-one", now=200,
    )
    # same revision -> enqueue_current_children re-returns the same APPLIED row,
    # claim() finds nothing claimable, so no new remote calls are made at all.
    assert result["prepared"] == 1
    assert len(fx["remote"].calls) == calls_before


def test_ph3_05_revoke_still_rotates_uuid_for_a_ph3_08_suspended_child(db):
    """A child that PH3-08 has merely suspended (status=disabled, same UUID)
    must not be mistaken by the broker's revoke idempotency check for an
    already-revoked child -- a real PH3-05 REVOKE against it must still
    rotate the credential."""
    fx = _build_applied_child(db, mapping="SYNC_THEN_REVOKE", tg=710011)
    account_id = fx["account"]["account_id"]
    _set_subscription(db, account_id, status="EXPIRED", current_expiry=1_000)
    parent_sync.run_account_sync_cycle(
        db, account_id, sync_fn=_sync_fn(fx["remote"]), worker_id="worker-one", now=2_000,
    )
    suspended = fx["remote"].users[fx["child_username"]]
    assert suspended["status"] == "disabled"
    assert suspended["proxies"]["vless"]["id"] == fx["child_uuid"]  # not yet rotated

    revoke_prepared = db.child_lifecycle.prepare_revoke(
        account_id=account_id, old_child_intent_id=fx["child_intent_id"],
        reason="ph3-08 test: revoke a suspended-not-revoked child",
        idempotency_key="ph308-revoke-suspended-child", now=2_100,
    )
    result = child_lifecycle.process_revoke(
        db, revoke_prepared["operation_id"], worker_id="w-revoke",
        revoke_fn=_revoke_fn(fx["remote"]), now=2_101,
    )
    assert result["state"] == "APPLIED"
    revoked = fx["remote"].users[fx["child_username"]]
    assert revoked["status"] == "disabled"
    assert revoked["proxies"]["vless"]["id"] != fx["child_uuid"]  # actually rotated this time


def test_revoked_generation_is_excluded_and_never_resurrected_by_renewal(db):
    fx = _build_applied_child(db, mapping="SYNC_REVOKED", tg=710005)
    account_id = fx["account"]["account_id"]
    _set_subscription(db, account_id, status="ACTIVE", current_expiry=50_000)

    revoke_prepared = db.child_lifecycle.prepare_revoke(
        account_id=account_id, old_child_intent_id=fx["child_intent_id"],
        reason="ph3-08 test: revoke before renewal", idempotency_key="ph308-revoke-before-renewal",
        now=150,
    )
    child_lifecycle.process_revoke(
        db, revoke_prepared["operation_id"], worker_id="w-revoke",
        revoke_fn=_revoke_fn(fx["remote"]), now=151,
    )
    rotated_uuid = fx["remote"].users[fx["child_username"]]["proxies"]["vless"]["id"]
    assert rotated_uuid != fx["child_uuid"]

    # Parent renewal must never resurrect the revoked device.
    _set_subscription(db, account_id, status="ACTIVE", current_expiry=99_000)
    result = parent_sync.run_account_sync_cycle(
        db, account_id, sync_fn=_sync_fn(fx["remote"]), worker_id="worker-one", now=3_000,
    )
    assert result["prepared"] == 0  # excluded: desired_state='REVOKED'
    remote_user = fx["remote"].users[fx["child_username"]]
    assert remote_user["status"] == "disabled"
    assert remote_user["proxies"]["vless"]["id"] == rotated_uuid  # untouched by renewal


def test_stale_enable_after_disable_is_superseded_not_dispatched(db):
    fx = _build_applied_child(db, mapping="SYNC_STALE_ENABLE", tg=710006)
    account_id = fx["account"]["account_id"]
    _set_subscription(db, account_id, status="ACTIVE", current_expiry=50_000)
    state = db.parent_sync.refresh_desired_state(account_id, now=100)
    prepared = db.parent_sync.enqueue_current_children(account_id, now=100)
    stale_op = prepared[0]
    assert stale_op["desired_status"] == "active"

    # parent transitions to disabled *after* the enable op was queued
    _set_account_status(db, account_id, "DISABLED")
    db.parent_sync.refresh_desired_state(account_id, now=200)

    claimed = db.parent_sync.claim(stale_op["operation_id"], worker_id="late-worker", now=300)
    assert claimed is None
    row = dict(db._conn.execute(
        "SELECT state FROM mgboost_parent_sync_operations WHERE operation_id=?",
        (stale_op["operation_id"],),
    ).fetchone())
    assert row["state"] == "SUPERSEDED"
    # the remote child was never touched by the stale op
    assert fx["remote"].users[fx["child_username"]]["status"] == "active"


def test_stale_disable_after_renewal_is_superseded_not_dispatched(db):
    fx = _build_applied_child(db, mapping="SYNC_STALE_DISABLE", tg=710007)
    account_id = fx["account"]["account_id"]
    _set_subscription(db, account_id, status="EXPIRED", current_expiry=1_000)
    db.parent_sync.refresh_desired_state(account_id, now=2_000)
    prepared = db.parent_sync.enqueue_current_children(account_id, now=2_000)
    stale_op = prepared[0]
    assert stale_op["desired_status"] == "disabled"

    # renewed *after* the disable op was queued
    _set_subscription(db, account_id, status="ACTIVE", current_expiry=99_000)
    db.parent_sync.refresh_desired_state(account_id, now=3_000)

    claimed = db.parent_sync.claim(stale_op["operation_id"], worker_id="late-worker", now=4_000)
    assert claimed is None
    row = dict(db._conn.execute(
        "SELECT state FROM mgboost_parent_sync_operations WHERE operation_id=?",
        (stale_op["operation_id"],),
    ).fetchone())
    assert row["state"] == "SUPERSEDED"
    assert fx["remote"].users[fx["child_username"]]["status"] == "active"  # never disabled


def test_multiple_children_partial_convergence_and_aggregate_state(db):
    fx1 = _build_applied_child(db, mapping="SYNC_MULTI", tg=720001, alias="alice")
    account_id = fx1["account"]["account_id"]
    remote = fx1["remote"]
    hmac_key = "slot-test-hwid-key-that-is-at-least-32-bytes"
    from src.child_contract import source_contract_hash

    extra_children = []
    for i in range(2):
        slot = db.device_slots.claim(account_id, f"sync-multi-hwid-{i}", hmac_key, now=100 + i)
        source = dict(remote.users["alice"])
        source["username"] = f"multi-source-{i}"
        remote.users[source["username"]] = source
        req_hash = source_contract_hash(source)
        prepared = db.child_provisioning.prepare_child_ensure(
            account_id=account_id, slot_generation_id=slot["generation_id"],
            source_alias_id=fx1["alias_id"], source_contract_hash=req_hash, expire=0,
            idempotency_key=f"sync-multi-child-{i}", now=100 + i,
        )
        claimed = db.child_provisioning.claim(
            prepared["operation_id"], worker_id="multi-worker", now=200 + i, lease_seconds=5,
        )
        created = BrokerOperations(remote).dispatch("child.user.ensure", claimed["payload"])
        child_uuid = created.pop("uuid")
        db.child_provisioning.acknowledge(
            prepared["operation_id"], worker_id="multi-worker",
            outcome=created["outcome"], child_uuid=child_uuid, remote_result=created, now=300 + i,
        )
        extra_children.append(prepared["child_username"])

    _set_subscription(db, account_id, status="ACTIVE", current_expiry=50_000)
    result = parent_sync.run_account_sync_cycle(
        db, account_id, sync_fn=_sync_fn(remote), worker_id="worker-multi", now=1_000,
    )
    assert result["prepared"] == 3
    assert result["applied"] == 3
    assert result["aggregate_state"] == "IN_SYNC"
    for username in [fx1["child_username"]] + extra_children:
        assert remote.users[username]["status"] == "active"
        assert remote.users[username]["expire"] == 50_000


def test_cross_account_sync_never_touches_another_accounts_children(db):
    fx_a = _build_applied_child(db, mapping="SYNC_CROSS_A", tg=730001, alias="alice")
    fx_b = _build_applied_child(db, mapping="SYNC_CROSS_B", tg=730002, alias="second-source")
    _set_subscription(db, fx_a["account"]["account_id"], status="ACTIVE", current_expiry=50_000)
    _set_subscription(db, fx_b["account"]["account_id"], status="EXPIRED", current_expiry=1_000)

    parent_sync.run_account_sync_cycle(
        db, fx_a["account"]["account_id"], sync_fn=_sync_fn(fx_a["remote"]), worker_id="worker-one", now=100,
    )
    # B's remote is a different FakeMarzban instance in this fixture setup, so
    # cross-account isolation here is proven at the DB layer directly.
    prepared_for_a = db.parent_sync.enqueue_current_children(fx_a["account"]["account_id"], now=200)
    assert len(prepared_for_a) == 1
    assert prepared_for_a[0]["child_intent_id"] == fx_a["child_intent_id"]


def test_rebind_new_generation_converges_to_current_parent_state_not_a_stale_snapshot(db):
    """PH3-05's rebind hands the new child off to PH3-03 provisioning using
    the *old* child's outbox expire (a stale snapshot). PH3-08's job is to
    correct that on the very next sync cycle: the new generation must reflect
    the account's *current* desired state, not whatever the old child had."""
    fx = _build_applied_child(db, mapping="SYNC_REBIND_FRESH", tg=710010)
    account_id = fx["account"]["account_id"]
    # old child was provisioned with expire=0 (see _build_applied_child); the
    # parent's *current* entitlement is a completely different finite expiry.
    _set_subscription(db, account_id, status="ACTIVE", current_expiry=77_000)

    rebind_prepared = db.child_lifecycle.prepare_rebind(
        account_id=account_id, old_child_intent_id=fx["child_intent_id"],
        reason="ph3-08 test: fresh-state rebind", idempotency_key="ph308-rebind-fresh",
        now=500,
    )
    rebind_result = child_lifecycle.process_rebind(
        db, rebind_prepared["operation_id"], worker_id="worker-rebind",
        revoke_fn=_revoke_fn(fx["remote"]), new_raw_hwid="ph308-rebind-fresh-new-hwid",
        hmac_key="slot-test-hwid-key-that-is-at-least-32-bytes", now=501,
    )
    new_outbox_row = db._conn.execute(
        "SELECT operation_id FROM mgboost_outbox WHERE child_intent_id=?",
        (rebind_result["new_child_intent_id"],),
    ).fetchone()
    claimed = db.child_provisioning.claim(
        new_outbox_row["operation_id"], worker_id="worker-rebind-provision", now=502, lease_seconds=5,
    )
    created = BrokerOperations(fx["remote"]).dispatch("child.user.ensure", claimed["payload"])
    new_uuid = created.pop("uuid")
    db.child_provisioning.acknowledge(
        new_outbox_row["operation_id"], worker_id="worker-rebind-provision",
        outcome=created["outcome"], child_uuid=new_uuid, remote_result=created, now=503,
    )
    new_username = db._conn.execute(
        "SELECT child_username FROM mgboost_child_user_intents WHERE id=?",
        (rebind_result["new_child_intent_id"],),
    ).fetchone()["child_username"]
    assert fx["remote"].users[new_username]["expire"] == 0  # the stale snapshot, before any sync

    parent_sync.run_account_sync_cycle(
        db, account_id, sync_fn=_sync_fn(fx["remote"]), worker_id="worker-rebind-sync", now=600,
    )
    assert fx["remote"].users[new_username]["status"] == "active"
    assert fx["remote"].users[new_username]["expire"] == 77_000  # now matches current parent state
    assert fx["remote"].users[new_username]["proxies"]["vless"]["id"] == new_uuid  # unchanged by sync


# --- broker-level typed operation ---------------------------------------------

def test_broker_sync_rejects_verifier_mismatch():
    remote = FakeMarzban()
    from src.child_contract import credential_verifier
    child_username = "mgc_" + "a" * 26
    remote.users[child_username] = {
        "username": child_username, "expire": 0, "status": "active",
        "proxies": {"vless": {"id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", "flow": ""}},
        "inbounds": {"vless": ["LEGACY"]}, "data_limit": None,
    }
    payload = {
        "operation_id": derive_sync_operation_id(child_username, 1),
        "child_username": child_username,
        "desired_status": "active",
        "desired_expire": 1_000,
        "uuid_verifier": credential_verifier("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),  # wrong
    }
    with pytest.raises(ValueError, match="verifier mismatch"):
        BrokerOperations(remote).dispatch("child.user.state.sync", payload)


def test_broker_sync_remote_missing_does_not_raise_and_is_not_auto_created():
    remote = FakeMarzban()
    from src.child_contract import credential_verifier
    child_username = "mgc_" + "b" * 26
    payload = {
        "operation_id": derive_sync_operation_id(child_username, 1),
        "child_username": child_username,
        "desired_status": "active",
        "desired_expire": 1_000,
        "uuid_verifier": credential_verifier("cccccccc-cccc-4ccc-8ccc-cccccccccccc"),
    }
    result = BrokerOperations(remote).dispatch("child.user.state.sync", payload)
    assert result == {"outcome": "REMOTE_MISSING"}
    assert child_username not in remote.users


def test_broker_sync_never_sends_expire_when_disabling(db):
    fx = _build_applied_child(db, mapping="SYNC_NO_EXPIRE_ON_DISABLE", tg=710008)
    payload = {
        "operation_id": derive_sync_operation_id(fx["child_username"], 1),
        "child_username": fx["child_username"],
        "desired_status": "disabled",
        "desired_expire": None,
        "uuid_verifier": fx["child"]["uuid_verifier"],
    }
    BrokerOperations(fx["remote"]).dispatch("child.user.state.sync", payload)
    modify_calls = [c for c in fx["remote"].calls if c[0] == "modify_user"]
    assert modify_calls[-1][2] == {"status": "disabled"}


def test_broker_sync_rejects_malformed_operation_id():
    remote = FakeMarzban()
    from src.child_contract import credential_verifier
    child_username = "mgc_" + "c" * 26
    payload = {
        "operation_id": "not-a-sync-op-id",
        "child_username": child_username,
        "desired_status": "active",
        "desired_expire": 1_000,
        "uuid_verifier": credential_verifier("dddddddd-dddd-4ddd-8ddd-dddddddddddd"),
    }
    with pytest.raises(ValueError):
        BrokerOperations(remote).dispatch("child.user.state.sync", payload)


def test_prepare_conflict_on_reused_child_revision_with_different_content(db):
    fx = _build_applied_child(db, mapping="SYNC_CONFLICT", tg=710009)
    account_id = fx["account"]["account_id"]
    _set_subscription(db, account_id, status="ACTIVE", current_expiry=50_000)
    db.parent_sync.refresh_desired_state(account_id, now=100)
    prepared = db.parent_sync.enqueue_current_children(account_id, now=100)
    assert prepared[0]["parent_revision"] == 1
    with pytest.raises(ParentSyncConflict):
        db.parent_sync._prepare_locked(
            account_id=account_id, child_intent_id=fx["child_intent_id"],
            child_username=fx["child_username"], uuid_verifier="sha256:" + "0" * 64,
            parent_revision=1, desired_status="disabled", desired_expire=None, now=999,
        )
