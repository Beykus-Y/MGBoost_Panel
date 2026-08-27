"""PH5-12 delivery routing: exact WL classification, STANDARD membership
CRUD with CAS/audit, backend WL-assignment rejection, admin route authz
matrix, and the topology-gated fresh-observation discipline."""

import importlib
import json
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tests.test_child_provisioning import PRIMARY, PRIMARY_LOGIN


@pytest.fixture
def db(monkeypatch):
    tmp = tempfile.mkdtemp()
    monkeypatch.setenv("DATA_DIR", tmp)
    monkeypatch.setenv("PRIMARY_MGBOOST_ADMIN_ACTOR_ID", PRIMARY)
    monkeypatch.setenv("PRIMARY_MGBOOST_ADMIN_LOGIN", PRIMARY_LOGIN)
    import src.config as config
    import src.database as database
    importlib.reload(config)
    importlib.reload(database)
    database.DB_PATH = os.path.join(tmp, "db.sqlite3")
    instance = database.Database()
    instance.delivery_routing.ensure_defaults(now=1)
    yield instance
    instance._conn.close()


@pytest.fixture
def capability(db):
    from src import security
    _raw_id, session = security.create_admin_session(PRIMARY_LOGIN, "test-jwt")
    return db.primary_admin_authority.authorize_session(session)


LIVE_TAGS = ("tcp-smart", "grpc-direct", "de-tcp-smart", "wl-tcp-direct")


def test_classify_is_exact_and_never_fuzzy(db):
    from src.delivery_routing import classify_inbound_tag
    assert classify_inbound_tag("wl-tcp-direct") == "WL_EXACT"
    # A node/inbound whose name merely CONTAINS wl is not WL by substring.
    assert classify_inbound_tag("wlunknown-new") == "WL_SUSPECT"
    assert classify_inbound_tag("Wl-tcp-direct".lower()) == "WL_EXACT"
    assert classify_inbound_tag("tcp-smart") == "STANDARD"
    # Mid-string "wl" is not the authority in either direction: the exact
    # allowlist alone decides WL, and the suspect bucket only mirrors
    # PH0-05's own startswith convention for extra_wl_like_tags (the node-4
    # "RU ONLY WL" lesson is about nodes, not substring magic).
    assert classify_inbound_tag("RU-ONLY-WL-CLONE") == "STANDARD"
    assert classify_inbound_tag("WL-new-host") == "WL_SUSPECT"


def test_exact_wl_host_rejected_for_standard(db, capability):
    from src.delivery_routing import WLHostRejected
    with pytest.raises(WLHostRejected):
        db.delivery_routing.apply_host_change(
            capability, profile_code="STANDARD", inbound_tag="wl-tcp-direct",
            operation="ADD", reason="must never be allowed", idempotency_key="wl-add-0000000001",
            observed_live_tags=LIVE_TAGS, now=10,
        )
    assert db.delivery_routing.membership("STANDARD") == []


def test_wl_like_unverified_tag_fail_closed(db, capability):
    from src.delivery_routing import WLLikeHostRejected
    with pytest.raises(WLLikeHostRejected):
        db.delivery_routing.apply_host_change(
            capability, profile_code="STANDARD", inbound_tag="wl-frankfurt-new",
            operation="ADD", reason="suspicious shape", idempotency_key="wllike-add-0000001",
            observed_live_tags=LIVE_TAGS + ("wl-frankfurt-new",), now=10,
        )


def test_unknown_host_not_in_live_observation_rejected(db, capability):
    from src.delivery_routing import UnknownHostRejected
    with pytest.raises(UnknownHostRejected):
        db.delivery_routing.apply_host_change(
            capability, profile_code="STANDARD", inbound_tag="ghost-host",
            operation="ADD", reason="not live anywhere", idempotency_key="ghost-add-0000001",
            observed_live_tags=LIVE_TAGS, now=10,
        )


