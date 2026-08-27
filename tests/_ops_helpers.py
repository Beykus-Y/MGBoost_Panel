"""Shared fixtures/builders for the operational-admin route tests.

Everything reuses the real chains: PH5-09/10 store + PH5-02 renewal +
PH5-04 proof + PH3-03 provisioning + PH3-05 lifecycle + PH3-08 sync driven
through the typed BrokerOperations against FakeMarzban.
"""

import importlib
import json
import os
import tempfile

import pytest

from src.broker_operations import BrokerOperations
from src.child_contract import source_contract_hash

PRIMARY = "owner:mgboost-primary:v1"
PRIMARY_LOGIN = "authenticated-primary-login"
HWID_KEY = "ops-hwid-key-that-is-at-least-32-bytes!!"


class BrokerBackedService:
    """Substitutes ServiceMarzbanClient inside admin_support via its
    documented test seam; every operation still flows through the real typed
    broker contract."""

    def __init__(self, remote):
        self.remote = remote

    def sync_child_user_state(self, request):
        return BrokerOperations(self.remote).dispatch("child.user.state.sync", request)

    def revoke_child_user(self, request):
        return BrokerOperations(self.remote).dispatch("child.user.revoke", request)


@pytest.fixture
def db(monkeypatch):
    tmp = tempfile.mkdtemp(prefix="ops-admin-test-")
    monkeypatch.setenv("DATA_DIR", tmp)
    monkeypatch.setenv("PRIMARY_MGBOOST_ADMIN_ACTOR_ID", PRIMARY)
    monkeypatch.setenv("PRIMARY_MGBOOST_ADMIN_LOGIN", PRIMARY_LOGIN)
    monkeypatch.setenv("DEVICE_SLOT_HMAC_KEY", HWID_KEY)
    import src.config as config
    import src.database as database
    importlib.reload(config)
    importlib.reload(database)
    database.DB_PATH = os.path.join(tmp, "db.sqlite3")
    instance = database.Database()
    from src.plan_catalog import seed_plan_catalog
    from src.wl_package_catalog import seed_wl_package_catalog
    seed_plan_catalog(instance.plan_catalog, now=1)
    seed_wl_package_catalog(instance.wl_package_catalog, now=1)

    from tests.test_marzban_broker import FakeMarzban
    from src.routes import admin_support
    remote = FakeMarzban()
    admin_support.set_service_marzban(BrokerBackedService(remote))
    instance._fake_remote = remote

    yield instance
    instance._conn.close()


def handler(db, *, command="POST", payload=None, primary=True, authenticated=True,
            with_csrf=True):
    """Back-compat alias to make_handler with positional db first."""
    return make_handler(db, command=command, payload=payload, primary=primary,
                        authenticated=authenticated, with_csrf=with_csrf)


def make_handler(db, *, command="POST", payload=None, primary=True,
                 authenticated=True, with_csrf=True, path="/x"):
    import io
    import json
    from src import security

    class FakeHandler:
        def __init__(self, *, headers=None, body=b""):
            self.command = command
            self.headers = headers or {}
            self.rfile = io.BytesIO(body)
            self.wfile = io.BytesIO()
            self.status = None
            self.response_headers = []
            self.path = path
            if command != "GET":
                self.headers.setdefault("Content-Length", str(len(self.rfile.getvalue())))

        def send_response(self, status):
            self.status = status

        def send_header(self, name, value):
            self.response_headers.append((name, value))

        def end_headers(self):
            pass

        def json(self):
            return json.loads(self.wfile.getvalue())

    login = PRIMARY_LOGIN if primary else "secondary-admin-login"
    headers = {"Content-Type": "application/json"}
    body = json.dumps(payload).encode() if payload is not None else b""
    if authenticated:
        raw_id, session = security.create_admin_session(login, "jwt")
        headers["Cookie"] = f"{security.ADMIN_SESSION_COOKIE}={raw_id}"
        if with_csrf:
            headers["X-CSRF-Token"] = session.csrf_token
    fake = FakeHandler(headers=headers, body=body)
    fake.server = type("Server", (), {"db": db})()
    return fake


def capability(db):
    from src.security import AdminSessionStore
    _raw, session = AdminSessionStore().create(PRIMARY_LOGIN, "jwt")
    return db.primary_admin_authority.authorize_session(session)


