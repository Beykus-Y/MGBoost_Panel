import json
import re

from ..http_utils import json_response as _json_response
from ..http_utils import read_body as _read_body
from ..security import require_admin_auth


def _validate_config_data(data):
    """Validate config add/reorder data."""
    if not isinstance(data, dict):
        raise ValueError("Expected dict")
    uri = data.get("uri")
    if not isinstance(uri, str) or not uri.strip():
        raise ValueError("URI must be a non-empty string")
    name = data.get("name", uri[:30])
    if not isinstance(name, str) or len(name) > 100:
        raise ValueError("Name must be a string with length <= 100")
    enabled = data.get("enabled", True)
    if not isinstance(enabled, bool):
        # Accept string "true"/"false" or int 0/1
        if isinstance(enabled, str):
            if enabled.lower() not in ("true", "false"):
                raise ValueError("Enabled must be boolean or string 'true'/'false'")
            enabled = enabled.lower() == "true"
        elif isinstance(enabled, int):
            if enabled not in (0, 1):
                raise ValueError("Enabled must be 0 or 1")
            enabled = bool(enabled)
        else:
            raise ValueError("Enabled must be boolean")
    return {"uri": uri.strip(), "name": name.strip(), "enabled": enabled}


def _validate_configs_list(configs):
    """Validate list of configs for reorder."""
    if not isinstance(configs, list):
        raise ValueError("Expected list")
    for i, c in enumerate(configs):
        if not isinstance(c, dict):
            raise ValueError(f"Config at index {i} must be a dict")
        if "id" not in c:
            raise ValueError(f"Config at index {i} missing 'id' field")
        try:
            cid = int(c["id"])
        except (ValueError, TypeError):
            raise ValueError(f"Config at index {i} has invalid id: {c['id']}")
        if "enabled" in c:
            enabled = c["enabled"]
            if isinstance(enabled, str):
                if enabled.lower() not in ("true", "false"):
                    raise ValueError(f"Config at index {i} enabled must be boolean or string 'true'/'false'")
                c["enabled"] = enabled.lower() == "true"
            elif isinstance(enabled, int):
                if enabled not in (0, 1):
                    raise ValueError(f"Config at index {i} enabled must be 0 or 1")
                c["enabled"] = bool(enabled)
            elif not isinstance(enabled, bool):
                raise ValueError(f"Config at index {i} enabled must be boolean")


def _validate_node_filters(data):
    """Validate node filters data."""
    if not isinstance(data, dict):
        raise ValueError("Expected dict")
    for username, filt in data.items():
        if not isinstance(username, str) or len(username) > 128:
            raise ValueError(f"Invalid username: {username}")
        if not isinstance(filt, dict):
            raise ValueError(f"Filter for user {username} must be a dict")
        # Validate filter structure
        if "all" in filt and not isinstance(filt["all"], bool):
            raise ValueError(f"Filter 'all' for user {username} must be boolean")
        if "allowed_configs" in filt:
            allowed = filt["allowed_configs"]
            if not isinstance(allowed, list):
                raise ValueError(f"Filter 'allowed_configs' for user {username} must be a list")
            for item in allowed:
                if not isinstance(item, str):
                    raise ValueError(f"Each allowed config must be a string for user {username}")
        # Reject unknown keys that could be dangerous
        allowed_keys = {"all", "allowed_configs", "hosts", "allowed_ips"}
        for key in filt:
            if key not in allowed_keys:
                raise ValueError(f"Unknown key '{key}' in filter for user {username}")


def _validate_per_user_configs(data):
    """Validate per-user configs data."""
    if not isinstance(data, dict):
        raise ValueError("Expected dict")
    for username, configs in data.items():
        if not isinstance(username, str) or len(username) > 128:
            raise ValueError(f"Invalid username: {username}")
        if not isinstance(configs, list):
            raise ValueError(f"Configs for user {username} must be a list")
        for i, c in enumerate(configs):
            if not isinstance(c, dict):
                raise ValueError(f"Config at index {i} for user {username} must be a dict")
            name = c.get("name")
            uri = c.get("uri")
            enabled = c.get("enabled", True)
            if not isinstance(name, str) or not name.strip():
                raise ValueError(f"Config at index {i} for user {username} must have non-empty name")
            if not isinstance(uri, str) or not uri.strip():
                raise ValueError(f"Config at index {i} for user {username} must have non-empty URI")
            if not isinstance(enabled, bool):
                if isinstance(enabled, str):
                    if enabled.lower() not in ("true", "false"):
                        raise ValueError(f"Config at index {i} for user {username} enabled must be boolean or string 'true'/'false'")
                    enabled = enabled.lower() == "true"
                elif isinstance(enabled, int):
                    if enabled not in (0, 1):
                        raise ValueError(f"Config at index {i} for user {username} enabled must be 0 or 1")
                    enabled = bool(enabled)
                else:
                    raise ValueError(f"Config at index {i} for user {username} enabled must be boolean")


def handle_configs_list(handler):
    db = handler.server.db
    configs = db.get_extra_configs()
    _json_response(handler, 200, configs)


def handle_configs_add(handler):
    if not require_admin_auth(handler):
        return
    db = handler.server.db
    try:
        data = json.loads(_read_body(handler))
        validated = _validate_config_data(data)
    except (json.JSONDecodeError, ValueError) as e:
        _json_response(handler, 400, {"error": str(e)})
        return
    db.add_extra_config(validated["name"], validated["uri"], validated["enabled"])
    _json_response(handler, 201, {"ok": True})


