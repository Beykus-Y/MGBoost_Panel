"""Typed `child.user.wl.set` contract shared by main and the broker (PH6-06).

The ONLY remote mutation this contract permits is a minimal partial update of
a child user's own `inbounds.vless` member list -- exactly the PH6-06 roadmap
requirement ("Remote: reread user, change only inbounds.vless, exact WL set,
never proxies/UUID/expire/data_limit, then verify").

Doctrine preserved: this is NOT a caller-suppliable inbound-config channel.
Every target the broker computes is derived from authoritative live reads
plus the static PH0-05 allowlist (`src/wl_topology.WL_INBOUND_TAGS`) only:

- EXCLUDED target = observed_vless - WL_INBOUND_TAGS  (pure subtraction)
- INCLUDED target = (observed_vless - WL_INBOUND_TAGS) | baseline_wl_tags,
  where every baseline tag must itself be a literal member of the static
  allowlist. The caller may therefore never introduce a tag that the
  project's own versioned topology does not already name.

The one piece of caller-supplied membership (`baseline_wl_tags`, INCLUDED
direction) is reference data -- the exact WL-tag subset recorded at a
paired prior disable for the same child -- not free-form configuration;
each element is statically validated and its use never shrinks a list.
"""

from __future__ import annotations

import base64
import hashlib
import re

from .child_contract import validate_child_username, _INBOUND_RE
from .wl_topology import WL_INBOUND_TAGS

WL_SET_OPERATION = "child.user.wl.set"
_WL_OPERATION_ID_RE = re.compile(r"^wla_[a-z2-7]{26}$")
_WL_DIRECTION_RE = re.compile(r"^(EXCLUDED|INCLUDED)$")


def _base32_128(raw: bytes) -> str:
    return base64.b32encode(raw[:16]).decode("ascii").lower().rstrip("=")


def derive_wl_operation_id(child_username: str, epoch: int, direction: str) -> str:
    """One deterministic id per (child, enforcement epoch, direction).

    A new decision epoch always derives a different id, so an operation from
    a superseded epoch can never collide with -- or be mistaken for -- the
    current one (the same anti-staleness shape as PH3-08's sync ids)."""
    validate_child_username(child_username)
    if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 1:
        raise ValueError("invalid enforcement epoch")
    if direction not in ("EXCLUDED", "INCLUDED"):
        raise ValueError("invalid enforcement direction")
    return "wla_" + _base32_128(
        hashlib.sha256(
            f"mgboost-wl-enforce-v1\0{epoch}\0{direction}\0{child_username}".encode()
        ).digest()
    )


def validate_wl_operation_id(value) -> str:
    if not isinstance(value, str) or not _WL_OPERATION_ID_RE.fullmatch(value):
        raise ValueError("invalid wl enforcement operation id")
    return value


def normalize_observed_vless(user: dict) -> list[str]:
    """Strictly shape the vless member tags of a live Marzban user payload."""
    if not isinstance(user, dict):
        raise ValueError("remote user payload is invalid")
    inbounds = user.get("inbounds")
    if not isinstance(inbounds, dict) or set(inbounds) - {"vless"}:
        raise ValueError("remote user inbound map is not exactly {vless: [...]}")
    tags = inbounds.get("vless")
    if not isinstance(tags, list) or len(tags) > 256:
        raise ValueError("remote user vless inbound list is invalid")
    normalized = []
    for tag in tags:
        if not isinstance(tag, str) or not _INBOUND_RE.fullmatch(tag):
            raise ValueError("remote user carries an invalid inbound tag")
        normalized.append(tag)
    if len(set(normalized)) != len(normalized):
        raise ValueError("remote user carries duplicate inbound tags")
    return sorted(normalized)


