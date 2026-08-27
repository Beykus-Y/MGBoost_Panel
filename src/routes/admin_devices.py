"""Wave B device-slot administration routes (PH7-05) over the existing
proven PH3-05 durable lifecycle primitives (`ChildLifecycleStore` +
`process_revoke/process_free/process_rebind`) plus the reversible pause
primitive (`DeviceSlotAdminStore`): revoke/free/rebind reuse the exact
orchestration the PH3-05 production canary used, each with deterministic
per-target idempotency keys so a double click or retry converges instead of
duplicating. Disable/Enable toggle the schema-blessed per-slot pause state
and converge remotely through the revision-stamped PH3-08 sync outbox.
"""

from __future__ import annotations

import hashlib
import re
import time

from ..child_lifecycle import (
    ChildLifecycleConflict,
    ChildLifecycleError,
    process_free,
    process_rebind,
    process_revoke,
)
from ..device_slot_admin import SlotAdminConflict, SlotAdminError
from ..http_utils import error_response, json_response
from ..parent_sync import run_account_sync_cycle
from ..security import require_admin_auth

from .admin_support import account_or_404, read_json_body, require_primary_capability

_HWID_RE = re.compile(r"^[A-Za-z0-9_.:@+-]{6,128}$")
_WORKER_ID = "admin-wave-b"


def _active_generation_or_error(handler, db, account_id: int, slot_number: int):
    """Resolve ONLY the slot's currently-ACTIVE generation + its own child
    intent. Scoping to the live generation (never "latest ever recorded") is
    deliberate: every lifecycle guard here must bind to the exact entity it
    mutates, not to the slot's history."""
    row = db._conn.execute(
        "SELECT g.id AS generation_row_id,g.generation FROM "
        "mgboost_device_slots s JOIN mgboost_device_slot_generations g "
        "ON g.slot_id=s.id AND g.account_id=s.account_id AND g.status='ACTIVE' "
        "AND g.generation=s.current_generation WHERE s.account_id=? AND s.slot_number=?",
        (int(account_id), int(slot_number)),
    ).fetchone()
    return dict(row) if row else None


def _drive_pause_convergence(db, account_id: int, *, worker_id: str, now: int) -> str:
    """One inline convergence pass for the just-toggled slot's account.
    Only PH3-08's IN_SYNC aggregate proves remote convergence; anything else
    stays recoverably PENDING behind durable revision-stamped ops."""
    result = run_account_sync_cycle(
        db, int(account_id), sync_fn=_sync_fn, worker_id=worker_id, now=now,
    )
    return result.get("aggregate_state") or "PENDING"


def _target_or_error(handler, db, account_id: int, slot_number: int):
    """Resolve the slot's latest generation + child intent without ever
    returning raw identifiers to the caller."""
    generation = db._conn.execute(
        "SELECT g.id AS generation_row_id,g.generation,g.status FROM "
        "mgboost_device_slots s JOIN mgboost_device_slot_generations g "
        "ON g.slot_id=s.id WHERE s.account_id=? AND s.slot_number=? "
        "ORDER BY g.generation DESC LIMIT 1", (int(account_id), int(slot_number)),
    ).fetchone()
    if generation is None:
        error_response(handler, 404, "Device slot not found")
        return None
    intent = db._conn.execute(
        "SELECT id,desired_state,observed_state FROM mgboost_child_user_intents "
        "WHERE slot_generation_id=? ORDER BY id DESC LIMIT 1",
        (generation["generation_row_id"],),
    ).fetchone()
    return {"slot_row": generation, "intent": dict(intent) if intent else None}


