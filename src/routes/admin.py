import json


def _read_body(handler):
    length = int(handler.headers.get("Content-Length", 0))
    return handler.rfile.read(length)


def _json_response(handler, status, data):
    body = json.dumps(data, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def handle_configs_list(handler):
    db = handler.server.db
    configs = db.get_extra_configs()
    _json_response(handler, 200, configs)


def handle_configs_add(handler):
    db = handler.server.db
    try:
        data = json.loads(_read_body(handler))
        uri = data["uri"]
        name = data.get("name", uri[:30])
        enabled = data.get("enabled", True)
    except Exception:
        handler.send_response(400)
        handler.end_headers()
        return
    db.add_extra_config(name, uri, enabled)
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
    except Exception:
        handler.send_response(400)
        handler.end_headers()
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
    except Exception:
        handler.send_response(400)
        handler.end_headers()
        return
    handler.server.db.save_per_user_configs_map(data)
    _json_response(handler, 200, {"ok": True})


def handle_node_filters_list(handler):
    filters = handler.server.db.get_node_filters()
    _json_response(handler, 200, filters)


def handle_node_filters_save(handler):
    try:
        data = json.loads(_read_body(handler))
    except Exception:
        handler.send_response(400)
        handler.end_headers()
        return
    handler.server.db.save_node_filters(data)
    _json_response(handler, 200, {"ok": True})


def handle_settings_get(handler):
    db = handler.server.db
    interval = db.get_setting("sub_update_interval")
    _json_response(handler, 200, {
        "sub_update_interval": int(interval) if interval is not None else None,
    })


def handle_settings_save(handler):
    db = handler.server.db
    try:
        data = json.loads(_read_body(handler))
    except Exception:
        handler.send_response(400)
        handler.end_headers()
        return
    if "sub_update_interval" in data:
        val = data["sub_update_interval"]
        if val is None:
            db.set_setting("sub_update_interval", "")
        else:
            db.set_setting("sub_update_interval", str(int(val)))
    _json_response(handler, 200, {"ok": True})
