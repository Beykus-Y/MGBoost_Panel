import json
import os
import time
from collections import defaultdict

from ..http_utils import read_body as _read_body
from ..marzban import MarzbanClient

_client = MarzbanClient()

# Simple in-memory rate limiter: {ip: [timestamps]}
_rate_limit: dict = defaultdict(list)
_RATE_LIMIT_WINDOW = 60
_RATE_LIMIT_MAX = 30
_last_cleanup = time.time()
_ADMIN_TOKEN_TTL = 3600
_admin_token_cache: list = [None, 0.0]


def _get_admin_token(user: str, password: str):
    if not password:
        return None
    now = time.time()
    if _admin_token_cache[0] and _admin_token_cache[1] > now:
        return _admin_token_cache[0]
    try:
        tok = _client.get_token(user, password)
        _admin_token_cache[0] = tok
        _admin_token_cache[1] = now + _ADMIN_TOKEN_TTL
        return tok
    except Exception:
        print("[LK] admin token fetch failed")
        return None


def _get_real_ip(handler) -> str:
    """Get real IP considering proxy headers."""
    forwarded = handler.headers.get("X-Real-IP")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return handler.client_address[0]


def _check_rate_limit(ip: str) -> bool:
    global _last_cleanup
    now = time.time()

    # Periodic cleanup of old records
    if now - _last_cleanup > 300:  # 5 minutes
        _last_cleanup = now
        stale = [k for k, v in _rate_limit.items()
                 if not v or v[-1] < now - _RATE_LIMIT_WINDOW]
        for k in stale:
            del _rate_limit[k]

    timestamps = _rate_limit[ip]
    timestamps[:] = [t for t in timestamps if now - t < _RATE_LIMIT_WINDOW]
    if len(timestamps) >= _RATE_LIMIT_MAX:
        return False
    timestamps.append(now)
    return True


# Define allowed origins - in production, this should come from config/environment
ALLOWED_ORIGINS = {
    "https://yourdomain.com",
    "https://panel.yourdomain.com",
    # Add other trusted origins as needed
}

def _json_ok(handler, data):
    body = json.dumps(data, ensure_ascii=False).encode("utf-8")
    handler.send_response(200)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    
    # Fix CORS: don't rely on Host header which can be spoofed
    origin = handler.headers.get("Origin", "")
    if origin in ALLOWED_ORIGINS:
        handler.send_header("Access-Control-Allow-Origin", origin)
    # Optionally allow credentials if needed
    # handler.send_header("Access-Control-Allow-Credentials", "true")
    
    handler.end_headers()
    handler.wfile.write(body)


def _error(handler, status, message):
    body = json.dumps({"error": message}).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def handle_lk_page(handler):
    html_path = os.path.join(os.path.dirname(__file__), "../../frontend/lk.html")
    html_path = os.path.normpath(html_path)
    try:
        with open(html_path, "rb") as f:
            body = f.read()
    except FileNotFoundError:
        _error(handler, 404, "lk.html not found")
        return
    handler.send_response(200)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _get_token_from_query(handler):
    from urllib.parse import unquote
    import re
    query = handler.path.split("?", 1)[1] if "?" in handler.path else ""
    for part in query.split("&"):
        if part.startswith("token="):
            token = unquote(part[6:])
            # Validation: only allow safe characters
            if re.match(r'^[a-zA-Z0-9_\-]{8,64}$', token):
                return token
            return None
    return None


def handle_lk_info(handler):
    ip = _get_real_ip(handler)
    if not _check_rate_limit(ip):
        _error(handler, 429, "Too many requests")
        return

    token = _get_token_from_query(handler)
    if not token:
        _error(handler, 400, "Missing token")
        return

    username = _client.get_username_for_token(token)
    if not username:
        _error(handler, 404, "User not found")
        return

    try:
        admin_user = os.environ.get("MARZBAN_ADMIN_USER", "admin")
        admin_pass = os.environ.get("MARZBAN_ADMIN_PASS", "")

        admin_token = _get_admin_token(admin_user, admin_pass)

        user_data = {}
        if admin_token:
            try:
                user_data = _client.get_user(username, admin_token)
            except Exception:
                pass

        expire = user_data.get("expire")
        status = user_data.get("status", "unknown")
        used_traffic = user_data.get("used_traffic", 0)
        data_limit = user_data.get("data_limit")

        subscription_url = f"https://{handler.headers.get('Host', '')}/sub/{token}"

        _json_ok(handler, {
            "username": username,
            "status": status,
            "expire": expire,
            "used_traffic": used_traffic,
            "data_limit": data_limit,
            "subscription_url": subscription_url,
        })
    except Exception as e:
        print(f"[LK] info error: {e}")
        _error(handler, 500, "Internal error")


