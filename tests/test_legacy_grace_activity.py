"""PH4-05 privacy-safe grace activity counters: recording, retention,
24h/72h aggregation, no raw identifiers stored."""

import importlib
import os
import tempfile

import pytest

from src.legacy_grace_activity import (
    InvalidChannel,
    RETENTION_DAYS,
    SECONDS_PER_DAY,
    cleanup_expired,
    count_since,
    last_seen,
    record_activity,
)

from tests.test_child_provisioning import PRIMARY, PRIMARY_LOGIN


@pytest.fixture
def db(monkeypatch):
    tmp = tempfile.mkdtemp()
    monkeypatch.setenv("DATA_DIR", tmp)
    monkeypatch.setenv("PRIMARY_MGBOOST_ADMIN_ACTOR_ID", PRIMARY)
    monkeypatch.setenv("PRIMARY_MGBOOST_ADMIN_LOGIN", PRIMARY_LOGIN)
    import src.config as config
    import src.database as database
    importlib.reload(config)
    importlib.reload(database)
    database.DB_PATH = os.path.join(tmp, "db.sqlite3")
    instance = database.Database()
    yield instance
    instance._conn.close()


def test_invalid_channel_rejected(db):
    import src.database as database
    acct = db.accounts.create_account("DIRECT")["id"]
    with pytest.raises(InvalidChannel):
        record_activity(database.DB_PATH, acct, "NOT_A_CHANNEL", now=1000)


def test_record_and_count_accumulate_within_day(db):
    import src.database as database
    acct = db.accounts.create_account("DIRECT")["id"]
    day = 1_000_000 - (1_000_000 % SECONDS_PER_DAY)
    record_activity(database.DB_PATH, acct, "LEGACY", now=day + 10)
    record_activity(database.DB_PATH, acct, "LEGACY", now=day + 20)
    record_activity(database.DB_PATH, acct, "OPAQUE", now=day + 30)

    assert count_since(db._conn, acct, "LEGACY", since=day, now=day + 100) == 2
    assert count_since(db._conn, acct, "OPAQUE", since=day, now=day + 100) == 1
    assert last_seen(db._conn, acct, "LEGACY") == day + 20


def test_count_since_windows_24h_72h(db):
    import src.database as database
    acct = db.accounts.create_account("DIRECT")["id"]
    now = 30 * SECONDS_PER_DAY
    record_activity(database.DB_PATH, acct, "LEGACY", now=now)                      # today
    record_activity(database.DB_PATH, acct, "LEGACY", now=now - 2 * SECONDS_PER_DAY)  # 2 days ago
    record_activity(database.DB_PATH, acct, "LEGACY", now=now - 10 * SECONDS_PER_DAY)  # outside 72h

    assert count_since(db._conn, acct, "LEGACY", since=now - SECONDS_PER_DAY, now=now) == 1
    assert count_since(db._conn, acct, "LEGACY", since=now - 3 * SECONDS_PER_DAY, now=now) == 2


def test_no_raw_identifiers_stored(db):
    """Only account_id/channel/day/counts are ever stored -- never a raw
    token, HWID, UUID, URL, cookie or header."""
    import src.database as database
    acct = db.accounts.create_account("DIRECT")["id"]
    record_activity(database.DB_PATH, acct, "LEGACY", now=1000)
    row = db._conn.execute(
        "SELECT * FROM mgboost_legacy_grace_activity_daily WHERE account_id=?", (acct,),
    ).fetchone()
    stored_columns = set(row.keys())
    assert stored_columns == {"day_start", "account_id", "channel", "request_count",
                               "first_seen", "last_seen"}


def test_cleanup_expired_enforces_retention(db):
    import src.database as database
    acct = db.accounts.create_account("DIRECT")["id"]
    now = 200 * SECONDS_PER_DAY
    old_day = now - (RETENTION_DAYS + 5) * SECONDS_PER_DAY
    record_activity(database.DB_PATH, acct, "LEGACY", now=old_day)
    result = cleanup_expired(database.DB_PATH, now=now)
    assert result["rows_deleted"] == 1
    assert last_seen(db._conn, acct, "LEGACY") is None


def test_cleanup_expired_safe_before_schema_exists(tmp_path):
    import sqlite3
    db_path = str(tmp_path / "bare.sqlite3")
    sqlite3.connect(db_path).close()
    result = cleanup_expired(db_path, now=1000)
    assert result == {"rows_deleted": 0}
