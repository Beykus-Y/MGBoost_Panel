"""Corrective pass regression tests for three proven, code-level subscription
lifecycle defects found during the account_id=16 (Incident A) production
forensic:

* BUG F/E -- an already-provisioned child whose `observed_state` is not
  ACTIVE (e.g. DISABLED after a parent expiry, before reconciliation catches
  up) is routed back into `prepare_child_ensure()` on the next resolve. Its
  idempotency key is stable per `slot_generation_id`, but the ENSURE payload
  embeds the *current* `expire` -- so a parent expiry change between the
  original ensure and this resolve changes `request_hash` under the same
  key, and `ChildProvisioningConflict` is raised and swallowed as a bare
  `OUTCOME_INTERNAL_ERROR`. An already-existing child identity must never be
  re-provisioned through ENSURE; only STATE_SYNC/reconciliation may change
  its status/expire.

* BUG I -- `redact_request_target()` only masks legacy `/sub/{token}` paths.
  The newer root-level 43-char opaque subscription token
  (`^/[A-Za-z0-9_-]{43}$`, see `src/routes/opaque_sub.py`) is logged
  unredacted by `src/server.py::log_message` into stdout/journalctl -- a raw
  bearer token in production logs.

* BUG C -- `ParentSyncStore.acknowledge()` records
  `remote_effect_verifier=_sha(outcome)`, i.e. a hash of the literal string
  "SYNCED"/"ALREADY_IN_SYNC". Two completely different real remote child
  states (e.g. active+expiry-A vs. active+expiry-B) that both happen to
  finish with outcome "SYNCED" produce the *same* verifier -- it verifies
  nothing about the actual remote state, only which code branch returned.
"""

import base64
import importlib
import os
import tempfile

import pytest

from src.broker_operations import BrokerOperations
from src.child_contract import derive_operation_id, source_contract_hash
from src.opaque_resolver import (
    OUTCOME_OK,
    OUTCOME_PROVISIONING_UNAVAILABLE,
    resolve_opaque_subscription,
)
from src.sensitive import redact_request_target
from src.subscription_credentials import generate_opaque_token

from tests.test_child_provisioning import HWID_KEY, PRIMARY, PRIMARY_LOGIN, _account
from tests.test_marzban_broker import FakeMarzban
from tests.test_parent_sync import _set_subscription


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
    yield instance
    instance._conn.close()


SUPPORTED_METADATA = {
    "client_name": "Happ", "client_version": "2.7.0", "platform": "windows",
}


def _known_hwid_meta(hwid):
    return {
        **SUPPORTED_METADATA, "hwid_candidate_present": True,
        "hwid_candidate_supported": True, "device_id": hwid,
    }


def _get_sub(self, token, extra_headers=None):
    self.calls.append(("get_sub", token))
    return b"child-config-body", {"profile-title": "child"}


def _remote_and_ensure_fn():
    remote = FakeMarzban()
    remote.get_sub = _get_sub.__get__(remote, FakeMarzban)
    remote.users["alice"]["subscription_url"] = "/sub/hardening-source-token"
    original_create_user = remote.create_user

    def create_user_with_sub_url(payload, token):
        created = original_create_user(payload, token)
        remote.users[created["username"]]["subscription_url"] = f"/sub/{created['username']}-token"
        created["subscription_url"] = remote.users[created["username"]]["subscription_url"]
        return created

    remote.create_user = create_user_with_sub_url

    def ensure_fn(payload):
        return BrokerOperations(remote).dispatch("child.user.ensure", payload)

    def subscription_fn(payload):
        return BrokerOperations(remote).dispatch("child.user.subscription.get", payload)

    return remote, ensure_fn, subscription_fn


def _seed_account_with_first_child(db, *, mapping, tg, expire=0):
    account, alias_id, slot = _account(db, mapping=mapping, tg=tg, alias="alice")
    remote, ensure_fn, subscription_fn = _remote_and_ensure_fn()
    request_hash = source_contract_hash(remote.users["alice"])
    prepared = db.child_provisioning.prepare_child_ensure(
        account_id=account["account_id"], slot_generation_id=slot["generation_id"],
        source_alias_id=alias_id, source_contract_hash=request_hash, expire=expire,
        idempotency_key=f"hardening-seed-{mapping}", now=100,
    )
    claimed = db.child_provisioning.claim(
        prepared["operation_id"], worker_id="seed-worker", now=101, lease_seconds=5,
    )
    created = ensure_fn(claimed["payload"])
    child_uuid = created.pop("uuid")
    db.child_provisioning.acknowledge(
        prepared["operation_id"], worker_id="seed-worker",
        outcome=created["outcome"], child_uuid=child_uuid, remote_result=created, now=102,
    )
    return account, alias_id, slot, remote, ensure_fn, subscription_fn


def _issue_active_credential(db, account_id, *, idem_prefix, now=200):
    prepared = db.subscription_credentials.prepare(
        account_id=account_id, actor_ref="primary-admin", reason="hardening test",
        idempotency_key=f"{idem_prefix}-prepare", now=now,
    )
    db.subscription_credentials.activate(
        credential_id=prepared["id"], account_id=account_id,
        expected_generation=prepared["generation"], actor_ref="primary-admin",
        idempotency_key=f"{idem_prefix}-activate", now=now + 1,
    )
    return prepared["raw_token"]


