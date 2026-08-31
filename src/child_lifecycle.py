"""Durable PH3-05 device revoke/free/rebind lifecycle repository.

Dormant: no legacy route or resolver imports this module. It never touches
Marzban directly -- remote mutation happens only through the typed
`child.user.revoke` broker operation, injected by the caller as a plain
callable so this module stays testable without a real broker.

Hard ordering guarantee: `apply_free` refuses to release a slot until the
matching REVOKE lifecycle operation is `APPLIED` (durably confirmed against a
remote reread). `process_rebind` never creates the new generation/child until
the old child's revoke is confirmed first. There is no code path that frees a
slot or starts new-child provisioning from a bare local flag.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time

from .child_contract import derive_lifecycle_operation_id


class ChildLifecycleError(RuntimeError):
    pass


class ChildLifecycleConflict(ChildLifecycleError):
    pass


def _canonical(value: dict) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class ChildLifecycleStore:
    def __init__(self, connection: sqlite3.Connection, lock):
        self._conn = connection
        self._lock = lock

    # --- generic prepare/claim, shared by REVOKE/FREE/REBIND -----------------

    def _prepare(
        self, *, account_id: int, old_child_intent_id: int, operation_kind: str,
        reason: str, idempotency_key: str, now: int,
    ) -> dict:
        reason = str(reason or "").strip()
        if not reason or len(reason) > 300:
            raise ChildLifecycleError("a bounded human-readable reason is required")
        if not isinstance(idempotency_key, str) or not 16 <= len(idempotency_key) <= 512:
            raise ChildLifecycleError("invalid idempotency key")
        idem_hash = _sha(f"child-lifecycle-{operation_kind}-v1\0" + idempotency_key)
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                intent = self._conn.execute(
                    "SELECT id,account_id,child_username,uuid_verifier,slot_generation_id,"
                    "observed_state,desired_state FROM mgboost_child_user_intents "
                    "WHERE id=? AND account_id=?",
                    (int(old_child_intent_id), int(account_id)),
                ).fetchone()
                if not intent:
                    raise ChildLifecycleError("child intent does not belong to this account")
                slot_generation = self._conn.execute(
                    "SELECT id,slot_id FROM mgboost_device_slot_generations WHERE id=?",
                    (intent["slot_generation_id"],),
                ).fetchone()
                if not slot_generation:
                    raise ChildLifecycleError("slot generation is missing")

                operation_id = derive_lifecycle_operation_id(
                    intent["child_username"], operation_kind
                )
                payload = {
                    "operation_id": operation_id,
                    "child_username": intent["child_username"],
                    "uuid_verifier": intent["uuid_verifier"],
                }
                payload_json = _canonical(payload)
                request_hash = _sha(payload_json)

                prior = self._conn.execute(
                    "SELECT * FROM mgboost_child_lifecycle_operations WHERE idempotency_key_hash=?",
                    (idem_hash,),
                ).fetchone()
                if prior:
                    if prior["request_hash"] != request_hash:
                        raise ChildLifecycleConflict(
                            "idempotency key reused with a different lifecycle request"
                        )
                    self._conn.commit()
                    return dict(prior)

                by_kind = self._conn.execute(
                    "SELECT * FROM mgboost_child_lifecycle_operations "
                    "WHERE old_child_intent_id=? AND operation_kind=?",
                    (intent["id"], operation_kind),
                ).fetchone()
                if by_kind:
                    if by_kind["idempotency_key_hash"] != idem_hash:
                        raise ChildLifecycleConflict(
                            f"a {operation_kind} operation already exists for this device "
                            "with a different idempotency key"
                        )
                    self._conn.commit()
                    return dict(by_kind)

                cursor = self._conn.execute(
                    "INSERT INTO mgboost_child_lifecycle_operations "
                    "(operation_id,account_id,slot_id,old_slot_generation_id,"
                    "old_child_intent_id,operation_kind,state,idempotency_key_hash,"
                    "request_hash,payload_json,reason,next_attempt_at,created_at,updated_at) "
                    "VALUES (?,?,?,?,?,?,'PENDING',?,?,?,?,?,?,?)",
                    (
                        operation_id, int(account_id), slot_generation["slot_id"],
                        intent["slot_generation_id"], intent["id"], operation_kind,
                        idem_hash, request_hash, payload_json, reason, now, now, now,
                    ),
                )
                row = self._conn.execute(
                    "SELECT * FROM mgboost_child_lifecycle_operations WHERE id=?",
                    (cursor.lastrowid,),
                ).fetchone()
                self._conn.commit()
                return dict(row)
            except Exception:
                self._conn.rollback()
                raise

    def prepare_revoke(self, *, account_id, old_child_intent_id, reason, idempotency_key, now=None):
        return self._prepare(
            account_id=account_id, old_child_intent_id=old_child_intent_id,
            operation_kind="REVOKE", reason=reason, idempotency_key=idempotency_key,
            now=int(time.time()) if now is None else int(now),
        )

    def prepare_free(self, *, account_id, old_child_intent_id, reason, idempotency_key, now=None):
        return self._prepare(
            account_id=account_id, old_child_intent_id=old_child_intent_id,
            operation_kind="FREE", reason=reason, idempotency_key=idempotency_key,
            now=int(time.time()) if now is None else int(now),
        )

    def prepare_rebind(self, *, account_id, old_child_intent_id, reason, idempotency_key, now=None):
        return self._prepare(
            account_id=account_id, old_child_intent_id=old_child_intent_id,
            operation_kind="REBIND", reason=reason, idempotency_key=idempotency_key,
            now=int(time.time()) if now is None else int(now),
        )

    def claim(self, operation_id: str, *, worker_id: str, now: int, lease_seconds: int = 30) -> dict | None:
        if not isinstance(worker_id, str) or not 3 <= len(worker_id) <= 128:
            raise ChildLifecycleError("invalid worker identity")
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                row = self._conn.execute(
                    "SELECT * FROM mgboost_child_lifecycle_operations WHERE operation_id=?",
                    (operation_id,),
                ).fetchone()
                if not row or row["state"] in {"APPLIED", "ERROR"}:
                    self._conn.rollback()
                    return None
                claimable = (
                    (row["state"] in {"PENDING", "RETRY"} and row["next_attempt_at"] <= now)
                    or (row["state"] == "IN_FLIGHT" and row["lease_expires_at"] <= now)
                )
                if not claimable:
                    self._conn.rollback()
                    return None
                attempt = row["attempts"] + 1
                self._conn.execute(
                    "UPDATE mgboost_child_lifecycle_operations SET state='IN_FLIGHT',attempts=?,"
                    "lease_owner=?,lease_expires_at=?,updated_at=?,row_version=row_version+1 "
                    "WHERE id=?",
                    (attempt, worker_id, now + max(5, int(lease_seconds)), now, row["id"]),
                )
                self._conn.execute(
                    "INSERT INTO mgboost_child_lifecycle_attempt_events "
                    "(lifecycle_operation_id,account_id,attempt_no,event_type,created_at) "
                    "VALUES (?,?,?,'STARTED',?)",
                    (row["id"], row["account_id"], attempt, now),
                )
                claimed = self._conn.execute(
                    "SELECT * FROM mgboost_child_lifecycle_operations WHERE id=?", (row["id"],)
                ).fetchone()
                self._conn.commit()
                result = dict(claimed)
                result["payload"] = json.loads(result.pop("payload_json"))
                return result
            except Exception:
                self._conn.rollback()
                raise

    def _event(self, lifecycle_id, account_id, attempt_no, event_type, *, outcome=None,
               remote_effect_verifier=None, safe_error_class=None, now):
        self._conn.execute(
            "INSERT INTO mgboost_child_lifecycle_attempt_events "
            "(lifecycle_operation_id,account_id,attempt_no,event_type,outcome,"
            "remote_effect_verifier,safe_error_class,created_at) VALUES (?,?,?,?,?,?,?,?)",
            (lifecycle_id, account_id, attempt_no, event_type, outcome,
             remote_effect_verifier, safe_error_class, now),
        )

    # --- REVOKE ---------------------------------------------------------------

    def acknowledge_revoke(self, operation_id: str, *, worker_id: str, outcome: str, now: int) -> dict:
        if outcome not in {"REVOKED", "ALREADY_REVOKED", "ALREADY_ABSENT"}:
            raise ChildLifecycleError("invalid revoke outcome")
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                row = self._conn.execute(
                    "SELECT * FROM mgboost_child_lifecycle_operations "
                    "WHERE operation_id=? AND state='IN_FLIGHT' AND lease_owner=? "
                    "AND operation_kind='REVOKE'",
                    (operation_id, worker_id),
                ).fetchone()
                if not row:
                    raise ChildLifecycleConflict("revoke lease is not owned by worker")
                self._conn.execute(
                    "UPDATE mgboost_child_user_intents SET desired_state='REVOKED',"
                    "observed_state='REVOKED',updated_at=?,row_version=row_version+1 WHERE id=?",
                    (now, row["old_child_intent_id"]),
                )
                self._conn.execute(
                    "UPDATE mgboost_child_lifecycle_operations SET state='APPLIED',"
                    "lease_owner=NULL,lease_expires_at=NULL,last_error_class=NULL,updated_at=?,"
                    "row_version=row_version+1 WHERE id=?",
                    (now, row["id"]),
                )
                self._event(
                    row["id"], row["account_id"], row["attempts"], "SUCCEEDED",
                    outcome=outcome, remote_effect_verifier=_sha(outcome), now=now,
                )
                result = self._conn.execute(
                    "SELECT * FROM mgboost_child_lifecycle_operations WHERE id=?", (row["id"],)
                ).fetchone()
                self._conn.commit()
                return dict(result)
            except Exception:
                self._conn.rollback()
                raise

    def record_error(self, operation_id: str, *, error_class: str, now: int) -> None:
        safe_error = (error_class or "").strip()
        if not safe_error or len(safe_error) > 128:
            raise ChildLifecycleError("safe error class is required")
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                row = self._conn.execute(
                    "SELECT * FROM mgboost_child_lifecycle_operations "
                    "WHERE operation_id=? AND state='IN_FLIGHT'",
                    (operation_id,),
                ).fetchone()
                if not row:
                    raise ChildLifecycleConflict("no in-flight lifecycle operation to fail")
                self._conn.execute(
                    "UPDATE mgboost_child_lifecycle_operations SET state='ERROR',"
                    "last_error_class=?,lease_owner=NULL,lease_expires_at=NULL,updated_at=?,"
                    "row_version=row_version+1 WHERE id=?",
                    (safe_error, now, row["id"]),
                )
                self._event(
                    row["id"], row["account_id"], row["attempts"], "FAILED",
                    safe_error_class=safe_error, now=now,
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def retry_later(self, operation_id: str, *, delay_seconds: int, now: int) -> None:
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                row = self._conn.execute(
                    "SELECT * FROM mgboost_child_lifecycle_operations "
                    "WHERE operation_id=? AND state='IN_FLIGHT'",
                    (operation_id,),
                ).fetchone()
                if not row:
                    raise ChildLifecycleConflict("no in-flight lifecycle operation to retry")
                self._conn.execute(
                    "UPDATE mgboost_child_lifecycle_operations SET state='RETRY',"
                    "lease_owner=NULL,lease_expires_at=NULL,next_attempt_at=?,updated_at=?,"
                    "row_version=row_version+1 WHERE id=?",
                    (now + max(1, int(delay_seconds)), now, row["id"]),
                )
                self._event(row["id"], row["account_id"], row["attempts"], "FAILED", now=now)
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    # --- FREE ------------------------------------------------------------------

    def revoke_state(self, old_child_intent_id: int) -> str | None:
        """Read-only: state of the REVOKE lifecycle op for this child intent,
        or None if no revoke was ever requested."""
        row = self._conn.execute(
            "SELECT state FROM mgboost_child_lifecycle_operations "
            "WHERE old_child_intent_id=? AND operation_kind='REVOKE'",
            (int(old_child_intent_id),),
        ).fetchone()
        return row["state"] if row else None

    def apply_free(self, operation_id: str, *, worker_id: str, now: int) -> dict:
        """Free the slot. Refuses unless the matching REVOKE op is APPLIED --
        this is the hard ordering guarantee: no local release before a
        durably confirmed remote revoke."""
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                row = self._conn.execute(
                    "SELECT * FROM mgboost_child_lifecycle_operations "
                    "WHERE operation_id=? AND state='IN_FLIGHT' AND lease_owner=? "
                    "AND operation_kind='FREE'",
                    (operation_id, worker_id),
                ).fetchone()
                if not row:
                    raise ChildLifecycleConflict("free lease is not owned by worker")
                revoke_row = self._conn.execute(
                    "SELECT state FROM mgboost_child_lifecycle_operations "
                    "WHERE old_child_intent_id=? AND operation_kind='REVOKE'",
                    (row["old_child_intent_id"],),
                ).fetchone()
                if not revoke_row or revoke_row["state"] != "APPLIED":
                    self._conn.rollback()
                    raise ChildLifecycleError(
                        "cannot free: matching REVOKE operation is not APPLIED yet"
                    )
                self._conn.commit()  # release the read lock before calling device_slots.release
                return dict(row)
            except Exception:
                self._conn.rollback()
                raise

    def finish_free(self, operation_id: str, *, worker_id: str, now: int) -> dict:
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                row = self._conn.execute(
                    "SELECT * FROM mgboost_child_lifecycle_operations "
                    "WHERE operation_id=? AND state='IN_FLIGHT' AND lease_owner=? "
                    "AND operation_kind='FREE'",
                    (operation_id, worker_id),
                ).fetchone()
                if not row:
                    raise ChildLifecycleConflict("free lease is not owned by worker")
                self._conn.execute(
                    "UPDATE mgboost_child_lifecycle_operations SET state='APPLIED',"
                    "lease_owner=NULL,lease_expires_at=NULL,updated_at=?,"
                    "row_version=row_version+1 WHERE id=?",
                    (now, row["id"]),
                )
                self._event(row["id"], row["account_id"], row["attempts"], "SUCCEEDED", now=now)
                result = self._conn.execute(
                    "SELECT * FROM mgboost_child_lifecycle_operations WHERE id=?", (row["id"],)
                ).fetchone()
                self._conn.commit()
                return dict(result)
            except Exception:
                self._conn.rollback()
                raise

    # --- REBIND ------------------------------------------------------------------

    def record_rebind_generation(
        self, operation_id: str, *, worker_id: str, new_slot_generation_id: int,
        new_child_intent_id: int, now: int,
    ) -> dict:
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                row = self._conn.execute(
                    "SELECT * FROM mgboost_child_lifecycle_operations "
                    "WHERE operation_id=? AND state='IN_FLIGHT' AND lease_owner=? "
                    "AND operation_kind='REBIND'",
                    (operation_id, worker_id),
                ).fetchone()
                if not row:
                    raise ChildLifecycleConflict("rebind lease is not owned by worker")
                if row["new_child_intent_id"] is not None:
                    if row["new_child_intent_id"] != new_child_intent_id:
                        raise ChildLifecycleConflict(
                            "rebind already recorded a different new child intent"
                        )
                else:
                    self._conn.execute(
                        "UPDATE mgboost_child_lifecycle_operations SET "
                        "new_slot_generation_id=?,new_child_intent_id=?,updated_at=?,"
                        "row_version=row_version+1 WHERE id=?",
                        (new_slot_generation_id, new_child_intent_id, now, row["id"]),
                    )
                self._conn.execute(
                    "UPDATE mgboost_child_lifecycle_operations SET state='APPLIED',"
                    "lease_owner=NULL,lease_expires_at=NULL,updated_at=?,"
                    "row_version=row_version+1 WHERE id=?",
                    (now, row["id"]),
                )
                self._event(
                    row["id"], row["account_id"], row["attempts"], "SUCCEEDED",
                    outcome="REBOUND", now=now,
                )
                result = self._conn.execute(
                    "SELECT * FROM mgboost_child_lifecycle_operations WHERE id=?", (row["id"],)
                ).fetchone()
                self._conn.commit()
                return dict(result)
            except Exception:
                self._conn.rollback()
                raise


# --- orchestration: durable revoke -> free / revoke -> generation swap -> ----
# hand off to the existing PH3-03 provisioning pipeline. Each step below is
# individually idempotent, so a crash/restart at any point converges safely
# on retry without ever creating a duplicate remote effect.

def process_revoke(db, operation_id: str, *, worker_id: str, revoke_fn, now: int) -> dict | None:
    """`revoke_fn(payload: dict) -> {"outcome": "REVOKED"|"ALREADY_REVOKED"|"ALREADY_ABSENT"}`
    is the typed `child.user.revoke` broker call, injected so this stays
    testable without a real broker/Marzban."""
    claimed = db.child_lifecycle.claim(operation_id, worker_id=worker_id, now=now)
    if claimed is None:
        return None
    # Deliberately does not auto-classify the exception here: the caller
    # (worker loop / test) decides RETRY (transient: broker/Marzban outage,
    # timeout) vs ERROR (permanent: verifier mismatch/contract drift) and
    # calls retry_later()/record_error() accordingly. The lease is simply
    # left to expire if the caller does neither, which is still safe.
    result = revoke_fn(claimed["payload"])
    return db.child_lifecycle.acknowledge_revoke(
        operation_id, worker_id=worker_id, outcome=result["outcome"], now=now
    )


def process_free(db, operation_id: str, *, worker_id: str, now: int,
                 strict_generation: bool = False) -> dict | None:
    """Refuses to release the slot until the matching REVOKE operation is
    durably `APPLIED`; only then calls the existing PH3-02 `release()`."""
    from .device_slots import StaleSlotGeneration

    claimed = db.child_lifecycle.claim(operation_id, worker_id=worker_id, now=now)
    if claimed is None:
        return None
    row = db.child_lifecycle.apply_free(operation_id, worker_id=worker_id, now=now)
    old_generation = db._conn.execute(
        "SELECT generation FROM mgboost_device_slot_generations WHERE id=?",
        (row["old_slot_generation_id"],),
    ).fetchone()
    try:
        db.device_slots.release(
            row["account_id"], row["slot_id"], old_generation["generation"],
            reason=row["reason"], now=now,
        )
    except StaleSlotGeneration:
        if strict_generation:
            raise
        pass  # a prior attempt already released this slot -- idempotent.
    return db.child_lifecycle.finish_free(operation_id, worker_id=worker_id, now=now)


def process_rebind(
    db, operation_id: str, *, worker_id: str, revoke_fn, new_raw_hwid: str,
    hmac_key, now: int,
) -> dict | None:
    claimed = db.child_lifecycle.claim(operation_id, worker_id=worker_id, now=now)
    if claimed is None:
        return None
    row = claimed
    old_intent = db._conn.execute(
        "SELECT * FROM mgboost_child_user_intents WHERE id=?", (row["old_child_intent_id"],)
    ).fetchone()
    if old_intent["observed_state"] != "REVOKED":
        revoke_payload = {
            "operation_id": derive_lifecycle_operation_id(old_intent["child_username"], "REVOKE"),
            "child_username": old_intent["child_username"],
            "uuid_verifier": old_intent["uuid_verifier"],
        }
        revoke_fn(revoke_payload)
        db._conn.execute(
            "UPDATE mgboost_child_user_intents SET desired_state='REVOKED',"
            "observed_state='REVOKED',updated_at=?,row_version=row_version+1 WHERE id=?",
            (now, old_intent["id"]),
        )
        db._conn.commit()

    old_generation = db._conn.execute(
        "SELECT generation FROM mgboost_device_slot_generations WHERE id=?",
        (row["old_slot_generation_id"],),
    ).fetchone()
    rebind_result = db.device_slots.rebind(
        row["account_id"], row["slot_id"], old_generation["generation"], new_raw_hwid,
        hmac_key, reason=row["reason"], now=now,
    )
    old_outbox = db._conn.execute(
        "SELECT payload_json FROM mgboost_outbox "
        "WHERE child_intent_id=? AND operation_kind='CHILD_USER_ENSURE'",
        (old_intent["id"],),
    ).fetchone()
    old_expire = json.loads(old_outbox["payload_json"])["expire"]
    prepared = db.child_provisioning.prepare_child_ensure(
        account_id=row["account_id"], slot_generation_id=rebind_result["generation_id"],
        source_alias_id=old_intent["source_alias_id"],
        source_contract_hash=old_intent["source_contract_hash"], expire=old_expire,
        idempotency_key=f"lifecycle-rebind-provisioning-v1:{row['operation_id']}",
        now=now,
    )
    return db.child_lifecycle.record_rebind_generation(
        operation_id, worker_id=worker_id, new_slot_generation_id=rebind_result["generation_id"],
        new_child_intent_id=prepared["child_intent_id"], now=now,
    )


# --- DL-019/038 tombstone retention policy check (180 days) -----------------
# This is deliberately only the eligibility CHECK. Every lifecycle-adjacent
# table (mgboost_child_lifecycle_operations/_attempt_events, and the PH3-03
# mgboost_child_user_intents/mgboost_outbox tables they reference) already
# has a schema trigger that unconditionally blocks DELETE, matching this
# codebase's existing permanent-tombstone precedent. Physical cleanup SQL is
# intentionally not implemented here -- it is a separate, explicitly scoped
# retention process, never part of an ordinary user-facing revoke/free/rebind
# request, and it would first need its own schema change to lift the
# immutability triggers under exactly this eligibility condition.

RETENTION_DAYS = 180
SECONDS_PER_DAY = 86400


def cleanup_eligible_at(revoked_at: int) -> int:
    return int(revoked_at) + RETENTION_DAYS * SECONDS_PER_DAY


def is_eligible_for_physical_cleanup(*, revoked_at: int, now: int, has_live_references: bool) -> bool:
    if has_live_references:
        return False
    return int(now) >= cleanup_eligible_at(revoked_at)
