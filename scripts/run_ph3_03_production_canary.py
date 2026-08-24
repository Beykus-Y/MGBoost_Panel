#!/usr/bin/env python3
"""Preflight and execute the single owner-approved dormant PH3-03 canary.

The script has no configurable account, alias, device or child target. Raw
Marzban credentials, subscription bearers, UUIDs and legacy request keys are
never printed or persisted by this tool.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import stat
import subprocess
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from dotenv import dotenv_values

from scripts.build_ph3_03_staging_xray import EFFECTIVE_VLESS_TAGS
from scripts.capture_phase1_masked_state import canonical_config, digest
from scripts.secure_db_backup import verify_backup
from src.admin_authority import PrimaryAdminAuthorizationError
from src.child_contract import (
    credential_verifier,
    derive_child_username,
    derive_operation_id,
    source_contract,
    source_contract_hash,
)
from src.child_provisioning_schema import (
    MIGRATION_ID as CHILD_MIGRATION_ID,
    NEW_RUNTIME_TABLES,
    SCHEMA_CHECKSUM as CHILD_SCHEMA_CHECKSUM,
    _verify as verify_child_schema,
)
from src.config import (
    COMPAT_TELEMETRY_HMAC_KEY,
    DATA_DIR,
    DEVICE_SLOT_HMAC_KEY,
    MARZBAN_URL,
    PRIMARY_MGBOOST_ADMIN_ACTOR_ID,
    PRIMARY_MGBOOST_ADMIN_LOGIN,
)
from src.database import Database
from src.internal_entitlements import derive_reviewed_account_public_id
from src.marzban import MarzbanClient
from src.security import AdminSessionStore
from src.sensitive import is_subscription_token_ref
from src.service_marzban import ServiceMarzbanClient
from src.shadowsocks_retirement import retirement_snapshot


ACTOR_ID = "owner:mgboost-primary:v1"
MAPPING_KEY = "INTERNAL_OWNER_PRIMARY"
TELEGRAM_ID = 905302972
SOURCE_USERNAME = "beykusios"
ALIASES = ("beykus", "beykusios", "BeykusLaptop")
ALIAS_DEVICE_COUNTS = {"beykus": 3, "beykusios": 4, "BeykusLaptop": 2}
DEVICE_ROW_ID = 56
PRIVACY_REF = "corr_701f5982b4"
EXPECTED_SOURCE_HASH = "52bd127165402fd429e47b4fa485a53566f8870af2514f6c82d4de204287ff47"
EXPECTED_ACCOUNT_PUBLIC_ID = "acct_435p4hjeoxeq3bzg4ifkdut4veower4r"
EXPECTED_CHILD_USERNAME = "mgc_sgg6v7t6he43yytsqmkdczzfpa"
EXPECTED_OPERATION_ID = "op_lw33pjhqhnvorrgh4p754bnc34"
EXPECTED_DEVICE = {
    "username": "beykusios",
    "device_name": "iPhone 17",
    "platform": "iOS",
    "client_name": "INCY",
    "client_version": "2.5.2",
    "is_active": 1,
}
CONFIRM = "execute-approved-internal-owner-primary-slot-1-generation-1"
PLAN_CODE = "INTERNAL_OWNER_CANARY"
PLAN_VERSION = 1
ACCOUNT_IDEMPOTENCY = "ph3-prod-internal-owner-primary-v1"
CHILD_IDEMPOTENCY = "ph3-prod-child-slot-1-generation-1-v1"
WORKER_ID = "ph3-prod-canary-worker-v1"
DORMANT_BASE_TABLES = (
    "mgboost_accounts", "mgboost_telegram_identities", "mgboost_plan_versions",
    "mgboost_subscriptions", "mgboost_entitlement_mutations",
    "mgboost_device_slots", "mgboost_device_slot_generations",
    "mgboost_internal_account_reviews", "mgboost_internal_entitlement_revisions",
)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _token(user: dict) -> str:
    raw_url = str(user.get("subscription_url") or "")
    token = raw_url.rstrip("/").rsplit("/", 1)[-1]
    if not token or token == raw_url:
        raise RuntimeError("subscription bearer is unavailable")
    return token


def _protected_auth(path: Path) -> tuple[str, str]:
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


def _read_all_users(client: ServiceMarzbanClient, sentinel) -> list[dict]:
    users = []
    offset = 0
    while True:
        page = client.get_users(sentinel, limit=100, offset=offset)
        rows = page.get("users", []) if isinstance(page, dict) else page
        users.extend(rows)
        if len(rows) < 100:
            return users
        offset += len(rows)


def _safe_legacy_snapshot(
    client: ServiceMarzbanClient, sentinel, connection: sqlite3.Connection,
    usernames: list[str],
) -> dict:
    identities = []
    configs = []
    fetch_errors = 0
    stored_token_refs = [tuple(row) for row in connection.execute(
        "SELECT DISTINCT username,token FROM user_devices ORDER BY username,token"
    )]
    if any(not is_subscription_token_ref(row[1]) for row in stored_token_refs):
        raise RuntimeError("raw legacy subscription bearer found in user_devices")
    for username in sorted(usernames):
        user = client.get_user(username, sentinel)
        identities.append({
            "username_ref": _hash(username),
            "created_at": user.get("created_at"),
            "sub_revoked_at": user.get("sub_revoked_at"),
            "expire": user.get("expire"),
            "status": user.get("status"),
            "data_limit": user.get("data_limit"),
            "data_limit_reset_strategy": user.get("data_limit_reset_strategy"),
            "proxies_digest": digest(user.get("proxies") or {}),
            "inbounds_digest": digest(user.get("inbounds") or {}),
        })
        # Marzban 0.8.4 creates a new timestamped, simultaneously-valid
        # subscription alias whenever UserResponse is serialized. It is not a
        # stored credential and therefore must not be compared as a rotation.
        # PH1-06 deliberately retained only hashes of historical raw bearers;
        # created_at/sub_revoked_at and those stable verifier rows are the
        # credential-validity invariant. This fresh alias only verifies that
        # the current functional subscription contract remains renderable.
        representative = _token(user)
        try:
            body, _headers = client.get_sub(
                representative, {"User-Agent": "MGBoost-PH3-03-Canary/1"}
            )
            links = canonical_config(body)
            if not links:
                raise RuntimeError("legacy subscription contains no VPN links")
            configs.append({"username_ref": _hash(username), "links": digest(links)})
        except Exception:
            fetch_errors += 1
    devices = [tuple(row) for row in connection.execute(
        "SELECT username,token,request_key,is_active,first_seen "
        "FROM user_devices ORDER BY username,request_key"
    )]
    locks = [tuple(row) for row in connection.execute(
        "SELECT request_key,username,locked_at FROM hwid_lock ORDER BY request_key"
    )]
    tariffs = [tuple(row) for row in connection.execute(
        "SELECT id,name,duration_days,stars_price,active,sort_order,created_at,updated_at "
        "FROM stars_tariffs ORDER BY id"
    )]
    return {
        "legacy_user_count": len(identities),
        "legacy_identity_digest": digest(identities),
        "legacy_config_count": len(configs),
        "legacy_config_digest": digest(configs),
        "legacy_config_fetch_errors": fetch_errors,
        "legacy_stored_token_ref_count": len(stored_token_refs),
        "legacy_stored_token_ref_user_count": len({row[0] for row in stored_token_refs}),
        "legacy_stored_token_ref_digest": digest(stored_token_refs),
        "device_count": len(devices),
        "device_digest": digest(devices),
        "hwid_lock_count": len(locks),
        "hwid_lock_digest": digest(locks),
        "stars_tariff_count": len(tariffs),
        "stars_tariff_digest": digest(tariffs),
    }


def _schema_preflight(connection: sqlite3.Connection, *, expect_empty: bool) -> dict:
    marker = connection.execute(
        "SELECT schema_checksum FROM mgboost_schema_migrations WHERE migration_id=?",
        (CHILD_MIGRATION_ID,),
    ).fetchone()
    if not marker or marker[0] != CHILD_SCHEMA_CHECKSUM:
        raise RuntimeError("exact PH3-03 migration marker is absent")
    verify_child_schema(connection)
    counts = {
        table: int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
        for table in (*DORMANT_BASE_TABLES, *NEW_RUNTIME_TABLES)
    }
    if expect_empty and any(counts.values()):
        raise RuntimeError("PH3-03 tables are not dormant/empty")
    if not expect_empty:
        maxima = {
            "mgboost_accounts": 1,
            "mgboost_telegram_identities": 1,
            "mgboost_plan_versions": 1,
            "mgboost_subscriptions": 1,
            "mgboost_entitlement_mutations": 1,
            "mgboost_device_slots": 1,
            "mgboost_device_slot_generations": 1,
            "mgboost_internal_account_reviews": 1,
            "mgboost_internal_entitlement_revisions": 1,
            "mgboost_legacy_alias_groups": 1,
            "mgboost_legacy_account_aliases": 3,
            "mgboost_child_user_intents": 1,
            "mgboost_outbox": 1,
            "mgboost_outbox_attempt_events": 1000,
        }
        if any(counts[table] > maximum for table, maximum in maxima.items()):
            raise RuntimeError("reconciliation state exceeds the approved canary scope")
        account = connection.execute(
            "SELECT public_id,account_source,status FROM mgboost_accounts"
        ).fetchone()
        if account and tuple(account) != (EXPECTED_ACCOUNT_PUBLIC_ID, "INTERNAL", "ACTIVE"):
            raise RuntimeError("reconciliation found an unrelated parent account")
        aliases = [row[0] for row in connection.execute(
            "SELECT legacy_username FROM mgboost_legacy_account_aliases "
            "ORDER BY legacy_username"
        )]
        if aliases and aliases != sorted(ALIASES):
            raise RuntimeError("reconciliation found unrelated legacy aliases")
        child = connection.execute(
            "SELECT child_username,slot_number,generation FROM mgboost_child_user_intents"
        ).fetchone()
        if child and tuple(child) != (EXPECTED_CHILD_USERNAME, 1, 1):
            raise RuntimeError("reconciliation found an unrelated child intent")
        outbox = connection.execute(
            "SELECT operation_id,operation_kind FROM mgboost_outbox"
        ).fetchone()
        if outbox and tuple(outbox) != (EXPECTED_OPERATION_ID, "CHILD_USER_ENSURE"):
            raise RuntimeError("reconciliation found an unrelated outbox operation")
    quick = connection.execute("PRAGMA quick_check").fetchone()[0]
    foreign_keys = len(connection.execute("PRAGMA foreign_key_check").fetchall())
    if quick != "ok" or foreign_keys:
        raise RuntimeError("production database integrity gate failed")
    return {"migration_exact": True, "table_counts": counts, "quick_check": quick,
            "foreign_key_violations": foreign_keys}


def _configuration_preflight() -> dict:
    if PRIMARY_MGBOOST_ADMIN_ACTOR_ID != ACTOR_ID or not PRIMARY_MGBOOST_ADMIN_LOGIN:
        raise RuntimeError("primary-admin mapping is not enabled exactly")
    if len(DEVICE_SLOT_HMAC_KEY.encode("utf-8")) < 32:
        raise RuntimeError("slot-HWID HMAC key is not configured")
    if len(COMPAT_TELEMETRY_HMAC_KEY.encode("utf-8")) < 32:
        raise RuntimeError("telemetry HMAC key is not configured")
    if DEVICE_SLOT_HMAC_KEY == COMPAT_TELEMETRY_HMAC_KEY:
        raise RuntimeError("slot and telemetry HMAC keys are not independent")
    client = ServiceMarzbanClient()
    client.assert_credential_boundary()
    if client.mode != "broker" or not client._broker().base_url.startswith(
        ("http://127.0.0.1:", "http://[::1]:")
    ):
        raise RuntimeError("child broker boundary is not loopback-only")
    return {
        "primary_actor_exact": True,
        "primary_login_configured": True,
        "slot_key_configured": True,
        "slot_telemetry_keys_distinct": True,
        "main_sudo_credentials_present": int(bool(
            os.environ.get("MARZBAN_ADMIN_USER") or os.environ.get("MARZBAN_ADMIN_PASS")
        )),
        "broker_loopback_only": True,
    }


def _backup_preflight(artifact: Path, passphrase: Path, max_age: int) -> dict:
    backup_root = Path("/var/backups/mgboost").resolve()
    resolved = artifact.resolve()
    if resolved.parent != backup_root or not resolved.name.endswith(".tar.gpg"):
        raise RuntimeError("backup artifact is outside the approved directory")
    age = max(0, int(time.time() - resolved.stat().st_mtime))
    if age > max_age:
        raise RuntimeError("verified backup is too old for canary")
    verify_backup(resolved, passphrase.resolve())
    return {"encrypted_restore_verified": True, "backup_age_seconds": age}


def _broker_preflight(client: ServiceMarzbanClient, *, allow_existing_child: bool) -> dict:
    absent_request = {
        "operation_id": EXPECTED_OPERATION_ID,
        "child_username": EXPECTED_CHILD_USERNAME,
        "source_contract_hash": EXPECTED_SOURCE_HASH,
        "expire": 0,
        "uuid_verifier": "sha256:" + "0" * 64,
    }
    child_absent = None
    try:
        client.get_child_credentials(absent_request)
    except HTTPError as exc:
        if exc.code == 404:
            child_absent = True
        elif allow_existing_child and exc.code == 400:
            # Existing child + deliberately wrong verifier is a safe failure;
            # exact identity/contract is verified later with the stored verifier.
            child_absent = False
        else:
            raise
    else:
        raise RuntimeError("wrong-verifier child credential probe unexpectedly succeeded")
    if not allow_existing_child and not child_absent:
        raise RuntimeError("approved child unexpectedly exists before canary")
    ensure_probe = {
        "operation_id": EXPECTED_OPERATION_ID,
        "child_username": EXPECTED_CHILD_USERNAME,
        "source_username": "mgboost-ph3-preflight-missing",
        "source_contract_hash": "0" * 64,
        "expire": 0,
    }
    try:
        client.ensure_child_user(ensure_probe)
    except HTTPError as exc:
        if exc.code != 404:
            raise
    else:
        raise RuntimeError("missing-source ensure probe unexpectedly succeeded")
    broker = client._broker()
    request = Request(
        broker.base_url + "/v1/operations/child.user.credentials.get",
        data=b"{}", method="POST", headers={"Content-Type": "application/json"},
    )
    try:
        urlopen(request, timeout=3)
    except HTTPError as exc:
        if exc.code != 401:
            raise
    else:
        raise RuntimeError("unauthenticated broker operation was accepted")
    return {
        "typed_child_ensure_available": True,
        "typed_credentials_reread_available": True,
        "unauthenticated_local_call_denied": True,
        "approved_child_absent": child_absent,
    }


def _source_preflight(
    client: ServiceMarzbanClient, sentinel, connection: sqlite3.Connection,
    *, allow_existing_child: bool,
) -> tuple[dict, list[str]]:
    users = _read_all_users(client, sentinel)
    all_usernames = sorted(str(user.get("username") or "") for user in users)
    child_count = all_usernames.count(EXPECTED_CHILD_USERNAME)
    if child_count not in ({0, 1} if allow_existing_child else {0}):
        raise RuntimeError("legacy user inventory changed before canary")
    usernames = [name for name in all_usernames if name != EXPECTED_CHILD_USERNAME]
    if len(usernames) != 25 or len(users) != 25 + child_count:
        raise RuntimeError("legacy user inventory changed before canary")
    source = client.get_user(SOURCE_USERNAME, sentinel)
    source_hash = source_contract_hash(source)
    contract = source_contract(source)
    topology = client.get_inbounds(sentinel)
    topology_vless = sorted(item.get("tag") for item in topology.get("vless", []))
    if (
        source_hash != EXPECTED_SOURCE_HASH
        or contract["protocols"] != ["vless"]
        or contract["proxy_options"]["vless"]["flow"] != "xtls-rprx-vision"
        or contract["inbounds"]["vless"] != sorted(EFFECTIVE_VLESS_TAGS)
        or topology_vless != sorted(EFFECTIVE_VLESS_TAGS)
        or topology.get("shadowsocks")
        or source.get("status") != "active"
        or source.get("data_limit") is not None
        or int(source.get("expire") or 0) != 0
    ):
        raise RuntimeError("approved source/topology contract changed")
    row = connection.execute(
        "SELECT id,username,request_key,device_name,platform,client_name,"
        "client_version,is_active FROM user_devices WHERE id=?", (DEVICE_ROW_ID,),
    ).fetchone()
    if not row or any(row[key] != expected for key, expected in EXPECTED_DEVICE.items()):
        raise RuntimeError("approved legacy device row changed")
    if not row["request_key"]:
        raise RuntimeError("approved legacy device correlation key is missing")
    counts = {
        alias: int(connection.execute(
            "SELECT COUNT(*) FROM user_devices WHERE username=?", (alias,)
        ).fetchone()[0]) for alias in ALIASES
    }
    if counts != ALIAS_DEVICE_COUNTS:
        raise RuntimeError("approved alias device evidence changed")
    baseline = _safe_legacy_snapshot(client, sentinel, connection, usernames)
    if (
        baseline["legacy_config_fetch_errors"]
        or baseline["legacy_config_count"] != 25
        or baseline["legacy_stored_token_ref_count"] != 45
        or baseline["legacy_stored_token_ref_user_count"] != 24
    ):
        raise RuntimeError("legacy subscription baseline is incomplete")
    return {
        "legacy_user_count": 25,
        "source_contract_hash": source_hash,
        "source_uuid_mask": retirement_snapshot(source)["vless_uuid_mask"],
        "exact_vless_inbound_count": len(topology_vless),
        "shadowsocks_inbound_count": 0,
        "device_row": DEVICE_ROW_ID,
        "privacy_ref": PRIVACY_REF,
        "device_metadata_match": True,
        "alias_device_counts": counts,
        "legacy_snapshot": baseline,
    }, usernames


def preflight(args, *, expect_empty: bool = True):
    configuration = _configuration_preflight()
    connection = sqlite3.connect(f"file:{Path(DATA_DIR) / 'db.sqlite3'}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    client = ServiceMarzbanClient()
    sentinel = client.get_admin_token_from_env()
    try:
        schema = _schema_preflight(connection, expect_empty=expect_empty)
        source, usernames = _source_preflight(
            client, sentinel, connection,
            allow_existing_child=not expect_empty,
        )
    finally:
        connection.close()
    # Prove the actor boundary with a real upstream-authenticated server-side
    # session, then immediately revoke that one-process session.
    db = Database()
    try:
        capability, session_store, session_id = _real_primary_capability(
            db, args.admin_auth_env
        )
        actor = db.primary_admin_authority.require(capability)
        session_store.revoke(session_id)
    finally:
        db._conn.close()
    if actor != ACTOR_ID:
        raise RuntimeError("primary capability actor mismatch")
    result = {
        "preflight": "PASS",
        "configuration": configuration,
        "schema": schema,
        "primary_admin": {
            "real_upstream_authentication": True,
            "server_session_authorized": True,
            "forged_session_rejected": True,
            "actor": ACTOR_ID,
        },
        "broker": _broker_preflight(
            client, allow_existing_child=not expect_empty
        ),
        "source": source,
        "backup": _backup_preflight(
            args.backup_artifact, args.backup_passphrase, args.backup_max_age
        ),
        "legacy_usernames": usernames,
        "secrets_printed": 0,
    }
    return result


def _alias_evidence(connection: sqlite3.Connection) -> list[dict]:
    rows = []
    for alias in ALIASES:
        count = int(connection.execute(
            "SELECT COUNT(*) FROM user_devices WHERE username=?", (alias,)
        ).fetchone()[0])
        rows.append({
            "legacy_username": alias,
            "alias_role": "PRIMARY" if alias == SOURCE_USERNAME else "SECONDARY",
            "ownership_provenance": "OWNER_APPROVED",
            "legacy_status": "UNLIMITED",
            "legacy_expiry": None,
            "observed_device_count": count,
            "observed_hwid_count": count,
            "evidence": {
                "schema": 1,
                "owner_decision": "DL-045",
                "privacy": "masked",
            },
        })
    return rows


def _leak_scan(raw_values: list[str], db: Database, *, since: int) -> dict:
    db_dump = "\n".join(db._conn.iterdump())
    journal = subprocess.run(
        ["journalctl", "--since", f"@{since}", "-u", "mgboost-panel",
         "-u", "mgboost-marzban-broker", "-u", "nginx", "--no-pager", "-q"],
        check=False, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    ).stdout.decode("utf-8", errors="replace")
    log_text = ""
    for directory in (Path("/var/log/nginx"), Path("/var/log/mgboost")):
        if not directory.is_dir():
            continue
        for path in directory.glob("*.log"):
            try:
                log_text += path.read_text(encoding="utf-8", errors="replace")
            except (OSError, PermissionError):
                continue
    counts = {
        "mgboost_db": sum(db_dump.count(value) for value in raw_values),
        "journal": sum(journal.count(value) for value in raw_values),
        "application_nginx_logs": sum(log_text.count(value) for value in raw_values),
    }
    if any(counts.values()):
        raise RuntimeError("raw child credential leakage detected")
    return counts


def execute(args, *, resume: bool = False) -> dict:
    started = int(time.time())
    gate = preflight(args, expect_empty=not resume)
    baseline = gate["source"]["legacy_snapshot"]
    usernames = gate.pop("legacy_usernames")
    client = ServiceMarzbanClient()
    sentinel = client.get_admin_token_from_env()
    db = Database()
    raw_values = []
    session_store = None
    session_id = None
    try:
        capability, session_store, session_id = _real_primary_capability(
            db, args.admin_auth_env
        )
        plan = db.internal_entitlements.create_internal_plan(
            capability=capability,
            plan_code=PLAN_CODE,
            version=PLAN_VERSION,
            display_name="Internal owner canary",
            device_limit_mode="LIMITED",
            device_limit=10,
            wl_mode="UNLIMITED",
            terms={"schema": 1, "internal": True, "owner_decision": "DL-045"},
        )
        aliases = _alias_evidence(db._conn)
        account = db.internal_entitlements.create_reviewed_account(
            capability=capability,
            plan_version_id=plan["id"],
            legacy_username=SOURCE_USERNAME,
            mapping_key=MAPPING_KEY,
            decision_ref="DL-045-owner-approved-ph3-03-canary",
            legacy_aliases=aliases,
            ownership_evidence="PROVEN",
            telegram_id=TELEGRAM_ID,
            legacy_status="UNLIMITED",
            legacy_expiry=None,
            device_evidence_count=9,
            hwid_evidence_count=9,
            internal_reason="Owner-approved first dormant PH3-03 production child canary",
            migration_confidence="HIGH",
            evidence={
                "schema": 1,
                "legacy_device_row": DEVICE_ROW_ID,
                "privacy_ref": PRIVACY_REF,
                "device": "iPhone 17 / INCY 2.5.2 / iOS",
            },
            idempotency_key=ACCOUNT_IDEMPOTENCY,
        )
        if account["public_id"] != EXPECTED_ACCOUNT_PUBLIC_ID:
            raise RuntimeError("server-derived parent identity drift")
        session_store.revoke(session_id)
        session_store = session_id = None

        device = db._conn.execute(
            "SELECT request_key FROM user_devices WHERE id=? AND username=?",
            (DEVICE_ROW_ID, SOURCE_USERNAME),
        ).fetchone()
        slot = db.device_slots.claim(
            account["account_id"], device["request_key"], DEVICE_SLOT_HMAC_KEY
        )
        if (slot["slot_number"], slot["generation"]) != (1, 1):
            raise RuntimeError("approved slot/generation drift")
        alias = db._conn.execute(
            "SELECT id FROM mgboost_legacy_account_aliases "
            "WHERE account_id=? AND legacy_username=?",
            (account["account_id"], SOURCE_USERNAME),
        ).fetchone()
        prepared = db.child_provisioning.prepare_child_ensure(
            account_id=account["account_id"],
            slot_generation_id=slot["generation_id"],
            source_alias_id=alias["id"],
            source_contract_hash=EXPECTED_SOURCE_HASH,
            expire=0,
            idempotency_key=CHILD_IDEMPOTENCY,
        )
        if (
            prepared["child_username"] != EXPECTED_CHILD_USERNAME
            or prepared["operation_id"] != EXPECTED_OPERATION_ID
            or prepared["state"] not in {"PENDING", "RETRY", "IN_FLIGHT", "APPLIED"}
        ):
            raise RuntimeError("approved child intent/outbox identity drift")
        payload = json.loads(prepared["payload_json"])

        # Fresh source reread is the final boundary before the only remote write.
        if source_contract_hash(client.get_user(SOURCE_USERNAME, sentinel)) != EXPECTED_SOURCE_HASH:
            raise RuntimeError("source changed after local intent creation")
        ack_outcome = None
        if prepared["state"] == "APPLIED":
            child = db._conn.execute(
                "SELECT * FROM mgboost_child_user_intents WHERE id=?",
                (prepared["child_intent_id"],),
            ).fetchone()
            if not child or child["observed_state"] != "ACTIVE" or not child["uuid_verifier"]:
                raise RuntimeError("applied outbox has no verified active child")
        else:
            claimed = db.child_provisioning.claim(
                EXPECTED_OPERATION_ID, worker_id=WORKER_ID,
                now=int(time.time()), lease_seconds=30,
            )
            if not claimed:
                raise RuntimeError(
                    "approved outbox is not claimable; wait for its lease/retry boundary"
                )
            try:
                ensured = client.ensure_child_user(claimed["payload"])
            except Exception:
                # Same immutable operation is the reconciliation primitive. It
                # rereads the deterministic username before any possible create.
                try:
                    ensured = client.ensure_child_user(claimed["payload"])
                except Exception as second_error:
                    db.child_provisioning.retry(
                        EXPECTED_OPERATION_ID, worker_id=WORKER_ID,
                        error_class=type(second_error).__name__, now=int(time.time()),
                    )
                    raise RuntimeError(
                        "child ensure is retryable after safe reconciliation failure"
                    )
            raw_uuid = ensured.pop("uuid")
            raw_values.append(raw_uuid)
            ack_outcome = ensured["outcome"]
            child = db.child_provisioning.acknowledge(
                EXPECTED_OPERATION_ID,
                worker_id=WORKER_ID,
                outcome=ack_outcome,
                child_uuid=raw_uuid,
                remote_result=ensured,
                now=int(time.time()),
            )

        repeated = client.ensure_child_user(payload)
        repeated_uuid = repeated.pop("uuid")
        raw_values.append(repeated_uuid)
        if repeated["outcome"] != "EXISTING":
            raise RuntimeError("idempotent child ensure verification failed")
        raw_uuid = repeated_uuid
        credential_request = {
            "operation_id": EXPECTED_OPERATION_ID,
            "child_username": EXPECTED_CHILD_USERNAME,
            "source_contract_hash": EXPECTED_SOURCE_HASH,
            "expire": 0,
            "uuid_verifier": child["uuid_verifier"],
        }
        credentials = client.get_child_credentials(credential_request)
        reread_uuid = credentials["credentials"].pop("vless_uuid")
        raw_values.append(reread_uuid)
        if reread_uuid != raw_uuid or credential_verifier(raw_uuid) != child["uuid_verifier"]:
            raise RuntimeError("typed child credential verifier mismatch")

        remote_child = client.get_user(EXPECTED_CHILD_USERNAME, sentinel)
        remote_token = _token(remote_child)
        raw_values.append(remote_token)
        if (
            source_contract_hash(remote_child) != EXPECTED_SOURCE_HASH
            or remote_child.get("status") != "active"
            or remote_child.get("data_limit") is not None
            or int(remote_child.get("expire") or 0) != 0
            or remote_child["proxies"]["vless"]["id"] != raw_uuid
            or raw_uuid == client.get_user(SOURCE_USERNAME, sentinel)["proxies"]["vless"]["id"]
        ):
            raise RuntimeError("remote child contract differs from approved VLESS contract")

        all_users = _read_all_users(client, sentinel)
        if len(all_users) != 26 or sum(
            1 for item in all_users if item.get("username") == EXPECTED_CHILD_USERNAME
        ) != 1:
            raise RuntimeError("remote child cardinality mismatch")
        post_legacy = _safe_legacy_snapshot(client, sentinel, db._conn, usernames)
        if post_legacy != baseline:
            raise RuntimeError("legacy runtime changed during dormant canary")

        counts = {
            "parents": db._conn.execute("SELECT COUNT(*) FROM mgboost_accounts").fetchone()[0],
            "telegram_identities": db._conn.execute(
                "SELECT COUNT(*) FROM mgboost_telegram_identities"
            ).fetchone()[0],
            "plan_versions": db._conn.execute(
                "SELECT COUNT(*) FROM mgboost_plan_versions"
            ).fetchone()[0],
            "subscriptions": db._conn.execute(
                "SELECT COUNT(*) FROM mgboost_subscriptions"
            ).fetchone()[0],
            "entitlement_mutations": db._conn.execute(
                "SELECT COUNT(*) FROM mgboost_entitlement_mutations"
            ).fetchone()[0],
            "internal_reviews": db._conn.execute(
                "SELECT COUNT(*) FROM mgboost_internal_account_reviews"
            ).fetchone()[0],
            "alias_groups": db._conn.execute(
                "SELECT COUNT(*) FROM mgboost_legacy_alias_groups"
            ).fetchone()[0],
            "aliases": db._conn.execute(
                "SELECT COUNT(*) FROM mgboost_legacy_account_aliases"
            ).fetchone()[0],
            "slots": db._conn.execute("SELECT COUNT(*) FROM mgboost_device_slots").fetchone()[0],
            "generations": db._conn.execute(
                "SELECT COUNT(*) FROM mgboost_device_slot_generations"
            ).fetchone()[0],
            "child_intents": db._conn.execute(
                "SELECT COUNT(*) FROM mgboost_child_user_intents"
            ).fetchone()[0],
            "outbox_operations": db._conn.execute(
                "SELECT COUNT(*) FROM mgboost_outbox"
            ).fetchone()[0],
        }
        if counts != {
            "parents": 1, "telegram_identities": 1, "plan_versions": 1,
            "subscriptions": 1, "entitlement_mutations": 1,
            "internal_reviews": 1, "alias_groups": 1, "aliases": 3,
            "slots": 1, "generations": 1,
            "child_intents": 1, "outbox_operations": 1,
        }:
            raise RuntimeError("local canary cardinality mismatch")
        entitlement = db.internal_entitlements.effective_entitlements(
            account["account_id"]
        )
        subscription = db._conn.execute(
            "SELECT status,current_expiry FROM mgboost_subscriptions WHERE account_id=?",
            (account["account_id"],),
        ).fetchone()
        identity = db._conn.execute(
            "SELECT telegram_id,role,provenance,revoked_at,linked_by_actor "
            "FROM mgboost_telegram_identities WHERE account_id=?",
            (account["account_id"],),
        ).fetchone()
        alias_names = [row[0] for row in db._conn.execute(
            "SELECT legacy_username FROM mgboost_legacy_account_aliases "
            "WHERE account_id=? ORDER BY legacy_username", (account["account_id"],),
        )]
        if (
            entitlement["billing_required"] is not False
            or entitlement["device_limit_mode"] != "LIMITED"
            or entitlement["device_limit"] != 10
            or entitlement["wl_mode"] != "UNLIMITED"
            or tuple(subscription) != ("UNLIMITED", None)
            or tuple(identity) != (TELEGRAM_ID, "OWNER", "MIGRATION", None, ACTOR_ID)
            or alias_names != sorted(ALIASES)
        ):
            raise RuntimeError("reviewed account/identity/entitlement contract mismatch")
        outbox = db._conn.execute(
            "SELECT id,state,attempts,last_error_class FROM mgboost_outbox "
            "WHERE operation_id=?", (EXPECTED_OPERATION_ID,),
        ).fetchone()
        events = [dict(row) for row in db._conn.execute(
            "SELECT attempt_no,event_type,outcome,safe_error_class "
            "FROM mgboost_outbox_attempt_events WHERE outbox_id=? ORDER BY id",
            (outbox["id"],),
        )]
        if outbox["state"] != "APPLIED" or child["observed_state"] != "ACTIVE":
            raise RuntimeError("local child state did not converge to ACTIVE/APPLIED")
        leak_counts = _leak_scan(list(dict.fromkeys(raw_values)), db, since=started)

        return {
            "canary": "PASS",
            "created": {
                "account_id": account["account_id"],
                "account_public_id": account["public_id"],
                "subscription_id": account["subscription_id"],
                "plan_version_id": plan["id"],
                "slot_id": slot["slot_id"],
                "slot_number": slot["slot_number"],
                "slot_generation_id": slot["generation_id"],
                "generation": slot["generation"],
                "hwid_masked": slot["hwid_masked"],
                "privacy_ref": PRIVACY_REF,
                "child_intent_id": child["id"],
                "child_public_id": child["public_id"],
                "child_username": child["child_username"],
                "uuid_masked": child["uuid_masked"],
                "operation_id": EXPECTED_OPERATION_ID,
                "outbox_id": outbox["id"],
            },
            "cardinality": counts,
            "outbox": {
                "state": outbox["state"],
                "attempts": outbox["attempts"],
                "last_error_class": outbox["last_error_class"],
                "events": events,
                "ack_outcome": ack_outcome or "ALREADY_APPLIED",
                "repeat_ensure_outcome": repeated["outcome"],
            },
            "remote": {
                "remote_user_count": len(all_users),
                "remote_child_count": 1,
                "observed_state": child["observed_state"],
                "protocols": credentials["observed"]["protocols"],
                "flow": credentials["observed"]["proxy_options"]["vless"]["flow"],
                "vless_inbound_count": len(credentials["observed"]["inbounds"]["vless"]),
                "vless_inbounds": credentials["observed"]["inbounds"]["vless"],
                "contract_hash": credentials["observed"]["contract_hash"],
                "status": credentials["observed"]["status"],
                "expire": credentials["observed"]["expire"],
                "data_limit": credentials["observed"]["data_limit"],
                "new_uuid_differs_from_legacy": True,
                "uuid_verifier_valid": True,
            },
            "legacy_invariants": {
                "pre": baseline,
                "post": post_legacy,
                "legacy_uuid_changes": 0,
                "legacy_subscription_url_token_changes": 0,
                "legacy_hwid_binding_changes": 0,
                "legacy_expiry_changes": 0,
                "legacy_tariff_changes": 0,
                "forced_client_reconfiguration": 0,
                "unexpected_legacy_config_changes": 0,
            },
            "raw_credential_leak_counts": leak_counts,
            "production_switches": {
                "legacy_sub_switched": False,
                "legacy_user_revoked": False,
                "ph3_04_enabled": False,
                "other_devices_migrated": 0,
                "other_children_created": 0,
            },
            "secrets_printed": 0,
        }
    finally:
        if session_store is not None and session_id is not None:
            session_store.revoke(session_id)
        db._conn.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--preflight", action="store_true")
    mode.add_argument("--execute", action="store_true")
    mode.add_argument("--reconcile", action="store_true")
    parser.add_argument(
        "--admin-auth-env", type=Path,
        default=Path("/etc/mgboost/marzban-broker.env"),
    )
    parser.add_argument("--backup-artifact", type=Path, required=True)
    parser.add_argument(
        "--backup-passphrase", type=Path,
        default=Path("/etc/mgboost/backup.passphrase"),
    )
    parser.add_argument("--backup-max-age", type=int, default=3600)
    parser.add_argument("--confirm")
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise PermissionError("PH3-03 production canary requires root")
    if args.execute or args.reconcile:
        if args.confirm != CONFIRM:
            raise RuntimeError("exact PH3-03 canary confirmation required")
        result = execute(args, resume=args.reconcile)
    else:
        result = preflight(args, expect_empty=True)
        result.pop("legacy_usernames", None)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
