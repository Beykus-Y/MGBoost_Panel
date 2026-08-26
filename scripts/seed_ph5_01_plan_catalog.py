#!/usr/bin/env python3
"""PH5-01 catalog seeding -- idempotent, explicit, no automatic startup wiring.

Creates the six approved plan versions (`ROADMAP.md` "Approved product
catalog"), their 30/60-day durations, and both channels' active price
catalogs (`TELEGRAM_STARS` = `STARS-2026-08-26-v1`, `RUB` =
`RUB-2026-08-23-v1`, DL-040). Re-running this script against an
already-seeded database is a no-op: every write is get-or-create keyed on
the exact same catalog data, never a blind INSERT.

Nothing in the legacy request, Stars, LK, Filin or Marzban paths reads these
tables yet -- this only populates the dormant PH5-01 schema so a later
purchase-flow phase (PH5-04/05) has real rows to read.

Usage:

    python -m scripts.seed_ph5_01_plan_catalog --db <path-to-db.sqlite3> [--now EPOCH]
"""

from __future__ import annotations

import argparse
import json
import sys
import time

sys.path.insert(0, ".")

from src.plan_catalog import seed_plan_catalog  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--now", type=int, default=int(time.time()))
    args = parser.parse_args()

    import src.database as database_module

    database_module.DB_PATH = args.db
    db = database_module.Database()
    try:
        result = seed_plan_catalog(db.plan_catalog, now=args.now)
    finally:
        db._conn.close()

    summary = {
        "plan_codes": sorted(result["plan_versions"]),
        "durations_per_plan": sorted({days for (_, days) in result["durations"]}),
        "catalog_versions": {
            channel: row["catalog_version"]
            for channel, row in result["catalog_versions"].items()
        },
        "prices_newly_created": result["prices_newly_created"],
    }
    print(json.dumps(summary, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
