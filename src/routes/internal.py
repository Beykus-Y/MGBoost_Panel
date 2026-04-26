import json
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, unquote, urlsplit

from ..http_utils import error_response, json_response, read_body
from ..marzban import MarzbanClient

_client = MarzbanClient()
_USERNAME_RE = re.compile(r"^[A-Za-z0-9_.@-]{1,128}$")
_RESET_STRATEGIES = {"no_reset", "day", "week", "month", "year"}


def _parse_int(query: dict, key: str, default: int, *, minimum: int | None = None, maximum: int | None = None) -> int:
    raw = query.get(key, [default])[0]
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = default
    if minimum is not None:
        value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


def _service_counts(db) -> dict:
    global_configs = db.get_extra_configs()
    node_filters = db.get_node_filters()
    per_user_configs_map = db.get_per_user_configs_map()
    filtered_users = sum(
        1 for filt in node_filters.values()
        if filt.get("all") is False and bool(filt.get("allowed_configs"))
    )
    per_user_config_total = sum(len(configs) for configs in per_user_configs_map.values())

    return {
        "global_configs": len(global_configs),
        "global_enabled_configs": sum(1 for config in global_configs if config.get("enabled")),
        "per_user_config_users": len(per_user_configs_map),
        "per_user_configs": per_user_config_total,
        "filtered_users": filtered_users,
    }


def _marzban_error(handler, exc: Exception, fallback: str):
    if isinstance(exc, HTTPError):
        try:
            payload = json.loads(exc.read().decode("utf-8"))
        except Exception:
            payload = {}
        message = payload.get("detail") or payload.get("error") or fallback
        error_response(handler, exc.code, message)
        return

    if isinstance(exc, URLError):
        error_response(handler, 502, fallback, details={"reason": str(exc.reason)})
        return

    error_response(handler, 502, fallback, details={"reason": str(exc)})


def _get_admin_token(handler):
    try:
        return _client.get_admin_token_from_env()
    except Exception as exc:
        error_response(handler, 503, "Marzban admin credentials are not configured", details={"reason": str(exc)})
        return None


def _validate_user_payload(data, *, creating: bool):
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
    payload = {key: value for key, value in data.items() if key in allowed}

    if creating:
        username = payload.get("username")
        if not isinstance(username, str) or not _USERNAME_RE.match(username.strip()):
            raise ValueError("username must be 1-128 chars: letters, digits, _, ., @ or -")
        payload["username"] = username.strip()
        payload.setdefault("status", "active")
    elif "username" in payload:
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
        if strategy not in _RESET_STRATEGIES:
            raise ValueError("invalid data_limit_reset_strategy")

    if "note" in payload and payload["note"] is not None:
        if not isinstance(payload["note"], str):
            raise ValueError("note must be a string or null")
        payload["note"] = payload["note"][:512]

    payload.setdefault("proxies", {})
    payload.setdefault("inbounds", {})
    payload.setdefault("data_limit_reset_strategy", "no_reset")
    return payload


def _validate_renew_payload(data):
    if not isinstance(data, dict):
        raise ValueError("Expected JSON object")

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


def handle_internal_status(handler):
    db = handler.server.db
    admin_token = _get_admin_token(handler)
    if not admin_token:
        return

    try:
        nodes = _client.get_nodes(admin_token)
    except Exception as exc:
        _marzban_error(handler, exc, "Could not load Marzban nodes")
        return

    interval = db.get_setting("sub_update_interval")
    json_response(handler, 200, {
        "service": "ok",
        "marzban": {"reachable": True},
        "nodes": nodes,
        "counts": _service_counts(db),
        "settings": {
            "sub_update_interval": int(interval) if interval not in (None, "") else None,
        },
    })


