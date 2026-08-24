"""Crash-safe PH3-03 outbox worker and periodic child reconciler."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from urllib.error import HTTPError, URLError

from .child_contract import credential_verifier
from .child_provisioning import ChildProvisioningError


logger = logging.getLogger(__name__)


def _canonical(value: dict) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _digest(value: dict) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _safe_error_class(exc: Exception) -> str:
    if isinstance(exc, ChildProvisioningError):
        value = str(exc).strip().upper().replace(" ", "_")
        return value[:128] if value else "CHILD_WORKFLOW_ERROR"
    if isinstance(exc, HTTPError):
        return f"BROKER_HTTP_{int(exc.code)}"
    if isinstance(exc, (URLError, TimeoutError, ConnectionError, OSError)):
        return "BROKER_OR_MARZBAN_UNAVAILABLE"
    return "UNEXPECTED_WORKER_ERROR"


class ChildProvisioningWorker:
    MODES = {"active", "reconcile_only"}

    def __init__(
        self, db, marzban, *, worker_id: str, allowed_operation_ids,
        mode: str = "reconcile_only", max_attempts: int = 8,
        lease_seconds: int = 30, retry_base_seconds: int = 5,
        retry_cap_seconds: int = 300, reconcile_interval_seconds: int = 60,
        clock=time.time, crash_hook=None,
    ):
        if mode not in self.MODES:
            raise ValueError("child worker mode must be active or reconcile_only")
        allowed = tuple(dict.fromkeys(str(value).strip() for value in allowed_operation_ids))
        if not allowed or any(not value.startswith("op_") for value in allowed):
            raise ValueError("an explicit child operation allowlist is required")
        if not 1 <= int(max_attempts) <= 100:
            raise ValueError("invalid child worker max attempts")
        self.db = db
        self.marzban = marzban
        self.worker_id = worker_id
        self.allowed_operation_ids = allowed
        self.mode = mode
        self.max_attempts = int(max_attempts)
        self.lease_seconds = max(5, int(lease_seconds))
        self.retry_base_seconds = max(1, int(retry_base_seconds))
        self.retry_cap_seconds = max(self.retry_base_seconds, int(retry_cap_seconds))
        self.reconcile_interval_seconds = max(5, int(reconcile_interval_seconds))
        self.clock = clock
        self.crash_hook = crash_hook or (lambda _stage, _operation: None)

    def _backoff(self, failure_number: int) -> int:
        return min(
            self.retry_cap_seconds,
            self.retry_base_seconds * (2 ** max(0, min(int(failure_number) - 1, 20))),
        )

    def run_once(self) -> dict:
        summary = {
            "examined": 0, "reconciled": 0, "provisioned": 0,
            "retried": 0, "manual_review": 0, "skipped": 0,
        }
        for operation_id in self.allowed_operation_ids:
            now = int(self.clock())
            operation = self.db.child_workflow.get_operation(operation_id)
            if not operation:
                summary["skipped"] += 1
                continue
            summary["examined"] += 1
            self.db.child_workflow.ensure_tracking(operation, now=now)
            try:
                self.db.child_workflow.validate_operation(operation)
            except Exception as exc:
                reason = _safe_error_class(exc)
                self.db.child_workflow.terminal_provisioning_error(
                    operation, safe_reason=reason, now=now
                )
                self._log("manual_review", operation, reason)
                summary["manual_review"] += 1
                continue
            if operation["state"] == "APPLIED":
                outcome = self._reconcile_applied(operation, now=now)
            elif operation["state"] in {"PENDING", "RETRY", "IN_FLIGHT"}:
                if self.mode != "active":
                    summary["skipped"] += 1
                    continue
                outcome = self._provision(operation, now=now)
            else:
                summary["skipped"] += 1
                continue
            summary[outcome] += 1
        summary["metrics"] = self.db.child_workflow.metrics(now=int(self.clock()))
        return summary

    def _provision(self, operation: dict, *, now: int) -> str:
        if operation["attempts"] >= self.max_attempts:
            self.db.child_workflow.terminal_provisioning_error(
                operation, safe_reason="PROVISIONING_RETRY_EXHAUSTED", now=now
            )
            return "manual_review"
        claimed = self.db.child_provisioning.claim(
            operation["operation_id"], worker_id=self.worker_id,
            now=now, lease_seconds=self.lease_seconds,
        )
        if not claimed:
            return "skipped"
        self.crash_hook("after_claim_before_remote", operation)
        try:
            observed = self.marzban.observe_child_user(claimed["payload"])
            presence = observed.get("presence") if isinstance(observed, dict) else None
            if presence == "ABSENT":
                ensured = self.marzban.ensure_child_user(claimed["payload"])
                if ensured.get("outcome") not in {"CREATED", "EXISTING"}:
                    raise ChildProvisioningError("INVALID_ENSURE_OUTCOME")
                self.crash_hook("after_remote_create_before_ack", operation)
                observed = self.marzban.observe_child_user(claimed["payload"])
                presence = observed.get("presence") if isinstance(observed, dict) else None
                ack_outcome = ensured["outcome"]
            else:
                ack_outcome = "EXISTING"
            if presence == "MATCH":
                child_uuid, safe_remote = self._validated_match(observed)
                self.db.child_provisioning.acknowledge(
                    operation["operation_id"], worker_id=self.worker_id,
                    outcome=ack_outcome, child_uuid=child_uuid,
                    remote_result=safe_remote, now=int(self.clock()),
                )
                self.crash_hook("after_local_ack", operation)
                refreshed = self.db.child_workflow.get_operation(operation["operation_id"])
                self._reconcile_applied(refreshed, now=int(self.clock()), force=True)
                self._log("applied", operation)
                return "provisioned"
            if presence in {"MISMATCH", "AMBIGUOUS"}:
                reason = (
                    "REMOTE_AMBIGUOUS" if presence == "AMBIGUOUS"
                    else str(observed.get("mismatch_code") or "REMOTE_CONTRACT_MISMATCH")[:128]
                )
                self.db.child_workflow.terminal_provisioning_error(
                    operation, safe_reason=reason, now=int(self.clock())
                )
                self._log("manual_review", operation, reason)
                return "manual_review"
            raise ChildProvisioningError("INVALID_OBSERVE_OUTCOME")
        except Exception as exc:
            if isinstance(exc, RuntimeError) and str(exc).startswith("TEST_CRASH_"):
                raise
            return self._retry_claimed(operation, claimed, exc)

    def _retry_claimed(self, operation: dict, claimed: dict, exc: Exception) -> str:
        now = int(self.clock())
        reason = _safe_error_class(exc)
        if claimed["attempts"] >= self.max_attempts:
            current = self.db.child_workflow.get_operation(operation["operation_id"])
            self.db.child_workflow.terminal_provisioning_error(
                current, safe_reason="PROVISIONING_RETRY_EXHAUSTED", now=now
            )
            self._log("manual_review", operation, "PROVISIONING_RETRY_EXHAUSTED")
            return "manual_review"
        self.db.child_provisioning.retry(
            operation["operation_id"], worker_id=self.worker_id,
            error_class=reason, now=now, delay=self._backoff(claimed["attempts"]),
        )
        self._log("retry", operation, reason)
        return "retried"

    def _reconcile_applied(self, operation: dict, *, now: int, force: bool = False) -> str:
        if force:
            with self.db._lock:
                self.db._conn.execute(
                    "UPDATE mgboost_child_workflow_state SET next_check_at=? "
                    "WHERE outbox_id=? AND lease_owner IS NULL",
                    (now, operation["id"]),
                )
                self.db._conn.commit()
        lease = self.db.child_workflow.claim_reconciliation(
            operation, worker_id=self.worker_id, now=now,
            lease_seconds=self.lease_seconds,
        )
        if not lease:
            return "skipped"
        try:
            observed = self.marzban.observe_child_user(operation["payload"])
            presence = observed.get("presence") if isinstance(observed, dict) else None
            if presence == "MATCH":
                child_uuid, safe_remote = self._validated_match(observed)
                verifier = credential_verifier(child_uuid)
                if not operation.get("uuid_verifier") or not hmac.compare_digest(
                    verifier, operation["uuid_verifier"]
                ):
                    return self._manual_reconcile(
                        operation, "REMOTE_UUID_VERIFIER_MISMATCH", now
                    )
                effect = _digest(safe_remote)
                self.db.child_workflow.finish_reconciliation(
                    operation, worker_id=self.worker_id, state="IN_SYNC", now=now,
                    next_check_at=now + self.reconcile_interval_seconds,
                    remote_effect_verifier=effect,
                )
                self._log("in_sync", operation)
                return "reconciled"
            if presence == "ABSENT":
                return self._failed_reconcile(operation, lease, "REMOTE_MISSING", now)
            if presence == "AMBIGUOUS":
                return self._manual_reconcile(operation, "REMOTE_AMBIGUOUS", now)
            if presence == "MISMATCH":
                reason = str(observed.get("mismatch_code") or "REMOTE_CONTRACT_MISMATCH")[:128]
                return self._manual_reconcile(operation, reason, now)
            return self._manual_reconcile(operation, "INVALID_OBSERVE_OUTCOME", now)
        except Exception as exc:
            return self._failed_reconcile(
                operation, lease, _safe_error_class(exc), now, unavailable=True
            )

    def _failed_reconcile(
        self, operation: dict, lease: dict, reason: str, now: int,
        unavailable: bool = False,
    ) -> str:
        failure_number = int(lease["failure_count"]) + 1
        if failure_number >= self.max_attempts:
            return self._manual_reconcile(
                operation, "RECONCILIATION_RETRY_EXHAUSTED", now
            )
        state = "UNAVAILABLE" if unavailable else "REMOTE_MISSING"
        self.db.child_workflow.finish_reconciliation(
            operation, worker_id=self.worker_id, state=state, now=now,
            next_check_at=now + self._backoff(failure_number),
            safe_reason=reason, failure=True,
        )
        self._log("reconcile_retry", operation, reason)
        return "retried"

    def _manual_reconcile(self, operation: dict, reason: str, now: int) -> str:
        self.db.child_workflow.finish_reconciliation(
            operation, worker_id=self.worker_id, state="MANUAL_REVIEW", now=now,
            next_check_at=now, safe_reason=reason, failure=True,
        )
        self._log("manual_review", operation, reason)
        return "manual_review"

    @staticmethod
    def _validated_match(observed: dict) -> tuple[str, dict]:
        child_uuid = observed.get("uuid")
        verifier = credential_verifier(child_uuid)
        safe = {key: value for key, value in observed.items() if key != "uuid"}
        safe["uuid_masked"] = "uuid_" + hashlib.sha256(
            ("mask\0" + child_uuid).encode("utf-8")
        ).hexdigest()[:8]
        safe["uuid_verifier_present"] = bool(verifier)
        return child_uuid, safe

    def _log(self, event: str, operation: dict, reason: str | None = None) -> None:
        logger.info(
            "child_workflow event=%s operation_id=%s account_id=%s slot=%s generation=%s reason=%s",
            event, operation["operation_id"], operation["account_id"],
            operation["slot_number"], operation["generation"], reason or "-",
        )
