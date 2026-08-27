#!/usr/bin/env python3
"""PH5-12 idempotent delivery-routing bootstrap.

Creates the STANDARD delivery profile shell, the plan -> profile mapping for
the three first-rollout sellable plans, and -- only with
``--seed-verified-baseline`` -- the STANDARD host membership.

The membership baseline is NEVER a hardcoded tag list. It is derived, every
time this script runs, from a FRESH live Marzban topology observation
(same broker path every admin route uses) that must first pass
``require_topology_ok()`` (exact PH0-05 allowlist/version match) --
otherwise the script exits non-zero without touching the database. The
seeded set is exactly "every live inbound tag `classify_inbound_tag()`
calls STANDARD" (i.e. live minus exact-WL minus wl-shaped-unverified) at
run time, so a host added or removed on the real server after this script
was written changes what gets seeded on the next run -- there is no
"STANDARD is these N tags because that's what live topology looked like on
some past date" constant anywhere in this file.

Safe to run repeatedly: existing rows are reused, never duplicated; every
seeded membership tag gets its own audited HOST_ADDED event (SYSTEM actor)
with a deterministic idempotency key, so re-runs replay instead of
duplicating.

This script never mutates anything remote (Marzban reads only, via the
existing read-only get_nodes/get_inbounds broker calls); the only writes
are to the local `mgboost_delivery_profile*` tables.
"""

from __future__ import annotations

import argparse
import sys

sys.path.insert(0, ".")

from src.database import Database
from src.delivery_routing import STANDARD_PROFILE_CODE, classify_inbound_tag
from src.routes.admin_support import service_marzban
from src.wl_topology_guard import fetch_live_topology_observation


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--seed-verified-baseline", action="store_true",
        help="also seed STANDARD membership from a fresh live topology "
             "observation (every currently-live non-WL inbound tag)",
    )
    args = parser.parse_args()

    db = Database()
    db.delivery_routing.ensure_defaults()

    if not args.seed_verified_baseline:
        print("profile shell + plan mapping ensured (membership untouched)")
        return 0

    client = service_marzban()
    try:
        observed_tags, observed_nodes = fetch_live_topology_observation(client, None)
        db.wl_topology_guard.run_assertion(observed_tags, observed_nodes)
        db.wl_topology_guard.require_topology_ok()
    except Exception as exc:
        print(f"FATAL: live topology assertion failed, no mutation performed: {exc}",
              file=sys.stderr)
        return 1

    baseline = sorted(
        tag for tag in observed_tags if classify_inbound_tag(tag) == "STANDARD"
    )
    if not baseline:
        print("FATAL: fresh topology observation yields zero STANDARD-classified "
              "hosts; refusing to seed an empty baseline", file=sys.stderr)
        return 1
    print(f"live_verified_baseline={baseline}")

    added, replayed = [], []
    for tag in baseline:
        idem_key = f"ph5-12-seed-standard-v1:{tag}"
        existing = db._conn.execute(
            "SELECT 1 FROM mgboost_delivery_profile_hosts h "
            "JOIN mgboost_delivery_profiles p ON p.id=h.profile_id "
            "WHERE p.profile_code=? AND h.inbound_tag=?",
            (STANDARD_PROFILE_CODE, tag),
        ).fetchone()
        result = db.delivery_routing.apply_host_change(
            None, profile_code=STANDARD_PROFILE_CODE, inbound_tag=tag,
            operation="ADD", reason="seed: fresh live-topology-verified non-WL baseline",
            idempotency_key=idem_key, observed_live_tags=observed_tags,
            system_actor=True,
        )
        if existing is not None:
            continue
        (replayed if result.get("already_applied") else added).append(tag)

    members = db.delivery_routing.membership(STANDARD_PROFILE_CODE)
    print(f"seeded_now={added} replayed={replayed}")
    print(f"standard_membership_count={len(members)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
