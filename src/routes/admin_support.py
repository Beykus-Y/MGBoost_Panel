"""Shared helpers for the operational admin mutation routes (PH7 Wave B/PH7-10).

Boundaries kept identical to every existing sensitive admin route:
`require_admin_auth` (session cookie + CSRF on unsafe methods) plus the
server-derived primary-admin capability for consequential mutations. Request
bodies are strictly bounded and parsed once here.
"""

from __future__ import annotations

import json
import re

from ..admin_authority import PrimaryAdminAuthorizationError
from ..http_utils import error_response, read_body
from ..service_marzban import ServiceMarzbanClient

_ACCOUNT_ID_RE = re.compile(r"^\d{1,18}$")
_MAX_BODY_BYTES = 16384

# Lazily-created typed localhost-broker client. Module-level so tests can
# substitute a fake without any real broker; production constructs it exactly
# like main.py does (config comes from environment).
_service_client: ServiceMarzbanClient | None = None


def service_marzban() -> ServiceMarzbanClient:
    global _service_client
    if _service_client is None:
        _service_client = ServiceMarzbanClient()
    return _service_client


def set_service_marzban(client) -> None:
    """Test seam only."""
    global _service_client
    _service_client = client


def require_primary_capability(handler, db):
    """Authorize the session against the configured primary-admin login and
    return the SEALED CAPABILITY OBJECT every domain store verifies via
    ``authority.require``. Sends 403 and returns None when unauthorized."""
    try:
        return db.primary_admin_authority.authorize_session(handler._admin_session)
    except PrimaryAdminAuthorizationError:
        error_response(handler, 403, "Primary admin capability required")
        return None


def account_or_404(handler, db, account_id_raw):
    if not _ACCOUNT_ID_RE.fullmatch(account_id_raw or ""):
        error_response(handler, 404, "Account not found")
        return None
    account = db.accounts.get_account(int(account_id_raw))
    if account is None:
        error_response(handler, 404, "Account not found")
        return None
    return account


def int_or_404(handler, raw, *, what="Resource") -> int | None:
    if not _ACCOUNT_ID_RE.fullmatch(str(raw or "")):
        error_response(handler, 404, f"{what} not found")
        return None
    return int(raw)


def read_json_body(handler, *, max_bytes: int = _MAX_BODY_BYTES):
    """Returns (data|None). Sends the error response itself on failure."""
    try:
        payload = read_body(handler)
    except Exception:
        error_response(handler, 400, "Invalid request body")
        return None
    if len(payload) > max_bytes:
        error_response(handler, 413, "Request body too large")
        return None
    try:
        data = json.loads(payload) if payload else {}
    except Exception:
        error_response(handler, 400, "Invalid request body")
        return None
    if not isinstance(data, dict):
        error_response(handler, 400, "Invalid request body")
        return None
    return data


def bounded_str(data, key: str, *, min_len: int = 1, max_len: int, required: bool = True):
    """Returns (value|None, error_message|None)."""
    value = data.get(key)
    if value is None:
        if required:
            return None, f"{key} is required"
        return None, None
    if not isinstance(value, str):
        return None, f"{key} must be a string"
    value = value.strip()
    if len(value) < min_len or len(value) > max_len:
        return None, f"{key} length must be {min_len}..{max_len}"
    return value, None


def bounded_int(data, key: str, *, minimum: int, maximum: int, required: bool = True):
    value = data.get(key)
    if value is None:
        if required:
            return None, f"{key} is required"
        return None, None
    if isinstance(value, bool) or not isinstance(value, int):
        return None, f"{key} must be an integer"
    if not minimum <= value <= maximum:
        return None, f"{key} out of range"
    return value, None
