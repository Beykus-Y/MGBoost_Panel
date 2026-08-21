import asyncio
import json
import re
import time

from ..http_utils import json_response as _json_response
from ..http_utils import read_body as _read_body
from ..security import require_admin_auth


REFUND_RESULT_TIMEOUT_SECONDS = 15
REFUND_RECONCILE_TIMEOUT_SECONDS = 30


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


def _optional_number(data, key):
    value = data.get(key)
    if value in (None, ""):
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{key} must be a number or null")
    if numeric < 0:
        raise ValueError(f"{key} must be >= 0")
    return numeric


def _validate_node_setting(data):
    """Validate local node metadata used for economics and AI analysis."""
    if not isinstance(data, dict):
        raise ValueError("Expected dict")

    node_id = data.get("node_id")
    if node_id not in (None, ""):
        try:
            node_id = int(node_id)
        except (TypeError, ValueError):
            raise ValueError("node_id must be an integer or null")

    importance = str(data.get("importance") or "normal").strip()
    if importance not in {"normal", "core", "backup", "test", "deprecated"}:
        raise ValueError("importance must be normal, core, backup, test or deprecated")

    can_remove = data.get("can_remove", True)
    if not isinstance(can_remove, bool):
        if isinstance(can_remove, int) and can_remove in (0, 1):
            can_remove = bool(can_remove)
        else:
            raise ValueError("can_remove must be boolean")

    currency = str(data.get("currency") or "USD").strip().upper()
    if not currency or len(currency) > 8:
        raise ValueError("currency must be 1-8 characters")

    def short_text(key, limit):
        value = str(data.get(key) or "").strip()
        if len(value) > limit:
            raise ValueError(f"{key} max {limit} chars")
        return value

    return {
        "node_id": node_id,
        "node_name": short_text("node_name", 128),
        "node_address": short_text("node_address", 256),
        "billing_group": short_text("billing_group", 128),
        "provider": short_text("provider", 64),
        "location": short_text("location", 64),
        "monthly_cost": _optional_number(data, "monthly_cost"),
        "currency": currency,
        "traffic_included_gb": _optional_number(data, "traffic_included_gb"),
        "traffic_price_per_tb": _optional_number(data, "traffic_price_per_tb"),
        "importance": importance,
        "can_remove": can_remove,
        "note": short_text("note", 512),
    }


def _coerce_bool(value, field_name):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        if value.lower() not in ("true", "false"):
            raise ValueError(f"{field_name} must be boolean or string 'true'/'false'")
        return value.lower() == "true"
    if isinstance(value, int):
        if value not in (0, 1):
            raise ValueError(f"{field_name} must be 0 or 1")
        return bool(value)
    raise ValueError(f"{field_name} must be boolean")


