"""
Tests for the audit log mechanism (Item 2) and the M:1 tg_id<->marzban_username
binding model it wraps (multiple telegram_ids may legitimately share one
marzban_username; each telegram_id keeps exactly one current binding).
"""
import os
import sys
import tempfile

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


# --- M:1 binding model (business rule, do not regress) --------------------

def test_multiple_telegram_ids_can_bind_same_username(db):
    """One marzban subscription may legitimately be shared by several
    Telegram accounts at once (e.g. a 5-device family plan)."""
    db.save_tg_user(111, "shared_user")
    db.save_tg_user(222, "shared_user")
    db.save_tg_user(333, "shared_user")

    assert db.get_tg_user(111)["marzban_username"] == "shared_user"
    assert db.get_tg_user(222)["marzban_username"] == "shared_user"
    assert db.get_tg_user(333)["marzban_username"] == "shared_user"


def test_rebind_does_not_affect_other_telegram_ids_on_old_username(db):
    db.save_tg_user(111, "old_user")
    db.save_tg_user(222, "old_user")  # sibling on the old subscription

    db.save_tg_user(111, "new_user")  # 111 rebinds away

    assert db.get_tg_user(111)["marzban_username"] == "new_user"
    assert db.get_tg_user(222)["marzban_username"] == "old_user"  # untouched


def test_rebind_does_not_affect_other_telegram_ids_already_on_new_username(db):
    db.save_tg_user(222, "new_user")  # already bound to the target username
    db.save_tg_user(111, "old_user")

    db.save_tg_user(111, "new_user")  # 111 rebinds onto the same username as 222

    assert db.get_tg_user(111)["marzban_username"] == "new_user"
    assert db.get_tg_user(222)["marzban_username"] == "new_user"  # unaffected, still bound


# --- audit log: bind / rebind ----------------------------------------------

def test_audit_log_bind_event_on_first_bind(db):
    db.save_tg_user(111, "alice")
    events = db.get_audit_log(telegram_id=111)
    assert len(events) == 1
    assert events[0]["event_type"] == "tg_bound"
    assert events[0]["telegram_id"] == 111
    assert events[0]["marzban_username"] == "alice"


def test_audit_log_no_event_when_rebinding_to_same_username(db):
    db.save_tg_user(111, "alice")
    db.save_tg_user(111, "alice")  # idempotent re-save, not a rebind
    events = db.get_audit_log(telegram_id=111)
    assert len(events) == 1
    assert events[0]["event_type"] == "tg_bound"


def test_audit_log_rebind_event_with_old_and_new_username(db):
    db.save_tg_user(111, "alice")
    db.save_tg_user(111, "bob")

    events = db.get_audit_log(telegram_id=111)
    assert len(events) == 2
    rebind = [e for e in events if e["event_type"] == "tg_rebound"][0]
    assert rebind["telegram_id"] == 111
    assert rebind["marzban_username"] == "bob"
    assert rebind["metadata"]["old_username"] == "alice"
    assert rebind["metadata"]["new_username"] == "bob"


def test_save_tg_user_return_value_reports_rebind(db):
    r1 = db.save_tg_user(111, "alice")
    assert r1["rebound"] is False
    r2 = db.save_tg_user(111, "bob")
    assert r2["rebound"] is True
    assert r2["old_username"] == "alice"


# --- audit log: device rename / deactivate ---------------------------------

def _register_device(db, username="alice", request_key="hwid:abc"):
    db.check_device_access(username, "tok1", {"request_key": request_key, "device_name": "Phone"})
    devices = db.get_user_devices(username)
    return devices[0]["id"]


def test_audit_log_device_renamed(db):
    device_id = _register_device(db)
    ok = db.rename_device(device_id, "alice", "My Phone")
    assert ok is True

    events = db.get_audit_log(event_type="device_renamed", marzban_username="alice")
    assert len(events) == 1
    assert events[0]["target"] == str(device_id)
    assert events[0]["metadata"]["new_name"] == "My Phone"


def test_audit_log_not_written_when_rename_fails(db):
    ok = db.rename_device(9999, "alice", "Nope")
    assert ok is False
    events = db.get_audit_log(event_type="device_renamed")
    assert events == []


def test_audit_log_device_deactivated(db):
    device_id = _register_device(db)
    ok = db.deactivate_device(device_id, "alice")
    assert ok is True

    events = db.get_audit_log(event_type="device_deactivated", marzban_username="alice")
    assert len(events) == 1
    assert events[0]["target"] == str(device_id)


def test_audit_log_never_stores_secrets(db):
    """Sanity check: nothing resembling a token/secret is ever passed as metadata
    by the wired-in call sites (bind/rebind/rename/deactivate)."""
    db.save_tg_user(111, "alice")
    db.save_tg_user(111, "bob")
    device_id = _register_device(db, username="bob")
    db.rename_device(device_id, "bob", "Renamed")
    db.deactivate_device(device_id, "bob")

    for event in db.get_audit_log(limit=100):
        blob = str(event["metadata"])
        assert "tok1" not in blob
        assert "hwid:" not in blob


# --- audit log write failure must not break the primary action -------------
#
# sqlite3.Connection is a C type whose bound methods can't be monkeypatched
# directly (attribute is read-only), so we swap db._conn for a thin proxy
# that intercepts only the audit_log INSERT and forwards everything else.

class _FailingAuditConn:
    def __init__(self, real):
        self._real = real

    def execute(self, sql, *args, **kwargs):
        if "INSERT INTO audit_log" in sql:
            raise RuntimeError("simulated audit_log write failure")
        return self._real.execute(sql, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_log_audit_event_failure_does_not_raise(db, monkeypatch):
    monkeypatch.setattr(db, "_conn", _FailingAuditConn(db._conn))
    # Should swallow the error internally (and log it), not raise.
    db.log_audit_event("tg_bound", telegram_id=1, marzban_username="x")


def test_rename_device_succeeds_even_if_audit_log_write_fails(db, monkeypatch):
    device_id = _register_device(db)
    monkeypatch.setattr(db, "_conn", _FailingAuditConn(db._conn))
    ok = db.rename_device(device_id, "alice", "Still Works")
    assert ok is True


def test_deactivate_device_succeeds_even_if_audit_log_write_fails(db, monkeypatch):
    device_id = _register_device(db)
    monkeypatch.setattr(db, "_conn", _FailingAuditConn(db._conn))
    ok = db.deactivate_device(device_id, "alice")
    assert ok is True


def test_save_tg_user_succeeds_even_if_audit_log_write_fails(db, monkeypatch):
    monkeypatch.setattr(db, "_conn", _FailingAuditConn(db._conn))
    db.save_tg_user(111, "alice")
    assert db.get_tg_user(111)["marzban_username"] == "alice"


def test_audit_log_failure_is_logged(db, monkeypatch, caplog):
    import logging
    caplog.set_level(logging.ERROR, logger="src.database")
    monkeypatch.setattr(db, "_conn", _FailingAuditConn(db._conn))
    db.log_audit_event("tg_bound", telegram_id=1, marzban_username="x")
    assert any("audit_log write failed" in r.message for r in caplog.records)