def handle_internal_users_list(handler):
    db = handler.server.db
    admin_token = _get_admin_token(handler)
    if not admin_token:
        return

    query = parse_qs(urlsplit(handler.path).query)
    limit = _parse_int(query, "limit", 500, minimum=1, maximum=1000)
    offset = _parse_int(query, "offset", 0, minimum=0)

    try:
        payload = _client.get_users(admin_token, limit=limit, offset=offset)
    except Exception as exc:
        _marzban_error(handler, exc, "Could not load Marzban users")
        return

    users = payload.get("users") if isinstance(payload, dict) else payload
    users = users or []
    node_filters = db.get_node_filters()
    per_user_configs_map = db.get_per_user_configs_map()
    last_devices = db.get_last_devices_by_usernames([user.get("username") for user in users])

    items = []
    for user in users:
        username = user.get("username")
        filt = node_filters.get(username)
        items.append({
            **user,
            "proxy_filtered": bool(filt and filt.get("all") is False and filt.get("allowed_configs")),
            "proxy_extra_configs": len(per_user_configs_map.get(username, [])),
            "proxy_last_device": last_devices.get(username),
        })

    total = payload.get("total") if isinstance(payload, dict) else None
    json_response(handler, 200, {
        "items": items,
        "total": total if isinstance(total, int) else len(items),
        "limit": limit,
        "offset": offset,
    })


def handle_internal_inbounds(handler):
    admin_token = _get_admin_token(handler)
    if not admin_token:
        return

    try:
        inbounds = _client.get_inbounds(admin_token)
    except Exception as exc:
        _marzban_error(handler, exc, "Could not load Marzban inbounds")
        return

    json_response(handler, 200, inbounds)


def handle_internal_user_create(handler):
    admin_token = _get_admin_token(handler)
    if not admin_token:
        return

    try:
        data = json.loads(read_body(handler) or b"{}")
        payload = _validate_user_payload(data, creating=True)
    except (json.JSONDecodeError, ValueError) as exc:
        error_response(handler, 400, str(exc))
        return

    try:
        user = _client.create_user(payload, admin_token)
    except Exception as exc:
        _marzban_error(handler, exc, "Could not create Marzban user")
        return

    json_response(handler, 201, user)


def handle_internal_user_renew(handler, username):
    username = unquote(username)
    admin_token = _get_admin_token(handler)
    if not admin_token:
        return

    try:
        data = json.loads(read_body(handler) or b"{}")
        payload = _validate_renew_payload(data)
    except (json.JSONDecodeError, ValueError) as exc:
        error_response(handler, 400, str(exc))
        return

    update_payload = {}
    if "add_days" in payload:
        try:
            user = _client.get_user(username, admin_token)
        except Exception as exc:
            _marzban_error(handler, exc, f"Could not load user {username}")
            return
        current_expire = user.get("expire") or 0
        base = max(int(current_expire or 0), int(time.time()))
        update_payload["expire"] = base + payload["add_days"] * 86400
    if "expire" in payload:
        update_payload["expire"] = payload["expire"]
    if "data_limit" in payload:
        update_payload["data_limit"] = payload["data_limit"] or None
    if "status" in payload:
        update_payload["status"] = payload["status"]

    try:
        user = _client.modify_user(username, update_payload, admin_token)
    except Exception as exc:
        _marzban_error(handler, exc, f"Could not renew user {username}")
        return

    json_response(handler, 200, user)


def handle_internal_user_delete(handler, username):
    username = unquote(username)
    admin_token = _get_admin_token(handler)
    if not admin_token:
        return

    try:
        _client.delete_user(username, admin_token)
    except Exception as exc:
        _marzban_error(handler, exc, f"Could not delete user {username}")
        return

    json_response(handler, 200, {"ok": True})


def handle_internal_user_detail(handler, username):
    username = unquote(username)
    db = handler.server.db
    admin_token = _get_admin_token(handler)
    if not admin_token:
        return

    try:
        user = _client.get_user(username, admin_token)
        usage = _client.get_user_usage(username, admin_token)
    except Exception as exc:
        _marzban_error(handler, exc, f"Could not load user {username}")
        return

    node_filter = db.get_node_filter(username) or {"all": True, "allowed_configs": []}
    per_user_configs = [
        {
            "name": config["name"],
            "uri": config["uri"],
            "enabled": bool(config["enabled"]),
        }
        for config in db.get_per_user_configs(username)
    ]
    device_history = db.get_device_history_by_username(username, limit=10)

    json_response(handler, 200, {
        "user": user,
        "usage": usage,
        "node_filter": node_filter,
        "per_user_configs": per_user_configs,
        "device_history": device_history,
    })


