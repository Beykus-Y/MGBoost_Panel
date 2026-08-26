"""Authenticated read-only HTTP surface for Wave A admin presentation models."""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

from ..config import DEVICE_SLOT_HMAC_KEY
from ..admin_read_models import (
    account_detail,
    account_summaries,
    dashboard_summary,
    migration_grace_summaries,
)
from ..http_utils import error_response, json_response
from ..marzban import MarzbanClient
from ..security import require_admin_auth


_marzban = MarzbanClient()


def _include_technical(handler) -> bool:
    values = parse_qs(urlsplit(getattr(handler, "path", "")).query).get("include_technical", [])
    return len(values) == 1 and values[0] == "1"


def _marzban_notes(handler) -> tuple[dict[str, str], bool]:
    """Read-only presentation metadata fetched with the server-held JWT."""
    session = getattr(handler, "_admin_session", None)
    if session is None:
        return {}, False
    try:
        result = _marzban.get_users(session.marzban_token, limit=500, offset=0)
    except Exception:
        return {}, False
    users = result.get("users", []) if isinstance(result, dict) else []
    notes = {}
    for user in users:
        if not isinstance(user, dict):
            continue
        username, note = user.get("username"), user.get("note")
        if isinstance(username, str) and isinstance(note, str) and note.strip():
            notes[username] = note.strip()
    return notes, True


def handle_admin_accounts_list(handler):
    if not require_admin_auth(handler):
        return
    notes, available = _marzban_notes(handler)
    include_technical = _include_technical(handler)
    visible = account_summaries(
        handler.server.db, notes_by_alias=notes, include_technical=include_technical,
    )
    all_rows = account_summaries(
        handler.server.db, notes_by_alias=notes, include_technical=True,
    )
    json_response(handler, 200, {
        "accounts": visible,
        "technical_hidden_count": len(all_rows) - len(visible),
        "presentation_metadata_available": available,
    })


def handle_admin_account_detail(handler, account_id):
    if not require_admin_auth(handler):
        return
    notes, available = _marzban_notes(handler)
    detail = account_detail(
        handler.server.db, int(account_id), notes_by_alias=notes,
        device_slot_hmac_key=DEVICE_SLOT_HMAC_KEY,
    )
    if detail is None:
        error_response(handler, 404, "Account not found")
        return
    detail["presentation_metadata_available"] = available
    json_response(handler, 200, detail)


def handle_admin_migration_grace(handler):
    if not require_admin_auth(handler):
        return
    notes, available = _marzban_notes(handler)
    result = migration_grace_summaries(
        handler.server.db, notes_by_alias=notes,
        include_technical=_include_technical(handler),
    )
    result["presentation_metadata_available"] = available
    json_response(handler, 200, result)


def handle_admin_dashboard(handler):
    if not require_admin_auth(handler):
        return
    notes, available = _marzban_notes(handler)
    result = dashboard_summary(handler.server.db, notes_by_alias=notes)
    result["presentation_metadata_available"] = available
    json_response(handler, 200, result)
