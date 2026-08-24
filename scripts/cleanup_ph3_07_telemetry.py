#!/usr/bin/env python3
"""Apply PH3-07 retention without reading or emitting identifiers."""

from __future__ import annotations

import argparse
import json
import time

from src.compat_telemetry import cleanup_expired


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--now", type=int, default=int(time.time()))
    args = parser.parse_args()
    result = cleanup_expired(args.db, now=args.now)
    result["raw_identifiers_emitted"] = False
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
