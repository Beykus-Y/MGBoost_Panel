"""Authenticated primary-admin Telegram ownership rebind route (OPD-39/
DL-041) over the existing proven `OwnershipRebindStore` + its own
`process_rebind` orchestration. Device/HWID possession is never treated as
ownership evidence; ORDINARY touches only identity state, while COMPROMISE
additionally rotates the opaque subscription credential first (that
rotation's raw token is deliberately not returned here -- a fresh URL must
be issued through the existing credential flow)."""

from __future__ import annotations

import time

from ..admin_authority import PrimaryAdminAuthorizationError
from ..http_utils import error_response, json_response
from ..ownership_rebind import (
    OwnershipRebindConflict,
    OwnershipRebindError,
    process_rebind as process_ownership_rebind,
)
from ..security import require_admin_auth

from .admin_support import account_or_404, read_json_body

_WORKER_ID = "admin-ownership-rebind"


def _capability(handler, db):
    try:
        return db.primary_admin_authority.authorize_session(handler._admin_session)
    except PrimaryAdminAuthorizationError:
        error_response(handler, 403, "Primary admin capability required")
        return None


def _telegram_id(data, key: str):
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        return None, f"{key} must be an integer"
    if not 1 <= value <= 999999999999999999:
        return None, f"{key} out of range"
    return value, None


def _reason(data):
    value = data.get("reason")
    if not isinstance(value, str):
        return None, "reason is required"
    value = value.strip()
    if not 3 <= len(value) <= 300:
        return None, "reason length must be 3..300"
    return value, None


def handle_telegram_ownership_rebind(handler, account_id):
    if not require_admin_auth(handler):
        return
    db = handler.server.db
    account = account_or_404(handler, db, account_id)
    if account is None:
        return
    capability = _capability(handler, db)
    if capability is None:
        return
    data = read_json_body(handler)
    if data is None:
        return
    expected_old, old_error = _telegram_id(data, "expected_old_telegram_id")
    new_tg, new_error = _telegram_id(data, "new_telegram_id")
    reason, reason_error = _reason(data)
    mode = data.get("mode") or "ORDINARY"
    if data.get("confirm") is not True:
        error_response(handler, 409,
                       "Confirmation required: resubmit with confirm: true. The old "
                       "Telegram binding will be revoked immediately after success")
        return
    for message in (old_error, new_error, reason_error):
        if message:
            error_response(handler, 400, message)
            return
    if mode not in ("ORDINARY", "COMPROMISE"):
        error_response(handler, 400, "mode must be ORDINARY or COMPROMISE")
        return
    # Deterministic idempotency: an identical repeat submit converges on the
    # same durable operation; any parameter change is a distinct audited op.
    idempotency_key = (
        f"admin-ownership-rebind-v1:{account['id']}:{expected_old}:{new_tg}:{mode}"
    )
    try:
        operation = db.ownership_rebind.prepare(
            capability=capability,
            account_id=account["id"],
            expected_old_telegram_id=expected_old,
            new_telegram_id=new_tg,
            mode=mode,
            reason=reason,
            idempotency_key=idempotency_key,
            now=int(time.time()),
        )
    except OwnershipRebindConflict as exc:
        error_response(handler, 409, str(exc))
        return
    except OwnershipRebindError as exc:
        error_response(handler, 400, str(exc))
        return
    if operation["state"] == "APPLIED":
        json_response(handler, 200, {"operation": {"mode": operation["mode"],
                                                   "state": operation["state"]},
                                     "converged": True})
        return
    try:
        finished = process_ownership_rebind(db, operation["operation_id"],
                                            worker_id=_WORKER_ID,
                                            now=int(time.time()))
    except OwnershipRebindConflict as exc:
        # Stale expected-old-owner / concurrent CAS denial surfaced during the
        # durable identity step: an operator input problem, not a failure.
        error_response(handler, 409, str(exc))
        return
    except Exception:
        fresh = db._conn.execute(
            "SELECT state FROM mgboost_ownership_rebind_operations WHERE operation_id=?",
            (operation["operation_id"],),
        ).fetchone()
        if fresh and fresh["state"] == "ERROR":
            error_response(handler, 502, "Ownership rebind failed at a durable step; "
                                         "the operation keeps its error state for "
                                         "reconciliation")
            return
        raise
    credential_rotated = bool(finished and finished.get("new_credential_id"))
    payload = {
        "operation": {
            "mode": finished["mode"] if finished else operation["mode"],
            "state": finished["state"] if finished else operation["state"],
        },
        "credential_rotated": credential_rotated,
    }
    if credential_rotated:
        payload["message"] = ("Opaque credential rotated (COMPROMISE). Issue a fresh URL "
                              "through the Subscription tab; the old one is already dead.")
    json_response(handler, 200, payload)
