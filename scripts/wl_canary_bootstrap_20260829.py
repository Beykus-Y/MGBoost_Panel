#!/usr/bin/env python3
"""production-wl-limited-canary-2026-08-29: issue the opaque subscription and
bootstrap the FIRST real device for the WL canary account created by
`wl_admin_grant_canary_20260829.py`, through the exact same production paths
a real customer/VPN client uses -- no raw Marzban provisioning, no raw SQL.

Hardcoded to exactly account_id=20 (asserted against the live DB: public_id,
telegram owner 8703542062, WL/LIMITED subscription, zero existing device
slots -- refuses to run against anything else).

Two canonical steps, both real production paths:
1. `subscription_credentials.prepare`/`activate` (PH2-01/PH4-04) -- the exact
   primitive the bot's `/newsub` flow uses (`bot_support.py::
   _issue_new_credential`/`_activate_new_credential`) to mint the opaque
   token. The raw token exists ONLY in this process's memory for the
   duration of one HTTPS request; it is never logged, printed or persisted
   by this script.
2. A real HTTPS GET against `https://sub.beykus.fun/<token>` with a
   device-shaped User-Agent + X-HWID header (never a browser UA) -- the same
   external endpoint and the same lazy-provisioning resolver
   (`opaque_resolver.resolve_opaque_subscription` -> PH3-03
   `ensure_child_user` via the real Marzban broker) a real VPN client
   triggers on first launch.

After bootstrap, read-only verification only: device slot #1/generation #1
claimed, the child's Marzban record fetched through the same narrow broker
(`ServiceMarzbanClient`, PH1-05 allowlist) real production code already
uses elsewhere. Never prints the raw UUID/token/bearer -- only booleans,
counts, and non-secret topology (inbound tag names, which already appear
in ROADMAP.md/AGENT_HANDOFF.md in the open).

Usage: python3 scripts/wl_canary_bootstrap_20260829.py [--dry-run]
"""

from __future__ import annotations

import argparse
import ssl
import time
import urllib.request

from src.child_contract import derive_child_username
from src.config import PRIMARY_MGBOOST_ADMIN_LOGIN
from src.database import Database
from src.service_marzban import ServiceMarzbanClient
from src.subscription_credentials import SubscriptionCredentialError

DECISION_REF = "production-wl-limited-canary-2026-08-29-bootstrap"
EXPECTED_ACCOUNT_ID = 20
EXPECTED_PUBLIC_ID = "acct_51iMmDClJ-qPxGAIzSBFRv9k"
EXPECTED_TELEGRAM_ID = 8703542062
SUB_HOST = "sub.beykus.fun"
DEVICE_USER_AGENT = "Happ/2.1 (Android 14; canary-wl-8703542062)"
DEVICE_HWID = "canary-wl-8703542062-device1"


class PreflightFailed(RuntimeError):
    pass