def _existing_slot_op(db, *, account_id: int, intent_id: int, kind: str):
    """Latest lifecycle op of this kind ever recorded against this exact
    child intent/generation. Scoped by `old_child_intent_id` (not just the
    slot) so an operation already applied against a superseded generation can
    never be mistaken for convergence against the slot's *current* generation
    -- matching `ChildLifecycleStore._prepare`'s own per-intent idempotency
    scope. A slot-wide match here would let e.g. a REVOKE against the
    successor generation silently report `converged: true` off a stale
    REVOKE recorded against the predecessor generation instead of ever
    revoking the current one, and would permanently refuse any REBIND of the
    same slot after its first one ever completed."""
    row = db._conn.execute(
        "SELECT o.operation_id,o.state,o.last_error_class,o.reason "
        "FROM mgboost_child_lifecycle_operations o "
        "WHERE o.account_id=? AND o.old_child_intent_id=? AND o.operation_kind=? "
        "ORDER BY o.updated_at DESC,o.id DESC LIMIT 1",
        (int(account_id), int(intent_id), kind),
    ).fetchone()
    return dict(row) if row else None


def _revoke_broker_payload_shape_ok(result) -> bool:
    return isinstance(result, dict) and isinstance(result.get("outcome"), str)


def handle_device_revoke(handler, account_id, slot_number):
    if not require_admin_auth(handler):
        return
    db = handler.server.db
    account = account_or_404(handler, db, account_id)
    if account is None:
        return
    capability = require_primary_capability(handler, db)
    if capability is None:
        return
    target = _target_or_error(handler, db, account["id"], int(slot_number))
    if target is None:
        return
    data = read_json_body(handler)
    if data is None:
        return
    reason, reason_error = _reason(data)
    if reason_error:
        error_response(handler, 400, reason_error)
        return
    intent = target["intent"]
    if intent is None:
        error_response(handler, 409, "This slot has no provisioned child to revoke")
        return
    existing = _existing_slot_op(db, account_id=account["id"],
                                 intent_id=intent["id"], kind="REVOKE")
    if existing and existing["state"] == "APPLIED":
        json_response(handler, 200, {"operation": existing, "converged": True})
        return
    now = int(time.time())
    idempotency_key = _deterministic_key("admin-device-revoke-v1",
                                         account["id"], slot_number,
                                         target["slot_row"]["generation"])
    try:
        operation = db.child_lifecycle.prepare_revoke(
            account_id=account["id"], old_child_intent_id=intent["id"],
            reason=reason, idempotency_key=idempotency_key, now=now,
        )
    except ChildLifecycleConflict as exc:
        error_response(handler, 409, str(exc))
        return
    except ChildLifecycleError as exc:
        error_response(handler, 400, str(exc))
        return
    try:
        process_revoke(db, operation["operation_id"], worker_id=_WORKER_ID,
                       revoke_fn=_revoke_fn, now=now)
    except Exception:
        # Remote/broker failure: leave a durably scheduled retry instead of an
        # orphaned lease, matching the worker-loop discipline the store
        # documents for its callers.
        try:
            db.child_lifecycle.retry_later(operation["operation_id"],
                                           delay_seconds=120, now=now)
        except ChildLifecycleConflict:
            pass
        json_response(handler, 202, {
            "operation": _safe_op(db, operation["operation_id"]),
            "state": "RETRY", "pending_remote": True,
            "message": "Remote revoke failed once; the durable operation will be retried",
        })
        return
    state = _operation_state(db, operation["operation_id"])
    json_response(handler, 200 if state == "APPLIED" else 202, {
        "operation": _safe_op(db, operation["operation_id"]), "state": state,
        "pending_remote": state != "APPLIED",
    })


