import json
import os
import time
from collections import defaultdict

from ..marzban import MarzbanClient

_client = MarzbanClient()

# Simple in-memory rate limiter: {ip: [timestamps]}
_rate_limit: dict = defaultdict(list)
_RATE_LIMIT_WINDOW = 60
_RATE_LIMIT_MAX = 30


# Cache admin token to avoid logging in on every request
_admin_token_cache: list = [None, 0.0]
_ADMIN_TOKEN_TTL = 3600


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
    except Exception as e:
        print(f"[LK] admin token fetch failed: {e}")
        return None


def _check_rate_limit(ip: str) -> bool:
    now = time.time()
    timestamps = _rate_limit[ip]
    timestamps[:] = [t for t in timestamps if now - t < _RATE_LIMIT_WINDOW]
    if len(timestamps) >= _RATE_LIMIT_MAX:
        return False
    timestamps.append(now)
    return True


def _json_ok(handler, data):
    body = json.dumps(data, ensure_ascii=False).encode("utf-8")
    handler.send_response(200)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Access-Control-Allow-Origin", f"https://{handler.headers.get('Host', '')}")
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
    query = handler.path.split("?", 1)[1] if "?" in handler.path else ""
    for part in query.split("&"):
        if part.startswith("token="):
            return part[6:]
    return None


def handle_lk_info(handler):
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
    history = db.get_device_history(token, limit=10)
    # Strip IP from response for privacy
    for entry in history:
        entry.pop("ip", None)

    _json_ok(handler, {"devices": history})
