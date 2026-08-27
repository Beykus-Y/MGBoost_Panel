"""PH7-01 admin expiry-operation routes over the durable
`SubscriptionAdminOpsStore` writer. No expiry arithmetic, status decision or
SQL edit lives here: the preview projects the SAME documented formulas the
apply path enforces (DL-044 anchor for +N), and apply requires the sealed
primary-admin capability plus mandatory reason. Child convergence after a
successful adjustment rides the existing PH3-08 `run_account_sync_cycle`
exactly like every other canonical driver (PH5-05 Stars / PH7-10 manual
payments), reporting only honest convergence states.
"""

from __future__ import annotations

import time

from ..admin_read_models import _subscription_summary
from ..http_utils import error_response, json_response
from ..parent_sync import run_account_sync_cycle
from ..security import require_admin_auth
from ..subscription_admin_ops import ADJUSTMENT_KINDS, AdminExpiryConflict, AdminExpiryError

from .admin_support import (
    account_or_404,
    bounded_int,
    bounded_str,
    read_json_body,
    require_primary_capability,
)

_WORKER_ID = "admin-expiry-ph7-01"


def _error_status(exc: AdminExpiryError) -> int:
    return 409 if isinstance(exc, AdminExpiryConflict) else 400


def _parse_body(handler, *, mutation: bool):
    """Returns (adjustment_kind, value, reason|None, idempotency_key|None) or
    None after sending the error response. reason/idempotency_key are parsed
    and enforced ONLY for the mutation path."""
    data = read_json_body(handler)
    if data is None:
        return None
    kind = data.get("adjustment_kind")
    if not isinstance(kind, str) or kind not in ADJUSTMENT_KINDS:
        error_response(handler, 400,
                       "adjustment_kind must be one of: " + ", ".join(ADJUSTMENT_KINDS))
        return None
    value = None
    if kind != "END_NOW":
        value, value_error = bounded_int(data, "value", minimum=1, maximum=3_200_000_000)
        if value_error:
            error_response(handler, 400, value_error)
            return None
    if not mutation:
        return kind, value, None, None
    reason, reason_error = bounded_str(data, "reason", min_len=3, max_len=300)
    if reason_error:
        error_response(handler, 400, reason_error)
        return None
    idempotency_key, key_error = bounded_str(
        data, "idempotency_key", min_len=16, max_len=128)
    if key_error:
        error_response(handler, 400, key_error)
        return None
    return kind, value, reason, idempotency_key


def handle_expiry_preview(handler, account_id):
    """Read-only projection of an expiry change; admin session suffices."""
    if not require_admin_auth(handler):
        return
    db = handler.server.db
    account = account_or_404(handler, db, account_id)
    if account is None:
        return
    parsed = _parse_body(handler, mutation=False)
    if parsed is None:
        return
    adjustment_kind, value = parsed[0], parsed[1]
    try:
        response = db.subscription_admin_ops.preview(
            int(account["id"]), adjustment_kind=adjustment_kind, value=value,
            now=int(time.time()),
        )
    except AdminExpiryError as exc:
        error_response(handler, _error_status(exc), str(exc))
        return
    subscription = _subscription_summary(db, int(account["id"]), now=int(time.time()))
    response["plan_display_name"] = subscription and subscription.get("display_name")
    json_response(handler, 200, response)


def handle_expiry_adjustment(handler, account_id):
    if not require_admin_auth(handler):
        return
    db = handler.server.db
    account = account_or_404(handler, db, account_id)
    if account is None:
        return
    capability = require_primary_capability(handler, db)
    if capability is None:
        return
    parsed = _parse_body(handler, mutation=True)
    if parsed is None:
        return
    adjustment_kind, value, reason, idempotency_key = parsed
    try:
        result = db.subscription_admin_ops.apply_adjustment(
            capability, account_id=int(account["id"]),
            adjustment_kind=adjustment_kind, value=value,
            reason=reason, idempotency_key=idempotency_key, now=int(time.time()),
        )
    except AdminExpiryError as exc:
        error_response(handler, _error_status(exc), str(exc))
        return
    payload = {key: result.get(key) for key in (
        "adjustment_kind", "value", "previous_expiry", "new_expiry",
        "already_applied", "mutation_id",
    )}
    if result.get("already_applied"):
        payload["aggregate_state"] = "REPLAYED"
        json_response(handler, 200, payload)
        return
    try:
        sync_result = run_account_sync_cycle(
            db, int(account["id"]), sync_fn=_sync_fn, worker_id=_WORKER_ID,
            now=int(time.time()),
        )
        payload["aggregate_state"] = sync_result.get("aggregate_state")
    except Exception as exc:
        payload["aggregate_state"] = "PENDING"
        payload["sync_error_class"] = type(exc).__name__
    entitlement = db.entitlements.calculate(account_id=int(account["id"]))
    subscription_effect = entitlement.get("subscription", {}) \
        if isinstance(entitlement, dict) else {}
    payload["entitlement_summary"] = {
        "effective_status": subscription_effect.get("effective_status"),
        "effective_expiry": subscription_effect.get("effective_expiry"),
    }
    json_response(handler, 200, payload)


def _sync_fn(payload: dict) -> dict:
    from .admin_support import service_marzban
    result = service_marzban().sync_child_user_state(payload)
    if not isinstance(result, dict) or "outcome" not in result:
        raise AdminExpiryError("invalid sync outcome contract")
    return result
