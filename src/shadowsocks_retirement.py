"""Narrow contract for retiring non-functional legacy Shadowsocks metadata."""

from __future__ import annotations

import hashlib
import json
import re
import uuid

from .legacy_contract import validate_username


OPERATION = "maintenance.user.retire_shadowsocks"
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_DYNAMIC_SID_RE = re.compile(r"([?&]sid=)[^&#]*")


def _digest(value) -> str:
    raw = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _credential_verifier(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _credential_mask(value: str) -> str:
    return "uuid_" + hashlib.sha256(("mask\0" + value).encode("utf-8")).hexdigest()[:8]


def _subscription_token_verifier(user: dict) -> str | None:
    raw_url = str(user.get("subscription_url") or "")
    token = raw_url.rstrip("/").rsplit("/", 1)[-1]
    if not token or token == raw_url:
        return None
    return _credential_verifier(token)


def _canonical_links(user: dict) -> list[str]:
    return sorted(
        _DYNAMIC_SID_RE.sub(r"\1<DYNAMIC-SID>", str(link).strip())
        for link in (user.get("links") or [])
        if str(link).strip()
    )


def _protocol_counts(user: dict) -> dict[str, int]:
    result: dict[str, int] = {}
    for link in _canonical_links(user):
        protocol = link.split("://", 1)[0].lower() if "://" in link else "unknown"
        result[protocol] = result.get(protocol, 0) + 1
    return dict(sorted(result.items()))


def functional_contract(user: dict) -> dict:
    if not isinstance(user, dict):
        raise ValueError("Marzban user must be an object")
    proxies = user.get("proxies") or {}
    vless = proxies.get("vless")
    if not isinstance(vless, dict):
        raise ValueError("VLESS proxy is required")
    try:
        vless_uuid = str(uuid.UUID(vless.get("id"))).lower()
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError("VLESS UUID is invalid") from exc
    inbounds = user.get("inbounds") or {}
    vless_inbounds = inbounds.get("vless")
    if not isinstance(vless_inbounds, list) or not vless_inbounds:
        raise ValueError("VLESS inbound set is required")
    if len(set(vless_inbounds)) != len(vless_inbounds):
        raise ValueError("duplicate VLESS inbound")
    counts = _protocol_counts(user)
    if any(protocol != "vless" for protocol in counts):
        raise ValueError("non-VLESS subscription entry is still effective")
    return {
        "username": validate_username(user.get("username")),
        "vless_uuid_verifier": _credential_verifier(vless_uuid),
        "vless_uuid_mask": _credential_mask(vless_uuid),
        "vless_flow": vless.get("flow") or "",
        "vless_inbounds": sorted(vless_inbounds),
        "expire": user.get("expire"),
        "status": user.get("status"),
        "data_limit": user.get("data_limit"),
        "data_limit_reset_strategy": user.get("data_limit_reset_strategy"),
        "subscription_token_verifier": _subscription_token_verifier(user),
        "subscription_links_digest": _digest(_canonical_links(user)),
        "subscription_protocol_counts": counts,
    }


def retirement_snapshot(user: dict) -> dict:
    contract = functional_contract(user)
    proxies = user.get("proxies") or {}
    shadowsocks = proxies.get("shadowsocks")
    contract["proxy_types"] = sorted(proxies)
    contract["shadowsocks_metadata"] = isinstance(shadowsocks, dict)
    contract["shadowsocks_method"] = (
        shadowsocks.get("method") if isinstance(shadowsocks, dict) else None
    )
    contract["state_digest"] = _digest(contract)
    return contract


def validate_retirement_request(data: dict) -> dict:
    if not isinstance(data, dict) or set(data) != {"username", "expected_state_digest"}:
        raise ValueError("invalid Shadowsocks retirement request")
    expected = data["expected_state_digest"]
    if not isinstance(expected, str) or not _HASH_RE.fullmatch(expected):
        raise ValueError("invalid expected state digest")
    return {
        "username": validate_username(data["username"]),
        "expected_state_digest": expected,
    }


def build_retirement_payload(user: dict) -> dict:
    """Build the only allowed mutation; caller never supplies proxy fields."""
    functional_contract(user)
    vless = user["proxies"]["vless"]
    return {
        "proxies": {
            "vless": {
                "id": str(uuid.UUID(vless["id"])).lower(),
                "flow": vless.get("flow") or "",
            }
        }
    }


def verify_retirement(before: dict, after: dict) -> dict:
    before_contract = functional_contract(before)
    after_contract = functional_contract(after)
    if before_contract != after_contract:
        raise ValueError("VLESS functional contract changed during metadata retirement")
    if "shadowsocks" in (after.get("proxies") or {}):
        raise ValueError("Shadowsocks metadata remains after retirement")
    result = retirement_snapshot(after)
    result["outcome"] = "REMOVED"
    return result


def build_functional_repair_payload(before: dict, after: dict) -> dict:
    """Best-effort per-user repair for an unexpected Marzban partial-update drift."""
    expected = functional_contract(before)
    observed = functional_contract(after)
    payload = {}
    if (
        expected["vless_uuid_verifier"] != observed["vless_uuid_verifier"]
        or expected["vless_flow"] != observed["vless_flow"]
    ):
        payload.update(build_retirement_payload(before))
    if expected["vless_inbounds"] != observed["vless_inbounds"]:
        payload["inbounds"] = {"vless": expected["vless_inbounds"]}
    if expected["expire"] != observed["expire"]:
        payload["expire"] = int(expected["expire"] or 0)
    if expected["status"] != observed["status"]:
        payload["status"] = expected["status"]
    if expected["data_limit"] != observed["data_limit"]:
        payload["data_limit"] = expected["data_limit"]
    if (
        expected["data_limit_reset_strategy"]
        != observed["data_limit_reset_strategy"]
    ):
        payload["data_limit_reset_strategy"] = expected["data_limit_reset_strategy"]
    return payload