def test_add_remove_roundtrip_with_row_version_cas_and_audit(db, capability):
    result = db.delivery_routing.apply_host_change(
        capability, profile_code="STANDARD", inbound_tag="tcp-smart",
        operation="ADD", reason="initial standard host", idempotency_key="rt-add-0000000001",
        observed_live_tags=LIVE_TAGS, now=10,
    )
    assert result["already_applied"] is False
    assert db.delivery_routing.membership("STANDARD") == ["tcp-smart"]
    version = result["row_version"]

    # Replay of the same key is honest already_applied, not a duplicate event.
    replay = db.delivery_routing.apply_host_change(
        capability, profile_code="STANDARD", inbound_tag="tcp-smart",
        operation="ADD", reason="initial standard host", idempotency_key="rt-add-0000000001",
        observed_live_tags=LIVE_TAGS, now=11,
    )
    assert replay["already_applied"] is True

    removed = db.delivery_routing.apply_host_change(
        capability, profile_code="STANDARD", inbound_tag="tcp-smart",
        operation="REMOVE", reason="host retired", idempotency_key="rt-remove-00000001",
        observed_live_tags=LIVE_TAGS, now=12,
    )
    assert db.delivery_routing.membership("STANDARD") == []
    events = db.delivery_routing.recent_events()
    assert {e["event_type"] for e in events} >= {"HOST_ADDED", "HOST_REMOVED"}
    assert all(e["actor_type"] == "PRIMARY_ADMIN" for e in events if e["event_type"] != "PROFILE_SEEDED")


def test_store_serializes_concurrent_writers_without_lost_update(db, capability):
    """Store-level writers serialize through BEGIN IMMEDIATE and bump the
    same monotonic row_version; the stale-writer conflict is enforced at
    the route's expected_row_version check (see the 409 route test)."""
    db.delivery_routing.apply_host_change(
        capability, profile_code="STANDARD", inbound_tag="tcp-smart",
        operation="ADD", reason="first write wins", idempotency_key="cas-a-0000000001",
        observed_live_tags=LIVE_TAGS, now=10,
    )
    db.delivery_routing.apply_host_change(
        capability, profile_code="STANDARD", inbound_tag="grpc-direct",
        operation="ADD", reason="second write lands", idempotency_key="cas-b-0000000001",
        observed_live_tags=LIVE_TAGS, now=11,
    )
    assert db.delivery_routing.membership("STANDARD") == ["grpc-direct", "tcp-smart"]
    assert db.delivery_routing.profile_by_code("STANDARD")["row_version"] == 3


def test_direct_sql_insert_of_wl_tag_cannot_reach_templates(db):
    """Corrupted routing storage (a WL tag smuggled into membership) must be
    caught by the template guard -- the safety policy is defense in depth."""
    db._conn.execute(
        "INSERT INTO mgboost_delivery_profile_hosts (profile_id,inbound_tag,created_at) "
        "SELECT id,'wl-selec-grpc-smart',999 FROM mgboost_delivery_profiles WHERE profile_code='STANDARD'"
    )
    db._conn.commit()
    from tests.test_marzban_broker import FakeMarzban
    remote = FakeMarzban()

    class M:
        def get_user(self, u, token=None):
            from src.broker_operations import BrokerOperations
            return BrokerOperations(remote).dispatch("legacy.user.get", {"username": u})

        def create_user(self, p, token=None):
            from src.broker_operations import BrokerOperations
            return BrokerOperations(remote).dispatch("legacy.user.create", {"user": p})

    account = db.accounts.create_account("DIRECT", now=5)
    from src.commercial_signup import derive_template_username
    db._conn.execute(
        "INSERT INTO mgboost_legacy_alias_groups (account_id,mapping_key,decision_ref,created_by_actor,created_at) "
        "VALUES (?,?,?,?,?)", (account["id"], f"k:{account['public_id']}", "test", "test", 5),
    )
    db._conn.execute(
        "INSERT INTO mgboost_legacy_account_aliases (account_id,legacy_username,alias_role,ownership_provenance,"
        "legacy_status,legacy_expiry,observed_device_count,observed_hwid_count,evidence_json,created_at) "
        "VALUES (?,?,'PRIMARY','EVIDENCE_PROVEN','ACTIVE',NULL,0,0,'{}',5)",
        (account["id"], derive_template_username(account["public_id"])),
    )
    result = db.commercial_signup.ensure_template_for_account(account["id"], marzban=M(), now=20)
    assert result["state"] == "MANUAL_REVIEW"
    assert result["error_class"] == "wl_tag_in_standard_profile"
    # No remote user was created carrying the WL tag.
    assert all(
        "wl-selec-grpc-smart" not in (u.get("inbounds", {}).get("vless") or [])
        for u in remote.users.values()
    )


# --- admin HTTP routes --------------------------------------------------------

class _Wfile:
    def __init__(self):
        self._buf = b""

    def write(self, data):
        self._buf += data if isinstance(data, bytes) else data.encode()


class _Rfile:
    def __init__(self, data):
        self._data = data

    def read(self, n):
        return self._data[:n]


