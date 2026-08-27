#!/usr/bin/env python3
"""PH5-12 idempotent delivery-routing bootstrap.

Creates the STANDARD delivery profile shell, the plan -> profile mapping for
the three first-rollout sellable plans, and -- only with
``--seed-verified-baseline`` -- the STANDARD host membership, using the
EXACT non-WL inbound tag set verified read-only against live production
Marzban on 2026-08-27 (every tag confirmed present in ``GET /api/inbounds``
and absent from the PH0-05 WL allowlist). Without the flag, membership is
left empty and the admin adds hosts through the panel UI.

Safe to run repeatedly: existing rows are reused, never duplicated; every
seeded membership tag gets its own audited HOST_ADDED event (SYSTEM actor)
with a deterministic idempotency key, so re-runs replay instead of
duplicating.

This script never talks to Marzban and never mutates anything remote.
"""

from __future__ import annotations

import argparse
import sys

sys.path.insert(0, ".")

from src.database import Database
from src.delivery_routing import STANDARD_PROFILE_CODE
from src.wl_topology import WL_INBOUND_TAGS

# Verified live 2026-08-27 (read-only production preflight): the exact set
# of live inbound tags minus the exact PH0-05 WL allowlist.
VERIFIED_STANDARD_BASELINE = (
    "de-grpc-smart",
    "de-tcp-smart",
    "grpc-direct",
    "grpc-smart",
    "nl-grpc-smart",
    "nl-tcp-smart",
    "tcp-direct",
    "tcp-smart",
    "vless-grpc-cdn",
    "vless-ws-cdn",
    "vless-xhttp-cdn",
    "xhttp-direct",
    "xhttp-smart",
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--seed-verified-baseline", action="store_true",
        help="also seed STANDARD membership with the 2026-08-27 verified "
             "live non-WL inbound tag set",
    )
    args = parser.parse_args()

    db = Database()
    db.delivery_routing.ensure_defaults()

    if not args.seed_verified_baseline:
        print("profile shell + plan mapping ensured (membership untouched)")
        return 0

    if any(tag in WL_INBOUND_TAGS for tag in VERIFIED_STANDARD_BASELINE):
        print("FATAL: verified baseline unexpectedly contains an exact WL tag", file=sys.stderr)
        return 1

    added, replayed = [], []
    for tag in VERIFIED_STANDARD_BASELINE:
        idem_key = f"ph5-12-seed-standard-v1:{tag}"
        existing = db._conn.execute(
            "SELECT 1 FROM mgboost_delivery_profile_hosts h "
            "JOIN mgboost_delivery_profiles p ON p.id=h.profile_id "
            "WHERE p.profile_code=? AND h.inbound_tag=?",
            (STANDARD_PROFILE_CODE, tag),
        ).fetchone()
        result = db.delivery_routing.apply_host_change(
            None, profile_code=STANDARD_PROFILE_CODE, inbound_tag=tag,
            operation="ADD", reason="seed: 2026-08-27 verified live non-WL baseline",
            idempotency_key=idem_key, observed_live_tags=VERIFIED_STANDARD_BASELINE,
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
