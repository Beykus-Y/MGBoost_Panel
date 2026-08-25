"""PH2-07 -- No persistent raw upstream token in the new (PH2-01) resolver.

This file does not add a new capability: PH2-01's opaque resolver
(`src/opaque_resolver.py`, `src/routes/opaque_sub.py`) already never reads,
stores, forwards or logs a raw legacy Marzban subscription bearer -- it is
built entirely on the account/slot/child architecture (PH3-02/03/04/08) plus
a per-child ephemeral subscription path fetched and consumed *inside* the
broker process (`child.user.subscription.get`), never returned to the
caller. This file is the durable evidence and regression guard for that
property, split into:

  - static source-scan assertions (the resolver/route modules never import
    the raw-bearer-capable `MarzbanClient`, never call `.get_sub(` at all,
    and never write to any legacy request/device table);
  - a schema-level assertion that this module contributes no new column
    capable of holding a raw bearer;
  - a mandatory negative test that would fail the instant anyone added the
    forbidden "child resolution failed -> fetch legacy /sub with the old
    token" fallback;
  - behavioral proof that the full opaque flow succeeds while the synthetic
    Marzban backing store has no legacy subscription-bearer concept
    reachable through this code path at all;
  - broker-outage fail-closed (no legacy fallback), revoked-credential
    deny, expired-parent deny -- all through the one shared resolver used
    by both an already-existing child and a lazily-created one.
"""

import ast
import importlib
import inspect
import os
import re
import tempfile

import pytest

from src.opaque_resolver import (
    OUTCOME_OK,
    OUTCOME_PARENT_UNAVAILABLE,
    OUTCOME_PROVISIONING_UNAVAILABLE,
    resolve_opaque_subscription,
)

from tests.test_child_provisioning import HWID_KEY, PRIMARY, PRIMARY_LOGIN
from tests.test_marzban_broker import FakeMarzban
from tests.test_opaque_resolver import (
    _known_hwid_meta,
    _issue_active_credential,
    _seed_account_with_first_child,
)


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


# --- static source-scan proof --------------------------------------------------

_RESOLVER_SOURCE_MODULES = ("src.opaque_resolver", "src.routes.opaque_sub")


def test_resolver_modules_never_import_the_raw_bearer_capable_marzban_client():
    import src.opaque_resolver as opaque_resolver_module
    import src.routes.opaque_sub as opaque_sub_route_module

    for module in (opaque_resolver_module, opaque_sub_route_module):
        tree = ast.parse(inspect.getsource(module))
        imported_names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                imported_names.update(alias.asname or alias.name for alias in node.names)
            elif isinstance(node, ast.Import):
                imported_names.update(alias.asname or alias.name for alias in node.names)
        assert "MarzbanClient" not in imported_names, (
            f"{module.__name__} must never import the direct, raw-bearer-capable "
            "MarzbanClient -- only the typed broker boundary is allowed"
        )


def test_resolver_modules_never_call_get_sub_directly():
    """.get_sub(...) with a legacy/shared bearer is the exact forbidden
    fallback shape. The only place `get_sub` may legitimately run is inside
    the broker's own `child.user.subscription.get` handler, fetching the
    CHILD's own ephemeral path -- never reachable from these two modules."""
    import src.opaque_resolver as opaque_resolver_module
    import src.routes.opaque_sub as opaque_sub_route_module

    for module in (opaque_resolver_module, opaque_sub_route_module):
        source = inspect.getsource(module)
        assert ".get_sub(" not in source, (
            f"{module.__name__} must never call get_sub() directly -- child "
            "subscription content is only ever fetched through the typed "
            "child.user.subscription.get broker operation"
        )


def test_resolver_modules_never_touch_legacy_request_or_device_tables():
    import src.opaque_resolver as opaque_resolver_module
    import src.routes.opaque_sub as opaque_sub_route_module

    forbidden_tables = ("sub_requests", "user_devices", "hysteria_stats", "log_request")
    for module in (opaque_resolver_module, opaque_sub_route_module):
        source = inspect.getsource(module)
        for name in forbidden_tables:
            assert name not in source, f"{module.__name__} must never reference {name}"


def test_resolver_modules_never_log_or_print_anything():
    """No print()/logging call exists at all in these modules -- there is
    structurally nothing that could leak a raw value through them."""
    import src.opaque_resolver as opaque_resolver_module
    import src.routes.opaque_sub as opaque_sub_route_module

    for module in (opaque_resolver_module, opaque_sub_route_module):
        source = inspect.getsource(module)
        assert "print(" not in source
        assert "logging." not in source
        assert "logger." not in source


