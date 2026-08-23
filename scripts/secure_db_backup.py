#!/usr/bin/env python3
"""Create and verify encrypted MGBoost/Marzban SQLite backups.

The scheduled mode never leaves a plaintext backup outside a private
TemporaryDirectory.  GnuPG symmetric encryption uses a root-only passphrase
file; the final artifact is atomically published with mode 0600.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import sqlite3
import subprocess
import tarfile
import tempfile
from pathlib import Path


DEFAULT_SOURCES = {
    "mgboost.sqlite3": Path("/opt/MGBoost_Panel/data/db.sqlite3"),
    "marzban.sqlite3": Path("/var/lib/marzban/db.sqlite3"),
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _quick_check(path: Path) -> None:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        result = connection.execute("PRAGMA quick_check").fetchone()[0]
        if result != "ok":
            raise RuntimeError(f"SQLite quick_check failed for {path.name}")
    finally:
        connection.close()


def _online_backup(source: Path, destination: Path) -> None:
    source_db = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    destination_db = sqlite3.connect(destination)
    try:
        source_db.backup(destination_db)
    finally:
        destination_db.close()
        source_db.close()
    os.chmod(destination, 0o600)
    _quick_check(destination)


def _gpg(passphrase_file: Path, *arguments: str) -> None:
    key_stat = passphrase_file.stat()
    if key_stat.st_uid != os.geteuid() or key_stat.st_mode & 0o077:
        raise PermissionError("backup passphrase file must be owned by the service UID and mode 0600")
    with tempfile.TemporaryDirectory(prefix="mgboost-gpg-") as home_name:
        os.chmod(home_name, 0o700)
        subprocess.run(
            [
                "gpg", "--no-options", "--homedir", home_name,
                "--batch", "--yes", "--pinentry-mode", "loopback",
                "--passphrase-file", str(passphrase_file), *arguments,
            ],
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )


def create_backup(output_dir: Path, passphrase_file: Path) -> Path:
    output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(output_dir, 0o700)
    timestamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    final_path = output_dir / f"mgboost-db-{timestamp}.tar.gpg"
    if final_path.exists():
        raise RuntimeError("backup artifact already exists")

    with tempfile.TemporaryDirectory(prefix="mgboost-backup-") as temp_name:
        temp = Path(temp_name)
        os.chmod(temp, 0o700)
        manifest = {
            "format": 1,
            "created_at_utc": timestamp,
            "retention_days": 90,
            "databases": {},
        }
        for archive_name, source in DEFAULT_SOURCES.items():
            destination = temp / archive_name
            _online_backup(source, destination)
            manifest["databases"][archive_name] = {
                "sha256": _sha256(destination),
                "size": destination.stat().st_size,
            }
        manifest_path = temp / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n")
        os.chmod(manifest_path, 0o600)
        tar_path = temp / "backup.tar"
        with tarfile.open(tar_path, "w") as archive:
            for name in sorted([*DEFAULT_SOURCES, "manifest.json"]):
                archive.add(temp / name, arcname=name, recursive=False)
        os.chmod(tar_path, 0o600)
        encrypted_temp = output_dir / (final_path.name + ".tmp")
        try:
            _gpg(
                passphrase_file,
                "--symmetric", "--cipher-algo", "AES256",
                "--output", str(encrypted_temp), str(tar_path),
            )
            os.chmod(encrypted_temp, 0o600)
            os.replace(encrypted_temp, final_path)
        finally:
            if encrypted_temp.exists():
                encrypted_temp.unlink()
    return final_path


def verify_backup(artifact: Path, passphrase_file: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="mgboost-restore-") as temp_name:
        temp = Path(temp_name)
        os.chmod(temp, 0o700)
        tar_path = temp / "backup.tar"
        _gpg(
            passphrase_file,
            "--decrypt", "--output", str(tar_path), str(artifact),
        )
        with tarfile.open(tar_path, "r") as archive:
            names = set(archive.getnames())
            expected = {*DEFAULT_SOURCES, "manifest.json"}
            if names != expected:
                raise RuntimeError("unexpected encrypted backup member set")
            archive.extractall(temp / "restore", filter="data")
        restore = temp / "restore"
        manifest = json.loads((restore / "manifest.json").read_text())
        if manifest.get("format") != 1 or manifest.get("retention_days") != 90:
            raise RuntimeError("invalid backup manifest")
        for name in DEFAULT_SOURCES:
            database = restore / name
            expected_hash = manifest["databases"][name]["sha256"]
            if _sha256(database) != expected_hash:
                raise RuntimeError(f"checksum mismatch for {name}")
            _quick_check(database)


def retention_candidates(output_dir: Path, *, now: float | None = None) -> list[Path]:
    cutoff = (now if now is not None else dt.datetime.now().timestamp()) - 90 * 86400
    return sorted(
        path for path in output_dir.glob("mgboost-db-*.tar.gpg")
        if path.is_file() and path.stat().st_mtime < cutoff
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=Path("/var/backups/mgboost"))
    parser.add_argument(
        "--passphrase-file", type=Path,
        default=Path("/etc/mgboost/backup.passphrase"),
    )
    parser.add_argument("--verify", type=Path)
    parser.add_argument("--retention-dry-run", action="store_true")
    parser.add_argument("--apply-retention", action="store_true")
    args = parser.parse_args()

    if args.verify:
        verify_backup(args.verify, args.passphrase_file)
        print("encrypted_backup_restore=PASS")
        return 0
    if args.retention_dry_run:
        print("retention_candidates=" + str(len(retention_candidates(args.output_dir))))
        return 0

    artifact = create_backup(args.output_dir, args.passphrase_file)
    verify_backup(artifact, args.passphrase_file)
    print("encrypted_backup_create=PASS")
    print("encrypted_backup_restore=PASS")
    if args.apply_retention:
        candidates = retention_candidates(args.output_dir)
        for candidate in candidates:
            candidate.unlink()
        print("retention_deleted=" + str(len(candidates)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