def build_wl_target(observed_vless: list[str], direction: str, baseline_wl_tags=None) -> list[str]:
    """Pure target computation. Raises rather than ever emitting a destructive
    or invented result:
      - EXCLUDED refuses to produce an empty list (a Marzban user with zero
        inbounds is nonsense and can never be what quota enforcement meant).
      - INCLUDED accepts baseline members from the static allowlist only."""
    if direction == "EXCLUDED":
        if baseline_wl_tags is not None:
            raise ValueError("baseline_wl_tags is only meaningful for INCLUDED")
        target = sorted(set(observed_vless) - set(WL_INBOUND_TAGS))
        if not target:
            raise ValueError(
                "refusing to remove all vless inbounds; the non-WL remainder "
                "would be empty"
            )
        return target
    if direction == "INCLUDED":
        if not baseline_wl_tags:
            raise ValueError("INCLUDED requires a validated baseline_wl_tags set")
        baseline = set(baseline_wl_tags)
        unknown = baseline - set(WL_INBOUND_TAGS)
        if unknown:
            raise ValueError(
                "baseline_wl_tags contains non-allowlisted tags"
            )
        return sorted((set(observed_vless) - set(WL_INBOUND_TAGS)) | baseline)
    raise ValueError("invalid direction")


def validate_wl_set_request(data: dict) -> dict:
    required = {
        "operation_id", "child_username", "uuid_verifier",
        "direction", "baseline_wl_tags",
    }
    if not isinstance(data, dict) or set(data) != required:
        raise ValueError("invalid wl state fields")
    operation_id = validate_wl_operation_id(data["operation_id"])
    child_username = validate_child_username(data["child_username"])
    uuid_verifier = data["uuid_verifier"]
    if not isinstance(uuid_verifier, str):
        raise ValueError("invalid uuid verifier")
    from .child_contract import credential_verifier
    if not re.fullmatch(r"^sha256:[0-9a-f]{64}$", uuid_verifier):
        raise ValueError("invalid uuid verifier format")
    direction = data["direction"]
    if not isinstance(direction, str) or not _WL_DIRECTION_RE.fullmatch(direction):
        raise ValueError("invalid wl direction")
    baseline = data["baseline_wl_tags"]
    if direction == "INCLUDED":
        if (
            not isinstance(baseline, list)
            or not baseline
            or len(baseline) > len(WL_INBOUND_TAGS)
            or any(not isinstance(t, str) or t not in WL_INBOUND_TAGS for t in baseline)
        ):
            raise ValueError(
                "baseline_wl_tags must be a non-empty subset of the exact "
                "PH0-05 WL allowlist for INCLUDED"
            )
        baseline = sorted(set(baseline))
    else:
        # The absence-of-membership meaning is fixed by construction; the
        # field stays on the wire so every request shape stays identical.
        if baseline is not None:
            raise ValueError("baseline_wl_tags must be null for EXCLUDED")
        baseline = None
    return {
        "operation_id": operation_id,
        "child_username": child_username,
        "uuid_verifier": uuid_verifier,
        "direction": direction,
        "baseline_wl_tags": baseline,
    }


def verify_wl_converged(
    after_user: dict,
    *,
    child_username: str,
    target_vless: list[str],
    before_uuid: str,
    before_status,
    before_expire,
) -> dict:
    """Post-mutation reread verification. The mutation was supposed to touch
    ONLY the inbound membership: identity, UUID, status and expire must be
    byte-identical to their pre-mutation values, and the vless member list
    must now equal the computed target exactly."""
    if not isinstance(after_user, dict) or after_user.get("username") != child_username:
        raise ValueError("remote child identity mismatch after wl mutation")
    import uuid as _uuid
    try:
        after_uuid = str(_uuid.UUID(after_user["proxies"]["vless"]["id"])).lower()
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise ValueError("remote child UUID became unreadable") from exc
    if after_uuid != before_uuid:
        raise RuntimeError(
            "unexpected credential rotation during an inbound-only wl mutation"
        )
    if after_user.get("status") != before_status:
        raise RuntimeError("unexpected status drift during an inbound-only wl mutation")
    if int(after_user.get("expire") or 0) != int(before_expire or 0):
        raise RuntimeError("unexpected expire drift during an inbound-only wl mutation")
    observed_after = normalize_observed_vless(after_user)
    if observed_after != sorted(target_vless):
        raise RuntimeError("remote vless membership did not converge to the exact target")
    return {"target_inbounds_count": len(target_vless)}