def handle_device_free(handler, account_id, slot_number):
    if not require_admin_auth(handler):
        return
    db = handler.server.db
    account = account_or_404(handler, db, account_id)
    if account is None:
        return
    capability = require_primary_capability(handler, db)
    if capability is None:
        return
    target = _target_or_error(handler, db, account["id"], int(slot_number))
    if target is None:
        return
    data = read_json_body(handler)
    if data is None:
        return
    reason, reason_error = _reason(data)
    if reason_error:
        error_response(handler, 400, reason_error)
        return
    intent = target["intent"]
    if intent is None:
        error_response(handler, 409, "This slot has no provisioned child; it is already free")
        return
    revoke_state = db.child_lifecycle.revoke_state(int(intent["id"]))
    if revoke_state != "APPLIED":
        error_response(handler, 409,
                       "Cannot free: the matching REVOKE is not durably applied yet "
                       "(hard PH3-05 ordering guarantee)")
        return
    existing = _existing_slot_op(db, account_id=account["id"],
                                 intent_id=intent["id"], kind="FREE")
    if existing and existing["state"] == "APPLIED":
        json_response(handler, 200, {"operation": existing, "converged": True})
        return
    now = int(time.time())
    idempotency_key = _deterministic_key("admin-device-free-v1",
                                         account["id"], slot_number,
                                         target["slot_row"]["generation"])
    try:
        operation = db.child_lifecycle.prepare_free(
            account_id=account["id"], old_child_intent_id=intent["id"],
            reason=reason, idempotency_key=idempotency_key, now=now,
        )
    except ChildLifecycleConflict as exc:
        error_response(handler, 409, str(exc))
        return
    except ChildLifecycleError as exc:
        error_response(handler, 400, str(exc))
        return
    try:
        process_free(db, operation["operation_id"], worker_id=_WORKER_ID, now=now)
    except Exception:
        try:
            db.child_lifecycle.retry_later(operation["operation_id"],
                                           delay_seconds=120, now=now)
        except ChildLifecycleConflict:
            pass
        json_response(handler, 202, {
            "operation": _safe_op(db, operation["operation_id"]),
            "state": "RETRY", "pending_remote": True,
            "message": "Remote free step failed once; the durable operation will be retried",
        })
        return
    state = _operation_state(db, operation["operation_id"])
    json_response(handler, 200 if state == "APPLIED" else 202, {
        "operation": _safe_op(db, operation["operation_id"]), "state": state,
        "pending_remote": state != "APPLIED",
    })


def handle_device_rebind(handler, account_id, slot_number):
    if not require_admin_auth(handler):
        return
    db = handler.server.db
    account = account_or_404(handler, db, account_id)
    if account is None:
        return
    capability = require_primary_capability(handler, db)
    if capability is None:
        return
    target = _target_or_error(handler, db, account["id"], int(slot_number))
    if target is None:
        return
    data = read_json_body(handler)
    if data is None:
        return
    reason, reason_error = _reason(data)
    if reason_error:
        error_response(handler, 400, reason_error)
        return
    new_hwid = data.get("new_device_hwid")
    if not isinstance(new_hwid, str) or not _HWID_RE.fullmatch(new_hwid.strip()):
        error_response(handler, 400, "new_device_hwid of 6..128 allowed characters is required")
        return
    new_hwid = new_hwid.strip()
    intent = target["intent"]
    if intent is None:
        error_response(handler, 409, "This slot has no provisioned child to rebind")
        return
    if data.get("confirm") is not True:
        error_response(handler, 409,
                       "Confirmation required: resubmit with confirm: true. Rebind "
                       "terminally revokes the current credential generation and "
                       "provisions a new one; the old UUID never resurrects")
        return
    existing = _existing_slot_op(db, account_id=account["id"],
                                 intent_id=intent["id"], kind="REBIND")
    if existing:
        if existing["state"] == "APPLIED":
            error_response(handler, 409,
                           "A REBIND already completed for this slot; the successor "
                           "generation's own lifecycle governs any future compromise")
        else:
            error_response(handler, 409,
                           f"An unfinished REBIND ({existing['state']}) already exists "
                           "for this slot; resolve it before starting another")
        return
    now = int(time.time())
    hwid_mark = hashlib.sha256(new_hwid.encode()).hexdigest()[:16]
    idempotency_key = _deterministic_key("admin-device-rebind-v1", account["id"],
                                         slot_number, target["slot_row"]["generation"],
                                         hwid_mark)
    try:
        operation = db.child_lifecycle.prepare_rebind(
            account_id=account["id"], old_child_intent_id=intent["id"],
            reason=reason, idempotency_key=idempotency_key, now=now,
        )
    except ChildLifecycleConflict as exc:
        error_response(handler, 409, str(exc))
        return
    except ChildLifecycleError as exc:
        error_response(handler, 400, str(exc))
        return
    try:
        process_rebind(db, operation["operation_id"], worker_id=_WORKER_ID,
                       revoke_fn=_revoke_fn, new_raw_hwid=new_hwid,
                       hmac_key=_slot_hmac_key(), now=now)
    except Exception:
        state = _operation_state(db, operation["operation_id"])
        if state in {"IN_FLIGHT", "ERROR"}:
            # A durable op row exists but remote progress failed; leave the
            # truth in the lifecycle tables instead of guessing.
            error_response(handler, 502, "Remote rebind step failed; the durable "
                                         "operation keeps its state for reconciliation")
            return
        raise
    state = _operation_state(db, operation["operation_id"])
    new_generation = None
    fresh_intent = db._conn.execute(
        "SELECT c.id,c.observed_state,g.generation FROM mgboost_child_user_intents c "
        "JOIN mgboost_device_slot_generations g ON g.id=c.slot_generation_id "
        "WHERE g.slot_id=(SELECT id FROM mgboost_device_slots WHERE account_id=? AND slot_number=?) "
        "ORDER BY g.generation DESC LIMIT 1", (account["id"], int(slot_number)),
    ).fetchone()
    if fresh_intent:
        new_generation = fresh_intent["generation"]
    json_response(handler, 200 if state == "APPLIED" else 202, {
        "operation": _safe_op(db, operation["operation_id"]),
        "state": state,
        "pending_remote": state != "APPLIED",
        "current_generation": new_generation,
    })


