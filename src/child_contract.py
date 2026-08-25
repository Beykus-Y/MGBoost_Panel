"""Typed `child.user.ensure` contract shared by main and the broker."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import uuid

from .legacy_contract import validate_username


CHILD_OPERATION = "child.user.ensure"
CHILD_OBSERVE_OPERATION = "child.user.observe"
CHILD_REVOKE_OPERATION = "child.user.revoke"
_CHILD_USERNAME_RE = re.compile(r"^mgc_[a-z2-7]{26}$")
_OPERATION_ID_RE = re.compile(r"^op_[a-z2-7]{26}$")
_LIFECYCLE_OPERATION_ID_RE = re.compile(r"^lc_[a-z2-7]{26}$")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_VERIFIER_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_INBOUND_RE = re.compile(r"^[^\x00-\x1f\x7f]{1,128}$")
LIFECYCLE_OPERATION_KINDS = frozenset({"REVOKE", "FREE", "REBIND"})
_SYNC_OPERATION_ID_RE = re.compile(r"^sy_[a-z2-7]{26}$")
_SYNC_STATUS_RE = re.compile(r"^(active|disabled)$")


def _canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _base32_128(raw: bytes) -> str:
    return base64.b32encode(raw[:16]).decode("ascii").lower().rstrip("=")


def derive_child_username(account_public_id: str, slot_number: int, generation: int) -> str:
    if not isinstance(account_public_id, str) or not account_public_id.startswith("acct_"):
        raise ValueError("invalid account public id")
    if not 1 <= int(slot_number) <= 99 or int(generation) < 1:
        raise ValueError("invalid slot generation")
    material = (
        f"mgboost-child-v1\0{account_public_id}\0{int(slot_number)}\0{int(generation)}"
    ).encode("utf-8")
    return "mgc_" + _base32_128(hashlib.sha256(material).digest())


def derive_operation_id(child_username: str) -> str:
    validate_child_username(child_username)
    return "op_" + _base32_128(
        hashlib.sha256(("mgboost-child-ensure-v1\0" + child_username).encode()).digest()
    )


def validate_child_username(value) -> str:
    if not isinstance(value, str) or not _CHILD_USERNAME_RE.fullmatch(value):
        raise ValueError("invalid server-derived child username")
    return value


def validate_operation_id(value) -> str:
    if not isinstance(value, str) or not _OPERATION_ID_RE.fullmatch(value):
        raise ValueError("invalid child operation id")
    return value


def _normalized_inbounds(value, protocols: set[str]) -> dict:
    if not isinstance(value, dict) or set(value) != protocols:
        raise ValueError("child source inbound map does not match allowed protocols")
    normalized = {}
    for protocol in sorted(protocols):
        tags = value[protocol]
        if not isinstance(tags, list) or len(tags) > 256:
            raise ValueError("invalid child inbound allowlist")
        if protocol == "vless" and not tags:
            raise ValueError("child source requires at least one vless inbound")
        if any(not isinstance(tag, str) or not _INBOUND_RE.fullmatch(tag) for tag in tags):
            raise ValueError("invalid child inbound tag")
        if len(set(tags)) != len(tags):
            raise ValueError("duplicate child inbound tag")
        normalized[protocol] = sorted(tags)
    return normalized


def source_contract(user: dict) -> dict:
    if not isinstance(user, dict):
        raise ValueError("source user is invalid")
    proxies = user.get("proxies")
    if not isinstance(proxies, dict) or set(proxies) != {"vless"}:
        raise ValueError("MGBoost child contract is VLESS-only")
    vless = proxies.get("vless")
    if not isinstance(vless, dict) or not isinstance(vless.get("id"), str):
        raise ValueError("source vless credential is missing")
    proxy_options = {"vless": {"flow": vless.get("flow") or ""}}
    protocols = set(proxies)
    return {
        "schema": 1,
        "protocols": sorted(protocols),
        "proxy_options": proxy_options,
        "inbounds": _normalized_inbounds(user.get("inbounds"), protocols),
    }


def source_contract_hash(user: dict) -> str:
    return hashlib.sha256(_canonical(source_contract(user))).hexdigest()


def validate_child_ensure_request(data: dict) -> dict:
    required = {
        "operation_id", "child_username", "source_username",
        "source_contract_hash", "expire",
    }
    if not isinstance(data, dict) or set(data) != required:
        raise ValueError("invalid child ensure fields")
    operation_id = validate_operation_id(data["operation_id"])
    child_username = validate_child_username(data["child_username"])
    if operation_id != derive_operation_id(child_username):
        raise ValueError("operation id does not match child identity")
    contract_hash = data["source_contract_hash"]
    if not isinstance(contract_hash, str) or not _HASH_RE.fullmatch(contract_hash):
        raise ValueError("invalid source contract hash")
    expire = data["expire"]
    if isinstance(expire, bool) or not isinstance(expire, int) or expire < 0:
        raise ValueError("expire must be a nonnegative integer")
    return {
        "operation_id": operation_id,
        "child_username": child_username,
        "source_username": validate_username(data["source_username"]),
        "source_contract_hash": contract_hash,
        "expire": expire,
    }


def validate_child_observe_request(data: dict) -> dict:
    """Observe uses the immutable ensure identity and never accepts a mutation payload."""
    return validate_child_ensure_request(data)


def build_child_payload(request: dict, source_user: dict) -> dict:
    normalized = validate_child_ensure_request(request)
    if source_contract_hash(source_user) != normalized["source_contract_hash"]:
        raise ValueError("source user contract changed")
    return {
        "username": normalized["child_username"],
        "proxies": source_contract(source_user)["proxy_options"],
        "inbounds": source_contract(source_user)["inbounds"],
        "expire": normalized["expire"],
        "data_limit": None,
        "data_limit_reset_strategy": "no_reset",
        "status": "active",
        "note": f"MGBoost child ensure {normalized['operation_id']}",
    }


def validate_created_child(user: dict, request: dict, source_user: dict) -> dict:
    normalized = validate_child_ensure_request(request)
    if not isinstance(user, dict) or user.get("username") != normalized["child_username"]:
        raise ValueError("remote child identity mismatch")
    if int(user.get("expire") or 0) != normalized["expire"]:
        raise ValueError("remote child expiry mismatch")
    if user.get("status") != "active" or user.get("data_limit") is not None:
        raise ValueError("remote child entitlement mismatch")
    contract = source_contract(source_user)
    protocols = set(contract["protocols"])
    if _normalized_inbounds(user.get("inbounds"), protocols) != contract["inbounds"]:
        raise ValueError("remote child inbound mismatch")
    proxies = user.get("proxies")
    try:
        child_uuid = str(uuid.UUID(proxies["vless"]["id"])).lower()
        source_uuid = str(uuid.UUID(source_user["proxies"]["vless"]["id"])).lower()
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise ValueError("remote child UUID is invalid") from exc
    if child_uuid == source_uuid:
        raise ValueError("child UUID must differ from legacy source UUID")
    if set(proxies) != protocols:
        raise ValueError("remote child proxy protocols mismatch")
    child_vless = proxies["vless"]
    if (child_vless.get("flow") or "") != contract["proxy_options"]["vless"]["flow"]:
        raise ValueError("remote child vless flow mismatch")
    return {
        "username": normalized["child_username"],
        "expire": normalized["expire"],
        "status": "active",
        "inbounds": contract["inbounds"],
        "protocols": contract["protocols"],
        "uuid": child_uuid,
    }


def validate_child_credentials_request(data: dict) -> dict:
    required = {
        "operation_id", "child_username", "source_contract_hash", "expire",
        "uuid_verifier",
    }
    if not isinstance(data, dict) or set(data) != required:
        raise ValueError("invalid child credential reread fields")
    child_username = validate_child_username(data["child_username"])
    operation_id = validate_operation_id(data["operation_id"])
    if operation_id != derive_operation_id(child_username):
        raise ValueError("operation id does not match child identity")
    contract_hash = data["source_contract_hash"]
    if not isinstance(contract_hash, str) or not _HASH_RE.fullmatch(contract_hash):
        raise ValueError("invalid child contract hash")
    uuid_verifier = data["uuid_verifier"]
    if not isinstance(uuid_verifier, str) or not _VERIFIER_RE.fullmatch(uuid_verifier):
        raise ValueError("invalid child UUID verifier")
    expire = data["expire"]
    if isinstance(expire, bool) or not isinstance(expire, int) or expire < 0:
        raise ValueError("invalid child credential expiry")
    return {
        "operation_id": operation_id,
        "child_username": child_username,
        "source_contract_hash": contract_hash,
        "expire": expire,
        "uuid_verifier": uuid_verifier,
    }


def derive_lifecycle_operation_id(child_username: str, operation_kind: str) -> str:
    validate_child_username(child_username)
    if operation_kind not in LIFECYCLE_OPERATION_KINDS:
        raise ValueError("invalid lifecycle operation kind")
    return "lc_" + _base32_128(
        hashlib.sha256(
            f"mgboost-child-lifecycle-v1\0{operation_kind}\0{child_username}".encode()
        ).digest()
    )


def validate_lifecycle_operation_id(value) -> str:
    if not isinstance(value, str) or not _LIFECYCLE_OPERATION_ID_RE.fullmatch(value):
        raise ValueError("invalid child lifecycle operation id")
    return value


def validate_child_revoke_request(data: dict) -> dict:
    required = {"operation_id", "child_username", "uuid_verifier"}
    if not isinstance(data, dict) or set(data) != required:
        raise ValueError("invalid child revoke fields")
    child_username = validate_child_username(data["child_username"])
    operation_id = validate_lifecycle_operation_id(data["operation_id"])
    if operation_id != derive_lifecycle_operation_id(child_username, "REVOKE"):
        raise ValueError("operation id does not match child identity")
    uuid_verifier = data["uuid_verifier"]
    if not isinstance(uuid_verifier, str) or not _VERIFIER_RE.fullmatch(uuid_verifier):
        raise ValueError("invalid child UUID verifier")
    return {
        "operation_id": operation_id,
        "child_username": child_username,
        "uuid_verifier": uuid_verifier,
    }


def build_revoke_payload(new_uuid: str, flow: str) -> dict:
    """Disable the child and rotate its VLESS UUID in the same mutation, so
    the old credential is unusable even if status handling ever changes."""
    try:
        normalized = str(uuid.UUID(new_uuid)).lower()
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError("invalid replacement uuid") from exc
    return {
        "status": "disabled",
        "proxies": {"vless": {"id": normalized, "flow": flow or ""}},
    }


def verify_revoked_child(user: dict, child_username: str) -> dict:
    if not isinstance(user, dict) or user.get("username") != child_username:
        raise ValueError("remote child identity mismatch")
    if user.get("status") != "disabled":
        raise ValueError("remote child is not disabled")
    return {"username": child_username, "status": "disabled"}


def derive_sync_operation_id(child_username: str, parent_revision: int) -> str:
    """PH3-08: one deterministic operation id per (child, parent desired-state
    revision). A new parent revision always derives a different id, so a stale
    operation from a superseded revision can never collide with -- or be
    mistaken for -- the current one."""
    validate_child_username(child_username)
    if isinstance(parent_revision, bool) or not isinstance(parent_revision, int) or parent_revision < 1:
        raise ValueError("invalid parent revision")
    return "sy_" + _base32_128(
        hashlib.sha256(
            f"mgboost-child-sync-v1\0{parent_revision}\0{child_username}".encode()
        ).digest()
    )


def validate_sync_operation_id(value) -> str:
    if not isinstance(value, str) or not _SYNC_OPERATION_ID_RE.fullmatch(value):
        raise ValueError("invalid child state-sync operation id")
    return value


def validate_child_state_sync_request(data: dict) -> dict:
    required = {"operation_id", "child_username", "desired_status", "desired_expire", "uuid_verifier"}
    if not isinstance(data, dict) or set(data) != required:
        raise ValueError("invalid child state-sync fields")
    child_username = validate_child_username(data["child_username"])
    operation_id = validate_sync_operation_id(data["operation_id"])
    desired_status = data["desired_status"]
    if not isinstance(desired_status, str) or not _SYNC_STATUS_RE.fullmatch(desired_status):
        raise ValueError("invalid desired status")
    desired_expire = data["desired_expire"]
    if desired_expire is not None:
        if isinstance(desired_expire, bool) or not isinstance(desired_expire, int) or desired_expire < 0:
            raise ValueError("invalid desired expire")
        if desired_status != "active":
            raise ValueError("desired expire is only meaningful for an active child")
    uuid_verifier = data["uuid_verifier"]
    if not isinstance(uuid_verifier, str) or not _VERIFIER_RE.fullmatch(uuid_verifier):
        raise ValueError("invalid child UUID verifier")
    return {
        "operation_id": operation_id,
        "child_username": child_username,
        "desired_status": desired_status,
        "desired_expire": desired_expire,
        "uuid_verifier": uuid_verifier,
    }


def build_state_sync_payload(desired_status: str, desired_expire) -> dict:
    """Minimal remote mutation for a reversible parent-driven status/expiry
    change -- never touches `proxies`/UUID, unlike PH3-05's revoke payload."""
    payload = {"status": desired_status}
    if desired_status == "active" and desired_expire is not None:
        payload["expire"] = int(desired_expire)
    return payload


