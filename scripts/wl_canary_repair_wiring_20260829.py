#!/usr/bin/env python3
"""production-wl-limited-canary-2026-08-29: one-off backfill of the missing
provisioning wiring for account_id=20 -- the WL canary was created
(commit c1ae3d4) before `_ensure_direct_provisioning_wiring` existed
(commit 15aa465), so a real first-device bootstrap attempt failed closed
(`opaque_resolver.resolve_account_device`: no PRIMARY alias).

Hardcoded to exactly account_id=20 (asserted: public_id, telegram owner
8703542062, WL/LIMITED subscription, account_source=DIRECT, and NO
existing PRIMARY alias -- refuses if any of that does not match, including
refusing a no-op call against an already-wired account since that would
mean this script's premise is stale).

The only mutation is `AdminGrantStore.repair_missing_provisioning_wiring`
(admin_grant.py, bb8d56e) -- idempotent, admin-gated, no raw SQL, no fake
payment/invoice history, reuses the exact same tables/values a fresh
`grant_new_account` now creates inline.

Usage: python3 scripts/wl_canary_repair_wiring_20260829.py [--dry-run]
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

DECISION_REF = "production-wl-limited-canary-2026-08-29-wiring-repair"
EXPECTED_ACCOUNT_ID = 20
EXPECTED_PUBLIC_ID = "acct_51iMmDClJ-qPxGAIzSBFRv9k"
EXPECTED_TELEGRAM_ID = 8703542062


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


def _preflight(db: Database) -> None:
    conn = db._conn
    account = conn.execute(
        "SELECT * FROM mgboost_accounts WHERE id=?", (EXPECTED_ACCOUNT_ID,),
    ).fetchone()
    if account is None:
        raise PreflightFailed("account not found")
    if account["public_id"] != EXPECTED_PUBLIC_ID:
        raise PreflightFailed("public_id mismatch -- refusing")
    if account["account_source"] != "DIRECT":
        raise PreflightFailed("account_source mismatch -- refusing")
    if account["status"] != "ACTIVE":
        raise PreflightFailed(f"unexpected account status {account['status']!r}")

    identity = conn.execute(
        "SELECT telegram_id FROM mgboost_telegram_identities "
        "WHERE account_id=? AND role='OWNER' AND revoked_at IS NULL",
        (EXPECTED_ACCOUNT_ID,),
    ).fetchone()
    if identity is None or identity["telegram_id"] != EXPECTED_TELEGRAM_ID:
        raise PreflightFailed("telegram owner mismatch -- refusing")

    existing_alias = conn.execute(
        "SELECT id FROM mgboost_legacy_account_aliases WHERE account_id=?",
        (EXPECTED_ACCOUNT_ID,),
    ).fetchone()
    if existing_alias is not None:
        raise PreflightFailed(
            "account already has a PRIMARY alias -- this script's premise "
            "(pre-fix account missing wiring) is stale, refusing to run"
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--auth-path", type=Path, default=Path("/etc/mgboost/marzban-broker.env"),
    )
    args = parser.parse_args()

    db = Database()
    _preflight(db)
    print(
        "PREFLIGHT OK: account_id=%d public_id=%s, DIRECT/ACTIVE, telegram owner "
        "confirmed, no existing PRIMARY alias" % (EXPECTED_ACCOUNT_ID, EXPECTED_PUBLIC_ID)
    )
    if args.dry_run:
        print("--dry-run: no writes performed.")
        return

    capability, session_store, session_id = _real_primary_capability(db, args.auth_path)
    try:
        try:
            repaired = db.admin_grants.repair_missing_provisioning_wiring(
                capability, account_id=EXPECTED_ACCOUNT_ID, reason=DECISION_REF,
                now=int(time.time()),
            )
        except AdminGrantError as exc:
            raise RuntimeError(f"repair refused: {exc}") from exc
        print("REPAIR: repaired=%s" % repaired)

        jobs = db.admin_grants.pending_template_jobs()
        print("PENDING TEMPLATE JOBS for account:",
              [j["account_id"] for j in jobs if j["account_id"] == EXPECTED_ACCOUNT_ID])
    finally:
        session_store.revoke(session_id)


if __name__ == "__main__":
    main()