# --- reversible pause (Disable / Enable, PH7-05) -------------------------------

def _handle_slot_pause(handler, account_id, slot_number, *, paused: bool):
    action = "disable" if paused else "enable"
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
    reason, reason_error = _reason(data)
    if reason_error:
        error_response(handler, 400, reason_error)
        return
    if data.get("confirm") is not True:
        error_response(
            handler, 409,
            "Confirmation required: resubmit with confirm: true",
        )
        return
    now = int(time.time())
    # The convergence drive needs the LIVE generation for the idempotency
    # scope; a paused slot keeps that generation, so the key stays stable
    # across double-clicks/retries of the same toggle.
    generation = _active_generation_or_error(handler, db, account["id"], int(slot_number))
    if generation is None:
        error_response(handler, 409,
                       "This slot has no live provisioned generation to "
                       f"{action}")
        return
    idempotency_key = _deterministic_key(f"admin-slot-{action}-v1", account["id"],
                                         slot_number, generation["generation"])
    try:
        result = db.device_slot_admin.set_paused(
            capability, account_id=account["id"], slot_number=int(slot_number),
            paused=paused, reason=reason, idempotency_key=idempotency_key, now=now,
        )
    except SlotAdminConflict as exc:
        error_response(handler, 409, str(exc))
        return
    except SlotAdminError as exc:
        error_response(handler, 400, str(exc))
        return
    if result.get("converged"):
        json_response(handler, 200, {
            "operation": {"operation_kind": result.get("operation_kind")
                          or ("SLOT_DISABLE" if paused else "SLOT_ENABLE"),
                          "state": "APPLIED"},
            "slot": db.device_slot_admin.current_pause_state(account["id"], int(slot_number)),
            "converged": True, "pending_remote": False,
        })
        return
    try:
        aggregate = _drive_pause_convergence(db, account["id"],
                                             worker_id=f"admin-slot-{action}", now=now)
    except Exception:
        aggregate = "PENDING"
    in_sync = aggregate == "IN_SYNC"
    if in_sync:
        db.device_slot_admin.mark_observed(account["id"], int(slot_number), now=now)
    state = "APPLIED" if in_sync else "PENDING"
    json_response(handler, 200 if in_sync else 202, {
        "operation": {"operation_kind": result["operation_kind"], "state": state},
        "slot": db.device_slot_admin.current_pause_state(account["id"], int(slot_number)),
        "mutation_id": result.get("mutation_id"),
        "aggregate_state": aggregate,
        "pending_remote": not in_sync,
    })