def handle_configs_delete(handler, config_id):
    try:
        config_id = int(config_id)
    except ValueError:
        handler.send_response(400)
        handler.end_headers()
        return
    handler.server.db.delete_extra_config(config_id)
    _json_response(handler, 200, {"ok": True})


def handle_configs_reorder(handler):
    db = handler.server.db
    try:
        configs = json.loads(_read_body(handler))
        _validate_configs_list(configs)
    except (json.JSONDecodeError, ValueError):
        _json_response(handler, 400, {"error": "Invalid configs payload"})
        return
    # configs is a list of full config objects with 'id' field
    ordered_ids = [c["id"] for c in configs if "id" in c]
    db.reorder_extra_configs(ordered_ids)
    # also sync enabled state while we're at it
    for c in configs:
        if "id" in c and "enabled" in c:
            db.toggle_extra_config(c["id"], c["enabled"])
    _json_response(handler, 200, {"ok": True})


def handle_stats_get(handler):
    stats = handler.server.db.get_hysteria_stats()
    _json_response(handler, 200, stats)


def handle_stats_update(handler):
    db = handler.server.db
    try:
        data = json.loads(_read_body(handler))
        token = data["token"]
        upload = data.get("upload", 0)
        download = data.get("download", 0)
    except Exception:
        handler.send_response(400)
        handler.end_headers()
        return
    db.update_hysteria_stats(token, upload, download)
    _json_response(handler, 200, {"ok": True})


def handle_per_user_list(handler):
    data = handler.server.db.get_per_user_configs_map()
    # Convert to legacy format: {username: [{name, uri, enabled}]}
    result = {}
    for username, configs in data.items():
        result[username] = [{"name": c["name"], "uri": c["uri"], "enabled": bool(c["enabled"])} for c in configs]
    _json_response(handler, 200, result)


def handle_per_user_save(handler):
    try:
        data = json.loads(_read_body(handler))
        _validate_per_user_configs(data)
    except (json.JSONDecodeError, ValueError) as e:
        _json_response(handler, 400, {"error": str(e)})
        return
    handler.server.db.save_per_user_configs_map(data)
    _json_response(handler, 200, {"ok": True})


def handle_node_filters_list(handler):
    filters = handler.server.db.get_node_filters()
    _json_response(handler, 200, filters)


def handle_node_filters_save(handler):
    try:
        data = json.loads(_read_body(handler))
        _validate_node_filters(data)
    except (json.JSONDecodeError, ValueError) as e:
        _json_response(handler, 400, {"error": str(e)})
        return
    handler.server.db.save_node_filters(data)
    _json_response(handler, 200, {"ok": True})


def handle_settings_get(handler):
    db = handler.server.db
    interval = db.get_setting("sub_update_interval")
    contact = db.get_setting("block_contact") or ""
    _json_response(handler, 200, {
        "sub_update_interval": int(interval) if interval not in (None, "") else None,
        "block_contact": contact,
    })


def handle_settings_save(handler):
    db = handler.server.db
    try:
        data = json.loads(_read_body(handler))
    except json.JSONDecodeError:
        _json_response(handler, 400, {"error": "Invalid JSON"})
        return
    if "sub_update_interval" in data:
        val = data["sub_update_interval"]
        if val is None:
            db.set_setting("sub_update_interval", "")
        else:
            try:
                numeric = int(val)
            except (TypeError, ValueError):
                _json_response(handler, 400, {"error": "sub_update_interval must be an integer or null"})
                return
            if numeric < 1 or numeric > 168:
                _json_response(handler, 400, {"error": "sub_update_interval must be between 1 and 168"})
                return
            db.set_setting("sub_update_interval", str(numeric))
    if "block_contact" in data:
        val = str(data["block_contact"] or "").strip()
        if len(val) > 128:
            _json_response(handler, 400, {"error": "block_contact max 128 chars"})
            return
        db.set_setting("block_contact", val)
    _json_response(handler, 200, {"ok": True})


# --- device management (admin) ---

def handle_admin_user_devices(handler, username):
    db = handler.server.db
    devices = db.get_user_devices(username)
    limit = db.get_device_limit(username)
    active_count = sum(1 for d in devices if d["is_active"])
    _json_response(handler, 200, {"devices": devices, "limit": limit, "active_count": active_count})


def handle_admin_set_device_limit(handler, username):
    try:
        data = json.loads(_read_body(handler))
        limit = int(data["limit"])
        if limit < 0 or limit > 20:
            raise ValueError("limit must be between 0 and 20 (0 = unlimited)")
    except (json.JSONDecodeError, KeyError, ValueError, TypeError) as e:
        _json_response(handler, 400, {"error": str(e)})
        return
    handler.server.db.set_device_limit(username, limit)
    _json_response(handler, 200, {"ok": True})


def handle_admin_remove_device(handler, device_id):
    try:
        did = int(device_id)
    except (ValueError, TypeError):
        _json_response(handler, 400, {"error": "Invalid device id"})
        return
    ok = handler.server.db.admin_remove_device(did)
    if not ok:
        _json_response(handler, 404, {"error": "Device not found"})
        return
    _json_response(handler, 200, {"ok": True})
