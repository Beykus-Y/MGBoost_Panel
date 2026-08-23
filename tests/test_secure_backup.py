import os
import sqlite3
import time
from pathlib import Path


def _database(path: Path, marker: str):
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE evidence (value TEXT NOT NULL)")
    connection.execute("INSERT INTO evidence VALUES (?)", (marker,))
    connection.commit()
    connection.close()


def test_encrypted_backup_round_trip_and_retention(tmp_path, monkeypatch):
    from scripts import secure_db_backup as backup

    raw_marker = "raw-subscription-bearer-must-not-appear-in-artifact"
    first = tmp_path / "first.sqlite3"
    second = tmp_path / "second.sqlite3"
    _database(first, raw_marker)
    _database(second, "marzban")
    monkeypatch.setattr(
        backup,
        "DEFAULT_SOURCES",
        {"mgboost.sqlite3": first, "marzban.sqlite3": second},
    )
    key = tmp_path / "passphrase"
    key.write_text("test-only-" + "x" * 64)
    key.chmod(0o600)
    output = tmp_path / "backups"

    artifact = backup.create_backup(output, key)
    backup.verify_backup(artifact, key)

    encrypted = artifact.read_bytes()
    assert raw_marker.encode() not in encrypted
    assert b"SQLite format 3" not in encrypted
    assert artifact.stat().st_mode & 0o077 == 0
    assert backup.retention_candidates(output) == []
    old = time.time() - 91 * 86400
    os.utime(artifact, (old, old))
    assert backup.retention_candidates(output) == [artifact]