def handle_device_disable(handler, account_id, slot_number):
    _handle_slot_pause(handler, account_id, slot_number, paused=True)


def handle_device_enable(handler, account_id, slot_number):
    _handle_slot_pause(handler, account_id, slot_number, paused=False)


def handle_device_sync(handler, account_id, slot_number):
    """Operator-driven retry of a slot's pending remote child convergence.
    Drives the SAME revision-stamped PH3-08 cycle; never invents mutations."""
    if not require_admin_auth(handler):
        return
    db = handler.server.db
    account = account_or_404(handler, db, account_id)
    if account is None:
        return
    if require_primary_capability(handler, db) is None:
        return
    data = read_json_body(handler, max_bytes=1024)
    if data is None:
        return
    now = int(time.time())
    try:
        aggregate = _drive_pause_convergence(db, account["id"],
                                             worker_id="admin-slot-sync", now=now)
    except Exception as exc:
        error_response(handler, 502, f"child sync failed: {type(exc).__name__}")
        return
    if aggregate == "IN_SYNC":
        db.device_slot_admin.mark_observed(account["id"], int(slot_number), now=now)
    slot = db.device_slot_admin.current_pause_state(account["id"], int(slot_number))
    json_response(handler, 200, {
        "aggregate_state": aggregate,
        "slot": slot,
        "aligned": bool(slot) and slot["slot_desired_state"] == slot["slot_observed_state"],
    })


# --- helpers -----------------------------------------------------------------

def _reason(data: dict):
    reason = data.get("reason")
    if not isinstance(reason, str):
        return None, "reason is required"
    reason = reason.strip()
    if not 3 <= len(reason) <= 300:
        return None, "reason length must be 3..300"
    return reason, None


def _deterministic_key(scope: str, *parts) -> str:
    return scope + ":" + ":".join(str(part) for part in parts)


def _slot_hmac_key():
    # Resolved at call time so a runtime/config reload of
    # DEVICE_SLOT_HMAC_KEY can never leave this route holding a stale value.
    from ..config import DEVICE_SLOT_HMAC_KEY
    return DEVICE_SLOT_HMAC_KEY


def _revoke_fn(payload: dict) -> dict:
    """The typed `child.user.revoke` broker call, injected exactly like the
    staging scripts and worker loops do."""
    from .admin_support import service_marzban
    result = service_marzban().revoke_child_user(payload)
    if not isinstance(result, dict) or "outcome" not in result:
        raise ChildLifecycleError("invalid revoke outcome contract")
    return result


def _sync_fn(payload: dict) -> dict:
    """The typed `child.user.state.sync` broker call, identical to the
    canonical PH5-05/PH7-10 drivers."""
    from .admin_support import service_marzban
    result = service_marzban().sync_child_user_state(payload)
    if not isinstance(result, dict) or "outcome" not in result:
        raise ChildLifecycleError("invalid sync outcome contract")
    return result


def _operation_state(db, operation_id: str) -> str | None:
    row = db._conn.execute(
        "SELECT state FROM mgboost_child_lifecycle_operations WHERE operation_id=?",
        (operation_id,),
    ).fetchone()
    return row["state"] if row else None


def _safe_op(db, operation_id: str) -> dict | None:
    row = db._conn.execute(
        "SELECT o.operation_kind,o.state,o.reason,o.attempts,o.last_error_class,"
        "(SELECT COUNT(*) FROM mgboost_child_lifecycle_attempt_events e "
        " WHERE e.lifecycle_operation_id=o.id) AS attempt_events "
        "FROM mgboost_child_lifecycle_operations o WHERE o.operation_id=?",
        (operation_id,),
    ).fetchone()
    return dict(row) if row else None
