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


# --- tg_users ---

def test_get_tg_user_returns_none_if_not_registered(db):
    assert db.get_tg_user(123456) is None


def test_save_and_get_tg_user(db):
    db.save_tg_user(123456, "vpn_user1")
    u = db.get_tg_user(123456)
    assert u is not None
    assert u["telegram_id"] == 123456
    assert u["marzban_username"] == "vpn_user1"
    assert "registered_at" in u


def test_save_tg_user_overwrites(db):
    db.save_tg_user(123456, "old_name")
    db.save_tg_user(123456, "new_name")
    assert db.get_tg_user(123456)["marzban_username"] == "new_name"


def test_tg_users_independent(db):
    db.save_tg_user(1, "alice")
    db.save_tg_user(2, "bob")
    assert db.get_tg_user(1)["marzban_username"] == "alice"
    assert db.get_tg_user(2)["marzban_username"] == "bob"


# --- tickets ---

def test_create_ticket_returns_id(db):
    tid = db.create_ticket(111)
    assert isinstance(tid, int)
    assert tid > 0


def test_get_open_ticket_none_if_no_ticket(db):
    assert db.get_open_ticket(999) is None


def test_get_open_ticket_after_create(db):
    db.create_ticket(111, marzban_username="alice", status="open")
    t = db.get_open_ticket(111)
    assert t is not None
    assert t["telegram_id"] == 111
    assert t["status"] == "open"
    assert t["marzban_username"] == "alice"


def test_get_open_ticket_ignores_closed(db):
    db.create_ticket(111, status="closed")
    assert db.get_open_ticket(111) is None


def test_update_ticket_status(db):
    tid = db.create_ticket(111)
    db.update_ticket_status(tid, "waiting_human")
    t = db.get_ticket(tid)
    assert t["status"] == "waiting_human"


def test_get_ticket_by_id(db):
    tid = db.create_ticket(222, marzban_username="bob", status="open")
    t = db.get_ticket(tid)
    assert t["id"] == tid
    assert t["telegram_id"] == 222
    assert t["marzban_username"] == "bob"


def test_get_ticket_none_for_unknown(db):
    assert db.get_ticket(9999) is None


def test_list_tickets_all(db):
    db.create_ticket(1, status="open")
    db.create_ticket(2, status="closed")
    db.create_ticket(3, status="waiting_human")
    tickets = db.list_tickets()
    assert len(tickets) == 3


def test_list_tickets_filter_by_status(db):
    db.create_ticket(1, status="open")
    db.create_ticket(2, status="closed")
    db.create_ticket(3, status="open")
    open_tickets = db.list_tickets(status="open")
    assert len(open_tickets) == 2
    assert all(t["status"] == "open" for t in open_tickets)


def test_list_tickets_newest_first(db):
    t1 = db.create_ticket(1, status="open")
    t2 = db.create_ticket(2, status="open")
    tickets = db.list_tickets()
    assert tickets[0]["id"] == t2
    assert tickets[1]["id"] == t1


# --- ticket_messages ---

def test_add_ticket_message_returns_id(db):
    tid = db.create_ticket(111)
    mid = db.add_ticket_message(tid, "user", "Привет!")
    assert isinstance(mid, int) and mid > 0


def test_get_ticket_messages_empty(db):
    tid = db.create_ticket(111)
    assert db.get_ticket_messages(tid) == []


def test_get_ticket_messages_in_order(db):
    tid = db.create_ticket(111)
    db.add_ticket_message(tid, "user", "Вопрос")
    db.add_ticket_message(tid, "ai", "Ответ")
    db.add_ticket_message(tid, "human", "Оператор")
    msgs = db.get_ticket_messages(tid)
    assert len(msgs) == 3
    assert msgs[0]["role"] == "user"
    assert msgs[1]["role"] == "ai"
    assert msgs[2]["role"] == "human"


def test_get_ticket_messages_limit(db):
    tid = db.create_ticket(111)
    for i in range(15):
        db.add_ticket_message(tid, "user", f"msg {i}")
    msgs = db.get_ticket_messages(tid, limit=5)
    assert len(msgs) == 5


def test_ticket_messages_isolated_per_ticket(db):
    t1 = db.create_ticket(1)
    t2 = db.create_ticket(2)
    db.add_ticket_message(t1, "user", "T1 msg")
    db.add_ticket_message(t2, "user", "T2 msg")
    assert len(db.get_ticket_messages(t1)) == 1
    assert db.get_ticket_messages(t1)[0]["text"] == "T1 msg"


def test_add_message_updates_ticket_updated_at(db):
    tid = db.create_ticket(111)
    t_before = db.get_ticket(tid)["updated_at"]
    time.sleep(0.01)
    db.add_ticket_message(tid, "user", "hey")
    t_after = db.get_ticket(tid)["updated_at"]
    assert t_after >= t_before
