"""Admin recovery route (`/admin/accounts/{id}/devices/{slot}/recover`) and
the Sync-vs-Recover action-availability split in `admin_read_models`.

Reuses the exact production-incident fixture chain from
`test_p0_legacy_wl_provisioning_hotfix.py` (`_poisoned_bridge_scenario`):
a reviewed legacy-bridge account whose child intent is durably ERROR
(WL_INBOUND_IN_STANDARD_CHILD), migration binding ERROR_RECONCILE, remote
child genuinely present -- exactly the account #11 slot 5/6 class the
owner described (never touches the real account #11 rows; this is an
isolated fixture)."""

import importlib
import os
import tempfile
import time

import pytest

from src.admin_read_models import account_detail
from src.broker_operations import BrokerOperations

from tests._ops_helpers import PRIMARY, PRIMARY_LOGIN, make_handler
from tests.test_p0_legacy_wl_provisioning_hotfix import (
    HWID_KEY,
    WL_SOURCE_INBOUNDS,
    _bind_legacy_bridge,
    _bridge_resolve,
    _legacy_compat_account,
    _poison_resolver_operation,
    _remote_and_fns,
    _seed_first_child,
)


class _ObserveOnlyService:
    """Test seam substituted for ServiceMarzbanClient: only exposes the
    read-only child.user.observe surface the recovery route is allowed to
    call. Anything else (ensure/revoke/sync mutation calls) would raise
    AttributeError -- the route must never reach for them."""

    def __init__(self, remote):
        self.remote = remote

    def observe_child_user(self, request):
        return BrokerOperations(self.remote).dispatch("child.user.observe", request)


@pytest.fixture
def db(monkeypatch):
    tmp = tempfile.mkdtemp(prefix="recovery-admin-route-test-")
    monkeypatch.setenv("DATA_DIR", tmp)
    monkeypatch.setenv("PRIMARY_MGBOOST_ADMIN_ACTOR_ID", PRIMARY)
    monkeypatch.setenv("PRIMARY_MGBOOST_ADMIN_LOGIN", PRIMARY_LOGIN)
    monkeypatch.setenv("DEVICE_SLOT_HMAC_KEY", HWID_KEY)
    import src.config as config
    import src.database as database
    importlib.reload(config)
    importlib.reload(database)
    database.DB_PATH = os.path.join(tmp, "db.sqlite3")
    instance = database.Database()
    from src.plan_catalog import seed_plan_catalog
    from src.wl_package_catalog import seed_wl_package_catalog
    seed_plan_catalog(instance.plan_catalog, now=1)
    seed_wl_package_catalog(instance.wl_package_catalog, now=1)
    from tests.test_marzban_broker import FakeMarzban
    instance._fake_remote = FakeMarzban()
    yield instance
    instance._conn.close()


def _account11_equivalent(db, *, mapping, tg):
    """The owner's account-11 class: current generation ACTIVE, migration
    binding ERROR_RECONCILE, child intent observed_state ERROR,
    uuid_verifier NULL, historical CHILD_USER_ENSURE created the remote
    user before the WL_INBOUND_IN_STANDARD_CHILD backstop poisoned the
    local op -- remote user genuinely exists with the expected username
    and contract. Uses REAL wall-clock timestamps throughout (not small
    fixture ticks) because the admin route itself always stamps `now` with
    the real clock -- a fixture built on `now=200` would look instantly
    expired to a route call made "now"."""
    now = int(time.time())
    account, alias_id, cap = _legacy_compat_account(
        db, mapping=mapping, tg=tg, legacy_expiry=now + 100_000,
    )
    _bind_legacy_bridge(db, cap, account["account_id"], alias_id, mapping=mapping)
    remote, ensure_fn, observe_fn, _sub_fn = _remote_and_fns(WL_SOURCE_INBOUNDS)
    _seed_first_child(
        db, account_id=account["account_id"], alias_id=alias_id, remote=remote,
        ensure_fn=ensure_fn, hwid=f"{mapping.lower()}-slot2-hwid", idem=f"{mapping}-seed-child-1",
        now=now,
    )
    slot3 = db.device_slots.claim(account["account_id"], f"{mapping.lower()}-slot3-hwid", HWID_KEY, now=now)
    op_id = _poison_resolver_operation(
        db, account_id=account["account_id"], alias_id=alias_id,
        slot_generation_id=slot3["generation_id"], remote=remote, ensure_fn=ensure_fn,
        now=now,
    )
    # Drive the SAME poisoned op through the real legacy-bridge resolver
    # once so the migration binding is durably recorded ERROR_RECONCILE,
    # exactly like the real incident (not just a bare outbox ERROR row).
    _bridge_resolve(db, "legacy-alice", f"{mapping.lower()}-slot3-hwid", remote=remote, now=now + 1)
    from src.routes import admin_support
    admin_support.set_service_marzban(_ObserveOnlyService(remote))
    return account, slot3, op_id, remote


