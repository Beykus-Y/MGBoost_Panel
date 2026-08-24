#!/usr/bin/env python3
"""Capture aggregate PH1 compatibility digests without emitting credentials."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import sqlite3
from pathlib import Path

from src.service_marzban import ServiceMarzbanClient


VPN_SCHEMES = ("vless://", "vmess://", "trojan://", "ss://", "hysteria2://")
LEGACY_INFO_NODE_PREFIX = (
    "vless://00000000-0000-0000-0000-000000000000@127.0.0.1:1?"
)
LEGACY_DYNAMIC_SID_RE = re.compile(r"([?&]sid=)[^&#]*")


def digest(value) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def masked(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def canonical_config(body: bytes) -> list[str]:
    candidates = [body]
    try:
        candidates.insert(0, base64.b64decode(body, validate=True))
    except Exception:
        pass
    for candidate in candidates:
        try:
            lines = candidate.decode("utf-8").splitlines()
        except UnicodeDecodeError:
            continue
        links = sorted(
            LEGACY_DYNAMIC_SID_RE.sub(r"\1<DYNAMIC-SID>", line.strip())
            for line in lines
            if line.strip().startswith(VPN_SCHEMES)
            and not line.strip().startswith(LEGACY_INFO_NODE_PREFIX)
        )
        if links:
            return links
    return []


def capture_remote() -> dict:
    client = ServiceMarzbanClient()
    client.assert_credential_boundary()
    sentinel = client.get_admin_token_from_env()

    users = []
    offset = 0
    while True:
        page = client.get_users(sentinel, limit=100, offset=offset)
        rows = page.get("users", []) if isinstance(page, dict) else page
        if not rows:
            break
        users.extend(rows)
        if len(rows) < 100:
            break
        offset += len(rows)

    identity_rows = []
    config_rows = []
    fetch_errors = 0
    for user in users:
        username = str(user.get("username") or "")
        username_ref = masked(username)
        identity_rows.append(
            {
                "username": username_ref,
                "expire": user.get("expire"),
                "status": user.get("status"),
                "data_limit": user.get("data_limit"),
                "data_limit_reset_strategy": user.get("data_limit_reset_strategy"),
                "proxies": digest(user.get("proxies") or {}),
                "inbounds": digest(user.get("inbounds") or {}),
            }
        )
        raw_url = str(user.get("subscription_url") or "")
        token = raw_url.rstrip("/").rsplit("/", 1)[-1]
        if not token or token == raw_url:
            fetch_errors += 1
            continue
        try:
            body, _ = client.get_sub(token, {"User-Agent": "Happ/PH1-gate"})
            config_rows.append(
                {"username": username_ref, "links": digest(canonical_config(body))}
            )
        except Exception:
            fetch_errors += 1

    return {
        "user_count": len(users),
        "identity_digest": digest(sorted(identity_rows, key=lambda row: row["username"])),
        "config_count": len(config_rows),
        "config_digest": digest(sorted(config_rows, key=lambda row: row["username"])),
        "config_fetch_errors": fetch_errors,
    }


def capture_local(db_path: Path) -> dict:
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        devices = list(
            connection.execute(
                """SELECT username, token, request_key, is_active, first_seen
                   FROM user_devices ORDER BY username, request_key"""
            )
        )
        locks = list(
            connection.execute(
                """SELECT request_key, username, locked_at
                   FROM hwid_lock ORDER BY request_key"""
            )
        )
    finally:
        connection.close()
    return {
        "device_count": len(devices),
        "device_digest": digest(devices),
        "hwid_lock_count": len(locks),
        "hwid_lock_digest": digest(locks),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, required=True)
    args = parser.parse_args()
    snapshot = {"remote": capture_remote(), "local": capture_local(args.db)}
    print(json.dumps(snapshot, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