# --- BUG F/E: existing non-ACTIVE child must never go through ENSURE again ----

def test_existing_disabled_child_with_changed_expiry_does_not_hit_ensure_conflict(db):
    """Reproduces the account_id=16-shaped defect: a child that already has a
    real remote identity goes DISABLED (e.g. between an expiry lapse and
    reconciliation), the parent's target expiry then changes (renewal /
    ADMIN_GRANT / commercial transition), and the very next resolve must
    NOT attempt to re-provision that identity through ENSURE.

    Before the fix this raised `ChildProvisioningConflict` (mutable expire
    under a slot-stable idempotency key) and was swallowed into a bare,
    permanently-repeating `OUTCOME_INTERNAL_ERROR` -- no amount of retrying
    or running the real convergence worker could ever fix it, because the
    conflicting outbox row was already durably written.

    After the fix, an existing identity is never re-ENSUREd: the resolver
    honestly reports `OUTCOME_PROVISIONING_UNAVAILABLE` (remote hasn't
    converged to the new expiry yet -- exactly true), and once the existing,
    unrelated `run_account_sync_cycle` convergence worker (src/parent_sync.py)
    does its ordinary job, the *same* identity resolves OK with the *same*
    child_username -- proving this is a transient, worker-recoverable state,
    not a poisoned one."""
    from src import parent_sync as parent_sync_module

    account, alias_id, slot, remote, ensure_fn, subscription_fn = _seed_account_with_first_child(
        db, mapping="HARDEN_F", tg=910001, expire=0,
    )
    _set_subscription(db, account["account_id"], status="ACTIVE", current_expiry=1_000)
    token = _issue_active_credential(db, account["account_id"], idem_prefix="harden-f")

    # First resolve: creates the device slot claim + reuses the seeded child.
    first = resolve_opaque_subscription(
        db, token, _known_hwid_meta("harden-f-hwid"),
        hmac_key=HWID_KEY, ensure_fn=ensure_fn, subscription_fn=subscription_fn,
        worker_id="harden-worker", now=300,
    )
    assert first.outcome == OUTCOME_OK
    child_username = first.child_username

    # Simulate the exact production sequence: the child intent's
    # observed_state flips to DISABLED (expired trial / stale reconciliation
    # window) while the remote child identity still exists...
    db._conn.execute(
        "UPDATE mgboost_child_user_intents SET observed_state='DISABLED' "
        "WHERE child_username=?", (child_username,),
    )
    # ...then the parent's entitlement changes to a NEW future expiry
    # (renewal / ADMIN_GRANT / legacy->commercial transition).
    db._conn.commit()
    _set_subscription(db, account["account_id"], status="ACTIVE", current_expiry=999_999)

    second = resolve_opaque_subscription(
        db, token, _known_hwid_meta("harden-f-hwid"),
        hmac_key=HWID_KEY, ensure_fn=ensure_fn, subscription_fn=subscription_fn,
        worker_id="harden-worker", now=400,
    )
    assert second.outcome == OUTCOME_PROVISIONING_UNAVAILABLE, (
        "an existing child identity must never be re-ENSUREd -- expected an "
        f"honest, worker-recoverable PROVISIONING_UNAVAILABLE, got {second.outcome!r}"
    )
    assert second.child_username is None

    def sync_fn(payload):
        return BrokerOperations(remote).dispatch("child.user.state.sync", payload)

    convergence = parent_sync_module.run_account_sync_cycle(
        db, account["account_id"], sync_fn=sync_fn, worker_id="harden-f-sync-worker", now=500,
    )
    assert convergence["errored"] == 0

    third = resolve_opaque_subscription(
        db, token, _known_hwid_meta("harden-f-hwid"),
        hmac_key=HWID_KEY, ensure_fn=ensure_fn, subscription_fn=subscription_fn,
        worker_id="harden-worker", now=600,
    )
    assert third.outcome == OUTCOME_OK
    assert third.child_username == child_username, "UUID/identity must not rotate on reactivation"


# --- BUG I: opaque root-token paths must be redacted in logs -----------------

def test_opaque_root_token_path_is_redacted_like_legacy_sub_path():
    token = generate_opaque_token()
    assert len(token) == 43
    redacted = redact_request_target(f"/{token}")
    assert token not in redacted, (
        "raw opaque subscription bearer must never appear in "
        "mgboost-panel.service stdout/journalctl logs"
    )


# --- BUG C: remote_effect_verifier must reflect real remote state ------------