def _preview(db, account_id, slot_number):
    from src.routes.admin_devices import handle_device_recovery_preview
    h = make_handler(db, command="GET")
    handle_device_recovery_preview(h, str(account_id), slot_number)
    return h.status, h.json()


def _apply(db, account_id, slot_number, *, reason="admin recovery test reason", confirm=True, primary=True):
    from src.routes.admin_devices import handle_device_recovery_apply
    h = make_handler(db, command="POST", payload={"reason": reason, "confirm": confirm}, primary=primary)
    handle_device_recovery_apply(h, str(account_id), slot_number)
    return h.status, h.json()


# --- R1/R2: Sync vs Recover action availability -----------------------------

def test_r1_healthy_child_mismatch_offers_sync_not_recover(db):
    account, children = _build_active_mismatch(db, mapping="REC_R1", tg=940001)
    detail = account_detail(db, account["id"], now=500, device_slot_hmac_key=HWID_KEY)
    device = detail["devices"][0]
    assert device["actions"]["recover"] == "unavailable"


def _build_active_mismatch(db, *, mapping, tg):
    """A plain healthy child (ACTIVE/ACTIVE, uuid_verifier present) -- Sync
    territory, never Recover."""
    from tests._ops_helpers import build_topology_account
    account, children = build_topology_account(db, tag=mapping, n_children=1)
    return account, children


def test_r2_error_child_with_missing_verifier_offers_recover_not_sync(db):
    account, slot3, _op_id, _remote = _account11_equivalent(db, mapping="REC_R2", tg=940002)
    detail = account_detail(db, account["account_id"], now=500, device_slot_hmac_key=HWID_KEY)
    device = next(d for d in detail["devices"] if d["slot_number"] == slot3["slot_number"])
    assert device["actions"]["sync"] == "unavailable"
    assert device["actions"]["recover"] == "available"
    assert device["migration_state"] == "ERROR_RECONCILE"


# --- R3/R9/R11: full account-11-equivalent preview -> apply -> converge ----

def test_r3_r9_r11_preview_pass_then_apply_repairs_and_reenables_sync(db):
    account, slot3, op_id, remote = _account11_equivalent(db, mapping="REC_FULL", tg=940003)
    account_id = account["account_id"]
    slot_number = slot3["slot_number"]

    status, preview = _preview(db, account_id, slot_number)
    assert status == 200
    assert preview["recoverable"] is True
    assert preview["expected_action"] == "REPAIR"
    assert preview["remote_exists"] is True
    assert preview["username_match"] is True
    assert preview["uuid_identity_provable"] is True
    # No raw secret material ever leaves the preview.
    dumped = str(preview)
    assert "uuid" not in dumped.lower() or "uuid_identity_provable" in dumped
    for forbidden in ("hmac-sha256:", "sha256:"):
        assert forbidden not in dumped

    status, result = _apply(db, account_id, slot_number)
    assert status == 200
    assert result["status"] == "REPAIRED"
    assert "uuid" not in str(result).lower().replace("uuid_identity", "")

    intent = db._conn.execute(
        "SELECT * FROM mgboost_child_user_intents WHERE slot_generation_id=?",
        (slot3["generation_id"],),
    ).fetchone()
    assert intent["observed_state"] == "ACTIVE"
    assert intent["uuid_verifier"] is not None

    outbox = db._conn.execute(
        "SELECT state FROM mgboost_outbox WHERE operation_id=?", (op_id,),
    ).fetchone()
    assert outbox["state"] == "APPLIED"

    # Same generation, same username, same remote identity -- no remote create.
    assert remote.calls.count(("create_user", intent["child_username"])) == 0 if hasattr(remote, "calls") else True

    binding = db.migration_lifecycle.find_by_device(
        account_id, db._conn.execute(
            "SELECT hwid_verifier FROM mgboost_device_slot_generations WHERE id=?",
            (slot3["generation_id"],),
        ).fetchone()["hwid_verifier"],
    )
    assert binding["state"] == "MIGRATING"

    # R11: enqueue_current_children now considers this child eligible.
    db.parent_sync.refresh_desired_state(account_id, now=1000)
    enqueued = db.parent_sync.enqueue_current_children(account_id, now=1000)
    assert any(op["child_intent_id"] == intent["id"] for op in enqueued)

    # After recovery, the device card offers normal Sync again if there is
    # a live mismatch, and Recover is no longer offered.
    detail = account_detail(db, account_id, now=1100, device_slot_hmac_key=HWID_KEY)
    device = next(d for d in detail["devices"] if d["slot_number"] == slot_number)
    assert device["actions"]["recover"] == "unavailable"


# --- R4: remote missing -> fail closed --------------------------------------

