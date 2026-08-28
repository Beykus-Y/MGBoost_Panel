"""PH6-09 -- durable version registry for the PH0-05 WL topology.

`WLTopologyGuardStore.run_assertion` records every positively-asserted
config_version here together with that version's exact `WL_INBOUND_TAGS`
set. Two pure reads on top:

- `tags_added_since(conn, current_tags, from_version)`: the exact set of
  tags an operator-approved versioned baseline update added after
  `from_version`. This is the ONLY data the DL-059 auto-add path may add
  to an entitled ACTIVE child -- never "the whole allowlist", never a
  fuzzy guess.
- Fail closed by construction: an unknown `from_version` (a manifest
  frozen before this registry existed, or a version this deployment never
  positively asserted) yields the empty set -- nothing is ever auto-added
  on the basis of unverifiable history.
"""

from __future__ import annotations

import json
import sqlite3
import time


def record_topology_version(connection: sqlite3.Connection, *, config_version: str,
                            wl_tags, now: int | None = None) -> bool:
    """Insert-if-absent one (config_version -> exact tag set) row. Called by
    `run_assertion` after a positive assertion only; the same version always
    names the same tag set, so a replay is a no-op. Tolerates a database
    that predates the PH6-09 registry (never blocks the PH6-01 contract)."""
    timestamp = int(time.time()) if now is None else int(now)
    try:
        cursor = connection.execute(
            "INSERT OR IGNORE INTO mgboost_wl_topology_versions "
            "(config_version,wl_tags_json,first_seen_at) VALUES (?,?,?)",
            (str(config_version), json.dumps(sorted(set(wl_tags))), timestamp),
        )
        connection.commit()
        return cursor.rowcount == 1
    except sqlite3.OperationalError:
        return False  # registry schema not applied -- auto-add stays conservatively disabled


def tags_added_since(connection: sqlite3.Connection, current_tags,
                     from_version: str | None) -> frozenset:
    """Exact tags added by approved updates after `from_version`, or an
    empty set when `from_version` is unknown/absent (fail closed)."""
    if not from_version:
        return frozenset()
    row = connection.execute(
        "SELECT wl_tags_json FROM mgboost_wl_topology_versions WHERE config_version=?",
        (str(from_version),),
    ).fetchone()
    if row is None:
        return frozenset()
    previous = set(json.loads(row["wl_tags_json"]))
    return frozenset(set(current_tags) - previous)
