#!/usr/bin/env python3
"""PH6-03 usage-ledger collector -- one on-demand, observe-only run.

Never mutates Marzban state, never touches
`mgboost_wl_periods`/subscriptions/entitlements/inbounds, never disables or
resets anyone. It only reads already-existing, already-safe usage endpoints
(through the same `ServiceMarzbanClient` broker path every other read-only
usage caller in this codebase already uses) and durably records what it
observed into the new, additive PH6-03 ledger tables. Nothing schedules
this automatically yet -- matching the PH6-01/02 "dormant, on-demand
library call" precedent -- run it by hand or wire it to a timer/cron once
its output has been reviewed for at least one full cycle.

Prints only a safe, aggregate JSON summary: child/sample counts, resets
detected, error *classes* (never messages, never raw usernames/tokens).

Usage:

    python -m scripts.run_wl_usage_collector --db <path-to-db.sqlite3> \
        [--worker-id host-1] [--now EPOCH]
"""

from __future__ import annotations

import argparse
import json
import time


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--worker-id", default=None)
    parser.add_argument("--now", type=int, default=int(time.time()))
    parser.add_argument("--lease-seconds", type=int, default=300)
    args = parser.parse_args()

    import socket
    import os

    worker_id = args.worker_id or f"{socket.gethostname()}:{os.getpid()}"

    import src.database as database_module
    from src.service_marzban import ServiceMarzbanClient
    from src.wl_usage_ledger import run_collection_cycle

    database_module.DB_PATH = args.db
    db = database_module.Database()
    service_marzban = ServiceMarzbanClient()
    try:
        summary = run_collection_cycle(
            db=db, service_marzban=service_marzban, worker_id=worker_id,
            now=args.now, lease_seconds=args.lease_seconds,
        )
    finally:
        db._conn.close()

    print(json.dumps(summary, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
