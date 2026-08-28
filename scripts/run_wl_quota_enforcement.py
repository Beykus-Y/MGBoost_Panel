#!/usr/bin/env python3
"""PH6-06 WL quota enforcement -- one on-demand, gated run.

Dormant, operator-driven tool (matching the PH6-03 collector precedent):
nothing schedules this automatically. Each invocation first refreshes the
PH6-01 topology assertion and fails closed on any mismatch/unreachability
(zero transitions minted), then derives per-account desired WL state from
the PH6-04 read model over the PH6-03 ledger and drives the exact
inbound-only state machine (`src/wl_enforcement.py`).

Remote mutation is limited to `inbounds.vless` of children whose account is
inside an exhausted LIMITED WL period; UUID/proxies/expire/data_limit and
all non-WL inbounds are never touched; Non-WL/UNLIMITED accounts are
structurally abstained from.

Prints only a safe, aggregate JSON summary: account/op counts, outcome and
error *classes* -- never usernames, tokens or raw identifiers.

Usage:

    python -m scripts.run_wl_quota_enforcement --db <path-to-db.sqlite3> \
        [--trigger SCHEDULED|MANUAL] [--worker-id host-1] [--now EPOCH]

PH6-07: this is the ONE entry point for both the systemd timer
(`--trigger SCHEDULED`, see `mgboost-wl-enforcement.timer`) and manual
operator runs (`--trigger MANUAL`). Both go through the same orchestrated
cycle (`src.wl_reconciliation.run_wl_reconciliation_cycle`): process-wide
non-blocking cycle lock (overlap refused, never queued), the existing
fail-closed engine cycle, the post-terminal drift scan and the identifier-
free cycle records. No secrets appear in argv, logs or the summary.
"""

from __future__ import annotations

import argparse
import json
import os
import time


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--trigger", choices=("SCHEDULED", "MANUAL"), default="MANUAL")
    parser.add_argument("--worker-id", default=None)
    parser.add_argument("--lock-file", default=None)
    parser.add_argument("--now", type=int, default=int(time.time()))
    args = parser.parse_args()

    import socket

    worker_id = args.worker_id or f"{socket.gethostname()}:{os.getpid()}"
    lock_file = args.lock_file or os.path.join(
        os.path.dirname(os.path.abspath(args.db)), "wl-enforcement-cycle.lock",
    )

    import src.database as database_module
    from src.service_marzban import ServiceMarzbanClient
    from src.wl_reconciliation import run_wl_reconciliation_cycle

    database_module.DB_PATH = args.db
    db = database_module.Database()
    service_marzban = ServiceMarzbanClient()
    try:
        summary = run_wl_reconciliation_cycle(
            db=db, service_marzban=service_marzban, worker_id=worker_id,
            now=args.now, trigger=args.trigger, lock_file=lock_file,
        )
    finally:
        db._conn.close()

    print(json.dumps(summary, sort_keys=True, indent=2, default=str))


if __name__ == "__main__":
    main()