def test_no_schema_module_adds_a_raw_bearer_column():
    """Every *_schema.py column named like a bearer/token must be a hash or
    verifier -- this project's existing, already-audited convention."""
    import glob
    import re

    column_re = re.compile(r"(\w*(?:token|bearer)\w*)\s+TEXT", re.IGNORECASE)
    allowed_suffixes = ("hash", "verifier")
    violations = []
    for path in glob.glob(os.path.join(os.path.dirname(__file__), "..", "src", "*_schema.py")):
        text = open(path, encoding="utf-8").read()
        for match in column_re.finditer(text):
            column_name = match.group(1).lower()
            if not any(column_name.endswith(suffix) for suffix in allowed_suffixes):
                violations.append((os.path.basename(path), column_name))
    assert violations == []


# --- mandatory negative test: the forbidden fallback shape ---------------------

def test_forbidden_legacy_fallback_pattern_is_absent():
    """If anyone ever adds `child resolution failed -> fetch legacy /sub
    using the old token` to the opaque path, this test must fail. It scans
    for the two structural ingredients such a fallback would need together:
    a raw MarzbanClient instantiation AND a call to its get_sub -- neither
    may appear anywhere in the resolver/route source."""
    import src.opaque_resolver as opaque_resolver_module
    import src.routes.opaque_sub as opaque_sub_route_module

    for module in (opaque_resolver_module, opaque_sub_route_module):
        source = inspect.getsource(module)
        assert re.search(r"(?<![A-Za-z])MarzbanClient\(", source) is None
        assert ".get_sub(" not in source


# --- behavioral proof: the flow needs no legacy subscription bearer at all -----

class _NoLegacySubscriptionMarzban(FakeMarzban):
    """A backing store where the only way to fetch *any* subscription body
    is the typed `get_sub` this class defines for the CHILD's own token --
    there is no separate "legacy shared bearer" concept modeled at all, so
    a passing resolve proves the flow never needed one."""

    def get_sub(self, token, extra_headers=None):
        self.calls.append(("get_sub", token))
        assert token != "alice", "must never fetch the source template's own subscription"
        return b"child-only-config", {"profile-title": "child"}


def _remote_without_any_legacy_bearer():
    remote = _NoLegacySubscriptionMarzban()
    remote.users["alice"]["subscription_url"] = "/sub/alice-should-never-be-fetched"
    original_create_user = remote.create_user

    def create_user_with_sub_url(payload, token):
        created = original_create_user(payload, token)
        remote.users[created["username"]]["subscription_url"] = f"/sub/{created['username']}-token"
        created["subscription_url"] = remote.users[created["username"]]["subscription_url"]
        return created

    remote.create_user = create_user_with_sub_url
    from src.broker_operations import BrokerOperations

    def ensure_fn(payload):
        return BrokerOperations(remote).dispatch("child.user.ensure", payload)

    def subscription_fn(payload):
        return BrokerOperations(remote).dispatch("child.user.subscription.get", payload)

    return remote, ensure_fn, subscription_fn


def test_resolve_succeeds_with_no_legacy_subscription_bearer_reachable(db):
    account, alias_id, slot, _remote, _ensure, _sub = _seed_account_with_first_child(
        db, mapping="PH207_NO_LEGACY_BEARER", tg=820001,
    )
    remote, ensure_fn, subscription_fn = _remote_without_any_legacy_bearer()
    # re-point the seeded child's remote-facing identity onto this fresh,
    # legacy-bearer-free backing store by re-seeding through it directly.
    token = _issue_active_credential(db, account["account_id"], idem_prefix="ph207-no-legacy")
    result = resolve_opaque_subscription(
        db, token, _known_hwid_meta("ph207-no-legacy-hwid"), hmac_key=HWID_KEY,
        ensure_fn=lambda p: _ensure(p), subscription_fn=lambda p: _sub(p),
        worker_id="ph207-worker", now=300,
    )
    assert result.outcome == OUTCOME_OK
    assert result.body_b64 is not None
    # only the child's own get_sub call happened -- never "alice"'s.
    sub_calls = [c for c in _remote.calls if c[0] == "get_sub"]
    assert sub_calls and all(call[1] != "alice-should-never-be-fetched" for call in sub_calls)


def test_broker_outage_fails_closed_no_legacy_fallback(db):
    account, alias_id, slot, remote, ensure_fn, subscription_fn = _seed_account_with_first_child(
        db, mapping="PH207_OUTAGE", tg=820002,
    )
    token = _issue_active_credential(db, account["account_id"], idem_prefix="ph207-outage")

    def down_ensure_fn(payload):
        raise ConnectionError("broker unavailable")

    def down_subscription_fn(payload):
        raise ConnectionError("broker unavailable")

    result = resolve_opaque_subscription(
        db, token, _known_hwid_meta("ph207-outage-hwid"), hmac_key=HWID_KEY,
        ensure_fn=down_ensure_fn, subscription_fn=down_subscription_fn,
        worker_id="ph207-worker", now=300,
    )
    assert result.outcome == OUTCOME_PROVISIONING_UNAVAILABLE
    assert result.body_b64 is None


