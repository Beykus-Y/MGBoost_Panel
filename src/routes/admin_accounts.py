"""Authenticated read-only HTTP surface for Wave A admin presentation models."""

from __future__ import annotations

from ..admin_read_models import (
    account_detail,
    account_summaries,
    dashboard_summary,
    migration_grace_summaries,
)
from ..http_utils import error_response, json_response
from ..security import require_admin_auth


def handle_admin_accounts_list(handler):
    if not require_admin_auth(handler):
        return
    json_response(handler, 200, {"accounts": account_summaries(handler.server.db)})


def handle_admin_account_detail(handler, account_id):
    if not require_admin_auth(handler):
        return
    detail = account_detail(handler.server.db, int(account_id))
    if detail is None:
        error_response(handler, 404, "Account not found")
        return
    json_response(handler, 200, detail)


def handle_admin_migration_grace(handler):
    if not require_admin_auth(handler):
        return
    json_response(handler, 200, migration_grace_summaries(handler.server.db))


def handle_admin_dashboard(handler):
    if not require_admin_auth(handler):
        return
    json_response(handler, 200, dashboard_summary(handler.server.db))
