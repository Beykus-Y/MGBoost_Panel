#!/usr/bin/env python3
"""Atomically enable only the approved PH3-03 dormant canary authority.

No secret value is printed. The slot verifier key is generated independently
and the primary login is derived from the protected broker environment rather
than from caller input.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import stat
import tempfile
from pathlib import Path

from dotenv import dotenv_values


ACTOR_ID = "owner:mgboost-primary:v1"


def _protected(path: Path, *, allow_group_read: bool) -> os.stat_result:
    info = path.stat()
    if info.st_uid != os.geteuid():
        raise PermissionError(f"{path.name} must be owned by the executor")
    forbidden = stat.S_IWGRP | stat.S_IXGRP | stat.S_IRWXO
    if not allow_group_read:
        forbidden |= stat.S_IRGRP
    if info.st_mode & forbidden:
        raise PermissionError(f"{path.name} permissions are too broad")
    return info


def _replace_values(text: str, replacements: dict[str, str]) -> str:
    pending = dict(replacements)
    output = []
    for line in text.splitlines():
        stripped = line.lstrip()
        key = stripped.split("=", 1)[0] if "=" in stripped else ""
        if key in pending and not stripped.startswith("#"):
            output.append(f"{key}={pending.pop(key)}")
        else:
            output.append(line)
    if pending:
        if output and output[-1]:
            output.append("")
        output.extend(f"{key}={value}" for key, value in pending.items())
    return "\n".join(output) + "\n"


def configure(main_env: Path, broker_env: Path, backup: Path) -> dict:
    main_info = _protected(main_env, allow_group_read=True)
    _protected(broker_env, allow_group_read=True)
    main = dotenv_values(main_env)
    broker = dotenv_values(broker_env)

    broker_login = str(broker.get("MARZBAN_ADMIN_USER") or "").strip()
    broker_password = str(broker.get("MARZBAN_ADMIN_PASS") or "")
    telemetry_key = str(main.get("COMPAT_TELEMETRY_HMAC_KEY") or "")
    slot_key = str(main.get("DEVICE_SLOT_HMAC_KEY") or "")
    actor = str(main.get("PRIMARY_MGBOOST_ADMIN_ACTOR_ID") or "").strip()
    primary_login = str(main.get("PRIMARY_MGBOOST_ADMIN_LOGIN") or "").strip()

    if not broker_login or not broker_password:
        raise RuntimeError("protected broker admin credential is incomplete")
    if len(telemetry_key.encode("utf-8")) < 32:
        raise RuntimeError("telemetry HMAC key is not configured")
    if actor not in {"", ACTOR_ID}:
        raise RuntimeError("conflicting primary actor configuration")
    if primary_login not in {"", broker_login}:
        raise RuntimeError("conflicting primary login configuration")
    generated = False
    if not slot_key:
        slot_key = secrets.token_urlsafe(48)
        generated = True
    if len(slot_key.encode("utf-8")) < 32:
        raise RuntimeError("slot HMAC key is too short")
    if secrets.compare_digest(slot_key, telemetry_key):
        raise RuntimeError("slot and telemetry HMAC keys must be independent")

    if backup.exists():
        _protected(backup, allow_group_read=False)
    else:
        shutil.copy2(main_env, backup)
        os.chown(backup, os.geteuid(), os.getegid())
        os.chmod(backup, 0o600)

    updated = _replace_values(main_env.read_text(encoding="utf-8"), {
        "DEVICE_SLOT_HMAC_KEY": slot_key,
        "PRIMARY_MGBOOST_ADMIN_ACTOR_ID": ACTOR_ID,
        "PRIMARY_MGBOOST_ADMIN_LOGIN": broker_login,
    })
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=main_env.parent,
        prefix=".env.ph3-03.", delete=False,
    ) as handle:
        temp = Path(handle.name)
        handle.write(updated)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.chown(temp, main_info.st_uid, main_info.st_gid)
        os.chmod(temp, stat.S_IMODE(main_info.st_mode))
        os.replace(temp, main_env)
    finally:
        if temp.exists():
            temp.unlink()

    check = dotenv_values(main_env)
    configured_slot = str(check.get("DEVICE_SLOT_HMAC_KEY") or "")
    result = {
        "actor_exact": check.get("PRIMARY_MGBOOST_ADMIN_ACTOR_ID") == ACTOR_ID,
        "primary_login_matches_protected_admin": (
            check.get("PRIMARY_MGBOOST_ADMIN_LOGIN") == broker_login
        ),
        "slot_key_configured": len(configured_slot.encode("utf-8")) >= 32,
        "slot_key_generated_now": generated,
        "slot_telemetry_keys_distinct": bool(
            configured_slot and telemetry_key
            and not secrets.compare_digest(configured_slot, telemetry_key)
        ),
        "backup_created_or_verified": backup.exists(),
        "secrets_printed": 0,
    }
    required = (
        "actor_exact", "primary_login_matches_protected_admin",
        "slot_key_configured", "slot_telemetry_keys_distinct",
        "backup_created_or_verified",
    )
    if not all(result[key] for key in required):
        raise RuntimeError("post-write canary configuration verification failed")
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--main-env", type=Path, default=Path("/opt/MGBoost_Panel/.env"))
    parser.add_argument(
        "--broker-env", type=Path,
        default=Path("/etc/mgboost/marzban-broker.env"),
    )
    parser.add_argument(
        "--backup", type=Path,
        default=Path("/etc/mgboost/mgboost-panel.env.pre-ph3-03-canary"),
    )
    parser.add_argument("--confirm", required=True)
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise PermissionError("PH3-03 canary configuration requires root")
    if args.confirm != "configure-approved-ph3-03-canary-v1":
        raise RuntimeError("explicit PH3-03 configuration confirmation required")
    print(json.dumps(
        configure(args.main_env.resolve(), args.broker_env.resolve(), args.backup.resolve()),
        sort_keys=True,
    ))


if __name__ == "__main__":
    main()