def handle_lk_usage(handler):
    ip = handler.client_address[0]
    if not _check_rate_limit(ip):
        _error(handler, 429, "Too many requests")
        return

    token = _get_token_from_query(handler)
    if not token:
        _error(handler, 400, "Missing token")
        return

    username = _client.get_username_for_token(token)
    if not username:
        _error(handler, 404, "User not found")
        return

    try:
        admin_user = os.environ.get("MARZBAN_ADMIN_USER", "admin")
        admin_pass = os.environ.get("MARZBAN_ADMIN_PASS", "")

        admin_token = _get_admin_token(admin_user, admin_pass)

        nodes_usage = []
        if admin_token:
            try:
                raw = _client.get_user_usage(username, admin_token)
                usages = raw.get("usages", [])
                total = sum(u.get("used_traffic", 0) for u in usages)
                for u in usages:
                    used = u.get("used_traffic", 0)
                    nodes_usage.append({
                        "node_name": u.get("node_name", "Unknown"),
                        "used_traffic": used,
                        "percent": round(used / total * 100) if total > 0 else 0,
                    })
            except Exception as e:
                print(f"[LK] usage fetch error: {e}")

        _json_ok(handler, {"usages": nodes_usage})
    except Exception as e:
        print(f"[LK] usage error: {e}")
        _error(handler, 500, "Internal error")


def handle_lk_devices(handler):
    ip = handler.client_address[0]
    if not _check_rate_limit(ip):
        _error(handler, 429, "Too many requests")
        return

    token = _get_token_from_query(handler)
    if not token:
        _error(handler, 400, "Missing token")
        return

    username = _client.get_username_for_token(token)
    if not username:
        _error(handler, 404, "User not found")
        return

    db = handler.server.db
    devices = db.get_user_devices(username)
    limit = db.get_device_limit(username)
    active_count = sum(1 for d in devices if d["is_active"])

    for d in devices:
        d.pop("request_key", None)

    _json_ok(handler, {"devices": devices, "limit": limit, "active_count": active_count})


def handle_lk_device_delete(handler, device_id):
    ip = handler.client_address[0]
    if not _check_rate_limit(ip):
        _error(handler, 429, "Too many requests")
        return

    token = _get_token_from_query(handler)
    if not token:
        _error(handler, 400, "Missing token")
        return

    username = _client.get_username_for_token(token)
    if not username:
        _error(handler, 404, "User not found")
        return

    try:
        did = int(device_id)
    except (ValueError, TypeError):
        _error(handler, 400, "Invalid device id")
        return

    ok = handler.server.db.deactivate_device(did, username)
    if not ok:
        _error(handler, 404, "Device not found")
        return

    _json_ok(handler, {"ok": True})


def handle_lk_device_rename(handler, device_id):
    ip = handler.client_address[0]
    if not _check_rate_limit(ip):
        _error(handler, 429, "Too many requests")
        return

    token = _get_token_from_query(handler)
    if not token:
        _error(handler, 400, "Missing token")
        return

    username = _client.get_username_for_token(token)
    if not username:
        _error(handler, 404, "User not found")
        return

    try:
        did = int(device_id)
    except (ValueError, TypeError):
        _error(handler, 400, "Invalid device id")
        return

    try:
        data = json.loads(_read_body(handler))
        name = str(data.get("name", "")).strip()
        if not name or len(name) > 50:
            raise ValueError("name must be 1-50 chars")
    except (ValueError, json.JSONDecodeError) as e:
        _error(handler, 400, str(e))
        return

    ok = handler.server.db.rename_device(did, username, name)
    if not ok:
        _error(handler, 404, "Device not found")
        return

    _json_ok(handler, {"ok": True})