class Handler:
    def __init__(self, method="GET", body=b"", headers=None):
        self.method = method
        self.command = method
        self._headers = dict(headers or {})
        self._headers.setdefault("Content-Length", str(len(body)))
        self.wfile = _Wfile()
        self.rfile = _Rfile(body)
        self.status = None
        self.response_headers = {}

    def send_response(self, code):
        self.status = code

    def send_header(self, k, v):
        self.response_headers[k] = v

    def end_headers(self):
        pass

    @property
    def headers(self):
        return self._headers

    def json(self):
        return json.loads(self.wfile._buf)


def _authed(db, *, method="GET", body=b"", primary=True, csrf=True):
    from src import security
    username = PRIMARY_LOGIN if primary else "secondary-admin"
    raw_session_id, session = security.create_admin_session(username, "jwt")
    headers = {"Cookie": f"{security.ADMIN_SESSION_COOKIE}={raw_session_id}"}
    if method != "GET" and csrf:
        headers["X-CSRF-Token"] = session.csrf_token
    handler = Handler(method=method, body=body, headers=headers)
    handler.server = type("S", (), {"db": db})()
    return handler


@pytest.fixture
def fake_topology(monkeypatch):
    """A fake broker-facing service client exposing the exact PH0-05
    baseline plus standard tags, so the route's fresh topology assertion
    passes without any real Marzban."""
    from src.wl_topology import WL_INBOUND_TAGS, WL_NODES

    class FakeService:
        def get_nodes(self, token=None):
            return [
                {"id": n["id"], "name": n["role"], "address": n["address"],
                 "usage_coefficient": n["usage_coefficient"], "status": "connected"}
                for n in WL_NODES
            ]

        def get_inbounds(self, token=None):
            return {"vless": [{"tag": t, "protocol": "vless", "port": 443,
                               "network": "tcp", "tls": "none"}
                              for t in sorted(WL_INBOUND_TAGS | {"tcp-smart", "grpc-direct"})]}

    from src.routes import admin_support
    admin_support.set_service_marzban(FakeService())
    yield
    admin_support.set_service_marzban(None)


def test_routing_hosts_requires_admin_auth(db, fake_topology):
    from src.routes.admin_routing import handle_routing_hosts
    handler = Handler()
    handler.server = type("S", (), {"db": db})()
    handle_routing_hosts(handler)
    assert handler.status == 401


def test_routing_hosts_lists_live_classification(db, fake_topology):
    from src.routes.admin_routing import handle_routing_hosts
    handler = _authed(db)
    handle_routing_hosts(handler)
    assert handler.status == 200
    data = handler.json()
    by_tag = {h["inbound_tag"]: h for h in data["hosts"]}
    assert by_tag["wl-tcp-direct"]["classification"] == "WL_EXACT"
    assert by_tag["wl-tcp-direct"]["in_standard"] is False
    assert by_tag["tcp-smart"]["classification"] == "STANDARD"
    assert data["plan_delivery"] == {"BASIC": "STANDARD", "BASIC_PLUS": "STANDARD", "BASIC_PRO": "STANDARD"}


def test_routing_add_requires_primary_capability(db, fake_topology):
    from src.routes.admin_routing import handle_routing_host_add
    handler = _authed(db, method="POST", primary=False, body=json.dumps({
        "inbound_tag": "tcp-smart", "reason": "non primary admin",
        "idempotency_key": "nonprimary-key-0001", "expected_row_version": 1,
    }).encode())
    handle_routing_host_add(handler)
    assert handler.status == 403
    assert db.delivery_routing.membership("STANDARD") == []


def test_routing_add_requires_csrf(db, fake_topology):
    from src.routes.admin_routing import handle_routing_host_add
    handler = _authed(db, method="POST", csrf=False, body=json.dumps({
        "inbound_tag": "tcp-smart", "reason": "csrf missing here",
        "idempotency_key": "no-csrf-key-000001", "expected_row_version": 1,
    }).encode())
    handle_routing_host_add(handler)
    assert handler.status in (401, 403)
    assert db.delivery_routing.membership("STANDARD") == []


def test_routing_add_rejects_wl_and_reports_reason(db, fake_topology):
    from src.routes.admin_routing import handle_routing_host_add
    handler = _authed(db, method="POST", body=json.dumps({
        "inbound_tag": "wl-tcp-direct", "reason": "attempted wl assignment",
        "idempotency_key": "wl-route-add-0001", "expected_row_version": 1,
    }).encode())
    handle_routing_host_add(handler)
    assert handler.status == 400
    assert "PH0-05" in handler.json()["error"]
    assert db.delivery_routing.membership("STANDARD") == []


