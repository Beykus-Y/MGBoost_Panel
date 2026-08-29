#!/usr/bin/env python3
"""production-wl-limited-canary-2026-08-29: create exactly ONE controlled,
no-payment WL canary account via the reviewed ADMIN_GRANT backend primitive
(`src/admin_grant.py::AdminGrantStore`), per explicit owner authorization.

Owner-confirmed controlled test identity: telegram_id=8703542062 (owner's
own controlled test Telegram account -- NOT a real customer, NOT rebound
from any existing account).

No configurable target. Hardcoded to exactly this one Telegram id and this
one exact commercial product (WL / 30 days), and refuses to run if that
identity is already linked to anything (any role, any status) -- fails
closed rather than reusing/rebinding an unexpected existing identity.

The only mutation performed is `AdminGrantStore.grant_new_account`, which
itself only ever calls the existing canonical `apply_same_plan_purchase`
engine (PH5-02) plus `AccountStore.create_account`/`link_telegram_owner`
(PH3-01/PH2-05) -- the same primitives real signup/Stars/manual-payment
flows use. No raw SQL write anywhere in this file. Zero financial rows are
created (payment_channel=ADMIN_GRANT, mutation_source=ADMIN); this is not
a Telegram Stars purchase and creates no invoice/charge history.

Real Marzban credentials are used only to mint a genuine
PrimaryAdminCapability (the same pattern `dl057_megochel_consolidation.py`,
`run_ph3_03_production_canary.py` and
`support_goodwill_extend_5d_20260828.py` use); no raw credential/token/UUID
is ever printed. Only safe internal DB ids / public_id are printed --
never the opaque subscription token/bearer/UUID.

Usage: python3 scripts/wl_admin_grant_canary_20260829.py [--dry-run]
`--dry-run` runs every preflight assertion and prints the exact plan, but
performs zero writes.
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

from dotenv import dotenv_values

from src.admin_authority import PrimaryAdminAuthorizationError
from src.admin_grant import AdminGrantError
from src.config import MARZBAN_URL, PRIMARY_MGBOOST_ADMIN_LOGIN
from src.database import Database
from src.marzban import MarzbanClient
from src.security import AdminSessionStore

DECISION_REF = "production-wl-limited-canary-2026-08-29"
TARGET_TELEGRAM_ID = 8703542062
PLAN_CODE = "WL"
DURATION_DAYS = 30
EXPECTED_DEVICE_LIMIT = 3
EXPECTED_WL_MODE = "LIMITED"
EXPECTED_QUOTA_BYTES = 100_000_000_000
EXPECTED_PERIOD_DAYS = 30
IDEMPOTENCY_KEY = "admin-grant-prod-wl-canary-8703542062-2026-08-29-v1"


class PreflightFailed(RuntimeError):
    pass


def _protected_auth(path: Path) -> tuple[str, str]:
    import os
    import stat

    info = path.stat()
    if info.st_uid != os.geteuid() or info.st_mode & (
        stat.S_IWGRP | stat.S_IXGRP | stat.S_IRWXO
    ):
        raise PermissionError("broker auth environment is not sufficiently protected")
    values = dotenv_values(path)
    username = str(values.get("MARZBAN_ADMIN_USER") or "").strip()
    password = str(values.get("MARZBAN_ADMIN_PASS") or "")
    if not username or not password:
        raise RuntimeError("protected admin authentication is incomplete")
    return username, password


def _real_primary_capability(db: Database, auth_path: Path):
    username, password = _protected_auth(auth_path)
    if username != PRIMARY_MGBOOST_ADMIN_LOGIN:
        raise RuntimeError("protected admin login differs from primary mapping")
    token = MarzbanClient(MARZBAN_URL).get_token(username, password)
    password = None
    if not token:
        raise RuntimeError("real Marzban primary-admin authentication failed")
    store = AdminSessionStore()
    raw_session_id, session = store.create(username, token)
    token = None
    try:
        capability = db.primary_admin_authority.authorize_session(session)
        try:
            db.primary_admin_authority.authorize_session(object())
        except PrimaryAdminAuthorizationError:
            forged_rejected = True
        else:
            forged_rejected = False
        if not forged_rejected:
            raise RuntimeError("forged primary-admin session was accepted")
        return capability, store, raw_session_id
    except Exception:
        store.revoke(raw_session_id)
        raise


def _preflight(db: Database) -> dict:
    conn = db._conn
    identity_rows = conn.execute(
        "SELECT COUNT(*) FROM mgboost_telegram_identities WHERE telegram_id=?",
        (TARGET_TELEGRAM_ID,),
    ).fetchone()[0]
    if identity_rows != 0:
        raise PreflightFailed(
            f"telegram_id={TARGET_TELEGRAM_ID} already has {identity_rows} "
            "identity row(s) -- refusing to reuse/rebind an existing identity"
        )
    legacy_rows = conn.execute(
        "SELECT COUNT(*) FROM tg_users WHERE telegram_id=?", (TARGET_TELEGRAM_ID,),
    ).fetchone()[0]
    if legacy_rows != 0:
        raise PreflightFailed(
            f"telegram_id={TARGET_TELEGRAM_ID} has {legacy_rows} legacy tg_users "
            "row(s) -- ownership ambiguity, refusing"
        )

    plan = db.plan_catalog.get_plan_version(PLAN_CODE)
    if plan is None:
        raise PreflightFailed(f"plan {PLAN_CODE!r} not found in catalog")
    if plan["wl_mode"] != EXPECTED_WL_MODE or plan["wl_quota_bytes"] != EXPECTED_QUOTA_BYTES:
        raise PreflightFailed("plan WL terms do not match expected canary terms")
    if plan["device_limit"] != EXPECTED_DEVICE_LIMIT:
        raise PreflightFailed("plan device_limit does not match expected canary terms")
    if plan["wl_period_days"] != EXPECTED_PERIOD_DAYS:
        raise PreflightFailed("plan wl_period_days does not match expected canary terms")
    duration = db.plan_catalog.get_plan_duration(plan["id"], DURATION_DAYS)
    if duration is None:
        raise PreflightFailed(f"plan {PLAN_CODE!r} has no {DURATION_DAYS}-day duration")

    return {"plan_id": plan["id"], "plan_version": plan["version"]}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--auth-path", type=Path, default=Path("/etc/mgboost/marzban-broker.env"),
    )
    args = parser.parse_args()

    db = Database()
    plan = _preflight(db)
    print(
        "PREFLIGHT OK: telegram_id=%d is free (0 identity rows, 0 legacy rows); "
        "plan_code=%s plan_version=%d matches expected WL 30d/D3/100GB terms"
        % (TARGET_TELEGRAM_ID, PLAN_CODE, plan["plan_version"])
    )
    if args.dry_run:
        print("--dry-run: no writes performed.")
        return

    capability, session_store, session_id = _real_primary_capability(db, args.auth_path)
    try:
        try:
            result = db.admin_grants.grant_new_account(
                capability,
                telegram_id=TARGET_TELEGRAM_ID,
                plan_code=PLAN_CODE,
                duration_days=DURATION_DAYS,
                reason=DECISION_REF,
                idempotency_key=IDEMPOTENCY_KEY,
                now=int(time.time()),
            )
        except AdminGrantError as exc:
            raise RuntimeError(f"grant_new_account refused: {exc}") from exc

        print(
            "GRANT %s: account_id=%s account_public_id=%s subscription_id=%s "
            "new_expiry=%s wl_periods=%s"
            % (
                "REPLAYED" if result.get("already_applied") else "OK",
                result.get("account_id"), result.get("account_public_id"),
                result.get("subscription_id"), result.get("new_expiry"),
                result.get("wl_periods"),
            )
        )

        entitlement = db.entitlements.calculate(
            account_id=result["account_id"], now=int(time.time()),
        )
        subscription_effect = entitlement.get("subscription", {}) if isinstance(entitlement, dict) else {}
        print(
            "ENTITLEMENT: plan=%s effective_status=%s wl_mode=%s"
            % (
                entitlement.get("plan", {}).get("code"),
                subscription_effect.get("effective_status"),
                entitlement.get("plan", {}).get("wl_mode"),
            )
        )
    finally:
        session_store.revoke(session_id)


if __name__ == "__main__":
    main()
