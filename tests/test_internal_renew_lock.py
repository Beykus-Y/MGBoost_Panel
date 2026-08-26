"""Tests for the in-process per-username lock wired into
handle_internal_user_renew (src/routes/internal.py), per Phase 2 design doc
§5.2 — including the explicit owner-required race test proving the Stars
apply-worker and handle_internal_user_renew actually serialize when they
target the same marzban_username, and that two different usernames do not
block each other."""
import asyncio
import json
import os
import sys
import tempfile
import threading
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


@pytest.fixture(autouse=True)
def _reset_marzban_locks():
    from src.marzban_lock import marzban_user_locks
    marzban_user_locks._locks.clear()
    yield
    marzban_user_locks._locks.clear()


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


def _make_running_loop():
    loop = asyncio.new_event_loop()
    ready = threading.Event()

    def runner():
        asyncio.set_event_loop(loop)
        loop.call_soon(ready.set)
        loop.run_forever()

    t = threading.Thread(target=runner, daemon=True)
    t.start()
    ready.wait(2)
    return loop, t


async def _is_locked(lock):
    return lock.locked()


class _Wfile:
    def __init__(self):
        self._buf = b""

    def write(self, data):
        self._buf += data


class _Rfile:
    def __init__(self, data):
        self._data = data

    def read(self, n):
        return self._data[:n]


class FakeInternalHandler:
    def __init__(self, body, bot_runner=None):
        self._response_code = None
        self._headers = {}
        self._request_body = body
        self.wfile = _Wfile()
        self.rfile = _Rfile(self._request_body)
        self.server = type("S", (), {"bot_runner": bot_runner})()

    def send_response(self, code):
        self._response_code = code

    def send_header(self, k, v):
        self._headers[k] = v

    def end_headers(self):
        pass

    @property
    def headers(self):
        return {"Content-Length": str(len(self._request_body))}

    def json_response(self):
        return json.loads(self.wfile._buf)


def test_lock_marzban_username_noop_when_bot_not_running():
    from src.routes import internal as internal_mod

    class FakeServer:
        bot_runner = None

    class FakeHandler:
        server = FakeServer()

    ctx = internal_mod._lock_marzban_username(FakeHandler(), "alice")
    assert isinstance(ctx, internal_mod._NullLockCtx)
    with ctx:
        pass  # must not raise even though nothing is actually locked


def test_lock_marzban_username_acquires_and_releases_shared_lock():
    from src.routes import internal as internal_mod
    from src.marzban_lock import marzban_user_locks

    loop, t = _make_running_loop()
    try:
        class FakeRunner:
            _loop = loop

        class FakeServer:
            bot_runner = FakeRunner()

        class FakeHandler:
            server = FakeServer()

        lock = marzban_user_locks.get("lock-acquire-release-test")
        assert asyncio.run_coroutine_threadsafe(_is_locked(lock), loop).result(2) is False

        ctx = internal_mod._lock_marzban_username(FakeHandler(), "lock-acquire-release-test")
        with ctx:
            assert asyncio.run_coroutine_threadsafe(_is_locked(lock), loop).result(2) is True
        assert asyncio.run_coroutine_threadsafe(_is_locked(lock), loop).result(2) is False
    finally:
        loop.call_soon_threadsafe(loop.stop)
        t.join(timeout=2)


class _FakeMarzbanClient:
    """In-memory Marzban stand-in shared between handle_internal_user_renew
    (patched onto internal._client) and the Stars worker in the race test
    below."""

    def __init__(self, users):
        self.users = users
        self.calls = []

    def get_user(self, username, admin_token):
        self.calls.append(("get", username))
        return dict(self.users[username])

    def modify_user(self, username, payload, admin_token):
        self.calls.append(("modify", username, dict(payload)))
        time.sleep(0.01)  # widen the race window
        self.users[username].update(payload)
        return dict(self.users[username])

    def renew_user(self, username, renewal, admin_token):
        user = self.get_user(username, admin_token)
        update = {}
        if "add_days" in renewal:
            update["expire"] = max(int(user.get("expire") or 0), int(time.time())) + renewal["add_days"] * 86400
        if "expire" in renewal:
            update["expire"] = renewal["expire"]
        if "data_limit" in renewal:
            update["data_limit"] = renewal["data_limit"] or None
        if "status" in renewal:
            update["status"] = renewal["status"]
        return self.modify_user(username, update, admin_token)

    def get_admin_token_from_env(self):
        return "tok"


