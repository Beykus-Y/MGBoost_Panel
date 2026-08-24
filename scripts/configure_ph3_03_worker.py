#!/usr/bin/env python3
"""Install the fixed, minimal, root-managed dormant worker environment."""

from __future__ import annotations

import argparse
import os
import secrets
from pathlib import Path


EXPECTED_CONFIRMATION = "ENABLE-ONLY-PH3-03-DORMANT-RECONCILER"
EXPECTED_OPERATION = "op_lw33pjhqhnvorrgh4p754bnc34"


def _read_env(path: Path) -> dict[str, str]:
    result = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        result[key.strip()] = value.strip().strip('"').strip("'")
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--confirm", required=True)
    parser.add_argument("--source-env", default="/opt/MGBoost_Panel/.env")
    parser.add_argument("--output", default="/etc/mgboost/child-worker.env")
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise RuntimeError("worker environment installation requires root")
    if args.confirm != EXPECTED_CONFIRMATION:
        raise RuntimeError("exact dormant worker confirmation is required")
    source = _read_env(Path(args.source_env))
    if source.get("MARZBAN_ADMIN_USER") or source.get("MARZBAN_ADMIN_PASS"):
        raise RuntimeError("main environment unexpectedly contains Marzban SUDO credentials")
    required = {
        "DATA_DIR": source.get("DATA_DIR", "/opt/MGBoost_Panel/data"),
        "MARZBAN_SERVICE_MODE": source.get("MARZBAN_SERVICE_MODE", ""),
        "MARZBAN_BROKER_URL": source.get("MARZBAN_BROKER_URL", ""),
        "MARZBAN_BROKER_AUTH_KEY": source.get("MARZBAN_BROKER_AUTH_KEY", ""),
        "MARZBAN_BROKER_CLIENT_ID": source.get("MARZBAN_BROKER_CLIENT_ID", "mgboost-main"),
    }
    if required["MARZBAN_SERVICE_MODE"] != "broker":
        raise RuntimeError("worker requires typed broker mode")
    if not required["MARZBAN_BROKER_URL"].startswith("http://127.0.0.1:"):
        raise RuntimeError("worker broker URL must be literal localhost")
    if len(required["MARZBAN_BROKER_AUTH_KEY"].encode("utf-8")) < 32:
        raise RuntimeError("worker broker authentication key is missing")
    values = {
        **required,
        "CHILD_WORKER_ENABLED": "1",
        "CHILD_WORKER_MODE": "reconcile_only",
        "CHILD_WORKER_ALLOWED_OPERATION_IDS": EXPECTED_OPERATION,
        "CHILD_WORKER_MAX_ATTEMPTS": "8",
        "CHILD_WORKER_LEASE_SECONDS": "30",
        "CHILD_WORKER_RETRY_BASE_SECONDS": "5",
        "CHILD_WORKER_RETRY_CAP_SECONDS": "300",
        "CHILD_WORKER_RECONCILE_INTERVAL_SECONDS": "60",
        "CHILD_WORKER_POLL_SECONDS": "15",
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.{secrets.token_hex(8)}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            for key, value in values.items():
                if "\n" in value or "\r" in value:
                    raise RuntimeError("invalid environment value")
                handle.write(f"{key}={value}\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chown(temporary, 0, 0)
        os.chmod(temporary, 0o600)
        os.replace(temporary, output)
        directory_fd = os.open(output.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary.exists():
            temporary.unlink()
    print("PH3-03 dormant worker environment installed (secrets not displayed)")


if __name__ == "__main__":
    main()
