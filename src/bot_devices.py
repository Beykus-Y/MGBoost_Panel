"""Owner-scoped device listing and one-request revoke/free orchestration."""

from __future__ import annotations

import time

from .child_lifecycle import process_free, process_revoke

_REASON = "Telegram owner device release"
_WORKER = "telegram-device-release"


def list_devices(db, account_id: int) -> list[dict]:
    """Return only current slots and telemetry proven for their generation."""
    with db._lock:
        rows = db._conn.execute(
            "SELECT s.slot_number,s.desired_state,g.id AS generation_id,g.generation,"
            "g.hwid_masked,c.id AS child_intent_id,t.model,t.platform,"
            "t.client_name,t.client_version "
            "FROM mgboost_device_slots s "
            "LEFT JOIN mgboost_device_slot_generations g ON g.slot_id=s.id "
            "AND g.account_id=s.account_id AND g.status='ACTIVE' "
            "AND g.generation=s.current_generation "
            "LEFT JOIN mgboost_child_user_intents c ON c.slot_generation_id=g.id "
            "LEFT JOIN mgboost_device_telemetry t ON t.slot_generation_id=g.id "
            "AND t.account_id=s.account_id AND t.hwid_verifier=g.hwid_verifier "
            "WHERE s.account_id=? ORDER BY s.slot_number", (int(account_id),)
        ).fetchall()
    return [dict(row) for row in rows if row["generation_id"] is not None]


def release_device(db, *, account_id: int, slot_number: int,
                   generation_id: int, revoke_fn, now: int | None = None) -> str:
    """One owner request drives durable REVOKE then FREE, in that order.

    A transient broker failure leaves the same request retryable by the
    background sweep. The generation fence makes stale buttons harmless.
    """
    timestamp = int(time.time()) if now is None else int(now)
    with db._lock:
        row = db._conn.execute(
            "SELECT g.id AS generation_id,c.id AS child_intent_id "
            "FROM mgboost_device_slots s "
            "JOIN mgboost_device_slot_generations g ON g.slot_id=s.id "
            "AND g.account_id=s.account_id AND g.status='ACTIVE' "
            "AND g.generation=s.current_generation "
            "LEFT JOIN mgboost_child_user_intents c ON c.slot_generation_id=g.id "
            "WHERE s.account_id=? AND s.slot_number=?",
            (int(account_id), int(slot_number)),
        ).fetchone()
    if row is None:
        return "done"
    if row["generation_id"] != int(generation_id):
        return "stale"
    if row["child_intent_id"] is None:
        return "unsupported"
    intent_id = int(row["child_intent_id"])
    key = f"telegram-device-release:{account_id}:{generation_id}"
    revoke = db.child_lifecycle.prepare_revoke(
        account_id=account_id, old_child_intent_id=intent_id,
        reason=_REASON, idempotency_key=key + ":revoke", now=timestamp,
    )
    if revoke["state"] != "APPLIED":
        try:
            process_revoke(db, revoke["operation_id"], worker_id=_WORKER,
                           revoke_fn=revoke_fn, now=timestamp)
        except Exception:
            try:
                db.child_lifecycle.retry_later(revoke["operation_id"],
                                               delay_seconds=120, now=timestamp)
            except Exception:
                pass
            return "pending"
    if db.child_lifecycle.revoke_state(intent_id) != "APPLIED":
        return "pending"
    free = db.child_lifecycle.prepare_free(
        account_id=account_id, old_child_intent_id=intent_id,
        reason=_REASON, idempotency_key=key + ":free", now=timestamp,
    )
    if free["state"] != "APPLIED":
        try:
            process_free(db, free["operation_id"], worker_id=_WORKER,
                         now=timestamp, strict_generation=True)
        except Exception:
            try:
                db.child_lifecycle.retry_later(free["operation_id"],
                                               delay_seconds=120, now=timestamp)
            except Exception:
                pass
            return "pending"
    with db._lock:
        state = db._conn.execute(
            "SELECT state FROM mgboost_child_lifecycle_operations WHERE operation_id=?",
            (free["operation_id"],),
        ).fetchone()
    return "done" if state and state["state"] == "APPLIED" else "pending"


def sweep_pending(db, *, revoke_fn, now: int | None = None, limit: int = 50) -> int:
    """Resume only owner-requested releases after a bot restart/outage."""
    timestamp = int(time.time()) if now is None else int(now)
    with db._lock:
        rows = db._conn.execute(
            "SELECT DISTINCT o.account_id,g.slot_number,g.id AS generation_id "
            "FROM mgboost_child_lifecycle_operations o "
            "JOIN mgboost_device_slot_generations g ON g.id=o.old_slot_generation_id "
            "WHERE o.operation_kind='REVOKE' AND o.reason=? "
            "AND o.state IN ('PENDING','RETRY','IN_FLIGHT','APPLIED') "
            "AND EXISTS (SELECT 1 FROM mgboost_device_slot_generations live "
            "WHERE live.id=g.id AND live.status='ACTIVE') "
            "ORDER BY o.id LIMIT ?", (_REASON, int(limit)),
        ).fetchall()
    for row in rows:
        try:
            release_device(db, account_id=row["account_id"],
                           slot_number=row["slot_number"],
                           generation_id=row["generation_id"],
                           revoke_fn=revoke_fn, now=timestamp)
        except Exception:
            pass
    return len(rows)
