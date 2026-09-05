#!/usr/bin/env python3
"""Read-only local bootstrap detector; never initialize/migrate the database."""
import argparse
import json
import os
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.bootstrap_retirement import preview


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', required=True)
    parser.add_argument('--account-id', type=int, action='append')
    args = parser.parse_args()
    key = os.getenv('DEVICE_SLOT_HMAC_KEY', '')
    if not key:
        parser.error('DEVICE_SLOT_HMAC_KEY must be configured')
    with sqlite3.connect(Path(args.database).resolve().as_uri() + '?mode=ro', uri=True) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA query_only=ON')
        print(json.dumps(preview(connection, hmac_key=key, account_ids=args.account_id), indent=2))


if __name__ == '__main__':
    main()
