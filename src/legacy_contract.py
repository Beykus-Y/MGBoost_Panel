"""Typed validation for the retained Phase 1 legacy Marzban operations."""

import re


USERNAME_RE = re.compile(r"^[A-Za-z0-9_.@-]{1,128}$")
RESET_STRATEGIES = {"no_reset", "day", "week", "month", "year"}


def validate_username(value) -> str:
    if not isinstance(value, str) or not USERNAME_RE.fullmatch(value.strip()):
        raise ValueError("username must be 1-128 chars: letters, digits, _, ., @ or -")
    return value.strip()


def validate_user_payload(data, *, creating: bool, strict: bool = False) -> dict:
    if not isinstance(data, dict):
        raise ValueError("Expected JSON object")

    allowed = {
        "username",
        "proxies",
        "inbounds",
        "expire",
        "data_limit",
        "data_limit_reset_strategy",
        "note",
        "status",
        "on_hold_expire_duration",
        "on_hold_timeout",
        "auto_delete_in_days",
        "next_plan",
    }
    if strict and set(data) - allowed:
        raise ValueError("Unexpected user fields")
    payload = {key: value for key, value in data.items() if key in allowed}

    if creating:
        payload["username"] = validate_username(payload.get("username"))
        payload.setdefault("status", "active")
    else:
        payload.pop("username", None)

    if "status" in payload and payload["status"] not in {"active", "disabled", "on_hold"}:
        raise ValueError("status must be active, disabled or on_hold")
    if creating and payload.get("status") == "disabled":
        raise ValueError("new user status must be active or on_hold")

    for key in ("proxies", "inbounds", "next_plan"):
        if key in payload and payload[key] is not None and not isinstance(payload[key], dict):
            raise ValueError(f"{key} must be an object")

    for key in ("expire", "data_limit", "on_hold_expire_duration", "auto_delete_in_days"):
        if key in payload and payload[key] is not None:
            try:
                value = int(payload[key])
            except (TypeError, ValueError):
                raise ValueError(f"{key} must be an integer or null")
            if value < 0:
                raise ValueError(f"{key} must be >= 0")
            payload[key] = value

    if "data_limit_reset_strategy" in payload:
        strategy = payload["data_limit_reset_strategy"]
        if strategy not in RESET_STRATEGIES:
            raise ValueError("invalid data_limit_reset_strategy")

    if "note" in payload and payload["note"] is not None:
        if not isinstance(payload["note"], str):
            raise ValueError("note must be a string or null")
        payload["note"] = payload["note"][:512]

    payload.setdefault("proxies", {})
    payload.setdefault("inbounds", {})
    payload.setdefault("data_limit_reset_strategy", "no_reset")
    return payload


def validate_renew_payload(data, *, strict: bool = False) -> dict:
    if not isinstance(data, dict):
        raise ValueError("Expected JSON object")

    if strict and set(data) - {"add_days", "expire", "data_limit", "status"}:
        raise ValueError("Unexpected renewal fields")
    payload = {}
    add_days = data.get("add_days")
    expire = data.get("expire")
    data_limit = data.get("data_limit")

    if add_days not in (None, ""):
        try:
            payload["add_days"] = int(add_days)
        except (TypeError, ValueError):
            raise ValueError("add_days must be an integer")
        if payload["add_days"] < 1 or payload["add_days"] > 3650:
            raise ValueError("add_days must be between 1 and 3650")

    if expire not in (None, ""):
        try:
            payload["expire"] = int(expire)
        except (TypeError, ValueError):
            raise ValueError("expire must be an integer")
        if payload["expire"] < 0:
            raise ValueError("expire must be >= 0")

    if data_limit not in (None, ""):
        try:
            payload["data_limit"] = int(data_limit)
        except (TypeError, ValueError):
            raise ValueError("data_limit must be an integer")
        if payload["data_limit"] < 0:
            raise ValueError("data_limit must be >= 0")

    if "status" in data:
        status = data["status"]
        if status not in {"active", "disabled", "on_hold"}:
            raise ValueError("status must be active, disabled or on_hold")
        payload["status"] = status

    if not payload:
        raise ValueError("Nothing to renew")
    return payload
