"""PH4-03 mass-migration batch orchestration for PH4-05 grace-cohort
accounts bootstrapped with `ownership_evidence='ABSENT'`.

Reuses the exact sequence already proven three times in production
(accounts 1, 3, 4): a genesis child is provisioned on the account's own
slot 1 -- via a deterministic, per-account synthetic placeholder HWID,
never the real customer's HWID -- entirely BEFORE any
`mgboost_legacy_bridge_bindings` row exists, so the real customer's own
device is never exposed to the "no prior child" fail-closed gap
(`resolve_account_device()` returns `PROVISIONING_UNAVAILABLE` for an
account with zero prior children). Only once that genesis child is
`ACTIVE` is the bridge binding created+enabled -- at that point the
customer's own real device transparently migrates the next time it hits
the unchanged legacy `/sub/{token}` URL, via the unmodified PH4-01/02
resolver (`legacy_bridge_resolver.resolve_legacy_bridge` ->
`opaque_resolver.resolve_account_device`). No second resolver, no change
to `routes/sub.py`.

Every step here is idempotent and safe to re-run: `device_slots.claim()`
returns the existing generation for a repeated HWID, `child_provisioning.
prepare_child_ensure()`/`claim()`/`acknowledge()` are all idempotent by
design (PH3-03), and `legacy_bridge.create_binding()` raising
`LegacyBridgeConflict` on an existing binding is treated as
"already done", not an error, by `migrate_bootstrapped_account()` below.
"""

from __future__ import annotations

import time

from .child_contract import source_contract_hash
from .legacy_bridge import LegacyBridgeConflict


class GraceMigrationError(RuntimeError):
    pass


class PrerequisiteMissing(GraceMigrationError):
    pass


def _genesis_hwid(account_id: int) -> str:
    """Deterministic per-account placeholder -- never a real customer HWID,
    never reused across accounts, stable across retries."""
    return f"legacy-grace-genesis-slot1-{int(account_id)}"


def migrate_bootstrapped_account(
    db, *, capability, account_id: int, hmac_key: bytes | str, marzban_user_snapshot: dict,
    ensure_fn, decision_ref: str, worker_id: str, now: int | None = None,
) -> dict:
    """`marzban_user_snapshot` is the account's REAL, freshly-fetched
    Marzban user dict (via the existing broker `legacy.user.get`
    capability) -- this function never fetches it itself, keeping every
    real network/broker call explicit and visible to the caller. Returns
    `{"account_id", "genesis_child_username", "bridge_enabled": bool,
    "already_migrated": bool}`.

    Fails closed (`PrerequisiteMissing`) if the account has no ACTIVE
    subscription/entitlement yet (e.g. a `DeviceOverageConflict` account
    still pending an owner device-limit decision) -- migration must never
    proceed without a real entitlement backing it."""
    timestamp = int(time.time()) if now is None else int(now)
    account_id = int(account_id)

    sub = db._conn.execute(
        "SELECT id, current_expiry, status FROM mgboost_subscriptions WHERE account_id=? "
        "AND status IN ('ACTIVE','UNLIMITED')",
        (account_id,),
    ).fetchone()
    if sub is None:
        raise PrerequisiteMissing(
            "account has no ACTIVE/UNLIMITED subscription -- entitlement must be "
            "assigned (or its device-limit conflict resolved) before migration"
        )

    alias = db._conn.execute(
        "SELECT id, legacy_username FROM mgboost_legacy_account_aliases "
        "WHERE account_id=? AND alias_role='PRIMARY'",
        (account_id,),
    ).fetchone()
    if alias is None:
        raise PrerequisiteMissing("account has no reviewed PRIMARY legacy alias")

    existing_bridge = db._conn.execute(
        "SELECT enabled FROM mgboost_legacy_bridge_bindings WHERE account_id=?", (account_id,),
    ).fetchone()
    if existing_bridge is not None and existing_bridge["enabled"]:
        return {
            "account_id": account_id, "genesis_child_username": None,
            "bridge_enabled": True, "already_migrated": True,
        }

    # --- genesis child on slot 1, synthetic placeholder HWID -------------
    genesis_hwid = _genesis_hwid(account_id)
    slot = db.device_slots.claim(account_id, genesis_hwid, hmac_key, now=timestamp)
    slot_generation_id = slot["generation_id"]

    existing_intent = db._conn.execute(
        "SELECT child_username, observed_state FROM mgboost_child_user_intents "
        "WHERE slot_generation_id=?", (slot_generation_id,),
    ).fetchone()

    if existing_intent is None or existing_intent["observed_state"] != "ACTIVE":
        contract_hash = source_contract_hash(marzban_user_snapshot)
        prepared = db.child_provisioning.prepare_child_ensure(
            account_id=account_id, slot_generation_id=slot_generation_id,
            source_alias_id=alias["id"], source_contract_hash=contract_hash,
            expire=int(sub["current_expiry"]) if sub["current_expiry"] is not None else 0,
            idempotency_key=f"grace-genesis-child-v1:{account_id}", now=timestamp,
        )
        if prepared["state"] != "APPLIED":
            claimed = db.child_provisioning.claim(
                prepared["operation_id"], worker_id=worker_id, now=timestamp, lease_seconds=30,
            )
            if claimed is not None:
                created = ensure_fn(claimed["payload"])
                child_uuid = created.pop("uuid")
                db.child_provisioning.acknowledge(
                    prepared["operation_id"], worker_id=worker_id,
                    outcome=created["outcome"], child_uuid=child_uuid, remote_result=created,
                    now=timestamp,
                )
        final_intent = db._conn.execute(
            "SELECT child_username, observed_state FROM mgboost_child_user_intents "
            "WHERE slot_generation_id=?", (slot_generation_id,),
        ).fetchone()
        if final_intent is None or final_intent["observed_state"] != "ACTIVE":
            raise GraceMigrationError(
                "genesis child did not reach ACTIVE -- retry this account, do not enable the bridge"
            )
        child_username = final_intent["child_username"]
    else:
        child_username = existing_intent["child_username"]

    # --- bridge binding, enabled -- only after genesis child is ACTIVE ---
    try:
        db.legacy_bridge.create_binding(
            capability=capability, account_id=account_id, legacy_alias_id=alias["id"],
            enabled=True, decision_ref=decision_ref, now=timestamp,
        )
    except LegacyBridgeConflict:
        pass  # already created by a prior attempt -- idempotent no-op

    return {
        "account_id": account_id, "genesis_child_username": child_username,
        "bridge_enabled": True, "already_migrated": False,
    }
