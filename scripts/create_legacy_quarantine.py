#!/usr/bin/env python3
"""Create the single encrypted PH1-06 legacy-token evidence snapshot."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
from pathlib import Path

from secure_db_backup import _gpg, _online_backup, _quick_check, _sha256


def _safe_name(path: Path, sequence: int) -> str:
    return f"nginx/{sequence:03d}-{path.name}"


def create_snapshot(output_dir: Path, passphrase_file: Path, legacy_alias: Path) -> Path:
    output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(output_dir, 0o700)
    existing = list(output_dir.glob("legacy-token-evidence-*.tar.gpg"))
    if existing:
        raise RuntimeError("the single quarantine snapshot already exists")
    timestamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    final = output_dir / f"legacy-token-evidence-{timestamp}.tar.gpg"

    with tempfile.TemporaryDirectory(prefix="mgboost-quarantine-") as temp_name:
        temp = Path(temp_name)
        os.chmod(temp, 0o700)
        files: dict[str, Path] = {}

        database = temp / "mgboost-legacy.sqlite3"
        _online_backup(Path("/opt/MGBoost_Panel/data/db.sqlite3"), database)
        files["mgboost-legacy.sqlite3"] = database

        alias_copy = temp / "legacy-alias.json"
        shutil.copyfile(legacy_alias, alias_copy)
        os.chmod(alias_copy, 0o600)
        files["legacy-alias.json"] = alias_copy

        journal = temp / "mgboost-journal.log"
        with journal.open("wb") as handle:
            subprocess.run(
                [
                    "journalctl", "-u", "mgboost-panel", "-u",
                    "mgboost-marzban-broker", "--no-pager", "--output=short-iso",
                ],
                check=True,
                stdout=handle,
                stderr=subprocess.PIPE,
            )
        os.chmod(journal, 0o600)
        files["mgboost-journal.log"] = journal

        for sequence, source in enumerate(sorted(Path("/var/log/nginx").glob("*.log*"))):
            if not source.is_file():
                continue
            destination = temp / f"nginx-{sequence:03d}-{source.name}"
            shutil.copyfile(source, destination)
            os.chmod(destination, 0o600)
            files[_safe_name(source, sequence)] = destination

        manifest = {
            "format": 1,
            "created_at_utc": timestamp,
            "retention_days": 180,
            "purpose": "PH1-06 legacy subscription-token evidence quarantine",
            "files": {
                name: {"sha256": _sha256(path), "size": path.stat().st_size}
                for name, path in sorted(files.items())
            },
        }
        manifest_path = temp / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n")
        os.chmod(manifest_path, 0o600)
        files["manifest.json"] = manifest_path

        tar_path = temp / "quarantine.tar"
        with tarfile.open(tar_path, "w") as archive:
            for name, source in sorted(files.items()):
                archive.add(source, arcname=name, recursive=False)
        os.chmod(tar_path, 0o600)
        encrypted_temp = output_dir / (final.name + ".tmp")
        try:
            _gpg(
                passphrase_file,
                "--symmetric", "--cipher-algo", "AES256",
                "--output", str(encrypted_temp), str(tar_path),
            )
            os.chmod(encrypted_temp, 0o600)
            os.replace(encrypted_temp, final)
        finally:
            if encrypted_temp.exists():
                encrypted_temp.unlink()
    return final


def verify_snapshot(artifact: Path, passphrase_file: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="mgboost-quarantine-restore-") as temp_name:
        temp = Path(temp_name)
        os.chmod(temp, 0o700)
        tar_path = temp / "quarantine.tar"
        _gpg(passphrase_file, "--decrypt", "--output", str(tar_path), str(artifact))
        restore = temp / "restore"
        with tarfile.open(tar_path, "r") as archive:
            archive.extractall(restore, filter="data")
        manifest = json.loads((restore / "manifest.json").read_text())
        if manifest.get("format") != 1 or manifest.get("retention_days") != 180:
            raise RuntimeError("invalid quarantine manifest")
        for name, expected in manifest["files"].items():
            path = restore / name
            if not path.is_file() or _sha256(path) != expected["sha256"]:
                raise RuntimeError(f"quarantine checksum mismatch: {name}")
        _quick_check(restore / "mgboost-legacy.sqlite3")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output-dir", type=Path,
        default=Path("/var/backups/mgboost-quarantine"),
    )
    parser.add_argument(
        "--passphrase-file", type=Path,
        default=Path("/etc/mgboost/quarantine.passphrase"),
    )
    parser.add_argument(
        "--legacy-alias", type=Path,
        default=Path("/root/mgboost-rollout-ph1-20260824-a/legacy-alias.json"),
    )
    parser.add_argument("--verify", type=Path)
    args = parser.parse_args()
    if args.verify:
        verify_snapshot(args.verify, args.passphrase_file)
        print("quarantine_restore=PASS")
        return 0
    artifact = create_snapshot(args.output_dir, args.passphrase_file, args.legacy_alias)
    verify_snapshot(artifact, args.passphrase_file)
    print("quarantine_create=PASS")
    print("quarantine_restore=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
