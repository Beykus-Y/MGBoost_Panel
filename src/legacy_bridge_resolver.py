"""PH4-01 legacy subscription alias bridge resolver.

Dormant: `src/routes/sub.py` only calls this when `LEGACY_BRIDGE_ENABLED` is
true (default false), and even then only for an account with an explicit,
root-only-created `enabled=1` `mgboost_legacy_bridge_bindings` row -- two
independent gates, mirroring PH2-01's own dormancy pattern.

    legacy username (from the existing, unmodified get_username_for_token)
        -> exact reviewed immutable alias + explicit enabled binding
           (LegacyBridgeStore.resolve_account_for_legacy_username --
            deterministic only, never inferred from username shape/HWID/TG)
        -> the exact same `resolve_account_device()` tail PH2-01 already
           uses (PH3-08 parent state -> PH3-04 HWID gate -> PH3-02 slot ->
           PH3-03 lazy child -> child subscription body)

No separate slot/child logic exists here. `resolve_account_for_legacy_username`
returns None for anything not deterministically resolved (no alias, no
binding, disabled binding) -- the caller must fall through to the unmodified
legacy response, never guess or auto-create an account.
"""

from __future__ import annotations

import time

from .opaque_resolver import OpaqueResolveResult, resolve_account_device

OUTCOME_NOT_BRIDGED = "NOT_BRIDGED"

# Outcomes for which `hwid_gate.evaluate()`/`resolve_account_device()` never
# committed a durable slot claim (every deny decision happens strictly
# before `DeviceSlotStore.claim()` would insert/commit a row, and the
# parent-state check happens even earlier) -- side-effect-free, so the
# caller may safely fall through to the exact unmodified legacy response,
# leaving the device on legacy exactly as if this bridge attempt never
# happened.
_FALL_THROUGH_OUTCOMES = frozenset({
    OUTCOME_NOT_BRIDGED,
    "PARENT_UNAVAILABLE",
    "DENY_UNSUPPORTED_CLIENT",
    "DENY_MISSING_HWID",
    "DENY_MALFORMED_HWID",
    "DENY_SLOT_LIMIT",
    "DENY_CROSS_ACCOUNT_HWID",
})


def is_fall_through_outcome(outcome: str) -> bool:
    return outcome in _FALL_THROUGH_OUTCOMES


def resolve_legacy_bridge(
    db, legacy_username: str, device_metadata: dict, *, hmac_key, ensure_fn, subscription_fn,
    worker_id: str, now: int | None = None,
) -> OpaqueResolveResult:
    timestamp = int(time.time()) if now is None else int(now)

    account_id = db.legacy_bridge.resolve_account_for_legacy_username(legacy_username)
    if account_id is None:
        return OpaqueResolveResult(OUTCOME_NOT_BRIDGED)

    return resolve_account_device(
        db, account_id, device_metadata, hmac_key=hmac_key, ensure_fn=ensure_fn,
        subscription_fn=subscription_fn, worker_id=worker_id, now=timestamp,
    )