def handle_internal_configs_list(handler):
    configs = handler.server.db.get_extra_configs()
    json_response(handler, 200, configs)


def handle_internal_configs_add(handler):
    from .admin import _validate_config_data

    try:
        data = json.loads(read_body(handler) or b"{}")
        validated = _validate_config_data(data)
    except (json.JSONDecodeError, ValueError) as exc:
        error_response(handler, 400, str(exc))
        return

    handler.server.db.add_extra_config(validated["name"], validated["uri"], validated["enabled"])
    json_response(handler, 201, {"ok": True})


def handle_internal_configs_delete(handler, config_id):
    try:
        numeric_id = int(config_id)
    except ValueError:
        error_response(handler, 400, "Invalid config id")
        return

    handler.server.db.delete_extra_config(numeric_id)
    json_response(handler, 200, {"ok": True})


def handle_internal_configs_reorder(handler):
    from .admin import _validate_configs_list

    try:
        configs = json.loads(read_body(handler) or b"[]")
        _validate_configs_list(configs)
    except (json.JSONDecodeError, ValueError) as exc:
        error_response(handler, 400, str(exc))
        return

    ordered_ids = [config["id"] for config in configs if "id" in config]
    db = handler.server.db
    db.reorder_extra_configs(ordered_ids)
    for config in configs:
        if "id" in config and "enabled" in config:
            db.toggle_extra_config(config["id"], config["enabled"])
    json_response(handler, 200, {"ok": True})


def handle_internal_per_user_list(handler):
    data = handler.server.db.get_per_user_configs_map()
    result = {}
    for username, configs in data.items():
        result[username] = [
            {"name": config["name"], "uri": config["uri"], "enabled": bool(config["enabled"])}
            for config in configs
        ]
    json_response(handler, 200, result)


def handle_internal_per_user_save(handler):
    from .admin import _validate_per_user_configs

    try:
        data = json.loads(read_body(handler) or b"{}")
        _validate_per_user_configs(data)
    except (json.JSONDecodeError, ValueError) as exc:
        error_response(handler, 400, str(exc))
        return

    handler.server.db.save_per_user_configs_map(data)
    json_response(handler, 200, {"ok": True})


def handle_internal_node_filters_list(handler):
    filters = handler.server.db.get_node_filters()
    json_response(handler, 200, filters)


def handle_internal_node_filters_save(handler):
    from .admin import _validate_node_filters

    try:
        data = json.loads(read_body(handler) or b"{}")
        _validate_node_filters(data)
    except (json.JSONDecodeError, ValueError) as exc:
        error_response(handler, 400, str(exc))
        return

    handler.server.db.save_node_filters(data)
    json_response(handler, 200, {"ok": True})


def handle_internal_settings_get(handler):
    interval = handler.server.db.get_setting("sub_update_interval")
    json_response(handler, 200, {
        "sub_update_interval": int(interval) if interval not in (None, "") else None,
    })


def handle_internal_settings_save(handler):
    try:
        data = json.loads(read_body(handler) or b"{}")
    except json.JSONDecodeError:
        error_response(handler, 400, "Invalid JSON body")
        return

    if "sub_update_interval" in data:
        value = data["sub_update_interval"]
        if value is None:
            handler.server.db.set_setting("sub_update_interval", "")
        else:
            try:
                numeric = int(value)
            except (TypeError, ValueError):
                error_response(handler, 400, "sub_update_interval must be an integer or null")
                return
            if numeric < 1 or numeric > 168:
                error_response(handler, 400, "sub_update_interval must be between 1 and 168")
                return
            handler.server.db.set_setting("sub_update_interval", str(numeric))

    json_response(handler, 200, {"ok": True})