def test_remote_effect_verifier_distinguishes_different_real_remote_states(db):
    from src import parent_sync as parent_sync_module

    account, alias_id, slot, remote, ensure_fn, subscription_fn = _seed_account_with_first_child(
        db, mapping="HARDEN_C", tg=910002, expire=0,
    )

    def sync_fn(payload):
        return BrokerOperations(remote).dispatch("child.user.state.sync", payload)

    db._conn.execute(
        "UPDATE mgboost_subscriptions SET status='ACTIVE',current_expiry=? WHERE account_id=?",
        (5_000, account["account_id"]),
    )
    db._conn.commit()
    parent_sync_module.run_account_sync_cycle(
        db, account["account_id"], sync_fn=sync_fn, worker_id="harden-c-worker", now=1_000,
    )
    first_verifier = db._conn.execute(
        "SELECT remote_effect_verifier FROM mgboost_parent_sync_attempt_events "
        "WHERE account_id=? AND event_type='SUCCEEDED' ORDER BY id DESC LIMIT 1",
        (account["account_id"],),
    ).fetchone()["remote_effect_verifier"]

    db._conn.execute(
        "UPDATE mgboost_subscriptions SET status='ACTIVE',current_expiry=? WHERE account_id=?",
        (9_000, account["account_id"]),
    )
    db._conn.commit()
    parent_sync_module.run_account_sync_cycle(
        db, account["account_id"], sync_fn=sync_fn, worker_id="harden-c-worker", now=2_000,
    )
    second_verifier = db._conn.execute(
        "SELECT remote_effect_verifier FROM mgboost_parent_sync_attempt_events "
        "WHERE account_id=? AND event_type='SUCCEEDED' ORDER BY id DESC LIMIT 1",
        (account["account_id"],),
    ).fetchone()["remote_effect_verifier"]

    assert first_verifier is not None and second_verifier is not None
    assert first_verifier != second_verifier, (
        "two distinct real remote expiries both finished with outcome "
        "SYNCED/ALREADY_IN_SYNC and must not collapse to the same verifier"
    )


# --- BUG D: a resolved, ACTIVE credential must never see "Subscription not
# found" for an internal/operational condition ---------------------------------

class _FakeHandler:
    def __init__(self, db, *, user_agent="Happ/2.7.0", peer="127.0.0.1"):
        import io
        self.client_address = (peer, 12345)
        self.headers = {"User-Agent": user_agent, "Host": "sub.beykus.fun"}
        self.server = type("S", (), {"db": db})()
        self.status = None
        self.sent_headers = []
        self.wfile = io.BytesIO()

    def send_response(self, status, message=None):
        self.status = status

    def send_header(self, k, v):
        self.sent_headers.append((k, v))

    def end_headers(self):
        pass


def test_valid_credential_with_remote_drift_gets_503_not_404(monkeypatch):
    """This is the exact account_id=16 Incident A shape at the HTTP layer:
    a real, ACTIVE, successfully-resolved credential whose child hit an
    internal PROVISIONING_UNAVAILABLE condition (remote drift / broker
    hiccup) must never produce the same response as an unknown/malformed/
    revoked bearer -- "Subscription not found" is a false, misleading
    signal once a real credential is in hand, and collapses a fixable
    backend condition into a dead end indistinguishable from a bad link."""
    import importlib
    import os
    import tempfile

    monkeypatch.setenv("DATA_DIR", tempfile.mkdtemp())
    monkeypatch.setenv("PRIMARY_MGBOOST_ADMIN_ACTOR_ID", PRIMARY)
    monkeypatch.setenv("PRIMARY_MGBOOST_ADMIN_LOGIN", PRIMARY_LOGIN)
    monkeypatch.setenv("OPAQUE_SUBSCRIPTION_ENABLED", "1")
    import src.config as config
    import src.database as database
    import src.routes.sub as sub_route
    import src.routes.opaque_sub as opaque_sub_route
    importlib.reload(config)
    importlib.reload(database)
    importlib.reload(sub_route)
    importlib.reload(opaque_sub_route)
    database.DB_PATH = os.path.join(tempfile.mkdtemp(), "db.sqlite3")
    db = database.Database()
    try:
        account, _alias_id, _slot = _account(db, mapping="HARDEN_D", tg=910003)
        token = _issue_active_credential(db, account["account_id"], idem_prefix="harden-d")

        from src.opaque_resolver import OpaqueResolveResult, OUTCOME_PROVISIONING_UNAVAILABLE
        monkeypatch.setattr(
            opaque_sub_route, "resolve_opaque_subscription",
            lambda *a, **k: OpaqueResolveResult(OUTCOME_PROVISIONING_UNAVAILABLE),
        )

        handler = _FakeHandler(db)
        opaque_sub_route.handle_opaque_sub(handler, token)

        assert handler.status == 503, (
            f"expected safe 503 for a resolved credential hitting an internal "
            f"condition, got {handler.status}"
        )
        body = handler.wfile.getvalue()
        assert body == b"Subscription temporarily unavailable\n"
        assert b"Subscription not found" not in body
        assert b"PROVISIONING_UNAVAILABLE" not in body, "no internal reason code leaks into the body"

        # The security fail-closed invariant must survive this fix
        # unchanged: an unresolved credential is still indistinguishable
        # from any other invalid bearer.
        monkeypatch.undo()
        unknown_handler = _FakeHandler(db)
        opaque_sub_route.handle_opaque_sub(unknown_handler, "A" * 43)
        assert unknown_handler.status == 404
        assert unknown_handler.wfile.getvalue() == b"Subscription not found\n"
    finally:
        db._conn.close()
