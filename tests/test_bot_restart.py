import os
import sys
import tempfile
import threading
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


class _Wfile:
    def __init__(self): self._buf = b""
    def write(self, data): self._buf += data

class _Rfile:
    def __init__(self, data): self._data = data
    def read(self, n): return self._data[:n]

class FakeServer:
    def __init__(self, db, bot_runner=None, factory=None):
        self.db = db
        self.bot_runner = bot_runner
        self.bot_runner_factory = factory or (lambda: None)

class FakeHandler:
    def __init__(self, db, bot_runner=None, factory=None, body=b""):
        self._response_code = None
        self.wfile = _Wfile()
        self.rfile = _Rfile(body)
        self.server = FakeServer(db, bot_runner, factory)
    def send_response(self, code): self._response_code = code
    def send_header(self, k, v): pass
    def end_headers(self): pass
    @property
    def headers(self): return {"Content-Length": "0"}
    def json_response(self):
        import json; return json.loads(self.wfile._buf)


def make_dummy_runner(db):
    """Runner that starts and stays alive until stopped."""
    from src.bot_runner import BotRunner
    started = threading.Event()

    class DummyRunner(BotRunner):
        async def _run_bot(self):
            started.set()
            import asyncio
            while not self._stop_event.is_set():
                await asyncio.sleep(0.05)

    r = DummyRunner(bot_token="fake", channel_id="@test", proxy_url=None, db=db)
    r.start()
    started.wait(timeout=2)
    return r


def test_restart_starts_runner_when_none(db):
    from src.routes.admin import handle_bot_restart
    started = threading.Event()

    def factory():
        from src.bot_runner import BotRunner
        class R(BotRunner):
            async def _run_bot(self):
                started.set()
                import asyncio
                while not self._stop_event.is_set():
                    await asyncio.sleep(0.05)
        return R(bot_token="t", channel_id="@c", proxy_url=None, db=db)

    h = FakeHandler(db, bot_runner=None, factory=factory)
    handle_bot_restart(h)
    assert h._response_code == 200
    assert h.json_response()["started"] is True
    assert started.wait(timeout=2)
    h.server.bot_runner.stop()


def test_restart_stops_existing_and_starts_new(db):
    from src.routes.admin import handle_bot_restart
    old_runner = make_dummy_runner(db)
    new_started = threading.Event()

    def factory():
        from src.bot_runner import BotRunner
        class R(BotRunner):
            async def _run_bot(self):
                new_started.set()
                import asyncio
                while not self._stop_event.is_set():
                    await asyncio.sleep(0.05)
        return R(bot_token="t", channel_id="@c", proxy_url=None, db=db)

    h = FakeHandler(db, bot_runner=old_runner, factory=factory)
    handle_bot_restart(h)

    assert not old_runner.is_alive()
    assert new_started.wait(timeout=2)
    assert h.server.bot_runner is not old_runner
    h.server.bot_runner.stop()


def test_restart_returns_started_false_when_factory_returns_none(db):
    from src.routes.admin import handle_bot_restart
    h = FakeHandler(db, bot_runner=None, factory=lambda: None)
    handle_bot_restart(h)
    assert h._response_code == 200
    assert h.json_response()["started"] is False
