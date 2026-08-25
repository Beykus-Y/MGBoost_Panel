"""PH4-01 LegacyBridgeStore: root-only staged-rollout binding, deterministic
evidence-based resolution, cross-account isolation."""

import importlib
import os
import tempfile

import pytest

from src.legacy_bridge import LegacyBridgeConflict, LegacyBridgeError, PrimaryAdminRequired
from src.security import AdminSessionStore

from tests.test_child_provisioning import PRIMARY, PRIMARY_LOGIN, _account


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


def _capability(db):
    _raw, session = AdminSessionStore().create(PRIMARY_LOGIN, "legacy-bridge-test-jwt")
    return db.primary_admin_authority.authorize_session(session)


def test_no_binding_means_no_resolution(db):
    account, alias_id, _slot = _account(db, mapping="BRIDGE_NONE", tg=830001)
    assert db.legacy_bridge.resolve_account_for_legacy_username("alice") is None


def test_create_binding_disabled_by_default_does_not_resolve(db):
    account, alias_id, _slot = _account(db, mapping="BRIDGE_DISABLED", tg=830002)
    cap = _capability(db)
    db.legacy_bridge.create_binding(
        capability=cap, account_id=account["account_id"], legacy_alias_id=alias_id,
        enabled=False, decision_ref="owner-approved-test-v1", now=100,
    )
    assert db.legacy_bridge.resolve_account_for_legacy_username("alice") is None


def test_create_binding_enabled_resolves_deterministically(db):
    account, alias_id, _slot = _account(db, mapping="BRIDGE_ENABLED", tg=830003, alias="alice")
    cap = _capability(db)
    db.legacy_bridge.create_binding(
        capability=cap, account_id=account["account_id"], legacy_alias_id=alias_id,
        enabled=True, decision_ref="owner-approved-test-v2", now=100,
    )
    assert db.legacy_bridge.resolve_account_for_legacy_username("alice") == account["account_id"]
    # a totally different username never resolves, no inference of any kind
    assert db.legacy_bridge.resolve_account_for_legacy_username("alice2") is None
    assert db.legacy_bridge.resolve_account_for_legacy_username("") is None


def test_enable_disable_toggle(db):
    account, alias_id, _slot = _account(db, mapping="BRIDGE_TOGGLE", tg=830004, alias="alice")
    cap = _capability(db)
    db.legacy_bridge.create_binding(
        capability=cap, account_id=account["account_id"], legacy_alias_id=alias_id,
        enabled=False, decision_ref="owner-approved-toggle-v1", now=100,
    )
    assert db.legacy_bridge.resolve_account_for_legacy_username("alice") is None
    db.legacy_bridge.enable(capability=cap, account_id=account["account_id"], reason="canary start", now=101)
    assert db.legacy_bridge.resolve_account_for_legacy_username("alice") == account["account_id"]
    db.legacy_bridge.disable(capability=cap, account_id=account["account_id"], reason="canary stop", now=102)
    assert db.legacy_bridge.resolve_account_for_legacy_username("alice") is None


def test_second_create_binding_for_same_account_conflicts(db):
    account, alias_id, _slot = _account(db, mapping="BRIDGE_DUP", tg=830005)
    cap = _capability(db)
    db.legacy_bridge.create_binding(
        capability=cap, account_id=account["account_id"], legacy_alias_id=alias_id,
        enabled=True, decision_ref="owner-approved-dup-v1", now=100,
    )
    with pytest.raises(LegacyBridgeConflict):
        db.legacy_bridge.create_binding(
            capability=cap, account_id=account["account_id"], legacy_alias_id=alias_id,
            enabled=True, decision_ref="owner-approved-dup-v2", now=101,
        )


def test_binding_requires_alias_to_belong_to_the_same_account(db):
    account_a, _alias_a, _slot_a = _account(db, mapping="BRIDGE_XACC_A", tg=830006)
    account_b, alias_b, _slot_b = _account(db, mapping="BRIDGE_XACC_B", tg=830007, alias="second-source")
    cap = _capability(db)
    with pytest.raises(LegacyBridgeError, match="does not belong"):
        db.legacy_bridge.create_binding(
            capability=cap, account_id=account_a["account_id"], legacy_alias_id=alias_b,
            enabled=True, decision_ref="owner-approved-cross-v1", now=100,
        )


def test_binding_requires_primary_admin_capability(db):
    account, alias_id, _slot = _account(db, mapping="BRIDGE_NOAUTH", tg=830008)
    with pytest.raises(PrimaryAdminRequired):
        db.legacy_bridge.create_binding(
            capability=None, account_id=account["account_id"], legacy_alias_id=alias_id,
            enabled=True, decision_ref="owner-approved-noauth-v1", now=100,
        )


def test_throwaway_account_binding_cannot_collide_with_a_real_account(db):
    """The disabled PH3-08 throwaway canary pattern (account 2 in production)
    must never leak into resolution for a real account's legacy username --
    proven here by two independent bindings for two independent accounts,
    each resolving only its own exact legacy_username."""
    real_account, real_alias, _slot1 = _account(db, mapping="BRIDGE_REAL", tg=830009, alias="alice")
    throwaway_account, throwaway_alias, _slot2 = _account(
        db, mapping="BRIDGE_THROWAWAY", tg=830010, alias="throwaway_canary_user",
    )
    cap = _capability(db)
    db.legacy_bridge.create_binding(
        capability=cap, account_id=real_account["account_id"], legacy_alias_id=real_alias,
        enabled=True, decision_ref="owner-approved-real-v1", now=100,
    )
    db.legacy_bridge.create_binding(
        capability=cap, account_id=throwaway_account["account_id"], legacy_alias_id=throwaway_alias,
        enabled=True, decision_ref="owner-approved-throwaway-v1", now=101,
    )
    assert db.legacy_bridge.resolve_account_for_legacy_username("alice") == real_account["account_id"]
    assert (
        db.legacy_bridge.resolve_account_for_legacy_username("throwaway_canary_user")
        == throwaway_account["account_id"]
    )
    # disabling the throwaway account's own binding never touches the real one
    db.legacy_bridge.disable(
        capability=cap, account_id=throwaway_account["account_id"], reason="throwaway cleanup", now=102,
    )
    assert db.legacy_bridge.resolve_account_for_legacy_username("throwaway_canary_user") is None
    assert db.legacy_bridge.resolve_account_for_legacy_username("alice") == real_account["account_id"]
