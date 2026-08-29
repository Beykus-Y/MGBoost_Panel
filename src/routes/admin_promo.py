"""PH5-13 admin UI backend: manage promo definitions and inspect redemptions
over the existing `PromoStore` (`src/promo.py`). Same boundaries as every
sensitive admin route: `require_admin_auth` plus the server-derived
primary-admin capability for consequential mutations, strictly bounded request
bodies. No new domain logic here -- pure route-layer wiring over the
already-reviewed store."""

from __future__ import annotations

import re
import time

from ..http_utils import error_response, json_response
from ..promo import PromoConflict, PromoError, PromoNotFound
from ..security import require_admin_auth

from .admin_support import (
    bounded_str,
    read_json_body,
    require_primary_capability,
)

_MIN_IDEMPOTENCY_KEY = 16
_MAX_IDEMPOTENCY_KEY = 128
_MIN_REASON = 8
_MAX_REASON = 1000
_CODE_RE = re.compile(r"^[A-Z0-9_]{3,64}$")
_EFFECT_KINDS = ("EXTEND_SUBSCRIPTION", "TRIAL_GRANT", "PURCHASE_DISCOUNT")


def handle_admin_promo_create(handler):
    if not require_admin_auth(handler):
        return
    db = handler.server.db
    capability = require_primary_capability(handler, db)
    if capability is None:
        return
    data = read_json_body(handler)
    if data is None:
        return
    code, code_error = bounded_str(data, "code", min_len=3, max_len=64)
    effect_kind, effect_error = bounded_str(data, "effect_kind", min_len=3, max_len=32)
    reason, reason_error = bounded_str(
        data, "reason", min_len=_MIN_REASON, max_len=_MAX_REASON)
    idempotency_key, key_error = bounded_str(
        data, "idempotency_key", min_len=_MIN_IDEMPOTENCY_KEY, max_len=_MAX_IDEMPOTENCY_KEY)
    for message in (code_error, effect_error, reason_error, key_error):
        if message:
            error_response(handler, 400, message)
            return
    if not _CODE_RE.fullmatch(code):
        error_response(handler, 400, "code must be UPPER A-Z/0-9/_ of length 3..64")
        return
    if effect_kind not in _EFFECT_KINDS:
        error_response(handler, 400, f"effect_kind must be one of {_EFFECT_KINDS}")
        return
    trial_class = data.get("trial_class")
    if trial_class is not None:
        if not isinstance(trial_class, str) or not 1 <= len(trial_class.strip()) <= 64:
            error_response(handler, 400, "trial_class must be a string of length 1..64")
            return
    per_user_limit = data.get("per_user_limit", 1)
    if isinstance(per_user_limit, bool) or not isinstance(per_user_limit, int) \
            or not 1 <= per_user_limit <= 1000:
        error_response(handler, 400, "per_user_limit must be an integer 1..1000")
        return
    effect_params = data.get("effect_params")
    if not isinstance(effect_params, dict):
        error_response(handler, 400, "effect_params must be an object")
        return
    try:
        result = db.promo.create_definition(
            capability, code=code, effect_kind=effect_kind, trial_class=trial_class,
            effect_params=effect_params, reason=reason, idempotency_key=idempotency_key,
            per_user_limit=per_user_limit, now=int(time.time()),
        )
    except PromoConflict as exc:
        error_response(handler, 409, str(exc))
        return
    except PromoError as exc:
        error_response(handler, 400, str(exc))
        return
    json_response(handler, 200, result)


def handle_admin_promo_disable(handler, code):
    if not require_admin_auth(handler):
        return
    db = handler.server.db
    capability = require_primary_capability(handler, db)
    if capability is None:
        return
    if not _CODE_RE.fullmatch(code or ""):
        error_response(handler, 404, "Promo code not found")
        return
    data = read_json_body(handler)
    if data is None:
        return
    reason, reason_error = bounded_str(
        data, "reason", min_len=_MIN_REASON, max_len=_MAX_REASON)
    if reason_error:
        error_response(handler, 400, reason_error)
        return
    try:
        result = db.promo.disable_definition(capability, code=code, reason=reason,
                                             now=int(time.time()))
    except PromoNotFound as exc:
        error_response(handler, 404, str(exc))
        return
    except PromoError as exc:
        error_response(handler, 400, str(exc))
        return
    json_response(handler, 200, result)


def handle_admin_promo_list(handler):
    if not require_admin_auth(handler):
        return
    db = handler.server.db
    json_response(handler, 200, {"definitions": db.promo.list_definitions()})


def handle_admin_promo_redemptions(handler):
    """Read-only inspection (latest 100, newest first). CANCELLED rows are
    shown so released/failed reservations stay visible to support."""
    if not require_admin_auth(handler):
        return
    db = handler.server.db
    rows = db._conn.execute(
        "SELECT r.id,r.promo_id,d.code AS promo_code,r.promo_version,r.trial_class,"
        "r.owner_telegram_id,r.account_id,r.status,r.reserved_until,"
        "r.per_user_limit_snapshot,r.actor_type,r.actor_ref,r.reason,"
        "r.created_at,r.updated_at "
        "FROM mgboost_promo_redemptions r "
        "JOIN mgboost_promo_definitions d ON d.id=r.promo_id "
        "ORDER BY r.id DESC LIMIT 100",
    ).fetchall()
    json_response(handler, 200, {"redemptions": [dict(row) for row in rows]})
