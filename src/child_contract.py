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
_CHILD_USERNAME_RE = re.compile(r"^mgc_[a-z2-7]{26}$")
_OPERATION_ID_RE = re.compile(r"^op_[a-z2-7]{26}$")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_VERIFIER_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_INBOUND_RE = re.compile(r"^[^\x00-\x1f\x7f]{1,128}$")


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
    if not isinstance(proxies, dict) or set(proxies) not in (
        {"vless"}, {"vless", "shadowsocks"}
    ):
        raise ValueError("child canary supports only vless with optional shadowsocks")
    vless = proxies.get("vless")
    if not isinstance(vless, dict) or not isinstance(vless.get("id"), str):
        raise ValueError("source vless credential is missing")
    proxy_options = {"vless": {"flow": vless.get("flow") or ""}}
    if "shadowsocks" in proxies:
        shadowsocks = proxies["shadowsocks"]
        if (
            not isinstance(shadowsocks, dict)
            or not isinstance(shadowsocks.get("password"), str)
            or not shadowsocks.get("password")
            or shadowsocks.get("method") not in {
                "aes-128-gcm", "aes-256-gcm", "chacha20-ietf-poly1305"
            }
        ):
            raise ValueError("source shadowsocks contract is invalid")
        proxy_options["shadowsocks"] = {"method": shadowsocks["method"]}
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
    child_shadowsocks_password = None
    if "shadowsocks" in protocols:
        child_ss = proxies["shadowsocks"]
        source_ss = source_user["proxies"]["shadowsocks"]
        if (
            not isinstance(child_ss, dict)
            or child_ss.get("method") != contract["proxy_options"]["shadowsocks"]["method"]
            or not isinstance(child_ss.get("password"), str)
            or not child_ss.get("password")
            or child_ss["password"] == source_ss["password"]
        ):
            raise ValueError("remote child shadowsocks credential mismatch")
        child_shadowsocks_password = child_ss["password"]
    return {
        "username": normalized["child_username"],
        "expire": normalized["expire"],
        "status": "active",
        "inbounds": contract["inbounds"],
        "protocols": contract["protocols"],
        "uuid": child_uuid,
        "shadowsocks_password": child_shadowsocks_password,
    }


def validate_child_credentials_request(data: dict) -> dict:
    required = {
        "operation_id", "child_username", "source_contract_hash", "expire",
        "uuid_verifier", "shadowsocks_verifier",
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
    ss_verifier = data["shadowsocks_verifier"]
    if ss_verifier is not None and (
        not isinstance(ss_verifier, str) or not _VERIFIER_RE.fullmatch(ss_verifier)
    ):
        raise ValueError("invalid child Shadowsocks verifier")
    expire = data["expire"]
    if isinstance(expire, bool) or not isinstance(expire, int) or expire < 0:
        raise ValueError("invalid child credential expiry")
    return {
        "operation_id": operation_id,
        "child_username": child_username,
        "source_contract_hash": contract_hash,
        "expire": expire,
        "uuid_verifier": uuid_verifier,
        "shadowsocks_verifier": ss_verifier,
    }


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
    ss_password = None
    if "shadowsocks" in source_contract(user)["protocols"]:
        ss_password = user["proxies"]["shadowsocks"].get("password")
        if normalized["shadowsocks_verifier"] is None or not hmac.compare_digest(
            credential_verifier(ss_password), normalized["shadowsocks_verifier"]
        ):
            raise ValueError("remote child Shadowsocks verifier mismatch")
    elif normalized["shadowsocks_verifier"] is not None:
        raise ValueError("unexpected Shadowsocks verifier")
    return {
        "username": normalized["child_username"],
        "credentials": {
            "vless_uuid": child_uuid,
            "shadowsocks_password": ss_password,
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
