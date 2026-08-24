"""Durable PH3-03 worker/reconciliation repository.

All correctness decisions are backed by SQLite transactions.  This module
never calls Marzban and never stores raw child credentials.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import time

from .child_contract import validate_child_ensure_request
from .child_provisioning import ChildProvisioningConflict, ChildProvisioningError


def _canonical(value: dict) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class ChildWorkflowStore:
    def __init__(self, connection: sqlite3.Connection, lock):
        self._conn = connection
        self._lock = lock

    def get_operation(self, operation_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT o.*,c.child_username,c.slot_id,c.slot_generation_id,"
                "c.slot_number,c.generation,c.source_alias_id,c.source_contract_hash,"
                "c.desired_state AS child_desired_state,c.observed_state,"
                "c.uuid_verifier,c.uuid_masked,a.status AS account_status,"
                "s.current_generation,s.desired_state AS slot_desired_state,"
                "g.status AS generation_status "
                "FROM mgboost_outbox o "
                "JOIN mgboost_child_user_intents c ON c.id=o.child_intent_id "
                "AND c.account_id=o.account_id "
                "JOIN mgboost_accounts a ON a.id=o.account_id "
                "JOIN mgboost_device_slots s ON s.id=c.slot_id AND s.account_id=c.account_id "
                "JOIN mgboost_device_slot_generations g ON g.id=c.slot_generation_id "
                "AND g.account_id=c.account_id AND g.slot_id=c.slot_id "
                "WHERE o.operation_id=?",
                (operation_id,),
            ).fetchone()
            if not row:
                return None
            result = dict(row)
            try:
                payload = json.loads(result["payload_json"])
            except (TypeError, json.JSONDecodeError):
                payload = None
                result["payload_error"] = "CORRUPT_PAYLOAD_JSON"
            result["payload"] = payload
            return result

    def validate_operation(self, operation: dict) -> None:
        if operation.get("payload_error"):
            raise ChildProvisioningError(operation["payload_error"])
        payload = validate_child_ensure_request(operation["payload"])
        if not hmac.compare_digest(_sha(_canonical(payload)), operation["request_hash"]):
            raise ChildProvisioningError("PAYLOAD_DIGEST_MISMATCH")
        if payload["operation_id"] != operation["operation_id"]:
            raise ChildProvisioningError("OPERATION_ID_MISMATCH")
        if payload["child_username"] != operation["child_username"]:
            raise ChildProvisioningError("CHILD_IDENTITY_MISMATCH")
        if payload["source_contract_hash"] != operation["source_contract_hash"]:
            raise ChildProvisioningError("SOURCE_DIGEST_MISMATCH")
        if (
            operation["account_status"] != "ACTIVE"
            or operation["child_desired_state"] != "ACTIVE"
            or operation["slot_desired_state"] != "ACTIVE"
            or operation["generation_status"] != "ACTIVE"
            or int(operation["current_generation"]) != int(operation["generation"])
        ):
            raise ChildProvisioningError("STALE_OR_INACTIVE_GENERATION")

    def ensure_tracking(self, operation: dict, *, now: int) -> dict:
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                self._conn.execute(
                    "INSERT OR IGNORE INTO mgboost_child_workflow_state "
                    "(outbox_id,account_id,child_intent_id,next_check_at,created_at,updated_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (
                        operation["id"], operation["account_id"],
                        operation["child_intent_id"], int(now), int(now), int(now),
                    ),
                )
                row = self._conn.execute(
                    "SELECT * FROM mgboost_child_workflow_state WHERE outbox_id=?",
                    (operation["id"],),
                ).fetchone()
                if (
                    row["account_id"] != operation["account_id"]
                    or row["child_intent_id"] != operation["child_intent_id"]
                ):
                    raise ChildProvisioningConflict("workflow scope mismatch")
                self._conn.commit()
                return dict(row)
            except Exception:
                self._conn.rollback()
                raise

    def claim_reconciliation(
        self, operation: dict, *, worker_id: str, now: int, lease_seconds: int
    ) -> dict | None:
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                row = self._conn.execute(
                    "SELECT * FROM mgboost_child_workflow_state WHERE outbox_id=?",
                    (operation["id"],),
                ).fetchone()
                if not row or row["reconcile_state"] == "MANUAL_REVIEW":
                    self._conn.rollback()
                    return None
                if row["next_check_at"] > now:
                    self._conn.rollback()
                    return None
                stale = row["lease_owner"] is not None and row["lease_expires_at"] <= now
                if row["lease_owner"] is not None and not stale:
                    self._conn.rollback()
                    return None
                self._conn.execute(
                    "UPDATE mgboost_child_workflow_state SET lease_owner=?,lease_expires_at=?,"
                    "last_checked_at=?,reconcile_count=reconcile_count+1,updated_at=?,"
                    "row_version=row_version+1 WHERE outbox_id=?",
                    (worker_id, now + max(5, int(lease_seconds)), now, now, row["outbox_id"]),
                )
                if stale:
                    self._event(
                        row, "STALE_LEASE_RECOVERED", now=now, worker_id=worker_id,
                        safe_reason="STALE_RECONCILIATION_LEASE",
                    )
                self._event(row, "CHECK_STARTED", now=now, worker_id=worker_id)
                claimed = self._conn.execute(
                    "SELECT * FROM mgboost_child_workflow_state WHERE outbox_id=?",
                    (row["outbox_id"],),
                ).fetchone()
                self._conn.commit()
                return dict(claimed)
            except Exception:
                self._conn.rollback()
                raise

    def finish_reconciliation(
        self, operation: dict, *, worker_id: str, state: str, now: int,
        next_check_at: int, safe_reason: str | None = None,
        remote_effect_verifier: str | None = None, failure: bool = False,
    ) -> None:
        event_for_state = {
            "IN_SYNC": "MATCHED", "REMOTE_MATCH": "MATCHED",
            "REMOTE_ABSENT": "ABSENT", "REMOTE_MISSING": "ABSENT",
            "REMOTE_MISMATCH": "MISMATCH", "REMOTE_AMBIGUOUS": "AMBIGUOUS",
            "UNAVAILABLE": "UNAVAILABLE", "MANUAL_REVIEW": "MANUAL_REVIEW",
        }
        if state not in event_for_state:
            raise ChildProvisioningError("invalid reconciliation state")
        if safe_reason is not None and (not safe_reason or len(safe_reason) > 128):
            raise ChildProvisioningError("invalid safe reconciliation reason")
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                row = self._conn.execute(
                    "SELECT * FROM mgboost_child_workflow_state WHERE outbox_id=? "
                    "AND lease_owner=?",
                    (operation["id"], worker_id),
                ).fetchone()
                if not row:
                    raise ChildProvisioningConflict("reconciliation lease is not owned")
                recovered = row["reconcile_state"] in {
                    "UNAVAILABLE", "REMOTE_ABSENT", "REMOTE_MISSING",
                    "REMOTE_MISMATCH", "REMOTE_AMBIGUOUS",
                } and state == "IN_SYNC"
                failures = row["failure_count"] + 1 if failure else 0
                manual_reason = safe_reason if state == "MANUAL_REVIEW" else None
                manual_at = now if state == "MANUAL_REVIEW" else None
                self._conn.execute(
                    "UPDATE mgboost_child_workflow_state SET reconcile_state=?,"
                    "failure_count=?,next_check_at=?,lease_owner=NULL,lease_expires_at=NULL,"
                    "last_error_class=?,last_remote_effect_verifier=?,"
                    "last_success_at=?,manual_review_reason=?,manual_review_at=?,"
                    "updated_at=?,row_version=row_version+1 WHERE outbox_id=?",
                    (
                        state, failures, int(next_check_at), safe_reason,
                        remote_effect_verifier, now if state == "IN_SYNC" else row["last_success_at"],
                        manual_reason, manual_at, now, row["outbox_id"],
                    ),
                )
                if state in {"REMOTE_MISSING", "REMOTE_MISMATCH", "REMOTE_AMBIGUOUS", "MANUAL_REVIEW"}:
                    self._conn.execute(
                        "UPDATE mgboost_child_user_intents SET observed_state='ERROR',"
                        "updated_at=?,row_version=row_version+1 WHERE id=?",
                        (now, operation["child_intent_id"]),
                    )
                elif state == "IN_SYNC":
                    self._conn.execute(
                        "UPDATE mgboost_child_user_intents SET observed_state='ACTIVE',"
                        "updated_at=?,row_version=row_version+1 WHERE id=?",
                        (now, operation["child_intent_id"]),
                    )
                self._event(
                    row, event_for_state[state], now=now, worker_id=worker_id,
                    safe_reason=safe_reason, remote_effect_verifier=remote_effect_verifier,
                )
                if recovered:
                    self._event(row, "RECOVERED", now=now, worker_id=worker_id)
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def terminal_provisioning_error(
        self, operation: dict, *, safe_reason: str, now: int
    ) -> None:
        if not safe_reason or len(safe_reason) > 128:
            raise ChildProvisioningError("invalid terminal reason")
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                row = self._conn.execute(
                    "SELECT * FROM mgboost_child_workflow_state WHERE outbox_id=?",
                    (operation["id"],),
                ).fetchone()
                if not row:
                    raise ChildProvisioningConflict("workflow state is required")
                if operation["state"] != "APPLIED":
                    self._conn.execute(
                        "UPDATE mgboost_outbox SET state='ERROR',lease_owner=NULL,"
                        "lease_expires_at=NULL,last_error_class=?,updated_at=?,"
                        "row_version=row_version+1 WHERE id=?",
                        (safe_reason, now, operation["id"]),
                    )
                self._conn.execute(
                    "UPDATE mgboost_child_user_intents SET observed_state='ERROR',"
                    "updated_at=?,row_version=row_version+1 WHERE id=?",
                    (now, operation["child_intent_id"]),
                )
                self._conn.execute(
                    "UPDATE mgboost_child_workflow_state SET reconcile_state='MANUAL_REVIEW',"
                    "lease_owner=NULL,lease_expires_at=NULL,last_error_class=?,"
                    "manual_review_reason=?,manual_review_at=?,updated_at=?,"
                    "row_version=row_version+1 WHERE outbox_id=?",
                    (safe_reason, safe_reason, now, now, operation["id"]),
                )
                self._event(
                    row, "MANUAL_REVIEW", now=now,
                    safe_reason=safe_reason,
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def metrics(self, *, now: int) -> dict:
        with self._lock:
            counts = {
                row["reconcile_state"]: row["n"]
                for row in self._conn.execute(
                    "SELECT reconcile_state,COUNT(*) AS n "
                    "FROM mgboost_child_workflow_state GROUP BY reconcile_state"
                )
            }
            pending = self._conn.execute(
                "SELECT COUNT(*) AS n,MIN(created_at) AS oldest FROM mgboost_outbox "
                "WHERE state IN ('PENDING','RETRY','IN_FLIGHT')"
            ).fetchone()
            retries = self._conn.execute(
                "SELECT COALESCE(SUM(CASE WHEN attempts>1 THEN attempts-1 ELSE 0 END),0) "
                "FROM mgboost_outbox"
            ).fetchone()[0]
            stale = self._conn.execute(
                "SELECT COUNT(*) FROM mgboost_child_reconciliation_events "
                "WHERE event_type='STALE_LEASE_RECOVERED'"
            ).fetchone()[0]
            failures = self._conn.execute(
                "SELECT COUNT(*) FROM mgboost_child_reconciliation_events "
                "WHERE event_type='UNAVAILABLE'"
            ).fetchone()[0]
            provisioning_failures = self._conn.execute(
                "SELECT COUNT(*) FROM mgboost_outbox_attempt_events "
                "WHERE event_type='FAILED'"
            ).fetchone()[0]
            oldest_age = 0 if pending["oldest"] is None else max(0, now - pending["oldest"])
            return {
                "pending_outbox_count": pending["n"],
                "oldest_pending_age_seconds": oldest_age,
                "retry_count": retries,
                "reconciliation_errors": sum(counts.get(key, 0) for key in (
                    "REMOTE_MISSING", "REMOTE_MISMATCH", "REMOTE_AMBIGUOUS", "MANUAL_REVIEW"
                )),
                "remote_mismatch_count": counts.get("REMOTE_MISMATCH", 0),
                "broker_marzban_failure_events": failures + provisioning_failures,
                "stale_worker_lease_events": stale,
                "manual_review_count": counts.get("MANUAL_REVIEW", 0),
                "desired_observed_divergence_count": sum(counts.get(key, 0) for key in (
                    "REMOTE_MISSING", "REMOTE_MISMATCH", "REMOTE_AMBIGUOUS", "UNAVAILABLE",
                    "MANUAL_REVIEW",
                )),
                "states": counts,
            }

    def _event(
        self, row, event_type: str, *, now: int, worker_id: str | None = None,
        safe_reason: str | None = None, remote_effect_verifier: str | None = None,
    ) -> None:
        self._conn.execute(
            "INSERT INTO mgboost_child_reconciliation_events "
            "(outbox_id,account_id,event_type,safe_reason,remote_effect_verifier,"
            "worker_id,created_at) VALUES (?,?,?,?,?,?,?)",
            (
                row["outbox_id"], row["account_id"], event_type, safe_reason,
                remote_effect_verifier, worker_id, int(now),
            ),
        )
