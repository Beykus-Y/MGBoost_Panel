#!/usr/bin/env python3
"""Dedicated PH3-03 worker process; disabled unless explicitly configured."""

import argparse
import json
import logging
import os
import socket
import time

from src.child_worker import ChildProvisioningWorker
from src.database import Database
from src.parent_sync import run_drift_audit_cycle, sweep_convergence
from src.service_marzban import ServiceMarzbanClient


logger = logging.getLogger(__name__)


def _enabled(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _parent_sync_scope() -> tuple[str, tuple[int, ...] | None]:
    """Fail closed for the newly wired reconciliation mechanism.

    The child-worker ``op_*`` allowlist and parent-sync ``sy_*`` operation
    IDs are different namespaces, so canary membership is explicitly account
    scoped instead of guessing a cross-workflow mapping.
    """
    mode = os.getenv("PARENT_SYNC_RECONCILIATION_MODE", "disabled").strip().lower()
    if mode == "disabled":
        return mode, ()
    if mode == "global":
        return mode, None
    if mode != "canary":
        logger.error("parent_sync_reconciliation_invalid_mode")
        return "disabled", ()
    try:
        account_ids = tuple(dict.fromkeys(
            int(value.strip()) for value in
            os.getenv("PARENT_SYNC_ALLOWED_ACCOUNT_IDS", "").split(",")
            if value.strip()
        ))
    except ValueError:
        logger.error("parent_sync_reconciliation_invalid_canary_scope")
        return "disabled", ()
    if not account_ids or any(account_id <= 0 for account_id in account_ids):
        logger.error("parent_sync_reconciliation_empty_canary_scope")
        return "disabled", ()
    return mode, account_ids


def build_worker():
    if not _enabled(os.getenv("CHILD_WORKER_ENABLED", "0")):
        raise RuntimeError("CHILD_WORKER_ENABLED is not enabled")
    allowed = [
        value.strip() for value in
        os.getenv("CHILD_WORKER_ALLOWED_OPERATION_IDS", "").split(",")
        if value.strip()
    ]
    worker_id = os.getenv(
        "CHILD_WORKER_ID", f"child-worker:{socket.gethostname()}:{os.getpid()}"
    )
    db = Database()
    marzban = ServiceMarzbanClient(
        broker_client_id=os.getenv("MARZBAN_BROKER_CLIENT_ID", "mgboost-main")
    )
    marzban.assert_credential_boundary()
    return db, ChildProvisioningWorker(
        db, marzban, worker_id=worker_id, allowed_operation_ids=allowed,
        mode=os.getenv("CHILD_WORKER_MODE", "reconcile_only"),
        max_attempts=int(os.getenv("CHILD_WORKER_MAX_ATTEMPTS", "8")),
        lease_seconds=int(os.getenv("CHILD_WORKER_LEASE_SECONDS", "30")),
        retry_base_seconds=int(os.getenv("CHILD_WORKER_RETRY_BASE_SECONDS", "5")),
        retry_cap_seconds=int(os.getenv("CHILD_WORKER_RETRY_CAP_SECONDS", "300")),
        reconcile_interval_seconds=int(
            os.getenv("CHILD_WORKER_RECONCILE_INTERVAL_SECONDS", "60")
        ),
    )


def run_reconciliation_tick(db, marzban, *, worker_id: str, now: int) -> dict:
    """One PH3-08 tick: durable entitlement convergence sweep (BUG G) first,
    so any account's desired state/revision is current, then the post-ACK
    stabilization + periodic drift audit (BUG A/A2/B) against that now-live
    revision. Each phase is independently try/excepted -- a broker/Marzban
    outage or an unexpected error on one account must never stop the other
    phase or kill the caller's loop; `sweep_convergence`/
    `run_drift_audit_cycle` already treat all of their DB state (sweep
    cursor, verify_after) as the durable due-work boundary, so a skipped
    tick here is simply retried on the next one."""
    mode, account_ids = _parent_sync_scope()
    summary = {"mode": mode, "sweep": None, "drift_audit": None}
    if mode == "disabled":
        return summary
    max_attempts = int(os.getenv("CHILD_WORKER_MAX_ATTEMPTS", "8"))
    retry_base_seconds = int(os.getenv("CHILD_WORKER_RETRY_BASE_SECONDS", "5"))
    retry_cap_seconds = int(os.getenv("CHILD_WORKER_RETRY_CAP_SECONDS", "300"))
    try:
        summary["sweep"] = sweep_convergence(
            db, sync_fn=marzban.sync_child_user_state, worker_id=worker_id,
            now=now, limit=int(os.getenv("PARENT_SYNC_SWEEP_LIMIT", "50")),
            sweep_interval_seconds=int(
                os.getenv("PARENT_SYNC_SWEEP_INTERVAL_SECONDS", "120")
            ),
            max_attempts=max_attempts, retry_base_seconds=retry_base_seconds,
            retry_cap_seconds=retry_cap_seconds, account_ids=account_ids,
        )
    except Exception:
        logger.exception("parent_sync_convergence_sweep_failed")
    try:
        summary["drift_audit"] = run_drift_audit_cycle(
            db, observe_fn=marzban.observe_child_user_state,
            sync_fn=marzban.sync_child_user_state, worker_id=worker_id,
            now=now, limit=int(os.getenv("PARENT_SYNC_DRIFT_AUDIT_LIMIT", "50")),
            audit_interval_seconds=int(
                os.getenv("PARENT_SYNC_DRIFT_AUDIT_INTERVAL_SECONDS", "300")
            ),
            max_attempts=max_attempts, retry_base_seconds=retry_base_seconds,
            retry_cap_seconds=retry_cap_seconds, account_ids=account_ids,
        )
    except Exception:
        logger.exception("parent_sync_drift_audit_failed")
    return summary


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=os.getenv("CHILD_WORKER_LOG_LEVEL", "INFO"))
    db, worker = build_worker()
    try:
        while True:
            summary = worker.run_once()
            reconciliation = run_reconciliation_tick(
                db, worker.marzban, worker_id=worker.worker_id, now=int(time.time()),
            )
            if args.json:
                print(json.dumps(summary, sort_keys=True))
                print(json.dumps(reconciliation, sort_keys=True))
            else:
                logging.getLogger(__name__).info(
                    "child_worker_cycle examined=%d reconciled=%d provisioned=%d "
                    "retried=%d manual_review=%d pending=%d divergence=%d",
                    summary["examined"], summary["reconciled"],
                    summary["provisioned"], summary["retried"],
                    summary["manual_review"],
                    summary["metrics"]["pending_outbox_count"],
                    summary["metrics"]["desired_observed_divergence_count"],
                )
                if reconciliation["sweep"] is not None or reconciliation["drift_audit"] is not None:
                    logger.info("parent_sync_reconciliation_tick %s", json.dumps(reconciliation, sort_keys=True))
            if args.once:
                break
            time.sleep(max(5, int(os.getenv("CHILD_WORKER_POLL_SECONDS", "15"))))
    finally:
        db._conn.close()


if __name__ == "__main__":
    main()