def test_stars_worker_and_internal_renew_race_on_same_username_serialize(db, monkeypatch):
    """The owner's explicitly required race test: the Stars apply-worker
    (running inside the bot thread's loop) and handle_internal_user_renew
    (an HTTP-thread call using asyncio.run_coroutine_threadsafe against
    that same loop) both target marzban_username 'alice' at the same time.
    Proves the shared per-username lock (§5.2) serializes them so the
    final `expire` reflects BOTH renewals rather than one clobbering the
    other."""
    from src.routes import internal as internal_mod
    from src.stars import process_invoice_row

    now = int(time.time())
    fake_client = _FakeMarzbanClient({"alice": {"username": "alice", "expire": now + 1000, "status": "active"}})
    monkeypatch.setattr(internal_mod, "_client", fake_client)

    loop, t = _make_running_loop()
    try:
        class FakeRunner:
            _loop = loop
            bot_instance = None

        class FakeServer:
            bot_runner = FakeRunner()

        class FakeHandler:
            server = FakeServer()

        inv = db.create_stars_invoice(
            created_by_telegram_id=1, marzban_username="alice",
            tariff_id=1, tariff_name="1 month", duration_days=30, stars_price=320,
        )
        db.mark_invoice_paid(inv["id"], "c1", None, payer_telegram_id=222, total_amount=320)
        row = db.get_invoice(inv["id"])

        class FakeBot:
            async def send_message(self, *a, **kw):
                pass

        def run_renew_on_http_thread():
            body = json.dumps({"add_days": 7}).encode()
            handler = FakeInternalHandler(body, bot_runner=FakeServer.bot_runner)
            internal_mod.handle_internal_user_renew(handler, "alice")

        # Fire the HTTP-thread renewal concurrently with the Stars
        # apply-worker's own processing of the same username, both
        # ultimately serialized through the same in-process lock.
        http_thread = threading.Thread(target=run_renew_on_http_thread)
        stars_future = asyncio.run_coroutine_threadsafe(
            process_invoice_row(FakeBot(), db, fake_client, "tok", row), loop,
        )
        http_thread.start()
        stars_future.result(timeout=5)
        http_thread.join(timeout=5)

        final_user = fake_client.users["alice"]
        final_invoice = db.get_invoice(inv["id"])

        # Both writers' effects must show up — total = initial + 30d + 7d,
        # regardless of which ran first (the second always extends from
        # the first's already-applied result, never from a stale read).
        assert final_user["expire"] == (now + 1000) + 37 * 86400
        assert final_invoice["status"] == "applied"
    finally:
        loop.call_soon_threadsafe(loop.stop)
        t.join(timeout=2)


def test_enter_timeout_does_not_orphan_the_lock():
    """F1 corrective regression: if __enter__'s own result(timeout=10) times
    out while another holder still owns the lock, the abandoned acquire()
    coroutine must not silently win the lock later with nobody left to
    release it. Reproduces the exact race: a slow holder releases AFTER our
    caller-side timeout fires, then proves a subsequent normal
    acquire/release still works (no permanent leak that would later stall
    the Stars apply-loop)."""
    from src.routes import internal as internal_mod
    from src.marzban_lock import marzban_user_locks

    loop, t = _make_running_loop()
    try:
        lock = marzban_user_locks.get("orphan-timeout-test")

        async def hold_then_release(seconds):
            await lock.acquire()
            await asyncio.sleep(seconds)
            lock.release()

        holder_fut = asyncio.run_coroutine_threadsafe(hold_then_release(0.6), loop)
        time.sleep(0.1)  # ensure the holder has actually acquired first

        import concurrent.futures as cf
        orig_result = cf.Future.result

        def result_that_times_out_at_the_real_10s_call(self, timeout=None):
            if timeout == 10:
                raise TimeoutError()
            return orig_result(self, timeout)

        cf.Future.result = result_that_times_out_at_the_real_10s_call
        try:
            ctx = internal_mod._CrossThreadLockCtx(lock, loop)
            with pytest.raises(TimeoutError):
                ctx.__enter__()
        finally:
            cf.Future.result = orig_result  # only this one artificial __enter__ should time out

        holder_fut.result(timeout=5)  # let the original holder finish and release
        time.sleep(0.3)  # give the abandoned acquire()/cancellation a chance to settle

        assert asyncio.run_coroutine_threadsafe(_is_locked(lock), loop).result(2) is False

        # A subsequent normal acquire/release must still work -- proves no
        # orphaned holder is left blocking this username forever.
        normal_ctx = internal_mod._CrossThreadLockCtx(lock, loop)
        with normal_ctx:
            assert asyncio.run_coroutine_threadsafe(_is_locked(lock), loop).result(2) is True
        assert asyncio.run_coroutine_threadsafe(_is_locked(lock), loop).result(2) is False
    finally:
        loop.call_soon_threadsafe(loop.stop)
        t.join(timeout=2)


def test_different_usernames_do_not_block_internal_renew_and_stars_worker(db, monkeypatch):
    """Two DIFFERENT usernames must not serialize against each other."""
    from src.routes import internal as internal_mod
    from src.stars import process_invoice_row

    now = int(time.time())
    fake_client = _FakeMarzbanClient({
        "alice": {"username": "alice", "expire": now + 1000, "status": "active"},
        "bob": {"username": "bob", "expire": now + 1000, "status": "active"},
    })
    monkeypatch.setattr(internal_mod, "_client", fake_client)

    loop, t = _make_running_loop()
    try:
        class FakeRunner:
            _loop = loop
            bot_instance = None

        class FakeServer:
            bot_runner = FakeRunner()

        inv = db.create_stars_invoice(
            created_by_telegram_id=1, marzban_username="bob",
            tariff_id=1, tariff_name="1 month", duration_days=30, stars_price=320,
        )
        db.mark_invoice_paid(inv["id"], "c1", None, payer_telegram_id=222, total_amount=320)
        row = db.get_invoice(inv["id"])

        class FakeBot:
            async def send_message(self, *a, **kw):
                pass

        def run_renew_for_alice():
            body = json.dumps({"add_days": 7}).encode()
            handler = FakeInternalHandler(body, bot_runner=FakeServer.bot_runner)
            internal_mod.handle_internal_user_renew(handler, "alice")

        start = time.time()
        http_thread = threading.Thread(target=run_renew_for_alice)
        stars_future = asyncio.run_coroutine_threadsafe(
            process_invoice_row(FakeBot(), db, fake_client, "tok", row), loop,
        )
        http_thread.start()
        stars_future.result(timeout=5)
        http_thread.join(timeout=5)
        elapsed = time.time() - start

        # Both must have completed correctly and quickly (no serialization
        # tax from an unrelated username's critical section).
        assert fake_client.users["alice"]["expire"] == (now + 1000) + 7 * 86400
        assert db.get_invoice(inv["id"])["status"] == "applied"
        assert elapsed < 2.0
    finally:
        loop.call_soon_threadsafe(loop.stop)
        t.join(timeout=2)
