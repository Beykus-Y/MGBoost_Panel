"""
Tests for Item 4 (SQLite concurrency): methods that previously accessed the
shared sqlite3.Connection without holding self._lock now do, closing the
TOCTOU race in update_hysteria_stats and the missing-lock gaps in
get_setting/set_setting and friends.
"""
import os
import sys
import tempfile
import threading

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


# --- behavioral: no lost updates under concurrent writers -------------------

def test_update_hysteria_stats_no_lost_updates_under_concurrency(db):
    token = "shared-token"
    n_threads = 8
    n_increments = 50

    def worker():
        for _ in range(n_increments):
            db.update_hysteria_stats(token, 1, 2)

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    upload, download = db.get_hysteria_traffic(token)
    assert upload == n_threads * n_increments
    assert download == n_threads * n_increments * 2


def test_set_setting_no_lost_updates_under_concurrent_different_keys(db):
    n_threads = 20

    def worker(i):
        db.set_setting(f"key:{i}", f"value:{i}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    for i in range(n_threads):
        assert db.get_setting(f"key:{i}") == f"value:{i}"


# --- lock is actually held (whitebox) ---------------------------------------

@pytest.mark.parametrize("method_name,args", [
    ("get_setting", ("some_key",)),
    ("set_setting", ("some_key", "some_value")),
    ("update_hysteria_stats", ("tok", 1, 1)),
    ("get_extra_configs", ()),
    ("get_hysteria_stats", ()),
    ("get_hysteria_traffic", ("tok",)),
    ("save_node_filters", ({},)),
])
def test_method_acquires_lock(db, method_name, args):
    """Whitebox check: patch self._lock with a tracking RLock subclass and
    assert each fixed method actually acquires it at least once."""
    acquisitions = []
    real_lock = db._lock

    class TrackingLock:
        def __enter__(self):
            acquisitions.append(1)
            return real_lock.__enter__()

        def __exit__(self, *exc):
            return real_lock.__exit__(*exc)

    db._lock = TrackingLock()
    try:
        getattr(db, method_name)(*args)
    finally:
        db._lock = real_lock

    assert acquisitions, f"{method_name} did not acquire self._lock"
