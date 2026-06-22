import os
import sys
import tempfile
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _node(id, name, status="connected", address="1.2.3.4"):
    return {"id": id, "name": name, "address": address, "status": status}


# --- is_node_up ---

def test_connected_is_up():
    from src.bot_monitor import is_node_up
    assert is_node_up("connected") is True


def test_error_is_down():
    from src.bot_monitor import is_node_up
    assert is_node_up("error") is False


def test_disabled_is_down():
    from src.bot_monitor import is_node_up
    assert is_node_up("disabled") is False


def test_connecting_is_down():
    from src.bot_monitor import is_node_up
    assert is_node_up("connecting") is False


# --- in_quiet_hours ---

def test_in_quiet_hours_false_when_empty():
    from src.bot_monitor import in_quiet_hours
    assert in_quiet_hours([]) is False


def test_in_quiet_hours_true_when_inside_window():
    import src.bot_monitor as bm
    from datetime import time as dtime
    original = bm.datetime

    class FakeDT:
        @staticmethod
        def now(tz=None):
            class T:
                def time(self): return dtime(14, 0)
            return T()

    bm.datetime = FakeDT
    try:
        assert bm.in_quiet_hours([{"from": "13:45", "to": "14:30"}]) is True
    finally:
        bm.datetime = original


def test_in_quiet_hours_false_when_outside_window():
    import src.bot_monitor as bm
    from datetime import time as dtime
    original = bm.datetime

    class FakeDT:
        @staticmethod
        def now(tz=None):
            class T:
                def time(self): return dtime(12, 0)
            return T()

    bm.datetime = FakeDT
    try:
        assert bm.in_quiet_hours([{"from": "13:45", "to": "14:30"}]) is False
    finally:
        bm.datetime = original


# --- compute_changes ---

def test_no_change_when_stable_up(db_fixture):
    from src.bot_monitor import compute_changes
    nodes = [_node(1, "beget")]
    states = {1: {"up": True, "last_check": datetime.now(timezone.utc)}}
    changes = compute_changes(nodes, states, db_fixture)
    assert changes == []


def test_detects_node_went_down(db_fixture):
    from src.bot_monitor import compute_changes
    nodes = [_node(1, "beget", status="error")]
    states = {1: {"up": True, "last_check": datetime.now(timezone.utc)}}
    changes = compute_changes(nodes, states, db_fixture)
    assert len(changes) == 1
    assert changes[0]["went_down"] is True
    assert changes[0]["node"]["id"] == 1


def test_detects_node_came_up(db_fixture):
    from src.bot_monitor import compute_changes
    nodes = [_node(1, "beget", status="connected")]
    states = {1: {"up": False, "last_check": datetime.now(timezone.utc)}}
    changes = compute_changes(nodes, states, db_fixture)
    assert len(changes) == 1
    assert changes[0]["came_up"] is True


def test_no_change_on_first_check(db_fixture):
    from src.bot_monitor import compute_changes
    nodes = [_node(1, "beget", status="error")]
    changes = compute_changes(nodes, {}, db_fixture)
    assert changes == []


def test_went_down_in_quiet_hours_sets_flag(db_fixture):
    from src.bot_monitor import compute_changes
    import src.bot_monitor as bm
    from datetime import time as dtime
    original = bm.datetime

    class FakeDT:
        @staticmethod
        def now(tz=None):
            class T:
                def time(self): return dtime(14, 0)
            return T()

    db_fixture.set_node_quiet_hours(1, [{"from": "13:45", "to": "14:30"}])
    nodes = [_node(1, "beget", status="error")]
    states = {1: {"up": True, "last_check": datetime.now(timezone.utc)}}
    bm.datetime = FakeDT
    try:
        changes = compute_changes(nodes, states, db_fixture)
    finally:
        bm.datetime = original
    assert len(changes) == 1
    assert changes[0]["went_down"] is True
    assert changes[0]["in_quiet"] is True


# --- display_name ---

def test_display_name_uses_node_settings_name(db_fixture):
    from src.bot_monitor import get_display_name
    import time
    db_fixture.save_node_setting({
        "node_id": 1, "node_name": "🇷🇺 Новосибирск",
        "node_address": "178.250.186.127", "updated_at": int(time.time()),
    })
    assert get_display_name(_node(1, "nsk"), db_fixture) == "🇷🇺 Новосибирск"


def test_display_name_falls_back_to_marzban_name(db_fixture):
    from src.bot_monitor import get_display_name
    assert get_display_name(_node(1, "beget"), db_fixture) == "beget"


def test_display_name_falls_back_when_node_name_empty(db_fixture):
    from src.bot_monitor import get_display_name
    import time
    db_fixture.save_node_setting({
        "node_id": 1, "node_name": "",
        "node_address": "1.2.3.4", "updated_at": int(time.time()),
    })
    assert get_display_name(_node(1, "beget"), db_fixture) == "beget"


# --- format_status ---

def test_format_status_no_states():
    from src.bot_monitor import format_status
    nodes = [_node(1, "beget")]
    text = format_status(nodes, {}, {})
    assert "beget" in text
    assert "⏳" in text


def test_format_status_up_node():
    from src.bot_monitor import format_status
    nodes = [_node(1, "beget")]
    states = {1: {"up": True, "last_check": datetime.now(timezone.utc)}}
    text = format_status(nodes, states, {1: "🇷🇺 Новосибирск"})
    assert "🟢" in text
    assert "🇷🇺 Новосибирск" in text


def test_format_status_down_node():
    from src.bot_monitor import format_status
    nodes = [_node(1, "beget")]
    states = {1: {"up": False, "last_check": datetime.now(timezone.utc)}}
    text = format_status(nodes, states, {1: "beget"})
    assert "🔴" in text


# --- fixtures ---

@pytest.fixture
def db_fixture():
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
