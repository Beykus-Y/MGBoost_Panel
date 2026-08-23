"""Helpers for containing legacy subscription bearer tokens.

Phase 1 keeps every existing Marzban subscription token valid. These helpers
only prevent raw bearers from becoming new log or local-database evidence.
"""

from __future__ import annotations

import hashlib
import re
from urllib.parse import urlsplit


TOKEN_REF_PREFIX = "sha256:"
_TOKEN_REF_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_SUB_PATH_RE = re.compile(r"^(/sub/)[^/?]+(?P<suffix>/info)?$")


def subscription_token_ref(raw_token: str) -> str:
    """Return a stable, non-reversible verifier/reference for a bearer."""
    value = str(raw_token or "")
    if _TOKEN_REF_RE.fullmatch(value):
        return value
    return TOKEN_REF_PREFIX + hashlib.sha256(value.encode("utf-8")).hexdigest()


def is_subscription_token_ref(value: str) -> bool:
    return bool(_TOKEN_REF_RE.fullmatch(str(value or "")))


def redact_request_target(target: str) -> str:
    """Redact subscription path segments and all query strings for logs."""
    value = str(target or "")
    try:
        parsed = urlsplit(value)
        path = parsed.path or "/"
    except ValueError:
        return "<invalid-target>"
    match = _SUB_PATH_RE.match(path)
    if match:
        suffix = match.group("suffix") or ""
        path = f"{match.group(1)}<redacted>{suffix}"
    if "?" in value:
        path += "?<redacted>"
    return path
