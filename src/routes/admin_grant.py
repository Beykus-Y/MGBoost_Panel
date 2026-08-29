"""Authenticated primary-admin routes over the existing `AdminGrantStore`
(`src/admin_grant.py`, in production since HEAD `c1ae3d4`): create a
canonical DIRECT account for a Telegram id, and grant it an exact
commercial plan/duration with no money moving (`ADMIN_GRANT` -- never
revenue, kept structurally separate from `MANUAL_RUB`). No new domain
logic here -- pure route-layer wiring over an already-reviewed store."""

from __future__ import annotations

import time

from ..admin_grant import (
    AdminGrantError,
    PlanMismatch,
    RenewalError,
    UnknownPlan,
    UnlimitedSubscriptionConflict,
)
from ..http_utils import error_response, json_response
from ..security import require_admin_auth

from .admin_support import (
    account_or_404,
    bounded_int,
    bounded_str,
    read_json_body,
    require_primary_capability,
)

_MIN_IDEMPOTENCY_KEY = 16
_MAX_IDEMPOTENCY_KEY = 128
_MIN_REASON = 8
_MAX_REASON = 1000
_MIN_TELEGRAM_ID = 1
_MAX_TELEGRAM_ID = 999999999999999999
_MIN_DURATION_DAYS = 1
_MAX_DURATION_DAYS = 3650


def handle_admin_account_create(handler):
    if not require_admin_auth(handler):
        return
    db = handler.server.db
    capability = require_primary_capability(handler, db)
    if capability is None:
        return
    data = read_json_body(handler)
    if data is None:
        return
    telegram_id, telegram_error = bounded_int(
        data, "telegram_id", minimum=_MIN_TELEGRAM_ID, maximum=_MAX_TELEGRAM_ID)
    reason, reason_error = bounded_str(
        data, "reason", min_len=_MIN_REASON, max_len=_MAX_REASON)
    idempotency_key, key_error = bounded_str(
        data, "idempotency_key", min_len=_MIN_IDEMPOTENCY_KEY, max_len=_MAX_IDEMPOTENCY_KEY)
    for message in (telegram_error, reason_error, key_error):
        if message:
            error_response(handler, 400, message)
            return
    try:
        result = db.admin_grants.create_account_only(
            capability, telegram_id=telegram_id, reason=reason,
            idempotency_key=idempotency_key, now=int(time.time()),
        )
    except AdminGrantError as exc:
        error_response(handler, 400, str(exc))
        return
    json_response(handler, 200, result)


def handle_admin_grant_apply(handler, account_id):
    if not require_admin_auth(handler):
        return
    db = handler.server.db
    account = account_or_404(handler, db, account_id)
    if account is None:
        return
    capability = require_primary_capability(handler, db)
    if capability is None:
        return
    data = read_json_body(handler)
    if data is None:
        return
    plan_code, plan_error = bounded_str(data, "plan_code", min_len=1, max_len=64)
    duration_days, duration_error = bounded_int(
        data, "duration_days", minimum=_MIN_DURATION_DAYS, maximum=_MAX_DURATION_DAYS)
    reason, reason_error = bounded_str(
        data, "reason", min_len=_MIN_REASON, max_len=_MAX_REASON)
    idempotency_key, key_error = bounded_str(
        data, "idempotency_key", min_len=_MIN_IDEMPOTENCY_KEY, max_len=_MAX_IDEMPOTENCY_KEY)
    for message in (plan_error, duration_error, reason_error, key_error):
        if message:
            error_response(handler, 400, message)
            return
    try:
        result = db.admin_grants.grant_existing_account(
            capability, account_id=account["id"], plan_code=plan_code,
            duration_days=duration_days, reason=reason,
            idempotency_key=idempotency_key, now=int(time.time()),
        )
    except (PlanMismatch, UnlimitedSubscriptionConflict) as exc:
        error_response(handler, 409, str(exc))
        return
    except (UnknownPlan, RenewalError, AdminGrantError) as exc:
        error_response(handler, 400, str(exc))
        return
    json_response(handler, 200, result)
