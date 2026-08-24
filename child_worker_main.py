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
from src.service_marzban import ServiceMarzbanClient


def _enabled(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


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
            if args.json:
                print(json.dumps(summary, sort_keys=True))
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
            if args.once:
                break
            time.sleep(max(5, int(os.getenv("CHILD_WORKER_POLL_SECONDS", "15"))))
    finally:
        db._conn.close()


if __name__ == "__main__":
    main()
