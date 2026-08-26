import os
import tempfile

import pytest


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


def _clean_observation():
    from src.wl_topology import WL_INBOUND_TAGS, WL_NODES
    tags = set(WL_INBOUND_TAGS)
    nodes = {n["id"]: {"role": n["role"], "address": n["address"], "usage_coefficient": n["usage_coefficient"]}
             for n in WL_NODES}
    return tags, nodes


def test_run_assertion_records_ok_event(db):
    tags, nodes = _clean_observation()
    result = db.wl_topology_guard.run_assertion(tags, nodes, now=1000)
    assert result["ok"] is True
    latest = db.wl_topology_guard.latest_assertion()
    assert latest["ok"] is True
    assert latest["checked_at"] == 1000
    from src.wl_topology import WL_TOPOLOGY_VERSION
    assert latest["config_version"] == WL_TOPOLOGY_VERSION


def test_run_assertion_records_mismatch_event(db):
    tags, nodes = _clean_observation()
    tags.discard("wl-tcp-direct")
    result = db.wl_topology_guard.run_assertion(tags, nodes, now=2000)
    assert result["ok"] is False
    assert result["missing_tags"] == ["wl-tcp-direct"]

    count = db._conn.execute("SELECT COUNT(*) FROM mgboost_wl_topology_assertions").fetchone()[0]
    assert count == 1


def test_assertion_log_is_append_only(db):
    tags, nodes = _clean_observation()
    db.wl_topology_guard.run_assertion(tags, nodes, now=1000)
    with pytest.raises(Exception):
        db._conn.execute("UPDATE mgboost_wl_topology_assertions SET ok=0")
    with pytest.raises(Exception):
        db._conn.execute("DELETE FROM mgboost_wl_topology_assertions")


def test_require_topology_ok_passes_after_clean_assertion(db):
    tags, nodes = _clean_observation()
    db.wl_topology_guard.run_assertion(tags, nodes, now=1000)
    db.wl_topology_guard.require_topology_ok()  # must not raise


def test_require_topology_ok_fails_closed_with_no_assertion_ever_recorded(db):
    from src.wl_topology_guard import TopologyMismatchError
    with pytest.raises(TopologyMismatchError):
        db.wl_topology_guard.require_topology_ok()


def test_require_topology_ok_fails_after_mismatch_assertion(db):
    from src.wl_topology_guard import TopologyMismatchError
    tags, nodes = _clean_observation()
    tags.discard("wl-tcp-direct")
    db.wl_topology_guard.run_assertion(tags, nodes, now=1000)
    with pytest.raises(TopologyMismatchError):
        db.wl_topology_guard.require_topology_ok()


def test_fetch_live_topology_observation_uses_existing_readonly_marzban_client_calls():
    from src.wl_topology_guard import fetch_live_topology_observation

    class FakeClient:
        def get_nodes(self, token):
            return [{"id": 4, "name": "RU ONLY WL", "address": "84.201.130.217", "usage_coefficient": 1.0}]

        def get_inbounds(self, token):
            return {"vless": [{"tag": "wl-tcp-direct"}]}

    tags, nodes = fetch_live_topology_observation(FakeClient(), "fake-token")
    assert tags == {"wl-tcp-direct"}
    assert nodes[4]["role"] == "RU ONLY WL"


def test_stale_and_non_wl_nodes_never_registered_by_the_guard(db):
    # Node 3 (Estonia) is a real, connected, non-WL node -- present live but
    # must never make the assertion "ok" without both real WL nodes present.
    tags, nodes = _clean_observation()
    del nodes[7]
    nodes[3] = {"role": "Estonia", "address": "150.241.74.147", "usage_coefficient": 1.0}
    result = db.wl_topology_guard.run_assertion(tags, nodes, now=1000)
    assert result["ok"] is False
    assert result["missing_node_ids"] == [7]
