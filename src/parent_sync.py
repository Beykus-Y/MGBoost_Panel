"""Durable PH3-08 parent status/expiry -> active child generations sync.

Dormant: no legacy route, resolver or worker service imports this module yet.

Core principle: the parent account/subscription is the source of truth for
whether its *current* (non-terminal) child generations should be active in
Marzban, and at what effective expiry. This module never touches PH3-05
territory (individual device revoke/free/rebind) -- it only ever flips
`status`/`expire` on children that are still the live generation of their
slot, and it never rotates a UUID.

    parent desired state (mgboost_entitlement_state, PH3-01 table)
        -> durable per-child sync op (this module's outbox)
        -> typed `child.user.state.sync` broker call
        -> authoritative reread
        -> local ACK

Staleness protection: every sync op is stamped with the parent revision that
produced it. `claim()` re-checks that stamped revision against the *live*
revision immediately before a worker may dispatch the remote mutation; a
mismatch means a newer parent transition has already superseded this op, so
it is marked SUPERSEDED and never dispatched. This is what stops a stale
queued ENABLE from winning after a DISABLE (and the symmetric case).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time

from .child_contract import derive_sync_operation_id


class ParentSyncError(RuntimeError):
    pass


class ParentSyncConflict(ParentSyncError):
    pass


_DESIRED_STATUSES = frozenset({"ACTIVE", "DISABLED", "EXPIRED", "UNLIMITED"})


def _canonical(value: dict) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def compute_desired_status(account_status: str, subscription_status: str, current_expiry, now: int) -> str:
    """Pure policy: canonical parent desired state. Never reads a caller-
    supplied status/expiry -- only actual account/subscription fields."""
    if account_status != "ACTIVE":
        return "DISABLED"
    if subscription_status == "UNLIMITED":
        return "UNLIMITED"
    if subscription_status == "ACTIVE":
        if current_expiry is not None and int(current_expiry) <= int(now):
            return "EXPIRED"
        return "ACTIVE"
    if subscription_status == "EXPIRED":
        return "EXPIRED"
    # PENDING, DISABLED, CANCELLED, UNKNOWN_LEGACY -- no entitlement to serve.
    return "DISABLED"


def child_target_for(desired_status: str, current_expiry) -> tuple[str, int | None]:
    """Map the canonical parent desired status to the minimal Marzban target
    (status, expire). `expire` is only meaningful (and only ever sent) when
    the target status is 'active'; 0 is this codebase's existing convention
    for unlimited, matching every other Marzban write path here."""
    if desired_status == "ACTIVE":
        return "active", (int(current_expiry) if current_expiry is not None else 0)
    if desired_status == "UNLIMITED":
        return "active", 0
    return "disabled", None


class ParentSyncStore:
    def __init__(self, connection: sqlite3.Connection, lock):
        self._conn = connection
        self._lock = lock

    # --- canonical parent desired state (writes PH3-01's mgboost_entitlement_state) ---

    def refresh_desired_state(self, account_id: int, *, now: int | None = None) -> dict:
        timestamp = int(time.time()) if now is None else int(now)
        account_id = int(account_id)
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                account = self._conn.execute(
                    "SELECT id,status FROM mgboost_accounts WHERE id=?", (account_id,),
                ).fetchone()
                if not account:
                    raise ParentSyncError("account does not exist")
                subscription = self._conn.execute(
                    "SELECT id,status,current_expiry FROM mgboost_subscriptions "
                    "WHERE account_id=? ORDER BY id DESC LIMIT 1",
                    (account_id,),
                ).fetchone()
                if not subscription:
                    raise ParentSyncError("account has no subscription to derive entitlement from")
                desired_status = compute_desired_status(
                    account["status"], subscription["status"], subscription["current_expiry"], timestamp,
                )
                current = self._conn.execute(
                    "SELECT * FROM mgboost_entitlement_state WHERE account_id=?", (account_id,),
                ).fetchone()
                if current is None:
                    self._conn.execute(
                        "INSERT INTO mgboost_entitlement_state "
                        "(account_id,subscription_id,desired_status,revision,updated_at) "
                        "VALUES (?,?,?,1,?)",
                        (account_id, subscription["id"], desired_status, timestamp),
                    )
                elif (current["desired_status"] != desired_status
                        or current["subscription_id"] != subscription["id"]):
                    self._conn.execute(
                        "UPDATE mgboost_entitlement_state SET subscription_id=?,desired_status=?,"
                        "revision=revision+1,updated_at=? WHERE account_id=?",
                        (subscription["id"], desired_status, timestamp, account_id),
                    )
                row = self._conn.execute(
                    "SELECT *,? AS current_expiry FROM mgboost_entitlement_state WHERE account_id=?",
                    (subscription["current_expiry"], account_id),
                ).fetchone()
                self._conn.commit()
                return dict(row)
            except Exception:
                self._conn.rollback()
                raise

    # --- durable per-child convergence outbox --------------------------------

    def enqueue_current_children(self, account_id: int, *, now: int | None = None) -> list[dict]:
        """One sync op per *current* (non-terminal, live-generation) child of
        this account, stamped with the account's live desired-state revision.
        Released/revoked generations and other accounts' children are never
        selected -- PH3-05 terminal transitions are structurally excluded by
        the ACTIVE-generation join, not by a convention this module could
        violate."""
        timestamp = int(time.time()) if now is None else int(now)
        account_id = int(account_id)
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                state = self._conn.execute(
                    "SELECT es.*,s.current_expiry FROM mgboost_entitlement_state AS es "
                    "JOIN mgboost_subscriptions AS s ON s.id=es.subscription_id "
                    "WHERE es.account_id=?",
                    (account_id,),
                ).fetchone()
                if not state:
                    raise ParentSyncError("desired state has not been computed for this account yet")
                desired_status, expire = child_target_for(state["desired_status"], state["current_expiry"])
                children = self._conn.execute(
                    "SELECT ci.id,ci.child_username,ci.uuid_verifier "
                    "FROM mgboost_child_user_intents AS ci "
                    "JOIN mgboost_device_slot_generations AS g ON g.id=ci.slot_generation_id "
                    "WHERE ci.account_id=? AND g.status='ACTIVE' AND ci.desired_state!='REVOKED'",
                    (account_id,),
                ).fetchall()
                results = []
                for child in children:
                    results.append(self._prepare_locked(
                        account_id=account_id, child_intent_id=child["id"],
                        child_username=child["child_username"], uuid_verifier=child["uuid_verifier"],
                        parent_revision=state["revision"], desired_status=desired_status,
                        desired_expire=expire, now=timestamp,
                    ))
                self._conn.commit()
                return results
            except Exception:
                self._conn.rollback()
                raise

    def _prepare_locked(
        self, *, account_id, child_intent_id, child_username, uuid_verifier,
        parent_revision, desired_status, desired_expire, now,
    ) -> dict:
        operation_id = derive_sync_operation_id(child_username, parent_revision)
        payload = {
            "operation_id": operation_id,
            "child_username": child_username,
            "desired_status": desired_status,
            "desired_expire": desired_expire,
            "uuid_verifier": uuid_verifier,
        }
        payload_json = _canonical(payload)
        request_hash = _sha(payload_json)
        idem_hash = _sha(f"parent-sync-v1\0{child_intent_id}\0{parent_revision}")
        existing = self._conn.execute(
            "SELECT * FROM mgboost_parent_sync_operations WHERE child_intent_id=? AND parent_revision=?",
            (child_intent_id, parent_revision),
        ).fetchone()
        if existing:
            if existing["request_hash"] != request_hash:
                raise ParentSyncConflict(
                    "a sync operation for this child/revision already exists with different content"
                )
            return dict(existing)
        cursor = self._conn.execute(
            "INSERT INTO mgboost_parent_sync_operations "
            "(operation_id,account_id,child_intent_id,parent_revision,desired_status,"
            "desired_expire,state,idempotency_key_hash,request_hash,payload_json,"
            "next_attempt_at,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,'PENDING',?,?,?,?,?,?)",
            (
                operation_id, account_id, child_intent_id, parent_revision, desired_status,
                desired_expire, idem_hash, request_hash, payload_json, now, now, now,
            ),
        )
        row = self._conn.execute(
            "SELECT * FROM mgboost_parent_sync_operations WHERE id=?", (cursor.lastrowid,),
        ).fetchone()
        return dict(row)

    def claim(self, operation_id: str, *, worker_id: str, now: int, lease_seconds: int = 30) -> dict | None:
        if not isinstance(worker_id, str) or not 3 <= len(worker_id) <= 128:
            raise ParentSyncError("invalid worker identity")
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                row = self._conn.execute(
                    "SELECT * FROM mgboost_parent_sync_operations WHERE operation_id=?",
                    (operation_id,),
                ).fetchone()
                if not row or row["state"] in {"APPLIED", "ERROR", "SUPERSEDED"}:
                    self._conn.rollback()
                    return None
                claimable = (
                    (row["state"] in {"PENDING", "RETRY"} and row["next_attempt_at"] <= now)
                    or (row["state"] == "IN_FLIGHT" and row["lease_expires_at"] <= now)
                )
                if not claimable:
                    self._conn.rollback()
                    return None
                live = self._conn.execute(
                    "SELECT revision FROM mgboost_entitlement_state WHERE account_id=?",
                    (row["account_id"],),
                ).fetchone()
                if not live or live["revision"] != row["parent_revision"]:
                    # A newer parent transition has already superseded this
                    # op. Never dispatch a stale mutation -- this is the
                    # anti-staleness guarantee for both stale-enable and
                    # stale-disable races.
                    superseded_attempt = row["attempts"] + 1
                    self._conn.execute(
                        "UPDATE mgboost_parent_sync_operations SET state='SUPERSEDED',attempts=?,"
                        "lease_owner=NULL,lease_expires_at=NULL,updated_at=?,"
                        "row_version=row_version+1 WHERE id=?",
                        (superseded_attempt, now, row["id"]),
                    )
                    self._event(row["id"], row["account_id"], superseded_attempt, "SUPERSEDED", now=now)
                    self._conn.commit()
                    return None
                attempt = row["attempts"] + 1
                self._conn.execute(
                    "UPDATE mgboost_parent_sync_operations SET state='IN_FLIGHT',attempts=?,"
                    "lease_owner=?,lease_expires_at=?,updated_at=?,row_version=row_version+1 "
                    "WHERE id=?",
                    (attempt, worker_id, now + max(5, int(lease_seconds)), now, row["id"]),
                )
                self._event(row["id"], row["account_id"], attempt, "STARTED", now=now)
                claimed = self._conn.execute(
                    "SELECT * FROM mgboost_parent_sync_operations WHERE id=?", (row["id"],)
                ).fetchone()
                self._conn.commit()
                result = dict(claimed)
                result["payload"] = json.loads(result.pop("payload_json"))
                return result
            except Exception:
                self._conn.rollback()
                raise

    def _event(self, sync_id, account_id, attempt_no, event_type, *, outcome=None,
               remote_effect_verifier=None, safe_error_class=None, now):
        self._conn.execute(
            "INSERT INTO mgboost_parent_sync_attempt_events "
            "(sync_operation_id,account_id,attempt_no,event_type,outcome,"
            "remote_effect_verifier,safe_error_class,created_at) VALUES (?,?,?,?,?,?,?,?)",
            (sync_id, account_id, attempt_no, event_type, outcome,
             remote_effect_verifier, safe_error_class, now),
        )

    def acknowledge(self, operation_id: str, *, worker_id: str, outcome: str, now: int) -> dict:
        if outcome not in {"SYNCED", "ALREADY_IN_SYNC"}:
            raise ParentSyncError("invalid sync outcome")
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                row = self._conn.execute(
                    "SELECT * FROM mgboost_parent_sync_operations "
                    "WHERE operation_id=? AND state='IN_FLIGHT' AND lease_owner=?",
                    (operation_id, worker_id),
                ).fetchone()
                if not row:
                    raise ParentSyncConflict("sync lease is not owned by worker")
                local_status = "ACTIVE" if row["desired_status"] == "active" else "DISABLED"
                self._conn.execute(
                    "UPDATE mgboost_child_user_intents SET desired_state=?,observed_state=?,"
                    "updated_at=?,row_version=row_version+1 "
                    "WHERE id=? AND desired_state!='REVOKED'",
                    (local_status, local_status, now, row["child_intent_id"]),
                )
                self._conn.execute(
                    "UPDATE mgboost_parent_sync_operations SET state='APPLIED',"
                    "lease_owner=NULL,lease_expires_at=NULL,last_error_class=NULL,updated_at=?,"
                    "row_version=row_version+1 WHERE id=?",
                    (now, row["id"]),
                )
                self._event(
                    row["id"], row["account_id"], row["attempts"], "SUCCEEDED",
                    outcome=outcome, remote_effect_verifier=_sha(outcome), now=now,
                )
                result = self._conn.execute(
                    "SELECT * FROM mgboost_parent_sync_operations WHERE id=?", (row["id"],)
                ).fetchone()
                self._conn.commit()
                return dict(result)
            except Exception:
                self._conn.rollback()
                raise

    def record_error(self, operation_id: str, *, error_class: str, now: int) -> None:
        safe_error = (error_class or "").strip()
        if not safe_error or len(safe_error) > 128:
            raise ParentSyncError("safe error class is required")
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                row = self._conn.execute(
                    "SELECT * FROM mgboost_parent_sync_operations WHERE operation_id=? AND state='IN_FLIGHT'",
                    (operation_id,),
                ).fetchone()
                if not row:
                    raise ParentSyncConflict("no in-flight sync operation to fail")
                self._conn.execute(
                    "UPDATE mgboost_parent_sync_operations SET state='ERROR',"
                    "last_error_class=?,lease_owner=NULL,lease_expires_at=NULL,updated_at=?,"
                    "row_version=row_version+1 WHERE id=?",
                    (safe_error, now, row["id"]),
                )
                self._event(row["id"], row["account_id"], row["attempts"], "FAILED",
                            safe_error_class=safe_error, now=now)
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def retry_later(self, operation_id: str, *, delay_seconds: int, now: int) -> None:
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                row = self._conn.execute(
                    "SELECT * FROM mgboost_parent_sync_operations WHERE operation_id=? AND state='IN_FLIGHT'",
                    (operation_id,),
                ).fetchone()
                if not row:
                    raise ParentSyncConflict("no in-flight sync operation to retry")
                self._conn.execute(
                    "UPDATE mgboost_parent_sync_operations SET state='RETRY',"
                    "lease_owner=NULL,lease_expires_at=NULL,next_attempt_at=?,updated_at=?,"
                    "row_version=row_version+1 WHERE id=?",
                    (now + max(1, int(delay_seconds)), now, row["id"]),
                )
                self._event(row["id"], row["account_id"], row["attempts"], "FAILED", now=now)
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def aggregate_state(self, account_id: int) -> str:
        """Aggregate convergence state for the account's *current* children:
        one of IN_SYNC, PENDING, PARTIAL, MANUAL_REVIEW."""
        state = self._conn.execute(
            "SELECT revision FROM mgboost_entitlement_state WHERE account_id=?",
            (int(account_id),),
        ).fetchone()
        if not state:
            return "PENDING"
        rows = self._conn.execute(
            "SELECT so.state FROM mgboost_parent_sync_operations AS so "
            "JOIN mgboost_child_user_intents AS ci ON ci.id=so.child_intent_id "
            "JOIN mgboost_device_slot_generations AS g ON g.id=ci.slot_generation_id "
            "WHERE so.account_id=? AND so.parent_revision=? "
            "AND g.status='ACTIVE' AND ci.desired_state!='REVOKED'",
            (int(account_id), state["revision"]),
        ).fetchall()
        if not rows:
            return "PENDING"
        states = {row["state"] for row in rows}
        if states == {"APPLIED"}:
            return "IN_SYNC"
        if "ERROR" in states:
            return "MANUAL_REVIEW"
        if "APPLIED" in states and states != {"APPLIED"}:
            return "PARTIAL"
        return "PENDING"


# --- orchestration: refresh desired state -> enqueue -> claim+dispatch -------

def process_sync(db, operation_id: str, *, worker_id: str, sync_fn, now: int) -> dict | None:
    """`sync_fn(payload: dict) -> {"outcome": "SYNCED"|"ALREADY_IN_SYNC"|"REMOTE_MISSING"}`
    is the typed `child.user.state.sync` broker call, injected so this stays
    testable without a real broker/Marzban."""
    claimed = db.parent_sync.claim(operation_id, worker_id=worker_id, now=now)
    if claimed is None:
        return None
    result = sync_fn(claimed["payload"])
    if result["outcome"] == "REMOTE_MISSING":
        # Never auto-create a remote child here -- that is PH3-03's job.
        # Surface this as a permanent error for reconciliation/hand-off.
        db.parent_sync.record_error(operation_id, error_class="REMOTE_MISSING", now=now)
        return None
    return db.parent_sync.acknowledge(
        operation_id, worker_id=worker_id, outcome=result["outcome"], now=now
    )


def run_account_sync_cycle(db, account_id: int, *, sync_fn, worker_id: str, now: int, lease_seconds: int = 30) -> dict:
    """One full convergence pass for one account: recompute desired state,
    enqueue sync ops for all current children, then drive every claimable op
    to completion. Safe to call repeatedly/concurrently -- every step is
    idempotent and stale ops are skipped by `claim()`."""
    db.parent_sync.refresh_desired_state(account_id, now=now)
    prepared = db.parent_sync.enqueue_current_children(account_id, now=now)
    applied, superseded, errored = 0, 0, 0
    for op in prepared:
        result = process_sync(
            db, op["operation_id"], worker_id=worker_id, sync_fn=sync_fn, now=now,
        )
        if result is None:
            fresh = db._conn.execute(
                "SELECT state FROM mgboost_parent_sync_operations WHERE operation_id=?",
                (op["operation_id"],),
            ).fetchone()
            if fresh and fresh["state"] == "SUPERSEDED":
                superseded += 1
            elif fresh and fresh["state"] == "ERROR":
                errored += 1
        else:
            applied += 1
    return {
        "prepared": len(prepared), "applied": applied,
        "superseded": superseded, "errored": errored,
        "aggregate_state": db.parent_sync.aggregate_state(account_id),
    }
