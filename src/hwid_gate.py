"""PH3-04 deterministic HWID fail-closed compatibility gate.

Dormant: no legacy route imports this module. It never determines account
identity, never touches Telegram ownership/binding tables, and never invents
a new provisioning path -- slot resolution reuses the existing PH3-02
`DeviceSlotStore.claim` primitive exactly as-is.

The caller must already have resolved `account_id` through some other,
unrelated authority. This module accepts no slot id, no generation, no
child username/UUID and no Telegram ownership proof from any caller --
`evaluate()`'s signature has no parameter for any of them, so there is
nothing for a caller/frontend to forge.

HWID is a practical device identifier, not a cryptographic credential: it is
not authentication, not proof of Telegram ownership, and does not authorize
ownership recovery or cross-account slot substitution (device_slots.claim
raises CrossAccountHWID and this module turns that into a deterministic
deny, never a silent takeover).
"""

from __future__ import annotations

from dataclasses import dataclass

from . import compat_registry
from .device_slots import (
    CapacityConflict,
    CapacityReached,
    CrossAccountHWID,
    DeviceSlotStore,
    EntitlementUnavailable,
    InvalidHWID,
)

DECISION_KNOWN_SLOT = "KNOWN_SLOT"
DECISION_ASSIGN_FREE_SLOT = "ASSIGN_FREE_SLOT"
DECISION_DENY_UNSUPPORTED_CLIENT = "DENY_UNSUPPORTED_CLIENT"
DECISION_DENY_MISSING_HWID = "DENY_MISSING_HWID"
DECISION_DENY_MALFORMED_HWID = "DENY_MALFORMED_HWID"
DECISION_DENY_SLOT_LIMIT = "DENY_SLOT_LIMIT"
DECISION_DENY_CROSS_ACCOUNT_HWID = "DENY_CROSS_ACCOUNT_HWID"
DECISION_INTERNAL_ERROR = "INTERNAL_ERROR"

_ALLOW_DECISIONS = frozenset({DECISION_KNOWN_SLOT, DECISION_ASSIGN_FREE_SLOT})


@dataclass(frozen=True)
class HwidGateDecision:
    decision: str
    slot_number: int | None = None
    generation: int | None = None
    slot_result: str | None = None  # "EXISTING" or "CLAIMED", mirrors device_slots.claim

    @property
    def allowed(self) -> bool:
        return self.decision in _ALLOW_DECISIONS


def evaluate(
    *,
    slots: DeviceSlotStore,
    account_id: int,
    client_name,
    client_version,
    platform,
    hwid_candidate_present: bool,
    hwid_candidate_supported: bool,
    raw_hwid: str | None,
    hmac_key,
    now: int | None = None,
) -> HwidGateDecision:
    """Fail-closed compatibility + slot decision for one already-authenticated
    account. Never creates/modifies a parent account, alias, Telegram link,
    child intent or outbox row -- only ever calls `slots.claim`, which is the
    existing PH3-02 atomic slot primitive."""
    if compat_registry.classify(client_name, client_version, platform) != compat_registry.SUPPORTED:
        return HwidGateDecision(DECISION_DENY_UNSUPPORTED_CLIENT)
    if not hwid_candidate_present:
        return HwidGateDecision(DECISION_DENY_MISSING_HWID)
    if not hwid_candidate_supported or not raw_hwid:
        return HwidGateDecision(DECISION_DENY_MALFORMED_HWID)

    try:
        result = slots.claim(int(account_id), raw_hwid, hmac_key, now=now)
    except CrossAccountHWID:
        # A practical HWID value already active under a different account.
        # This is a deterministic refusal, never a cross-account takeover.
        return HwidGateDecision(DECISION_DENY_CROSS_ACCOUNT_HWID)
    except (CapacityConflict, CapacityReached):
        return HwidGateDecision(DECISION_DENY_SLOT_LIMIT)
    except (InvalidHWID, EntitlementUnavailable):
        # Fail closed on any unexpected/invalid state rather than guessing.
        return HwidGateDecision(DECISION_INTERNAL_ERROR)

    decision = DECISION_KNOWN_SLOT if result["result"] == "EXISTING" else DECISION_ASSIGN_FREE_SLOT
    return HwidGateDecision(
        decision, slot_number=result["slot_number"], generation=result["generation"],
        slot_result=result["result"],
    )
