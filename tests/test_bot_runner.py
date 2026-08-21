import os
import sys
import tempfile
import threading
import time
from types import SimpleNamespace

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


@pytest.mark.parametrize("field,value", [
    ("port", ""),
    ("port", "invalid"),
    ("port", "70000"),
    ("user", ""),
    ("password", ""),
])
def test_build_proxy_url_rejects_incomplete_configuration(field, value):
    from src.bot_runner import build_proxy_url

    config = {"host": "proxy.example", "port": "1080", "user": "proxy-user", "password": "proxy-password"}
    config[field] = value
    assert build_proxy_url(**config) is None


class SettingsDb:
    def __init__(self, **settings):
        self.settings = settings

    def get_setting(self, key, default=None):
        return self.settings.get(key, default)


def _runner_settings(**overrides):
    settings = {
        "bot:token": "test-token",
        "bot:channel_id": "@test",
        "bot:proxy_enabled": "1",
        "bot:proxy_host": "proxy.example",
        "bot:proxy_port": "1080",
        "bot:proxy_user": "proxy-user",
        "bot:proxy_pass": "proxy-password",
    }
    settings.update(overrides)
    return SettingsDb(**settings)


def test_proxy_enabled_with_valid_config_reaches_run_all(monkeypatch):
    import asyncio
    from src.bot_runner import BotRunner
    import src.bot_monitor as monitor

    runner = BotRunner.from_db(_runner_settings())
    assert runner is not None and runner.proxy_url is not None
    captured = {}

    async def fake_run_all(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(monitor, "run_all", fake_run_all)
    asyncio.run(runner._run_bot())
    assert captured["proxy_url"] == runner.proxy_url


def test_run_all_creates_proxied_session(monkeypatch):
    import asyncio
    import aiogram
    import aiogram.client.session.aiohttp as aiohttp_session_module
    import src.bot_monitor as monitor
    import src.bot_support as support
    import src.stars as stars

    created = {}

    class FakeSession:
        def __init__(self, proxy):
            created["proxy"] = proxy

        async def close(self):
            pass

    class FakeBot:
        def __init__(self, token, session=None):
            self.session = session
            created["bot_session"] = session

    class FakeDispatcher:
        def __init__(self, storage):
            self.stopped = False

        async def start_polling(self, bot, **kwargs):
            created["polling_bot"] = bot
            while not self.stopped:
                await asyncio.sleep(0.001)

        async def stop_polling(self):
            self.stopped = True

    async def wait_for_stop(*args, **kwargs):
        while not args[-1].is_set():
            await asyncio.sleep(0.001)

    monkeypatch.setattr(aiogram, "Bot", FakeBot)
    monkeypatch.setattr(aiogram, "Dispatcher", FakeDispatcher)
    monkeypatch.setattr(aiohttp_session_module, "AiohttpSession", FakeSession)
    monkeypatch.setattr(support, "setup_support_handlers", lambda *a, **kw: None)
    monkeypatch.setattr(monitor, "_monitor_loop", wait_for_stop)
    monkeypatch.setattr(stars, "apply_pending_payments_loop", wait_for_stop)

    stop = threading.Event()
    stop.set()
    asyncio.run(monitor.run_all(
        "test-token", "@test", "socks5h://proxy-user:proxy-password@proxy.example:1080",
        object(), object(), stop,
    ))
    assert created["proxy"].startswith("socks5://")
    assert created["bot_session"] is not None
    assert created["polling_bot"].session is created["bot_session"]


def test_proxy_enabled_with_empty_host_fails_closed(caplog):
    from src.bot_runner import BotRunner

    runner = BotRunner.from_db(_runner_settings(**{"bot:proxy_host": ""}))
    assert runner is None
    assert "configuration is incomplete" in caplog.text


@pytest.mark.parametrize("key", ["bot:proxy_port", "bot:proxy_user", "bot:proxy_pass"])
def test_proxy_enabled_with_incomplete_config_fails_closed(key, caplog):
    from src.bot_runner import BotRunner

    runner = BotRunner.from_db(_runner_settings(**{key: ""}))
    assert runner is None
    assert "configuration is incomplete" in caplog.text
    assert "proxy-user" not in caplog.text
    assert "proxy-password" not in caplog.text
    assert "test-token" not in caplog.text


def test_proxy_disabled_ignores_saved_proxy_fields():
    from src.bot_runner import BotRunner

    runner = BotRunner.from_db(_runner_settings(**{"bot:proxy_enabled": "0"}))
    assert runner is not None
    assert runner.proxy_url is None


def test_proxy_disabled_with_empty_fields_uses_direct_mode():
    from src.bot_runner import BotRunner

    runner = BotRunner.from_db(_runner_settings(
        **{
            "bot:proxy_enabled": "0",
            "bot:proxy_host": "",
            "bot:proxy_user": "",
            "bot:proxy_pass": "",
        }
    ))
    assert runner is not None
    assert runner.proxy_url is None


def test_stars_admin_uses_runner_singleton_bot():
    from src.routes.admin import _running_stars_bot

    bot = object()
    loop = SimpleNamespace(is_running=lambda: True)
    handler = SimpleNamespace(server=SimpleNamespace(
        bot_runner=SimpleNamespace(bot_instance=bot, _loop=loop)
    ))
    selected_bot, selected_loop = _running_stars_bot(handler)
    assert selected_bot is bot
    assert selected_loop is loop


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
    db.set_setting("bot:proxy_enabled", "1")

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
