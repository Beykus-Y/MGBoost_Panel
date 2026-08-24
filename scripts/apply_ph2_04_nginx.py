#!/usr/bin/env python3
"""Apply the narrow PH2-04 nginx version/HSTS policy idempotently."""

from __future__ import annotations

import argparse
import os
import re
import shutil
import tempfile
from pathlib import Path


HSTS_DIRECTIVE = 'add_header Strict-Transport-Security "max-age=31536000" always;'
SERVER_TOKENS_RE = re.compile(r"(?m)^\s*server_tokens\s+off\s*;")
HSTS_RE = re.compile(
    r'(?m)^\s*add_header\s+Strict-Transport-Security\s+"max-age=31536000"\s+always\s*;'
)
CACHE_DIRECTIVE_RE = re.compile(
    r'(?m)^(?P<indent>\s*)add_header\s+Cache-Control\s+no-store\s+always\s*;\s*$'
)


def harden_nginx_conf(text: str) -> str:
    match = re.search(r"(?m)^(?P<indent>\s*)http\s*\{\s*$", text)
    if not match:
        raise ValueError("nginx http block not found")
    additions = []
    if not SERVER_TOKENS_RE.search(text):
        additions.append(f"{match.group('indent')}    server_tokens off;")
    if not HSTS_RE.search(text):
        additions.append(f"{match.group('indent')}    {HSTS_DIRECTIVE}")
    if not additions:
        return text
    insertion = match.end()
    return text[:insertion] + "\n" + "\n".join(additions) + text[insertion:]


def harden_site(text: str) -> tuple[str, int]:
    inserted = 0

    def add_hsts(match: re.Match) -> str:
        nonlocal inserted
        line_end = match.end()
        following = text[line_end:line_end + 180]
        if re.match(rf"\s*{re.escape(HSTS_DIRECTIVE)}", following):
            return match.group(0)
        inserted += 1
        return f"{match.group(0)}\n{match.group('indent')}{HSTS_DIRECTIVE}"

    return CACHE_DIRECTIVE_RE.sub(add_hsts, text), inserted


def atomic_write(path: Path, content: str) -> None:
    stat = path.stat()
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.ph2-04-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, stat.st_mode)
        os.chown(temporary, stat.st_uid, stat.st_gid)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--nginx-conf", type=Path, required=True)
    parser.add_argument("--site", type=Path, action="append", default=[])
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if not args.site:
        parser.error("at least one --site is required")
    if not args.dry_run and args.backup_dir is None:
        parser.error("--backup-dir is required unless --dry-run is used")

    targets: list[tuple[Path, str]] = []
    nginx_path = args.nginx_conf.resolve()
    nginx_original = nginx_path.read_text(encoding="utf-8")
    targets.append((nginx_path, harden_nginx_conf(nginx_original)))

    cache_location_count = 0
    for supplied in args.site:
        path = supplied.resolve()
        original = path.read_text(encoding="utf-8")
        hardened, inserted = harden_site(original)
        cache_location_count += len(CACHE_DIRECTIVE_RE.findall(original))
        targets.append((path, hardened))
    if cache_location_count == 0:
        raise ValueError("no sensitive cache-control locations found")

    changed = [(path, content) for path, content in targets if path.read_text(encoding="utf-8") != content]
    if args.dry_run:
        print(f"changed_files={len(changed)}")
        print(f"sensitive_locations={cache_location_count}")
        return

    backup_dir = args.backup_dir
    backup_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
    os.chmod(backup_dir, 0o700)
    for index, (path, _) in enumerate(targets):
        backup = backup_dir / f"{index:02d}-{path.name}"
        shutil.copy2(path, backup)
        os.chmod(backup, 0o600)
    for path, content in changed:
        atomic_write(path, content)
    print(f"changed_files={len(changed)}")
    print(f"sensitive_locations={cache_location_count}")
    print("backup_created=1")


if __name__ == "__main__":
    main()