def test_routing_add_stale_row_version_conflicts_409(db, fake_topology):
    from src.routes.admin_routing import handle_routing_host_add
    first = _authed(db, method="POST", body=json.dumps({
        "inbound_tag": "tcp-smart", "reason": "first add wins here",
        "idempotency_key": "stale-add-a-000001", "expected_row_version": 1,
    }).encode())
    handle_routing_host_add(first)
    assert first.status == 200
    stale = _authed(db, method="POST", body=json.dumps({
        "inbound_tag": "grpc-direct", "reason": "stale writer loses",
        "idempotency_key": "stale-add-b-000001", "expected_row_version": 1,
    }).encode())
    handle_routing_host_add(stale)
    assert stale.status == 409


def test_routing_remove_roundtrip_via_routes(db, fake_topology):
    from src.routes.admin_routing import handle_routing_host_add, handle_routing_host_remove
    add = _authed(db, method="POST", body=json.dumps({
        "inbound_tag": "tcp-smart", "reason": "roundtrip add host",
        "idempotency_key": "rt-route-add-00001", "expected_row_version": 1,
    }).encode())
    handle_routing_host_add(add)
    assert add.status == 200
    version = add.json()["row_version"]
    remove = _authed(db, method="POST", body=json.dumps({
        "inbound_tag": "tcp-smart", "reason": "roundtrip remove host",
        "idempotency_key": "rt-route-rem-00001", "expected_row_version": version,
    }).encode())
    handle_routing_host_remove(remove)
    assert remove.status == 200
    assert db.delivery_routing.membership("STANDARD") == []


def test_routing_mutation_reason_is_mandatory(db, fake_topology):
    from src.routes.admin_routing import handle_routing_host_add
    handler = _authed(db, method="POST", body=json.dumps({
        "inbound_tag": "tcp-smart", "reason": "no",
        "idempotency_key": "short-reason-00001", "expected_row_version": 1,
    }).encode())
    handle_routing_host_add(handler)
    assert handler.status == 400


# --- scripts/seed_delivery_routing.py: no hardcoded-baseline eternal constant -----

def _load_seed_script():
    import importlib.util
    script_path = os.path.join(
        os.path.dirname(__file__), "..", "scripts", "seed_delivery_routing.py"
    )
    spec = importlib.util.spec_from_file_location("ph5_12_seed", script_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_seed_script_derives_baseline_from_fresh_live_topology(db, monkeypatch):
    """The seed script must classify STANDARD membership from a live
    topology read at run time -- never from a tag list baked into the
    script's source. A brand-new host that did not exist on any historical
    snapshot must still be seeded, and any exact WL tag must never be."""
    from src.wl_topology import WL_INBOUND_TAGS, WL_NODES

    live_standard_tags = {"tcp-smart", "grpc-direct", "brand-new-standard-host"}

    class FakeService:
        def get_nodes(self, token=None):
            return [
                {"id": n["id"], "name": n["role"], "address": n["address"],
                 "usage_coefficient": n["usage_coefficient"], "status": "connected"}
                for n in WL_NODES
            ]

        def get_inbounds(self, token=None):
            return {"vless": [{"tag": t, "protocol": "vless", "port": 443,
                               "network": "tcp", "tls": "none"}
                              for t in sorted(WL_INBOUND_TAGS | live_standard_tags)]}

    from src.routes import admin_support
    admin_support.set_service_marzban(FakeService())
    try:
        module = _load_seed_script()
        monkeypatch.setattr(sys, "argv", ["seed_delivery_routing.py", "--seed-verified-baseline"])
        rc = module.main()
        assert rc == 0
    finally:
        admin_support.set_service_marzban(None)

    members = set(db.delivery_routing.membership("STANDARD"))
    assert members == live_standard_tags
    assert members.isdisjoint(WL_INBOUND_TAGS)

    # Re-running is idempotent: no duplicate membership rows, same result.
    admin_support.set_service_marzban(FakeService())
    try:
        rc = module.main()
        assert rc == 0
    finally:
        admin_support.set_service_marzban(None)
    assert set(db.delivery_routing.membership("STANDARD")) == live_standard_tags


def test_seed_script_fails_closed_on_topology_mismatch(db, monkeypatch):
    """A stale/unhealthy topology observation must abort the seed with no
    mutation at all -- never fall back to any hardcoded tag list."""
    class BrokenService:
        def get_nodes(self, token=None):
            return []

        def get_inbounds(self, token=None):
            return {"vless": []}

    from src.routes import admin_support
    admin_support.set_service_marzban(BrokenService())
    try:
        module = _load_seed_script()
        monkeypatch.setattr(sys, "argv", ["seed_delivery_routing.py", "--seed-verified-baseline"])
        rc = module.main()
        assert rc != 0
    finally:
        admin_support.set_service_marzban(None)
    assert db.delivery_routing.membership("STANDARD") == []
