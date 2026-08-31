"""PH8-05 canonical-slot <-> real-device telemetry projection.

Never guesses. The only accepted proof key is `hwid_verifier` -- the same
PH3-02 keyed-HMAC form already used by `device_slots.claim()` and the PH4-02
migration lineage (`mgboost_migration_bindings.hwid_verifier`). A telemetry
observation counts as evidence for a slot only when it carries the exact
same `hwid_verifier` under the exact same `account_id`. No fuzzy scoring,
no "closest timestamp", no platform/client-name similarity heuristics.

As of this writing, no live request path stamps a legacy `user_devices`
telemetry row with this verifier -- `routes/sub.py`'s live traffic only
ever calls `Database.check_device_access()` (a completely separate,
unsalted-hash identity space), while the code path that *would* claim a
real customer HWID into a canonical slot generation
(`legacy_bridge_resolver` -> `opaque_resolver.resolve_account_device` ->
`hwid_gate.evaluate` -> `device_slots.claim()`) is gated off by
`LEGACY_BRIDGE_ENABLED` (default false) and is documented dormant. So
`admin_read_models._real_device_projection()` always calls this module
with an empty `telemetry_observations` list today: every current slot
honestly resolves to `NOT_CLAIMED`, `GENESIS_PLACEHOLDER`, or `UNKNOWN`.
`MATCH_CONFIRMED` is fully implemented and tested here so that the day a
durable evidence pipeline exists, wiring it in requires no change to this
matching contract.
"""

from __future__ import annotations

MATCH_CONFIRMED = "CONFIRMED"
MATCH_GENESIS_PLACEHOLDER = "GENESIS_PLACEHOLDER"
MATCH_NOT_CLAIMED = "NOT_CLAIMED"
MATCH_UNKNOWN = "UNKNOWN"

_EMPTY_FIELDS = {
    "model": None, "model_source": None, "platform": None,
    "client_name": None, "client_version": None, "last_seen_at": None,
}


def _empty(match_state: str) -> dict:
    return {"matched": False, "match_state": match_state, **_EMPTY_FIELDS}


def project_real_device(slot: dict, telemetry_observations: list[dict]) -> dict:
    """`slot` describes exactly one canonical slot's CURRENT identity --
    callers must pass only the slot's active generation (never a
    RELEASED/REVOKED one); that is the entire rebind-safety guarantee, this
    function has no notion of history at all.

        slot = {"account_id", "generation_status", "is_genesis", "hwid_verifier"}

    `telemetry_observations` are candidate real-device facts, each of which
    must carry its own `account_id`/`hwid_verifier` to even be considered:

        {"account_id", "hwid_verifier", "model", "platform", "client_name",
         "client_version", "last_seen_at", "observed_id"}

    Returns a bounded, privacy-safe dict. Never returns hwid_verifier,
    hwid_masked, uuid, tokens or any other secret/internal id.
    """
    if slot.get("generation_status") != "ACTIVE":
        return _empty(MATCH_NOT_CLAIMED)
    if slot.get("is_genesis"):
        return _empty(MATCH_GENESIS_PLACEHOLDER)

    account_id = slot.get("account_id")
    verifier = slot.get("hwid_verifier")
    if not account_id or not verifier or not isinstance(verifier, str):
        return _empty(MATCH_UNKNOWN)

    candidates = [
        obs for obs in (telemetry_observations or [])
        if isinstance(obs, dict)
        and obs.get("account_id") == account_id
        and isinstance(obs.get("hwid_verifier"), str)
        and obs["hwid_verifier"] == verifier
    ]
    if not candidates:
        return _empty(MATCH_UNKNOWN)

    # Every remaining candidate already proves the exact same physical
    # device (identical proof key); the only question left is which
    # observation of it to surface. Deterministic: newest last_seen_at,
    # then largest observed_id as a stable tie-breaker -- never "closest
    # timestamp among different identities".
    best = max(candidates, key=lambda obs: (obs.get("last_seen_at") or 0, obs.get("observed_id") or 0))
    return {
        "matched": True,
        "match_state": MATCH_CONFIRMED,
        "model": best.get("model"),
        "model_source": "CLIENT_REPORTED",
        "platform": best.get("platform"),
        "client_name": best.get("client_name"),
        "client_version": best.get("client_version"),
        "last_seen_at": best.get("last_seen_at"),
    }