def paid_wl_subscription(db, account_id, *, plan="WL", days=30, tg_suffix=1):
    """Real subscription through the PH5-02 renewal engine. The APPLY step
    uses the real wall clock -- so the DL-044 anchor
    max(current_expiry, now) lands in the live future exactly like a real
    purchase would.

    PH5-11 note: the Stars purchase CHANNEL now carries the first-rollout
    sellable-plan gate (BASIC/BASIC_PLUS/BASIC_PRO only), so fixtures that
    need a WL/EXTENDED/FAMILY grant apply through the engine directly --
    the gate is deliberately channel-level, not engine-level."""
    import time as _time

    telegram_id = 900_000_000 + account_id * 10 + tg_suffix
    db.accounts.link_telegram_owner(account_id, telegram_id,
                                    provenance="MIGRATION", actor="test", now=1)
    return db.subscription_renewal.apply_same_plan_purchase(
        account_id=account_id, plan_code=plan, duration_days=days,
        payment_channel="TELEGRAM_STARS", mutation_source="DIRECT_PURCHASE",
        actor_type="TELEGRAM", actor_ref=str(telegram_id),
        reason="fixture subscription",
        idempotency_key=f"fixture-stars-{account_id}-{plan}-{days}-{tg_suffix}",
        now=int(_time.time()),
    )


def finish_child_provisioning(db, remote, child_intent_id, *, worker_id="fixture-worker", now=None):
    """Simulate the real `mgboost-child-worker` draining a pending
    `CHILD_USER_ENSURE` outbox entry to completion -- the same claim/dispatch
    /acknowledge sequence `build_topology_account` runs inline for a fresh
    slot's first generation. A slot generation produced by admin REBIND is
    only *prepared* (durably queued) by `process_rebind`, not synchronously
    provisioned; tests that need to act on that successor generation as if it
    were actually live in Marzban (e.g. revoking it) must drain its outbox
    entry exactly like the real worker would before doing so.

    `now` defaults to the real wall clock, matching the admin device routes
    (`src/routes/admin_devices.py`) which stamp the outbox row's
    `next_attempt_at` with real `time.time()`, not a small fixture tick --
    a small `now` here would make the row look not-yet-claimable."""
    import time as _time

    from src.child_contract import derive_operation_id

    if now is None:
        now = int(_time.time())
    intent = db._conn.execute(
        "SELECT child_username FROM mgboost_child_user_intents WHERE id=?",
        (child_intent_id,),
    ).fetchone()
    operation_id = derive_operation_id(intent["child_username"])
    claim = db.child_provisioning.claim(operation_id, worker_id=worker_id, now=now, lease_seconds=60)
    created = BrokerOperations(remote).dispatch("child.user.ensure", claim["payload"])
    child_uuid = created.pop("uuid")
    db.child_provisioning.acknowledge(operation_id, worker_id=worker_id, outcome=created["outcome"],
                                      child_uuid=child_uuid, remote_result=created, now=now + 1)


def build_topology_account(db, *, tag, n_children=1, plan="WL", days=30):
    """Account + paid subscription + provisioned children via the real
    PH3-03 chain (fixture-worker) — mirrors tests/test_manual_renewal_ph510."""
    remote = db._fake_remote
    account = db.accounts.create_account("DIRECT", now=1)
    template = f"ops_parent_{tag}_{account['id']}"
    db._conn.execute(
        "INSERT INTO mgboost_legacy_alias_groups "
        "(account_id,mapping_key,decision_ref,created_by_actor,created_at) VALUES (?,?,?,?,?)",
        (account["id"], f"ops-map-{tag}", "test-decision-ref", "TEST", 1),
    )
    alias_id = db._conn.execute(
        "INSERT INTO mgboost_legacy_account_aliases "
        "(account_id,legacy_username,alias_role,ownership_provenance,legacy_status,"
        "legacy_expiry,observed_device_count,observed_hwid_count,evidence_json,created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (account["id"], template, "PRIMARY", "OWNER_APPROVED", "ACTIVE", None,
         n_children, n_children, "{}", 1),
    ).lastrowid
    db._conn.commit()

    applied = paid_wl_subscription(db, account["id"], plan=plan, days=days)
    remote.users[template] = json.loads(json.dumps(remote.users["alice"]))
    remote.users[template]["username"] = template

    children = []
    for index in range(n_children):
        slot = db.device_slots.claim(account["id"], f"raw-hwid-{tag}-{index}",
                                     HWID_KEY, now=100 + index)
        prepared = db.child_provisioning.prepare_child_ensure(
            account_id=account["id"], slot_generation_id=slot["generation_id"],
            source_alias_id=alias_id,
            source_contract_hash=source_contract_hash(remote.users[template]),
            expire=applied["new_expiry"],
            idempotency_key=f"ops-child-{tag}-{index}---------", now=120 + index,
        )
        claim = db.child_provisioning.claim(prepared["operation_id"],
                                            worker_id="fixture-worker",
                                            now=121 + index, lease_seconds=60)
        created = BrokerOperations(remote).dispatch("child.user.ensure", claim["payload"])
        child_uuid = created.pop("uuid")
        db.child_provisioning.acknowledge(prepared["operation_id"], worker_id="fixture-worker",
                                          outcome=created["outcome"], child_uuid=child_uuid,
                                          remote_result=created, now=122 + index)
        children.append({"slot": slot, "prepared": prepared})
    return account, children
