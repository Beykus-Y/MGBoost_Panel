import json
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

from ..http_utils import error_response, json_response, read_body
from ..marzban import MarzbanClient

_client = MarzbanClient()


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

    items = []
    for user in users:
        username = user.get("username")
        filt = node_filters.get(username)
        items.append({
            **user,
            "proxy_filtered": bool(filt and filt.get("all") is False and filt.get("allowed_configs")),
            "proxy_extra_configs": len(per_user_configs_map.get(username, [])),
        })

    total = payload.get("total") if isinstance(payload, dict) else None
    json_response(handler, 200, {
        "items": items,
        "total": total if isinstance(total, int) else len(items),
        "limit": limit,
        "offset": offset,
    })


def handle_internal_user_detail(handler, username):
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
