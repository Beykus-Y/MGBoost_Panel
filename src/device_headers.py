import hashlib
import re


MAX_HEADER_VALUE = 180

HEADER_ALIASES = {
    "device_id": (
        "hwid",
        "x-hwid",
        "x-device-id",
        "x-deviceid",
        "device-id",
        "deviceid",
        "x-client-hwid",
        "x-client-device-id",
        "x-user-hwid",
        "x-sub-hwid",
        "x-happ-hwid",
        "x-hiddify-hwid",
        "x-sing-box-hwid",
        "x-v2ray-hwid",
        "x-mihomo-device-id",
        "x-clash-device-id",
    ),
    "device_name": (
        "x-device-name",
        "device-name",
        "x-device-model",
        "device-model",
        "x-model",
        "x-client-device",
        "x-phone-model",
    ),
    "client_name": (
        "x-client-name",
        "client-name",
        "x-app-name",
        "app-name",
        "x-sub-client",
        "x-proxy-client",
        "x-vpn-client",
    ),
    "client_version": (
        "x-client-version",
        "client-version",
        "x-app-version",
        "app-version",
        "x-version",
    ),
    "platform": (
        "x-platform",
        "platform",
        "x-client-platform",
        "x-os-platform",
    ),
    "os": (
        "x-os",
        "os",
        "x-operating-system",
        "x-client-os",
    ),
}

INTERESTING_HEADERS = {
    header_name
    for aliases in HEADER_ALIASES.values()
    for header_name in aliases
}

PLATFORMS = {
    "android": "Android",
    "ios": "iOS",
    "iphone": "iOS",
    "ipad": "iPadOS",
    "windows": "Windows",
    "win32": "Windows",
    "macos": "macOS",
    "darwin": "Darwin",
    "linux": "Linux",
}


def _normalize_header_name(name: str) -> str:
    return name.strip().lower().replace("_", "-")


def _clean_value(value):
    if value is None:
        return None
    cleaned = re.sub(r"[\x00-\x1f\x7f]+", " ", str(value)).strip()
    if not cleaned:
        return None
    return cleaned[:MAX_HEADER_VALUE]


def _headers_dict(headers) -> dict:
    result = {}
    for key, value in headers.items():
        normalized = _normalize_header_name(key)
        if normalized not in result:
            result[normalized] = _clean_value(value)
    return result


def _first_header(headers, aliases):
    for name in aliases:
        value = headers.get(name)
        if value:
            return value, name
    return None, None


def _parse_user_agent(user_agent):
    if not user_agent:
        return {}

    first_token = user_agent.split(" ", 1)[0]
    parts = [part.strip() for part in first_token.split("/") if part.strip()]
    if len(parts) < 2:
        return {}

    client_name = _clean_value(parts[0])
    if not client_name or client_name.lower().startswith("mozilla"):
        return {}

    parsed = {
        "client_name": client_name,
        "client_version": _clean_value(parts[1]),
    }

    for part in parts[2:]:
        platform = PLATFORMS.get(part.strip().lower())
        if platform:
            parsed["platform"] = platform
            break

    # Happ commonly appends a stable Android identifier after platform.
    if client_name.lower() == "happ" and len(parts) >= 4:
        candidate = _clean_value(parts[3])
        if candidate and re.match(r"^[A-Za-z0-9._:-]{6,}$", candidate):
            parsed["device_id"] = candidate
            parsed["device_id_source"] = "user-agent"

    if "platform" not in parsed:
        for key, value in PLATFORMS.items():
            if re.search(rf"\b{re.escape(key)}\b", user_agent, re.IGNORECASE):
                parsed["platform"] = value
                break

    return parsed


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def extract_device_metadata(headers) -> dict:
    normalized_headers = _headers_dict(headers)
    user_agent = normalized_headers.get("user-agent")
    ua_fields = _parse_user_agent(user_agent)

    metadata = {
        "user_agent": user_agent,
        "device_id": None,
        "device_name": None,
        "client_name": None,
        "client_version": None,
        "platform": None,
        "os": None,
        "fingerprint": None,
        "request_key": None,
        "metadata": {
            "headers": {
                key: value
                for key, value in normalized_headers.items()
                if key in INTERESTING_HEADERS and value
            },
            "sources": {},
        },
    }

    for field, aliases in HEADER_ALIASES.items():
        value, source = _first_header(normalized_headers, aliases)
        if value:
            metadata[field] = value
            metadata["metadata"]["sources"][field] = f"header:{source}"

    for field in ("device_id", "client_name", "client_version", "platform"):
        if not metadata.get(field) and ua_fields.get(field):
            metadata[field] = ua_fields[field]
            metadata["metadata"]["sources"][field] = ua_fields.get(f"{field}_source", "user-agent")

    if metadata["device_id"]:
        seed = f"device:{metadata['device_id']}"
        metadata["request_key"] = f"hwid:{_hash(seed)[:32]}"
    else:
        seed_parts = [
            user_agent,
            metadata["client_name"],
            metadata["client_version"],
            metadata["platform"],
            metadata["os"],
            metadata["device_name"],
        ]
        seed = "|".join(part or "" for part in seed_parts).strip("|")
        if seed:
            metadata["request_key"] = f"fp:{_hash(seed)[:32]}"

    if metadata["request_key"]:
        metadata["fingerprint"] = metadata["request_key"].split(":", 1)[1][:16]

    if not metadata["metadata"]["headers"]:
        metadata["metadata"].pop("headers", None)
    if not metadata["metadata"]["sources"]:
        metadata["metadata"].pop("sources", None)

    return metadata
