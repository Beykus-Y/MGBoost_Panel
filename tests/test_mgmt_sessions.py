"""
Tests for the device-management one-time-code / session flow at the
database layer: src.database.Database.create_mgmt_code /
exchange_mgmt_code / get_mgmt_session.

Covers: single-use enforcement, expiry, scope/username on the returned
session, and that raw codes/sessions are never stored in plaintext.
"""
import os
import sys
import tempfile
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


@pytest.fixture
def db():
    tmp = tempfile.mkdtemp()
    os.environ["DATA_DIR"] = tmp
    from importlib import reload
    import src.config as cfg
    reload(cfg)
    cfg.DATA_DIR = tmp
    import src.database as db_mod
    reload(db_mod)
    db_mod.DB_PATH = os.path.join(tmp, "db.sqlite3")
    instance = db_mod.Database()
    yield instance
    instance._conn.close()


def test_create_mgmt_code_returns_raw_code_not_stored_in_plaintext(db):
    code = db.create_mgmt_code(111, "alice")
    assert isinstance(code, str) and len(code) > 10

    rows = db._conn.execute("SELECT code_hash FROM mgmt_codes").fetchall()
    assert len(rows) == 1
    # The raw code must never appear verbatim in the stored hash column.
    assert code not in rows[0]["code_hash"]
    assert rows[0]["code_hash"] != code


def test_exchange_valid_code_creates_session(db):
    code = db.create_mgmt_code(111, "alice")
    result = db.exchange_mgmt_code(code)
    assert result is not None
    assert result["telegram_id"] == 111
    assert result["marzban_username"] == "alice"
    assert result["scope"] == "devices:manage"
    assert isinstance(result["session_id"], str) and len(result["session_id"]) > 10


def test_exchange_code_is_single_use(db):
    code = db.create_mgmt_code(111, "alice")
    first = db.exchange_mgmt_code(code)
    assert first is not None

    second = db.exchange_mgmt_code(code)
    assert second is None


def test_exchange_unknown_code_fails(db):
    assert db.exchange_mgmt_code("this-code-was-never-issued") is None


def test_exchange_code_expires_after_ttl(db):
    code = db.create_mgmt_code(111, "alice", ttl_seconds=1)
    # Force expiry without sleeping the test.
    db._conn.execute(
        "UPDATE mgmt_codes SET expires_at=? WHERE code_hash=(SELECT code_hash FROM mgmt_codes)",
        (int(time.time()) - 5,),
    )
    db._conn.commit()
    assert db.exchange_mgmt_code(code) is None


def test_get_mgmt_session_valid(db):
    code = db.create_mgmt_code(111, "alice")
    result = db.exchange_mgmt_code(code)
    session = db.get_mgmt_session(result["session_id"])
    assert session is not None
    assert session["marzban_username"] == "alice"
    assert session["telegram_id"] == 111
    assert session["scope"] == "devices:manage"


def test_get_mgmt_session_unknown_returns_none(db):
    assert db.get_mgmt_session("not-a-real-session-id") is None


def test_get_mgmt_session_expired_returns_none(db):
    code = db.create_mgmt_code(111, "alice")
    result = db.exchange_mgmt_code(code)
    db._conn.execute(
        "UPDATE mgmt_sessions SET expires_at=? WHERE session_hash IS NOT NULL",
        (int(time.time()) - 5,),
    )
    db._conn.commit()
    assert db.get_mgmt_session(result["session_id"]) is None


def test_session_hash_not_stored_in_plaintext(db):
    code = db.create_mgmt_code(111, "alice")
    result = db.exchange_mgmt_code(code)
    rows = db._conn.execute("SELECT session_hash FROM mgmt_sessions").fetchall()
    assert len(rows) == 1
    assert rows[0]["session_hash"] != result["session_id"]
    assert result["session_id"] not in rows[0]["session_hash"]


def test_two_telegram_ids_can_get_independent_sessions_for_same_username(db):
    # Business model: M:1 — multiple telegram_ids bound to one
    # marzban_username must each be able to obtain management access.
    code_a = db.create_mgmt_code(111, "shared_sub")
    code_b = db.create_mgmt_code(222, "shared_sub")

    session_a = db.exchange_mgmt_code(code_a)
    session_b = db.exchange_mgmt_code(code_b)

    assert session_a["marzban_username"] == "shared_sub"
    assert session_b["marzban_username"] == "shared_sub"
    assert session_a["telegram_id"] == 111
    assert session_b["telegram_id"] == 222
    assert session_a["session_id"] != session_b["session_id"]


def test_code_issuance_logs_audit_event_without_raw_code(db):
    code = db.create_mgmt_code(111, "alice")
    events = db.get_audit_log(event_type="mgmt_code_issued")
    assert len(events) == 1
    assert events[0]["telegram_id"] == 111
    assert events[0]["marzban_username"] == "alice"
    # Raw code must never be written to the audit log.
    assert code not in str(events[0])


def test_exchange_logs_audit_event(db):
    code = db.create_mgmt_code(111, "alice")
    result = db.exchange_mgmt_code(code)
    events = db.get_audit_log(event_type="mgmt_session_created")
    assert len(events) == 1
    assert events[0]["telegram_id"] == 111
    assert events[0]["marzban_username"] == "alice"
    assert result["session_id"] not in str(events[0])