def _preflight(db: Database) -> dict:
    conn = db._conn
    account = conn.execute(
        "SELECT * FROM mgboost_accounts WHERE id=?", (EXPECTED_ACCOUNT_ID,),
    ).fetchone()
    if account is None:
        raise PreflightFailed("account not found")
    if account["public_id"] != EXPECTED_PUBLIC_ID:
        raise PreflightFailed("public_id mismatch -- refusing")
    if account["status"] != "ACTIVE":
        raise PreflightFailed(f"unexpected account status {account['status']!r}")

    identity = conn.execute(
        "SELECT telegram_id FROM mgboost_telegram_identities "
        "WHERE account_id=? AND role='OWNER' AND revoked_at IS NULL",
        (EXPECTED_ACCOUNT_ID,),
    ).fetchone()
    if identity is None or identity["telegram_id"] != EXPECTED_TELEGRAM_ID:
        raise PreflightFailed("telegram owner mismatch -- refusing")

    term = conn.execute(
        "SELECT wl_mode_snapshot FROM mgboost_subscription_terms "
        "WHERE account_id=? ORDER BY sequence_no DESC LIMIT 1",
        (EXPECTED_ACCOUNT_ID,),
    ).fetchone()
    if term is None or term["wl_mode_snapshot"] != "LIMITED":
        raise PreflightFailed("account is not on a LIMITED WL subscription -- refusing")

    existing_slots = conn.execute(
        "SELECT COUNT(*) FROM mgboost_device_slot_generations WHERE account_id=?",
        (EXPECTED_ACCOUNT_ID,),
    ).fetchone()[0]
    if existing_slots != 0:
        raise PreflightFailed(
            f"account already has {existing_slots} device-slot generation row(s) "
            "-- this is not a first-device bootstrap, refusing"
        )

    existing_credentials = conn.execute(
        "SELECT COUNT(*) FROM mgboost_subscription_credentials WHERE account_id=?",
        (EXPECTED_ACCOUNT_ID,),
    ).fetchone()[0]
    if existing_credentials != 0:
        raise PreflightFailed(
            f"account already has {existing_credentials} subscription credential row(s) "
            "-- refusing to issue a second one"
        )

    return {"public_id": account["public_id"]}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    db = Database()
    plan = _preflight(db)
    print("PREFLIGHT OK: account_id=%d public_id=%s, WL/LIMITED confirmed, "
          "zero existing slots/credentials" % (EXPECTED_ACCOUNT_ID, plan["public_id"]))
    if args.dry_run:
        print("--dry-run: no writes / no HTTP request performed.")
        return

    timestamp = int(time.time())
    actor_ref = f"system:{PRIMARY_MGBOOST_ADMIN_LOGIN}:canary-bootstrap"
    op_key = f"{EXPECTED_ACCOUNT_ID}:{timestamp}:canary-bootstrap-v1"

    try:
        prepared = db.subscription_credentials.prepare(
            account_id=EXPECTED_ACCOUNT_ID, actor_ref=actor_ref, reason=DECISION_REF,
            idempotency_key=f"canary-bootstrap-prepare-v1:{op_key}", now=timestamp,
        )
    except SubscriptionCredentialError as exc:
        raise RuntimeError(f"prepare refused: {exc}") from exc

    raw_token = prepared["raw_token"]
    print("CREDENTIAL PREPARED: credential_id=%s generation=%s (opaque; token/bearer never printed)"
          % (prepared["id"], prepared["generation"]))

    # `resolve()` only ever matches status='ACTIVE' -- activation must
    # happen BEFORE the client's first HTTP hit, exactly like the real bot
    # flow activates before the customer can actually open the link.
    activated = db.subscription_credentials.activate(
        credential_id=prepared["id"], account_id=EXPECTED_ACCOUNT_ID,
        expected_generation=prepared["generation"], actor_ref=actor_ref,
        idempotency_key=f"canary-bootstrap-activate-v1:{op_key}", now=timestamp,
    )
    print("CREDENTIAL ACTIVATED: already_applied=%s" % activated.get("already_applied"))

    url = f"https://{SUB_HOST}/{raw_token}"
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": DEVICE_USER_AGENT,
            "X-HWID": DEVICE_HWID,
            "X-Device-Name": "canary-test-device",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20, context=ssl.create_default_context()) as resp:
            status = resp.status
            body_len = len(resp.read())
    except urllib.error.HTTPError as exc:
        status = exc.code
        body_len = len(exc.read() or b"")
    finally:
        raw_token = None
        del prepared

    print("BOOTSTRAP HTTP GET: status=%d body_len=%d" % (status, body_len))

    slot = db._conn.execute(
        "SELECT g.slot_number,g.generation,g.status,g.hwid_masked,g.claimed_at "
        "FROM mgboost_device_slot_generations g WHERE g.account_id=? AND g.status='ACTIVE'",
        (EXPECTED_ACCOUNT_ID,),
    ).fetchone()
    if slot is None:
        raise RuntimeError("bootstrap HTTP request did not result in an ACTIVE device slot")
    print(
        "SLOT: slot_number=%d generation=%d status=%s hwid_masked=%s"
        % (slot["slot_number"], slot["generation"], slot["status"], slot["hwid_masked"])
    )

    child_username = derive_child_username(plan["public_id"], slot["slot_number"], slot["generation"])
    template_username = f"tpl-{plan['public_id']}"

    marzban = ServiceMarzbanClient()
    admin_token = marzban.get_admin_token_from_env()
    child_user = marzban.get_user(child_username, admin_token)
    template_user = marzban.get_user(template_username, admin_token)

    child_uuid = (child_user.get("proxies") or {}).get("vless", {}).get("id")
    template_uuid = (template_user.get("proxies") or {}).get("vless", {}).get("id")
    child_inbounds = sorted((child_user.get("inbounds") or {}).get("vless") or [])
    template_inbounds = sorted((template_user.get("inbounds") or {}).get("vless") or [])

    print(
        "CHILD MARZBAN RECORD: username=%s status=%s expire=%s "
        "child_uuid_differs_from_template=%s"
        % (
            child_username, child_user.get("status"), child_user.get("expire"),
            bool(child_uuid) and bool(template_uuid) and child_uuid != template_uuid,
        )
    )
    print("CHILD INBOUND TAGS (vless):", child_inbounds)
    print("TEMPLATE INBOUND TAGS (vless, infra-only, for delivery diff only):", template_inbounds)


if __name__ == "__main__":
    main()
