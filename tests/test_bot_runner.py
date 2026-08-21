import os
import sys
import tempfile
import threading
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp())


def test_build_proxy_url_uses_socks5h():
    from src.bot_runner import build_proxy_url
    url = build_proxy_url("150.241.74.147", 1080, "socks", "telegram")
    assert url.startswith("socks5h://")
    assert "150.241.74.147:1080" in url
    assert "socks:telegram" in url


def test_build_proxy_url_none_when_no_host():
    from src.bot_runner import build_proxy_url
    assert build_proxy_url(None, 1080, "socks", "telegram") is None
    assert build_proxy_url("", 1080, "socks", "telegram") is None


def test_bot_thread_starts_and_sets_flag():
    """BotRunner.start() запускает поток, stop() его останавливает."""
    from src.bot_runner import BotRunner

    started = threading.Event()

    class FakeRunner(BotRunner):
        async def _run_bot(self):
            started.set()
            while not self._stop_event.is_set():
                await __import__("asyncio").sleep(0.05)

    runner = FakeRunner(bot_token="fake", channel_id="@test", proxy_url=None, db=None)
    runner.start()
    assert started.wait(timeout=2), "бот не стартовал за 2 секунды"
    runner.stop()
    runner.join(timeout=3)
    assert not runner.is_alive()


def test_bot_runner_reads_settings_from_db():
    """BotRunner.from_db() читает настройки из БД."""
    import importlib
    tmp = tempfile.mkdtemp()
    os.environ["DATA_DIR"] = tmp
    import src.config as cfg
    importlib.reload(cfg)
    cfg.DATA_DIR = tmp
    import src.database as db_mod
    importlib.reload(db_mod)
    db_mod.DB_PATH = os.path.join(tmp, "db.sqlite3")
    db = db_mod.Database()

    db.set_setting("bot:token", "999:TOKEN")
    db.set_setting("bot:channel_id", "@TestChan")
    db.set_setting("bot:proxy_host", "150.241.74.147")
    db.set_setting("bot:proxy_port", "1080")
    db.set_setting("bot:proxy_user", "socks")
    db.set_setting("bot:proxy_pass", "telegram")

    from src.bot_runner import BotRunner
    runner = BotRunner.from_db(db)
    assert runner.bot_token == "999:TOKEN"
    assert runner.channel_id == "@TestChan"
    assert runner.proxy_url == "socks5h://socks:telegram@150.241.74.147:1080"


def test_bot_runner_from_db_returns_none_if_no_token():
    import importlib
    tmp = tempfile.mkdtemp()
    os.environ["DATA_DIR"] = tmp
    import src.config as cfg
    importlib.reload(cfg)
    cfg.DATA_DIR = tmp
    import src.database as db_mod
    importlib.reload(db_mod)
    db_mod.DB_PATH = os.path.join(tmp, "db.sqlite3")
    db = db_mod.Database()

    from src.bot_runner import BotRunner
    assert BotRunner.from_db(db) is None


def test_bot_runner_cooperative_shutdown_and_restart_processes_pending_payments(caplog):
    import asyncio
    import importlib

    tmp = tempfile.mkdtemp()
    os.environ["DATA_DIR"] = tmp
    import src.config as cfg
    importlib.reload(cfg)
    cfg.DATA_DIR = tmp
    import src.database as db_mod
    importlib.reload(db_mod)
    db_mod.DB_PATH = os.path.join(tmp, "db.sqlite3")
    db = db_mod.Database()

    class Marzban:
        def __init__(self):
            now = int(time.time())
            self.users = {
                "alice": {"username": "alice", "expire": now + 1000, "status": "active"},
                "bob": {"username": "bob", "expire": now + 2000, "status": "active"},
            }
            self.modify_calls = []

        def get_user(self, username, token):
            return dict(self.users[username])

        def modify_user(self, username, payload, token):
            self.modify_calls.append((username, dict(payload)))
            self.users[username].update(payload)

    class Bot:
        async def send_message(self, chat_id, text, **kwargs):
            pass

    marzban = Marzban()

    def paid(username, charge):
        inv = db.create_stars_invoice(111, username, None, "t", 1, 10)
        db.mark_invoice_paid(inv["id"], charge, None, 111, 10)
        return inv

    first_invoice = paid("alice", "runner-charge-1")

    from src.bot_runner import BotRunner
    from src.stars import _tick

    class PaymentRunner(BotRunner):
        def __init__(self, started):
            super().__init__("fake", "@test", None, db, marzban)
            self.started_event = started

        async def _run_bot(self):
            await _tick(Bot(), db, marzban, "tok")
            self.started_event.set()
            while not self._stop_event.is_set():
                await asyncio.sleep(0.01)

    first_started = threading.Event()
    first = PaymentRunner(first_started)
    first.start()
    assert first_started.wait(timeout=3)
    first.stop()
    first.join(timeout=3)
    assert not first.is_alive()
    assert db.get_invoice(first_invoice["id"])["status"] == "applied"

    second_invoice = paid("bob", "runner-charge-2")
    second_started = threading.Event()
    second = PaymentRunner(second_started)
    second.start()
    assert second_started.wait(timeout=3)
    second.stop()
    second.join(timeout=3)
    assert not second.is_alive()
    assert db.get_invoice(second_invoice["id"])["status"] == "applied"
    assert [call[0] for call in marzban.modify_calls] == ["alice", "bob"]
    assert "Event loop stopped before Future completed" not in caplog.text
    db._conn.close()
