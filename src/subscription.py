import base64
from urllib.parse import unquote


FORWARD_HEADERS = {
    "subscription-userinfo",
    "profile-update-interval",
    "profile-title",
    "profile-web-page-url",
    "support-url",
    "profile-expire",
    "content-disposition",
}

SKIP_HEADERS = {"transfer-encoding", "connection", "content-length"}


def extract_fragment_from_uri(uri):
    try:
        if "#" not in uri:
            return None
        return unquote(uri.split("#", 1)[1]) or None
    except Exception:
        return None


def extract_host_from_uri(uri):
    try:
        if "://" not in uri:
            return None
        scheme, rest = uri.split("://", 1)
        rest = rest.split("#", 1)[0].split("?", 1)[0]
        auth = rest.split("/", 1)[0]
        if "@" in auth:
            auth = auth.rsplit("@", 1)[1]
        elif scheme.lower() == "ss":
            try:
                decoded = base64.b64decode(auth + "==").decode("utf-8")
                if "@" in decoded:
                    auth = decoded.rsplit("@", 1)[1]
            except Exception:
                pass
        if auth.startswith("["):
            host = auth.split("]", 1)[0][1:]
        else:
            host = auth.split(":", 1)[0]
        return host or None
    except Exception:
        return None


def filter_by_node_filters(lines, username, db):
    """Filter subscription lines using node_filters from DB."""
    filt = db.get_node_filter(username) if username else None
    if not filt:
        return lines
    if filt.get("all", True):
        return lines
    allowed = set(filt.get("allowed_configs") or [])
    if not allowed:
        return lines
    out = []
    for line in lines:
        scheme = line.split("://", 1)[0].lower() if "://" in line else ""
        if scheme == "hysteria2":
            out.append(line)
            continue
        fragment = extract_fragment_from_uri(line)
        if fragment is None or fragment in allowed:
            out.append(line)
    return out


def add_extra_configs(lines, username, db):
    """Append global enabled extra configs + per-user configs."""
    global_configs = [c["uri"] for c in db.get_extra_configs() if c["enabled"]]
    user_configs = []
    if username:
        for c in db.get_per_user_configs(username):
            if c["enabled"] and c["uri"]:
                user_configs.append(c["uri"])
    return lines + global_configs + user_configs


def parse_userinfo(header_value):
    result = {}
    if not header_value:
        return result
    for part in header_value.split(";"):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            try:
                result[k.strip()] = int(v.strip())
            except ValueError:
                result[k.strip()] = v.strip()
    return result


def build_userinfo(info_dict):
    return "; ".join(f"{k}={v}" for k, v in info_dict.items())


def patch_userinfo_header(header_value, token, db):
    """Add hysteria traffic to subscription-userinfo if available."""
    info = parse_userinfo(header_value)
    hy_up, hy_down = db.get_hysteria_traffic(token)
    if hy_up or hy_down:
        info["upload"] = info.get("upload", 0) + hy_up
        info["download"] = info.get("download", 0) + hy_down
    return build_userinfo(info)


def process_subscription(body, marzban_headers, token, username, db):
    """
    Decode base64 subscription, apply filters and extra configs, re-encode.
    Returns (new_body_bytes, filtered_headers_dict).
    """
    try:
        decoded = base64.b64decode(body).decode("utf-8")
        lines = [l for l in decoded.strip().split("\n") if l.strip()]
    except Exception:
        # Not base64 — pass through as-is
        out_headers = {k: v for k, v in marzban_headers.items() if k.lower() not in SKIP_HEADERS}
        return body, out_headers

    lines = filter_by_node_filters(lines, username, db)
    lines = add_extra_configs(lines, username, db)
    new_body = base64.b64encode("\n".join(lines).encode("utf-8"))

    custom_interval = db.get_setting("sub_update_interval")

    out_headers = {}
    for key, val in marzban_headers.items():
        key_lower = key.lower()
        if key_lower in SKIP_HEADERS:
            continue
        if key_lower == "subscription-userinfo":
            out_headers[key] = patch_userinfo_header(val, token, db)
        elif key_lower == "profile-update-interval":
            out_headers[key] = custom_interval if custom_interval else val
        elif key_lower in FORWARD_HEADERS:
            out_headers[key] = val

    return new_body, out_headers