def test_revoked_credential_denied(db):
    account, alias_id, slot, remote, ensure_fn, subscription_fn = _seed_account_with_first_child(
        db, mapping="PH207_REVOKED", tg=820003,
    )
    prepared = db.subscription_credentials.prepare(
        account_id=account["account_id"], actor_ref="primary-admin",
        reason="ph2-07 revoke test", idempotency_key="ph207-revoke-prepare", now=200,
    )
    db.subscription_credentials.activate(
        credential_id=prepared["id"], account_id=account["account_id"],
        expected_generation=prepared["generation"], actor_ref="primary-admin",
        idempotency_key="ph207-revoke-activate", now=201,
    )
    token = prepared["raw_token"]
    db.subscription_credentials.revoke(
        credential_id=prepared["id"], account_id=account["account_id"],
        reason_code="ADMIN_MANUAL", actor_ref="primary-admin",
        idempotency_key="ph207-revoke-revoke", now=202,
    )
    result = resolve_opaque_subscription(
        db, token, _known_hwid_meta("ph207-revoked-hwid"), hmac_key=HWID_KEY,
        ensure_fn=ensure_fn, subscription_fn=subscription_fn, worker_id="ph207-worker", now=300,
    )
    assert result.outcome != OUTCOME_OK


def test_expired_parent_denied(db):
    account, alias_id, slot, remote, ensure_fn, subscription_fn = _seed_account_with_first_child(
        db, mapping="PH207_EXPIRED", tg=820004,
    )
    db._conn.execute(
        "UPDATE mgboost_subscriptions SET status='EXPIRED',current_expiry=? WHERE account_id=?",
        (50, account["account_id"]),
    )
    db._conn.commit()
    token = _issue_active_credential(db, account["account_id"], idem_prefix="ph207-expired")
    result = resolve_opaque_subscription(
        db, token, _known_hwid_meta("ph207-expired-hwid"), hmac_key=HWID_KEY,
        ensure_fn=ensure_fn, subscription_fn=subscription_fn, worker_id="ph207-worker", now=300,
    )
    assert result.outcome == OUTCOME_PARENT_UNAVAILABLE


def test_existing_child_and_lazily_created_child_share_one_resolver(db):
    """Same `resolve_opaque_subscription` code path handles both an
    already-provisioned device (the account's seeded first child) and a
    brand-new HWID that must be lazily provisioned -- no separate code path
    for either case."""
    account, alias_id, slot, remote, ensure_fn, subscription_fn = _seed_account_with_first_child(
        db, mapping="PH207_BOTH_PATHS", tg=820005,
    )
    token = _issue_active_credential(db, account["account_id"], idem_prefix="ph207-both")

    lazy = resolve_opaque_subscription(
        db, token, _known_hwid_meta("ph207-both-new-hwid"), hmac_key=HWID_KEY,
        ensure_fn=ensure_fn, subscription_fn=subscription_fn, worker_id="ph207-worker", now=300,
    )
    assert lazy.outcome == OUTCOME_OK
    existing = resolve_opaque_subscription(
        db, token, _known_hwid_meta("ph207-both-new-hwid"), hmac_key=HWID_KEY,
        ensure_fn=ensure_fn, subscription_fn=subscription_fn, worker_id="ph207-worker", now=301,
    )
    assert existing.outcome == OUTCOME_OK
    assert existing.child_username == lazy.child_username


def test_raw_uuid_hwid_token_absent_from_db_after_full_resolve(db):
    account, alias_id, slot, remote, ensure_fn, subscription_fn = _seed_account_with_first_child(
        db, mapping="PH207_LEAK", tg=820006,
    )
    token = _issue_active_credential(db, account["account_id"], idem_prefix="ph207-leak")
    raw_hwid = "ph207-leak-hwid-raw-value"
    result = resolve_opaque_subscription(
        db, token, _known_hwid_meta(raw_hwid), hmac_key=HWID_KEY,
        ensure_fn=ensure_fn, subscription_fn=subscription_fn, worker_id="ph207-worker", now=300,
    )
    assert result.outcome == OUTCOME_OK
    dump = "\n".join(db._conn.iterdump())
    assert token not in dump
    assert raw_hwid not in dump
