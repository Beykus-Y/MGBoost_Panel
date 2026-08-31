"""PH8-04 -- authenticated operator health surface, kept separate from the
account-centric admin routes (`admin_accounts.py`) by design: this module
is operational/observability presentation only, never account detail."""

from __future__ import annotations

from ..http_utils import json_response
from ..ops_observability import health_snapshot
from ..security import require_admin_auth


def handle_admin_ops_health(handler):
    if not require_admin_auth(handler):
        return
    result = health_snapshot(handler.server.db)
    json_response(handler, 200, result)
