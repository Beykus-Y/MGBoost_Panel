#!/usr/bin/env python3
"""PH5-13 promo: register the WL_TRIAL plan_version -- idempotent, explicit,
no automatic startup wiring (same convention as
`seed_ph5_01_plan_catalog.py`).

Creates the single `mgboost_plan_versions` row that legitimizes TRIAL_GRANT
promo redemptions (`billing_required=0` -- structurally unsellable through
Stars/manual-RUB, same principle that already protects `WL_PACKAGE_*`).
Re-running this script is a no-op if the row already exists.

Usage:

    python -m scripts.seed_promo_wl_trial_plan --db <path-to-db.sqlite3> [--now EPOCH]
"""

from __future__ import annotations

import argparse
import json
import sys
import time

sys.path.insert(0, ".")

from src.promo import ensure_wl_trial_plan_version  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--now", type=int, default=int(time.time()))
    args = parser.parse_args()

    import src.database as database_module

    database_module.DB_PATH = args.db
    db = database_module.Database()
    try:
        plan_version = ensure_wl_trial_plan_version(db.accounts, now=args.now)
    finally:
        db._conn.close()

    print(json.dumps({
        "plan_code": plan_version["plan_code"], "version": plan_version["version"],
        "plan_version_id": plan_version["id"], "billing_required": bool(plan_version["billing_required"]),
        "device_limit": plan_version["device_limit"], "wl_quota_bytes": plan_version["wl_quota_bytes"],
    }, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