def _validate_stars_tariff(data):
    """Mirrors _validate_node_setting's style. Stars are always whole
    numbers (Bot API LabeledPrice.amount is an integer), so both
    duration_days and stars_price must be positive ints."""
    if not isinstance(data, dict):
        raise ValueError("Expected dict")

    name = str(data.get("name") or "").strip()
    if not name or len(name) > 64:
        raise ValueError("name must be 1-64 chars")

    duration_days = data.get("duration_days")
    if isinstance(duration_days, bool) or not isinstance(duration_days, int):
        raise ValueError("duration_days must be an integer")
    if not 1 <= duration_days <= 3650:
        raise ValueError("duration_days must be between 1 and 3650")

    stars_price = data.get("stars_price")
    if isinstance(stars_price, bool) or not isinstance(stars_price, int):
        raise ValueError("stars_price must be an integer")
    if not 1 <= stars_price <= 1_000_000:
        raise ValueError("stars_price must be between 1 and 1000000")

    active = _coerce_bool(data.get("active", True), "active")

    sort_order = data.get("sort_order")
    if sort_order not in (None, ""):
        if isinstance(sort_order, bool) or not isinstance(sort_order, int):
            raise ValueError("sort_order must be an integer")
    else:
        sort_order = None

    result = {"name": name, "duration_days": duration_days, "stars_price": stars_price, "active": active}
    if sort_order is not None:
        result["sort_order"] = sort_order
    if data.get("id") not in (None, ""):
        if isinstance(data["id"], bool) or not isinstance(data["id"], int):
            raise ValueError("id must be an integer")
        result["id"] = data["id"]
    return result


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
    custom_title = db.get_setting("sub_custom_title") or ""
    custom_desc = db.get_setting("sub_custom_desc") or ""
    inbound_client_extras_raw = db.get_setting("inbound_client_extras") or "{}"
    try:
        inbound_client_extras = json.loads(inbound_client_extras_raw)
    except json.JSONDecodeError:
        inbound_client_extras = {}

    _json_response(handler, 200, {
        "sub_update_interval": int(interval) if interval not in (None, "") else None,
        "block_contact": contact,
        "sub_custom_title": custom_title,
        "sub_custom_desc": custom_desc,
        "inbound_client_extras": inbound_client_extras,
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
    if "sub_custom_title" in data:
        val = str(data["sub_custom_title"] or "").strip()
        if len(val) > 256:
            _json_response(handler, 400, {"error": "sub_custom_title max 256 chars"})
            return
        db.set_setting("sub_custom_title", val)
    if "sub_custom_desc" in data:
        val = str(data["sub_custom_desc"] or "").strip()
        if len(val) > 1024:
            _json_response(handler, 400, {"error": "sub_custom_desc max 1024 chars"})
            return
        db.set_setting("sub_custom_desc", val)
    if "inbound_client_extras" in data:
        val = data["inbound_client_extras"]
        if not isinstance(val, dict):
            _json_response(handler, 400, {"error": "inbound_client_extras must be an object"})
            return
        # clean and validate
        cleaned = {}
        for k, v in val.items():
            if not isinstance(k, str) or not isinstance(v, str):
                continue
            k_clean, v_clean = k.strip(), v.strip()
            if k_clean:
                cleaned[k_clean] = v_clean
        db.set_setting("inbound_client_extras", json.dumps(cleaned))
    _json_response(handler, 200, {"ok": True})


def handle_node_settings_get(handler):
    db = handler.server.db
    settings = db.get_node_settings()
    for node_key, s in settings.items():
        node_id = s.get("node_id")
        s["monitor_quiet_hours"] = db.get_node_quiet_hours(node_id)
    _json_response(handler, 200, settings)


def handle_node_settings_save(handler):
    try:
        data = json.loads(_read_body(handler))
        validated = _validate_node_setting(data)
    except (json.JSONDecodeError, ValueError) as e:
        _json_response(handler, 400, {"error": str(e)})
        return
    db = handler.server.db
    result = db.save_node_setting(validated)
    quiet_hours = data.get("monitor_quiet_hours")
    if isinstance(quiet_hours, list):
        db.set_node_quiet_hours(data.get("node_id"), quiet_hours)
        result["monitor_quiet_hours"] = quiet_hours
    else:
        result["monitor_quiet_hours"] = db.get_node_quiet_hours(data.get("node_id"))
    _json_response(handler, 200, result)


def handle_bot_restart(handler):
    server = handler.server
    old = getattr(server, "bot_runner", None)
    if old and old.is_alive():
        old.stop()
        old.join(timeout=15)
        if old.is_alive():
            _json_response(handler, 503, {
                "error": "Previous bot runner did not stop; a second runner was not started"
            })
            return
    factory = getattr(server, "bot_runner_factory", None)
    new_runner = factory() if factory else None
    if new_runner:
        new_runner.start()
        server.bot_runner = new_runner
        _json_response(handler, 200, {"ok": True, "started": True})
    else:
        server.bot_runner = None
        _json_response(handler, 200, {"ok": True, "started": False})


def handle_bot_settings_get(handler):
    db = handler.server.db
    token = db.get_setting("bot:token") or ""
    proxy_pass = db.get_setting("bot:proxy_pass") or ""
    openrouter_api_key = db.get_setting("bot:openrouter_api_key") or ""
    _json_response(handler, 200, {
        "enabled": db.get_setting("bot:enabled") == "1",
        # Secrets: never returned in any form (not even a masked fragment) —
        # only a boolean presence flag. The frontend shows "настроено" when
        # *_set is true and leaves the field blank; typing a new value
        # replaces it, leaving it blank on save keeps the existing secret.
        "token_set": bool(token),
        "channel_id": db.get_setting("bot:channel_id") or "@MGBoost_News",
        "proxy_enabled": db.get_setting("bot:proxy_enabled") == "1",
        "proxy_host": db.get_setting("bot:proxy_host") or "",
        "proxy_port": int(db.get_setting("bot:proxy_port") or 1080),
        "proxy_user": db.get_setting("bot:proxy_user") or "socks",
        "proxy_pass_set": bool(proxy_pass),
        "support_enabled": db.get_setting("bot:support_enabled", "1") == "1",
        "openrouter_api_key_set": bool(openrouter_api_key),
        "openrouter_model": db.get_setting("bot:openrouter_model") or "openai/gpt-4o-mini",
        "admin_tg_id": db.get_setting("bot:admin_tg_id") or "",
        "support_faq": db.get_setting("bot:support_faq") or "",
    })


def handle_bot_settings_save(handler):
    try:
        data = json.loads(_read_body(handler))
    except (json.JSONDecodeError, ValueError) as e:
        _json_response(handler, 400, {"error": str(e)})
        return
    db = handler.server.db
    if "enabled" in data:
        db.set_setting("bot:enabled", "1" if data.get("enabled") else "0")
    if "token" in data:
        db.set_setting("bot:token", str(data.get("token") or "").strip())
    if "channel_id" in data:
        db.set_setting("bot:channel_id", str(data.get("channel_id") or "@MGBoost_News").strip())
    if "proxy_enabled" in data:
        db.set_setting("bot:proxy_enabled", "1" if data.get("proxy_enabled") else "0")
    if "proxy_host" in data:
        db.set_setting("bot:proxy_host", str(data.get("proxy_host") or "").strip())
    if "proxy_port" in data:
        db.set_setting("bot:proxy_port", str(int(data.get("proxy_port") or 1080)))
    if "proxy_user" in data:
        db.set_setting("bot:proxy_user", str(data.get("proxy_user") or "socks").strip())
    if "proxy_pass" in data:
        db.set_setting("bot:proxy_pass", str(data.get("proxy_pass") or "").strip())
    if "support_enabled" in data:
        db.set_setting("bot:support_enabled", "1" if data.get("support_enabled") else "0")
    if "openrouter_api_key" in data:
        db.set_setting("bot:openrouter_api_key", str(data.get("openrouter_api_key") or "").strip())
    if "openrouter_model" in data:
        db.set_setting("bot:openrouter_model", str(data.get("openrouter_model") or "openai/gpt-4o-mini").strip())
    if "admin_tg_id" in data:
        db.set_setting("bot:admin_tg_id", str(data.get("admin_tg_id") or "").strip())
    if "support_faq" in data:
        db.set_setting("bot:support_faq", str(data.get("support_faq") or "").strip())
    _json_response(handler, 200, {"ok": True})


# --- device management (admin) ---

def handle_admin_user_devices(handler, username):
    db = handler.server.db
    devices = db.get_user_devices(username)
    limit = db.get_device_limit(username)
    active_count = sum(1 for d in devices if d["is_active"])
    _json_response(handler, 200, {"devices": devices, "limit": limit, "active_count": active_count})


def handle_admin_user_device_counts(handler):
    try:
        data = json.loads(_read_body(handler))
        usernames = data.get("usernames", [])
        if not isinstance(usernames, list):
            raise ValueError("usernames must be a list")
    except (json.JSONDecodeError, ValueError) as e:
        _json_response(handler, 400, {"error": str(e)})
        return
    counts = handler.server.db.get_active_device_counts(usernames[:1000])
    _json_response(handler, 200, counts)


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


# --- ticket management ---

def _send_ticket_notification(handler, telegram_id: int, text: str):
    runner = getattr(handler.server, "bot_runner", None)
    if not runner:
        return
    bot = getattr(runner, "bot_instance", None)
    if not bot:
        return
    loop = getattr(runner, "_loop", None)
    if not loop or not loop.is_running():
        return
    from ..bot_support import send_operator_reply, notify_ticket_closed
    import asyncio
    asyncio.run_coroutine_threadsafe(
        send_operator_reply(bot, telegram_id, text) if text else
        notify_ticket_closed(bot, telegram_id),
        loop,
    )


def handle_tickets_list(handler, status: str | None = None):
    db = handler.server.db
    tickets = db.list_tickets(status=status or None)
    _json_response(handler, 200, tickets)


def handle_ticket_detail(handler, ticket_id):
    try:
        tid = int(ticket_id)
    except (ValueError, TypeError):
        _json_response(handler, 400, {"error": "Invalid ticket id"})
        return
    db = handler.server.db
    ticket = db.get_ticket(tid)
    if not ticket:
        _json_response(handler, 404, {"error": "Ticket not found"})
        return
    messages = db.get_ticket_messages(tid, limit=100)
    _json_response(handler, 200, {"ticket": ticket, "messages": messages})


def handle_ticket_close(handler, ticket_id):
    try:
        tid = int(ticket_id)
    except (ValueError, TypeError):
        _json_response(handler, 400, {"error": "Invalid ticket id"})
        return
    db = handler.server.db
    ticket = db.get_ticket(tid)
    if not ticket:
        _json_response(handler, 404, {"error": "Ticket not found"})
        return
    db.update_ticket_status(tid, "closed")
    _send_ticket_notification(handler, ticket["telegram_id"], "")
    _json_response(handler, 200, {"ok": True})


def handle_ticket_reply(handler, ticket_id):
    try:
        tid = int(ticket_id)
        data = json.loads(_read_body(handler))
        text = data.get("text", "").strip()
        if not text:
            raise ValueError("text is required")
    except (json.JSONDecodeError, ValueError, TypeError) as e:
        _json_response(handler, 400, {"error": str(e)})
        return
    db = handler.server.db
    ticket = db.get_ticket(tid)
    if not ticket:
        _json_response(handler, 404, {"error": "Ticket not found"})
        return
    db.add_ticket_message(tid, "human", text)
    _send_ticket_notification(handler, ticket["telegram_id"], text)
    _json_response(handler, 200, {"ok": True})


# --- Telegram Stars: tariffs (settings, admin-managed, no seeded defaults) ---

def handle_stars_tariffs_list(handler):
    db = handler.server.db
    _json_response(handler, 200, db.get_stars_tariffs())


def handle_stars_tariffs_save(handler):
    db = handler.server.db
    try:
        data = json.loads(_read_body(handler))
        validated = _validate_stars_tariff(data)
    except (json.JSONDecodeError, ValueError) as e:
        _json_response(handler, 400, {"error": str(e)})
        return
    if "id" in validated and db.get_stars_tariff(validated["id"]) is None:
        _json_response(handler, 404, {"error": "Tariff not found"})
        return
    result = db.save_stars_tariff(validated)
    _json_response(handler, 200, result)


def handle_stars_tariffs_toggle(handler, tariff_id):
    try:
        tariff_id = int(tariff_id)
        data = json.loads(_read_body(handler))
        if not isinstance(data, dict):
            raise ValueError("Expected dict")
        active = _coerce_bool(data.get("active"), "active")
    except (json.JSONDecodeError, ValueError, TypeError) as e:
        _json_response(handler, 400, {"error": str(e) or "Invalid payload"})
        return
    handler.server.db.toggle_stars_tariff(tariff_id, active)
    _json_response(handler, 200, {"ok": True})


def handle_stars_tariffs_delete(handler, tariff_id):
    try:
        tariff_id = int(tariff_id)
    except (ValueError, TypeError):
        _json_response(handler, 400, {"error": "Invalid tariff id"})
        return
    handler.server.db.delete_stars_tariff(tariff_id)
    _json_response(handler, 200, {"ok": True})


def handle_stars_tariffs_reorder(handler):
    db = handler.server.db
    try:
        items = json.loads(_read_body(handler))
        if not isinstance(items, list):
            raise ValueError("Expected list")
        ordered_ids = []
        for item in items:
            if isinstance(item, dict) and "id" in item:
                ordered_ids.append(int(item["id"]))
            else:
                ordered_ids.append(int(item))
    except (json.JSONDecodeError, ValueError, TypeError) as e:
        _json_response(handler, 400, {"error": str(e) or "Invalid payload"})
        return
    db.reorder_stars_tariffs(ordered_ids)
    _json_response(handler, 200, {"ok": True})


def handle_stars_settings_get(handler):
    db = handler.server.db
    _json_response(handler, 200, {"enabled": db.get_setting("stars:enabled") == "1"})


def handle_stars_settings_save(handler):
    db = handler.server.db
    try:
        data = json.loads(_read_body(handler))
    except json.JSONDecodeError as e:
        _json_response(handler, 400, {"error": str(e)})
        return
    if not isinstance(data, dict) or "enabled" not in data:
        _json_response(handler, 400, {"error": "enabled is required"})
        return
    try:
        enabled = _coerce_bool(data["enabled"], "enabled")
    except ValueError as e:
        _json_response(handler, 400, {"error": str(e)})
        return
    db.set_setting("stars:enabled", "1" if enabled else "0")
    _json_response(handler, 200, {"ok": True})


# --- Telegram Stars: payments ledger (read-only + operator actions, §4.4/§9) ---

def handle_stars_payments_list(handler):
    from urllib.parse import parse_qs, urlsplit
    db = handler.server.db
    query = parse_qs(urlsplit(handler.path).query)
    status = (query.get("status", [None])[0]) or None
    _json_response(handler, 200, db.list_stars_invoices(status=status))


def handle_stars_orphan_payments_list(handler):
    from urllib.parse import parse_qs, urlsplit
    db = handler.server.db
    query = parse_qs(urlsplit(handler.path).query)
    status = (query.get("status", [None])[0]) or None
    _json_response(handler, 200, db.list_stars_orphan_payments(status=status))


def _get_stars_admin_token(handler):
    from ..marzban import MarzbanClient
    client = MarzbanClient()
    try:
        return client.get_admin_token_from_env(), client
    except Exception:
        return None, client


def handle_stars_payment_recheck(handler, payment_id):
    try:
        invoice_id = int(payment_id)
    except (ValueError, TypeError):
        _json_response(handler, 400, {"error": "Invalid payment id"})
        return
    db = handler.server.db
    row = db.get_invoice(invoice_id)
    if not row:
        _json_response(handler, 404, {"error": "Payment not found"})
        return
    admin_token, client = _get_stars_admin_token(handler)
    if not admin_token:
        _json_response(handler, 503, {"error": "Marzban admin credentials are not configured"})
        return
    try:
        user = client.get_user(row["marzban_username"], admin_token)
    except Exception as e:
        _json_response(handler, 502, {"error": f"Could not load Marzban user: {e}"})
        return
    _json_response(handler, 200, {"invoice": row, "live_expire": user.get("expire"), "live_status": user.get("status")})


def handle_stars_payment_confirm_applied(handler, payment_id):
    try:
        invoice_id = int(payment_id)
    except (ValueError, TypeError):
        _json_response(handler, 400, {"error": "Invalid payment id"})
        return
    db = handler.server.db
    row = db.get_invoice(invoice_id)
    if not row:
        _json_response(handler, 404, {"error": "Payment not found"})
        return
    if row["status"] not in ("manual_review", "apply_retry_exhausted"):
        _json_response(handler, 409, {"error": f"Cannot confirm-applied from status {row['status']}"})
        return
    admin_token, client = _get_stars_admin_token(handler)
    if not admin_token:
        _json_response(handler, 503, {"error": "Marzban admin credentials are not configured"})
        return
    try:
        user = client.get_user(row["marzban_username"], admin_token)
    except Exception as e:
        _json_response(handler, 502, {"error": f"Could not load Marzban user: {e}"})
        return
    live_expire = int(user.get("expire") or 0)
    ok = db.resolve_manual_review_confirm_applied(invoice_id, applied_expire=live_expire)
    if not ok:
        _json_response(handler, 409, {"error": "Payment status changed concurrently, no action taken"})
        return
    db.log_audit_event(
        "subscription_extended", marzban_username=row["marzban_username"],
        telegram_id=row["payer_telegram_id"],
        metadata={"invoice_id": invoice_id, "new_expire": live_expire, "via": "admin_confirm_applied"},
    )
    _json_response(handler, 200, {"ok": True, "applied_expire": live_expire})


def handle_stars_payment_requeue(handler, payment_id):
    try:
        invoice_id = int(payment_id)
    except (ValueError, TypeError):
        _json_response(handler, 400, {"error": "Invalid payment id"})
        return
    db = handler.server.db
    row = db.get_invoice(invoice_id)
    if not row:
        _json_response(handler, 404, {"error": "Payment not found"})
        return
    ok = db.resolve_manual_review_requeue(invoice_id)
    if not ok:
        _json_response(handler, 409, {"error": f"Cannot requeue from status {row['status']}"})
        return
    # No fast-path trigger available here (the apply-worker's asyncio.Event
    # lives inside the bot thread, not reachable from the server object) —
    # the worker's own 30s poll will pick this row up; requeue is not a
    # latency-sensitive admin action. The message field tells the operator
    # this explicitly rather than leaving the ~30s delay unexplained.
    _json_response(handler, 200, {
        "ok": True,
        "message": "Платёж поставлен в очередь и будет повторно обработан автоматически в течение ~30 секунд.",
    })


def _running_stars_bot(handler):
    runner = getattr(handler.server, "bot_runner", None)
    bot = getattr(runner, "bot_instance", None) if runner else None
    loop = getattr(runner, "_loop", None) if runner else None
    if not bot or not loop or not loop.is_running():
        return None, None
    return bot, loop


def _issue_stars_refund(handler, row, payment_id: int, *, orphan: bool = False):
    db = handler.server.db
    bot, loop = _running_stars_bot(handler)
    if bot is None:
        _json_response(handler, 503, {"error": "Bot is not running — cannot issue a Stars refund"})
        return

    begin = db.begin_orphan_refund if orphan else db.begin_invoice_refund
    unknown = db.mark_orphan_refund_unknown if orphan else db.mark_invoice_refund_unknown
    complete = db.mark_orphan_refunded if orphan else db.mark_invoice_refunded

    # This CAS is committed before the first Telegram API call. Concurrent
    # and subsequent blind refund requests therefore cannot call Telegram.
    if not begin(payment_id):
        _json_response(handler, 409, {"error": "Refund is already pending, unknown, or completed"})
        return

    future = None
    refund_coro = bot.refund_star_payment(
        int(row["payer_telegram_id"]), row["telegram_payment_charge_id"]
    )
    try:
        future = asyncio.run_coroutine_threadsafe(refund_coro, loop)
        refunded = future.result(timeout=REFUND_RESULT_TIMEOUT_SECONDS)
    except Exception as e:
        # A timeout or exception does not prove the remote method failed.
        # Keep it durably non-repeatable until an operator reconciliation.
        if future is not None:
            future.cancel()
        else:
            refund_coro.close()
        unknown(payment_id, f"{type(e).__name__}: {e}")
        _json_response(handler, 202, {
            "ok": False,
            "status": "refund_unknown",
            "message": "Telegram refund outcome is unknown; blind retry is blocked. Use reconciliation.",
        })
        return
    if refunded is not True:
        # Bot API documents only True as confirmed success. Any other result
        # is ambiguous and must never make the payment blindly refundable.
        unknown(payment_id, "refundStarPayment returned a non-True result")
        _json_response(handler, 202, {
            "ok": False,
            "status": "refund_unknown",
            "message": "Telegram refund was not positively confirmed; blind retry is blocked.",
        })
        return

    ok = complete(payment_id)
    if not ok:
        _json_response(handler, 500, {
            "error": "Refund confirmed by Telegram but local finalization failed; reconcile before any action"
        })
        return
    _json_response(handler, 200, {"ok": True})


def handle_stars_payment_refund(handler, payment_id):
    try:
        invoice_id = int(payment_id)
    except (ValueError, TypeError):
        _json_response(handler, 400, {"error": "Invalid payment id"})
        return
    db = handler.server.db
    row = db.get_invoice(invoice_id)
    if not row:
        _json_response(handler, 404, {"error": "Payment not found"})
        return
    if row["status"] not in ("applied", "manual_review", "apply_retry_exhausted", "apply_failed_user_missing"):
        _json_response(handler, 409, {"error": f"Cannot refund from status {row['status']}"})
        return
    if not row.get("payer_telegram_id") or not row.get("telegram_payment_charge_id"):
        _json_response(handler, 409, {"error": "Invoice has no recorded payer/charge id — nothing to refund"})
        return
    _issue_stars_refund(handler, row, invoice_id)


def handle_stars_orphan_payment_refund(handler, payment_id):
    try:
        orphan_id = int(payment_id)
    except (ValueError, TypeError):
        _json_response(handler, 400, {"error": "Invalid orphan payment id"})
        return
    db = handler.server.db
    row = db.get_stars_orphan_payment(orphan_id)
    if not row:
        _json_response(handler, 404, {"error": "Orphan payment not found"})
        return
    if row["status"] != "manual_review":
        _json_response(handler, 409, {"error": f"Cannot refund from status {row['status']}"})
        return
    _issue_stars_refund(handler, row, orphan_id, orphan=True)


async def _find_confirmed_star_refund(bot, charge_id: str, payer_telegram_id: int):
    """Return True only for positive proof; None means inconclusive.

    Bot API identifies the refund with the original charge id and exposes
    it as an outgoing transaction (receiver is populated). We scan a bounded
    history; absence is never treated as proof that a refund did not happen.
    """
    for offset in range(0, 1000, 100):
        result = await bot.get_star_transactions(offset=offset, limit=100)
        transactions = list(getattr(result, "transactions", []) or [])
        for transaction in transactions:
            if getattr(transaction, "id", None) != charge_id:
                continue
            receiver = getattr(transaction, "receiver", None)
            receiver_user = getattr(receiver, "user", None) if receiver is not None else None
            receiver_user_id = getattr(receiver_user, "id", None)
            if receiver_user_id == int(payer_telegram_id):
                return True
        if len(transactions) < 100:
            return None
    return None


def _reconcile_stars_refund(handler, row, payment_id: int, *, orphan: bool = False):
    if row["status"] not in ("refund_pending", "refund_unknown"):
        _json_response(handler, 409, {"error": f"Cannot reconcile from status {row['status']}"})
        return
    bot, loop = _running_stars_bot(handler)
    if bot is None:
        _json_response(handler, 503, {"error": "Bot is not running — cannot reconcile Stars transactions"})
        return
    future = asyncio.run_coroutine_threadsafe(
        _find_confirmed_star_refund(
            bot,
            row["telegram_payment_charge_id"],
            int(row["payer_telegram_id"]),
        ),
        loop,
    )
    try:
        confirmed = future.result(timeout=REFUND_RECONCILE_TIMEOUT_SECONDS)
    except Exception as e:
        future.cancel()
        _json_response(handler, 502, {"error": f"Could not reconcile Telegram transactions: {e}"})
        return
    if not confirmed:
        _json_response(handler, 200, {
            "ok": False,
            "status": row["status"],
            "message": "Refund was not proven in the scanned Telegram history; state is unchanged and blind retry remains blocked.",
        })
        return
    complete = (handler.server.db.mark_orphan_refunded
                if orphan else handler.server.db.mark_invoice_refunded)
    if not complete(payment_id, reconciled=True):
        _json_response(handler, 409, {"error": "Payment state changed concurrently"})
        return
    _json_response(handler, 200, {"ok": True, "status": "refunded", "reconciled": True})


def handle_stars_payment_reconcile_refund(handler, payment_id):
    try:
        invoice_id = int(payment_id)
    except (ValueError, TypeError):
        _json_response(handler, 400, {"error": "Invalid payment id"})
        return
    row = handler.server.db.get_invoice(invoice_id)
    if not row:
        _json_response(handler, 404, {"error": "Payment not found"})
        return
    _reconcile_stars_refund(handler, row, invoice_id)


def handle_stars_orphan_payment_reconcile_refund(handler, payment_id):
    try:
        orphan_id = int(payment_id)
    except (ValueError, TypeError):
        _json_response(handler, 400, {"error": "Invalid orphan payment id"})
        return
    row = handler.server.db.get_stars_orphan_payment(orphan_id)
    if not row:
        _json_response(handler, 404, {"error": "Orphan payment not found"})
        return
    _reconcile_stars_refund(handler, row, orphan_id, orphan=True)
