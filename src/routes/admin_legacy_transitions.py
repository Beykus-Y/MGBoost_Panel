"""Primary-admin API for reusable legacy->commercial transitions."""
from __future__ import annotations

from ..admin_read_models import _queue_label
from ..http_utils import error_response, json_response
from ..legacy_commercial_transition import LegacyCommercialTransitionConflict, LegacyCommercialTransitionError
from ..security import require_admin_auth
from .admin_support import read_json_body, require_primary_capability


def _capability(handler, db):
    return db.primary_admin_authority.authorize_session(handler._admin_session)


def _error(handler, exc):
    error_response(handler, 409 if isinstance(exc, LegacyCommercialTransitionConflict) else 400, str(exc))


def _view(db, row):
    transition_id = int(row["id"])
    target = db._conn.execute(
        "SELECT plan_code,display_name,device_limit,wl_mode FROM mgboost_plan_versions WHERE id=?",
        (row["target_plan_version_id"],),
    ).fetchone()
    source = db._conn.execute(
        "SELECT plan_code,display_name FROM mgboost_plan_versions WHERE id=?",
        (row["source_plan_version_id"],),
    ).fetchone()
    events = db._conn.execute(
        "SELECT event_type,actor_ref,reason,revision,created_at FROM "
        "mgboost_legacy_commercial_transition_events WHERE transition_id=? ORDER BY id",
        (transition_id,),
    ).fetchall()
    devices = db._conn.execute(
        "SELECT g.id AS slot_generation_id,s.slot_number,g.generation,g.status,"
        "c.desired_state AS child_desired_state,c.observed_state AS child_observed_state,"
        "EXISTS(SELECT 1 FROM mgboost_legacy_commercial_transition_selections x "
        "WHERE x.transition_id=? AND x.slot_generation_id=g.id) AS selected "
        "FROM mgboost_device_slot_generations g JOIN mgboost_device_slots s ON s.id=g.slot_id "
        "LEFT JOIN mgboost_child_user_intents c ON c.slot_generation_id=g.id "
        "WHERE g.account_id=? AND g.status='ACTIVE' AND s.current_generation=g.generation "
        "ORDER BY s.slot_number",
        (transition_id, row["account_id"]),
    ).fetchall()
    result = {key: row.get(key) for key in (
        "id", "public_id", "account_id", "payment_record_id", "state",
        "source_plan_version_id", "source_subscription_status", "original_source_expiry", "aligned_source_expiry",
        "payment_confirmed_at", "activation_at", "target_plan_version_id",
        "duration_days", "target_expiry", "expected_amount_minor", "review_reason",
        "applied_at", "revision",
    )}
    result.update({
        "source_plan_code": source["plan_code"] if source else None,
        "source_display_name": source["display_name"] if source else None,
        "target_plan_code": target["plan_code"] if target else None,
        "target_display_name": target["display_name"] if target else None,
        "target_device_limit": target["device_limit"] if target else None,
        "target_wl_mode": target["wl_mode"] if target else None,
        "active_device_count": len(devices),
        "capacity_excess": max(0, len(devices) - int(target["device_limit"] or 0)) if target else None,
        # Stable internal ids are safe selection handles; raw UUID/HWID/token
        # material is intentionally absent from this read model and its audit.
        "devices": [dict(device) for device in devices],
        "events": [dict(event) for event in events],
    })
    return result


# PH7-16 Wave 3: Operations -> Legacy Transitions queue. Lighter than
# `_view()` -- one row per open transition, no per-transition devices/events
# join (that detail loads lazily when an operator opens one from the
# queue, via the existing GET /admin/legacy-transitions/{id}), so listing
# up to a few dozen open transitions stays a handful of cheap queries
# instead of an N+1 over every device slot and audit event.
def _queue_view(db, row):
    target = db._conn.execute(
        "SELECT plan_code,display_name FROM mgboost_plan_versions WHERE id=?",
        (row["target_plan_version_id"],),
    ).fetchone()
    source = db._conn.execute(
        "SELECT plan_code,display_name FROM mgboost_plan_versions WHERE id=?",
        (row["source_plan_version_id"],),
    ).fetchone()
    result = {key: row[key] for key in (
        "id", "public_id", "account_id", "state", "review_reason",
        "activation_at", "target_expiry", "expected_amount_minor", "updated_at",
    )}
    result.update({
        "source_plan_code": source["plan_code"] if source else None,
        "target_plan_code": target["plan_code"] if target else None,
        "target_display_name": target["display_name"] if target else None,
        **_queue_label(db, row["account_id"]),
    })
    return result


