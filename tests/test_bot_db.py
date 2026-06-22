import json
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


# --- monitor_quiet_hours в node_settings ---

def test_node_settings_has_monitor_quiet_hours_column(db):
    cols = {r["name"] for r in db._conn.execute("PRAGMA table_info(node_settings)").fetchall()}
    assert "monitor_quiet_hours" in cols


def test_get_node_quiet_hours_default_empty(db):
    assert db.get_node_quiet_hours(1) == []


def test_set_and_get_node_quiet_hours(db):
    qh = [{"from": "13:45", "to": "14:30"}]
    db.set_node_quiet_hours(1, qh)
    assert db.get_node_quiet_hours(1) == qh


def test_set_node_quiet_hours_overwrites(db):
    db.set_node_quiet_hours(1, [{"from": "01:00", "to": "02:00"}])
    db.set_node_quiet_hours(1, [{"from": "13:45", "to": "14:30"}])
    assert db.get_node_quiet_hours(1) == [{"from": "13:45", "to": "14:30"}]


def test_quiet_hours_independent_per_node(db):
    db.set_node_quiet_hours(1, [{"from": "01:00", "to": "02:00"}])
    db.set_node_quiet_hours(2, [{"from": "13:00", "to": "14:00"}])
    assert db.get_node_quiet_hours(1) == [{"from": "01:00", "to": "02:00"}]
    assert db.get_node_quiet_hours(2) == [{"from": "13:00", "to": "14:00"}]


# --- bot settings ---

def test_get_bot_setting_default(db):
    assert db.get_setting("bot:token") is None
    assert db.get_setting("bot:token", "default_val") == "default_val"


def test_set_and_get_bot_setting(db):
    db.set_setting("bot:token", "123:ABCDEF")
    assert db.get_setting("bot:token") == "123:ABCDEF"


def test_bot_settings_independent_from_other_settings(db):
    db.set_setting("some_other_key", "value1")
    db.set_setting("bot:channel_id", "@MGBoost_News")
    assert db.get_setting("some_other_key") == "value1"
    assert db.get_setting("bot:channel_id") == "@MGBoost_News"


def test_pinned_message_id_persisted(db):
    assert db.get_setting("bot:pinned_message_id") is None
    db.set_setting("bot:pinned_message_id", "99887766")
    assert db.get_setting("bot:pinned_message_id") == "99887766"


def test_pinned_message_id_overwrite(db):
    db.set_setting("bot:pinned_message_id", "111")
    db.set_setting("bot:pinned_message_id", "222")
    assert db.get_setting("bot:pinned_message_id") == "222"
