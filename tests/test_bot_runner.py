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
