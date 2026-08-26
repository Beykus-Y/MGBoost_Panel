"""Explicit, idempotent PH5-03 package catalog seed; never starts sales."""
from __future__ import annotations
import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.database import Database
from src.wl_package_catalog import seed_wl_package_catalog


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", help="explicit SQLite database path")
    parser.add_argument("--now", type=int)
    args = parser.parse_args()
    if args.db:
        import src.database as db_mod
        db_mod.DB_PATH = args.db
    db = Database()
    try:
        result = seed_wl_package_catalog(db.wl_package_catalog, now=args.now)
        print(json.dumps({"products": sorted(result["products"]), "prices_newly_created": result["prices_newly_created"]}, sort_keys=True))
    finally:
        db._conn.close()


if __name__ == "__main__":
    main()