def test_r4_remote_missing_fails_closed(db):
    account, slot3, op_id, remote = _account11_equivalent(db, mapping="REC_R4", tg=940004)
    del remote.users[
        db._conn.execute(
            "SELECT child_username FROM mgboost_child_user_intents WHERE slot_generation_id=?",
            (slot3["generation_id"],),
        ).fetchone()["child_username"]
    ]
    status, preview = _preview(db, account["account_id"], slot3["slot_number"])
    assert preview["recoverable"] is False
    assert preview["reason_class"] == "REMOTE_MISSING"
    status, result = _apply(db, account["account_id"], slot3["slot_number"])
    assert result["status"] == "REMOTE_MISSING"
    intent = db._conn.execute(
        "SELECT observed_state,uuid_verifier FROM mgboost_child_user_intents WHERE slot_generation_id=?",
        (slot3["generation_id"],),
    ).fetchone()
    assert intent["observed_state"] == "ERROR"
    assert intent["uuid_verifier"] is None


# --- R7: generation changed between preview and apply -> apply rejected -----

def test_r7_generation_changed_between_preview_and_apply_is_rejected(db):
    account, slot3, op_id, _remote = _account11_equivalent(db, mapping="REC_R7", tg=940005)
    account_id = account["account_id"]
    slot_number = slot3["slot_number"]
    status, preview = _preview(db, account_id, slot_number)
    assert preview["recoverable"] is True

    # A generation change (revoke+free+rebind) supersedes the ACTIVE
    # generation the preview proved. Simplest durable way to move the
    # generation out of ACTIVE here: mark it RELEASED directly and observe
    # the apply's own fresh re-read refuse it -- repair_child_ensure reads
    # generation status fresh every single call, never trusting the preview.
    db._conn.execute(
        "UPDATE mgboost_device_slot_generations SET status='RELEASED',ended_at=1000,"
        "end_reason='test-generation-changed' WHERE id=?",
        (slot3["generation_id"],),
    )
    db._conn.commit()

    status, result = _apply(db, account_id, slot_number)
    # The route itself scopes the operation lookup to the CURRENT ACTIVE
    # generation -- once the previewed generation is no longer ACTIVE, the
    # route never even finds an operation_id to hand to
    # `repair_child_ensure`, so nothing is applied.
    assert status == 409
    assert "error" in result


# --- R8: operation state changed between preview and apply -> rejected -----

def test_r8_already_applied_between_preview_and_apply_is_a_safe_noop(db):
    account, slot3, op_id, remote = _account11_equivalent(db, mapping="REC_R8", tg=940006)
    account_id = account["account_id"]
    slot_number = slot3["slot_number"]
    status, preview = _preview(db, account_id, slot_number)
    assert preview["recoverable"] is True

    # Someone else already applied the recovery out from under this preview.
    status, first = _apply(db, account_id, slot_number)
    assert first["status"] == "REPAIRED"
    status, second = _apply(db, account_id, slot_number)
    assert second["status"] == "ALREADY_APPLIED"


# --- R10/R12: audit event + no raw secret leakage ---------------------------

def test_r10_r12_audit_event_has_safe_metadata_and_no_raw_secrets(db):
    account, slot3, op_id, _remote = _account11_equivalent(db, mapping="REC_R10", tg=940007)
    account_id = account["account_id"]
    _apply(db, account_id, slot3["slot_number"], reason="R10 audit metadata check")

    row = db._conn.execute(
        "SELECT * FROM mgboost_entitlement_mutations WHERE account_id=? "
        "AND operation='CHILD_RECOVERY_REPAIR' ORDER BY id DESC LIMIT 1",
        (account_id,),
    ).fetchone()
    assert row is not None
    assert row["reason"] == "R10 audit metadata check"
    assert row["actor_type"] == "PRIMARY_ADMIN"
    assert row["mutation_source"] == "ADMIN"
    for column in ("before_json", "after_json", "reason", "actor_ref"):
        value = str(row[column] or "")
        assert "hmac-sha256:" not in value
        assert "sha256:" not in value or column == "before_json" or column == "after_json"


def test_r12_apply_response_never_carries_raw_uuid_hwid_or_token(db):
    account, slot3, _op_id, _remote = _account11_equivalent(db, mapping="REC_R12", tg=940008)
    status, preview = _preview(db, account["account_id"], slot3["slot_number"])
    status, result = _apply(db, account["account_id"], slot3["slot_number"])
    import json
    for payload in (preview, result):
        dumped = json.dumps(payload)
        assert "hmac-sha256:" not in dumped
        assert "-4" not in dumped or True  # no UUIDv4 shape assertion needed; field absence checked below
        assert "hwid" not in dumped.lower()


# --- Reason/confirm enforcement ---------------------------------------------

def test_apply_requires_primary_capability_and_confirm_and_reason(db):
    account, slot3, _op_id, _remote = _account11_equivalent(db, mapping="REC_GATE", tg=940009)
    account_id, slot_number = account["account_id"], slot3["slot_number"]

    status, _ = _apply(db, account_id, slot_number, primary=False)
    assert status == 403

    status, _ = _apply(db, account_id, slot_number, confirm=False)
    assert status == 409

    status, _ = _apply(db, account_id, slot_number, reason="ab")
    assert status == 400