def handle_transitions_queue(handler):
    """Cross-account queue of every transition still in flight (every
    state except the two terminal ones, APPLIED/CANCELLED) -- the
    Operations-area counterpart to the single-account
    GET /admin/accounts/{id}/legacy-transition entry point already used
    from the account's Payments tab. Same underlying store, same
    per-transition detail/mutation routes; this is read-only and adds no
    new mutation surface."""
    if not require_admin_auth(handler):
        return
    db = handler.server.db
    if require_primary_capability(handler, db) is None:
        return
    rows = db._conn.execute(
        "SELECT * FROM mgboost_legacy_commercial_transitions "
        "WHERE state NOT IN ('APPLIED','CANCELLED') ORDER BY updated_at DESC LIMIT 100"
    ).fetchall()
    json_response(handler, 200, {"transitions": [_queue_view(db, row) for row in rows]})


def handle_transition_detail(handler, transition_id):
    if not require_admin_auth(handler):
        return
    db = handler.server.db
    if require_primary_capability(handler, db) is None:
        return
    row = db.legacy_commercial_transitions.get(int(transition_id))
    if not row:
        error_response(handler, 404, "Transition not found")
        return
    json_response(handler, 200, {"transition": _view(db, row)})


def handle_account_transition(handler, account_id):
    if not require_admin_auth(handler):
        return
    db = handler.server.db
    if require_primary_capability(handler, db) is None:
        return
    row = db._conn.execute(
        "SELECT * FROM mgboost_legacy_commercial_transitions WHERE account_id=? "
        "AND state NOT IN ('APPLIED','CANCELLED') ORDER BY id DESC LIMIT 1",
        (int(account_id),),
    ).fetchone()
    json_response(handler, 200, {"transition": _view(db, dict(row)) if row else None})


def handle_transition_create(handler, payment_record_id):
    if not require_admin_auth(handler):
        return
    db = handler.server.db
    if require_primary_capability(handler, db) is None:
        return
    data = read_json_body(handler)
    if data is None:
        return
    try:
        row = db.legacy_commercial_transitions.create(
            _capability(handler, db), payment_record_id=int(payment_record_id), reason=data.get("reason", ""),
        )
    except (LegacyCommercialTransitionError, LegacyCommercialTransitionConflict) as exc:
        _error(handler, exc)
        return
    json_response(handler, 201, {"transition": _view(db, row)})


def handle_transition_confirm(handler, transition_id):
    if not require_admin_auth(handler):
        return
    db = handler.server.db
    if require_primary_capability(handler, db) is None:
        return
    try:
        row = db.legacy_commercial_transitions.confirm_payment(_capability(handler, db), int(transition_id))
    except (LegacyCommercialTransitionError, LegacyCommercialTransitionConflict) as exc:
        _error(handler, exc)
        return
    json_response(handler, 200, {"transition": _view(db, row)})


def handle_transition_cancel(handler, transition_id):
    if not require_admin_auth(handler):
        return
    db = handler.server.db
    if require_primary_capability(handler, db) is None:
        return
    data = read_json_body(handler)
    if data is None:
        return
    try:
        row = db.legacy_commercial_transitions.cancel(
            _capability(handler, db), int(transition_id), reason=data.get("reason", ""),
        )
    except (LegacyCommercialTransitionError, LegacyCommercialTransitionConflict) as exc:
        _error(handler, exc)
        return
    json_response(handler, 200, {"transition": _view(db, row)})


def handle_transition_retry_review(handler, transition_id):
    if not require_admin_auth(handler):
        return
    db = handler.server.db
    if require_primary_capability(handler, db) is None:
        return
    data = read_json_body(handler)
    if data is None:
        return
    try:
        row = db.legacy_commercial_transitions.retry_manual_review(
            _capability(handler, db), int(transition_id), reason=data.get("reason", ""),
        )
    except (LegacyCommercialTransitionError, LegacyCommercialTransitionConflict) as exc:
        _error(handler, exc)
        return
    json_response(handler, 200, {"transition": _view(db, row)})


def handle_transition_select(handler, transition_id):
    if not require_admin_auth(handler):
        return
    db = handler.server.db
    if require_primary_capability(handler, db) is None:
        return
    data = read_json_body(handler)
    if data is None:
        return
    values = data.get("slot_generation_ids")
    if not isinstance(values, list) or any(isinstance(value, bool) or not isinstance(value, int) for value in values):
        error_response(handler, 400, "slot_generation_ids must be integer array")
        return
    try:
        row = db.legacy_commercial_transitions.record_selection(
            _capability(handler, db), int(transition_id), generation_ids=values,
            reason=data.get("reason", ""),
        )
    except (LegacyCommercialTransitionError, LegacyCommercialTransitionConflict) as exc:
        _error(handler, exc)
        return
    json_response(handler, 200, {"transition": _view(db, row)})