def verify_synced_child(user: dict, child_username: str, desired_status: str, desired_expire) -> dict:
    if not isinstance(user, dict) or user.get("username") != child_username:
        raise ValueError("remote child identity mismatch")
    if user.get("status") != desired_status:
        raise ValueError("remote child status did not converge")
    if desired_status == "active" and desired_expire is not None:
        if int(user.get("expire") or 0) != int(desired_expire):
            raise ValueError("remote child expiry did not converge")
    return {"username": child_username, "status": desired_status}


def credential_verifier(raw: str) -> str:
    if not isinstance(raw, str) or not raw:
        raise ValueError("raw credential is missing")
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def reread_child_credentials(user: dict, request: dict) -> dict:
    normalized = validate_child_credentials_request(request)
    if not isinstance(user, dict) or user.get("username") != normalized["child_username"]:
        raise ValueError("remote child identity mismatch")
    if source_contract_hash(user) != normalized["source_contract_hash"]:
        raise ValueError("remote child contract drift")
    if int(user.get("expire") or 0) != normalized["expire"]:
        raise ValueError("remote child expiry drift")
    if user.get("status") != "active" or user.get("data_limit") is not None:
        raise ValueError("remote child entitlement drift")
    try:
        child_uuid = str(uuid.UUID(user["proxies"]["vless"]["id"])).lower()
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise ValueError("remote child UUID is invalid") from exc
    if not hmac.compare_digest(
        credential_verifier(child_uuid), normalized["uuid_verifier"]
    ):
        raise ValueError("remote child UUID verifier mismatch")
    return {
        "username": normalized["child_username"],
        "credentials": {
            "vless_uuid": child_uuid,
        },
        "observed": {
            "contract_hash": normalized["source_contract_hash"],
            "protocols": source_contract(user)["protocols"],
            "inbounds": source_contract(user)["inbounds"],
            "proxy_options": source_contract(user)["proxy_options"],
            "status": "active",
            "expire": normalized["expire"],
            "data_limit": None,
        },
    }
