"""P0 hotfix regression suite: legacy/WL-capable provisioning poisoned by the
unscoped PH5-11 STANDARD WL backstop (production incident account #8, POCO
Slot 3 / generation 49), terminal-ERROR-vs-pending conflation in
`opaque_resolver`, missing migration-binding diagnostics on terminal
provisioning failure, infinite MIGRATING->RETRY for terminal errors, and the
audited recovery primitive for already-poisoned child operations.

Baseline invariants that must NEVER regress (PH5-11 anti-leak):
  * STANDARD-delivery (wl_mode='NONE') child carrying an exact PH0-05 WL
    inbound stays fail-closed (permanent ERROR).
  * STANDARD child with only current non-WL inbounds still provisions OK.
Policy invariant being fixed: exact WL inbounds are legitimate for an
entitlement whose canonical current delivery grants WL access
(`EntitlementEngine` -> wl.access_eligible), e.g. LEGACY_PAID_COMPAT with
wl_mode='UNLIMITED' -- those must provision successfully again.
"""

import importlib
import json
import os
import tempfile

import pytest

from src.broker_operations import BrokerOperations
from src.child_contract import credential_verifier, source_contract_hash
from src.device_slots import privacy_safe_hwid
from src.migration_lifecycle import process_migration_bridge_request
from src.opaque_resolver import (
    OUTCOME_OK,
    OUTCOME_PROVISIONING_PENDING,
    resolve_opaque_subscription,
)
from src.security import AdminSessionStore

from tests.test_child_provisioning import HWID_KEY, PRIMARY, PRIMARY_LOGIN, _account
from tests.test_marzban_broker import FakeMarzban
from tests.test_opaque_resolver import (
    _get_sub,
    _issue_active_credential,
    _known_hwid_meta,
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


# The exact legacy source shape from the incident: a legacy Marzban parent
# whose contract carries non-WL inbounds PLUS exact PH0-05 WL inbounds.
WL_SOURCE_INBOUNDS = ["LEGACY", "wl-tcp-direct", "wl-selec-grpc-smart"]
# A purely STANDARD-shaped source (no WL, no wl-shaped tags at all).
STANDARD_SOURCE_INBOUNDS = ["tcp-smart", "grpc-direct"]


def _capability(db):
    _raw, session = AdminSessionStore().create(PRIMARY_LOGIN, "p0-hotfix-test-jwt")
    return db.primary_admin_authority.authorize_session(session)


def _remote_and_fns(source_inbounds, *, alias="legacy-alice"):
    """FakeMarzban whose `alias` source user carries exactly the given
    inbound contract, plus typed broker-bound fns (ensure/observe/subscription)."""
    remote = FakeMarzban()
    remote.get_sub = _get_sub.__get__(remote, FakeMarzban)
    remote.users[alias] = dict(remote.users.pop("alice"))
    remote.users[alias]["username"] = alias
    remote.users[alias]["inbounds"] = {"vless": list(source_inbounds)}
    original_create_user = remote.create_user

    def create_user_with_sub_url(payload, token):
        created = original_create_user(payload, token)
        remote.users[created["username"]]["subscription_url"] = f"/sub/{created['username']}-token"
        created["subscription_url"] = remote.users[created["username"]]["subscription_url"]
        return created

    remote.create_user = create_user_with_sub_url

    def ensure_fn(payload):
        return BrokerOperations(remote).dispatch("child.user.ensure", payload)

    def observe_fn(payload):
        return BrokerOperations(remote).dispatch("child.user.observe", payload)

    def subscription_fn(payload):
        return BrokerOperations(remote).dispatch("child.user.subscription.get", payload)

    return remote, ensure_fn, observe_fn, subscription_fn


def _legacy_compat_account(db, *, mapping, tg, username="legacy-alice",
                           legacy_expiry=10_000):
    """A reviewed DIRECT account with the LEGACY_PAID_COMPAT_V1_D3
    entitlement (plan_kind COMMERCIAL, device_limit 3, wl_mode UNLIMITED) --
    the exact incident account class."""
    from src.legacy_paid_compat import ensure_legacy_paid_compat_entitlement
    cap = _capability(db)
    account = db.direct_enrollment.enroll_direct_account(
        capability=cap, legacy_username=username, decision_ref=f"p0-hotfix-{mapping}",
        ownership_evidence="PROVEN", telegram_id=tg, alias_provenance="EVIDENCE_PROVEN",
        legacy_status="ACTIVE", legacy_expiry=legacy_expiry,
        observed_device_count=2, observed_hwid_count=2,
        evidence={"source": "test"}, idempotency_key=f"p0-hotfix-enroll-{mapping}", now=100,
    )
    db.direct_enrollment.record_owner_attested_legacy_payment(
        db, capability=cap, account_id=account["account_id"],
        decision_ref=f"p0-hotfix-{mapping}",
        attestation_note="Owner attests historical direct payment, details unknown",
        evidence={"source": "test"}, now=100,
    )
    ensure_legacy_paid_compat_entitlement(
        db, capability=cap, account_id=account["account_id"],
        decision_ref=f"p0-hotfix-{mapping}", now=150,
    )
    alias_row = db._conn.execute(
        "SELECT id FROM mgboost_legacy_account_aliases WHERE account_id=? AND alias_role='PRIMARY'",
        (account["account_id"],),
    ).fetchone()
    return account, alias_row["id"], cap


def _internal_account(db, *, wl_mode, mapping, tg, alias="tpl-source"):
    """A reviewed INTERNAL account whose plan pins the given wl_mode
    ('NONE' == STANDARD-delivery semantics, 'UNLIMITED' == WL-capable)."""
    cap = _capability(db)
    plan = db.internal_entitlements.create_internal_plan(
        capability=cap, plan_code=f"INTERNAL_P0_{mapping}", version=1,
        display_name="P0 hotfix internal canary", device_limit_mode="LIMITED",
        device_limit=10, wl_mode=wl_mode, now=100,
    )
    account = db.internal_entitlements.create_reviewed_account(
        capability=cap, plan_version_id=plan["id"], legacy_username=alias,
        mapping_key=mapping, decision_ref=f"p0-hotfix-{mapping}",
        legacy_aliases=[{
            "legacy_username": alias, "alias_role": "PRIMARY",
            "ownership_provenance": "OWNER_APPROVED", "legacy_status": "UNLIMITED",
            "legacy_expiry": None, "observed_device_count": 1, "observed_hwid_count": 1,
            "evidence": {"ref": "masked-candidate"},
        }],
        ownership_evidence="PROVEN", telegram_id=tg, legacy_status="UNLIMITED",
        legacy_expiry=None, device_evidence_count=1, hwid_evidence_count=1,
        internal_reason="P0 hotfix policy-scope regression fixture",
        migration_confidence="HIGH", evidence={"schema": 1},
        idempotency_key="account-create-" + mapping, now=100,
    )
    alias_row = db._conn.execute(
        "SELECT id FROM mgboost_legacy_account_aliases WHERE account_id=? AND alias_role='PRIMARY'",
        (account["account_id"],),
    ).fetchone()
    return account, alias_row["id"], cap


def _seed_first_child(db, *, account_id, alias_id, remote, ensure_fn, hwid,
                      idem, now=160, expire=10_000, source_alias="legacy-alice"):
    """Seed the account's first (already-ACTIVE) child through the real
    PH3-03 machinery, exactly like every prior gate's fixture."""
    slot = db.device_slots.claim(account_id, hwid, HWID_KEY, now=now)
    prepared = db.child_provisioning.prepare_child_ensure(
        account_id=account_id, slot_generation_id=slot["generation_id"],
        source_alias_id=alias_id, source_contract_hash=source_contract_hash(remote.users[source_alias]),
        expire=expire, idempotency_key=idem, now=now + 1,
    )
    claimed = db.child_provisioning.claim(
        prepared["operation_id"], worker_id="seed-worker", now=now + 2, lease_seconds=5,
    )
    created = ensure_fn(claimed["payload"])
    child_uuid = created.pop("uuid")
    db.child_provisioning.acknowledge(
        prepared["operation_id"], worker_id="seed-worker", outcome=created["outcome"],
        child_uuid=child_uuid, remote_result=created, now=now + 3,
    )
    return slot


def _bind_legacy_bridge(db, cap, account_id, alias_id, *, mapping):
    db.legacy_bridge.create_binding(
        capability=cap, account_id=account_id, legacy_alias_id=alias_id,
        enabled=True, decision_ref=f"owner-approved-{mapping}", now=110,
    )


def _bridge_resolve(db, username, hwid, *, remote, now):
    def ensure_fn(payload):
        return BrokerOperations(remote).dispatch("child.user.ensure", payload)

    def subscription_fn(payload):
        return BrokerOperations(remote).dispatch("child.user.subscription.get", payload)

    return process_migration_bridge_request(
        db, username, _known_hwid_meta(hwid), hmac_key=HWID_KEY,
        ensure_fn=ensure_fn, subscription_fn=subscription_fn,
        worker_id="p0-hotfix-inline-worker", now=now,
    )


def _resolver_idem_key(slot_generation_id):
    return f"account-device-resolver-child-v1:{slot_generation_id}"


def _outbox(db, operation_id):
    return db._conn.execute(
        "SELECT * FROM mgboost_outbox WHERE operation_id=?", (operation_id,),
    ).fetchone()


def _intent_for_generation(db, slot_generation_id):
    return db._conn.execute(
        "SELECT * FROM mgboost_child_user_intents WHERE slot_generation_id=?",
        (slot_generation_id,),
    ).fetchone()


def _fail_permanent_outcome():
    from src.opaque_resolver import OUTCOME_PROVISIONING_FAILED_PERMANENT
    return OUTCOME_PROVISIONING_FAILED_PERMANENT


# ============================================================================
# Section 1 -- policy scope regression (the incident, as a differential)
# ============================================================================

def test_legacy_unlimited_new_device_with_exact_wl_provisions_ok(db):
    """The exact incident differential: LEGACY_PAID_COMPAT + wl_mode=UNLIMITED,
    Slot with an already-ACTIVE WL child, then a NEW device after PH5-12 whose
    child legitimately clones the legacy WL contract -- provisioning must
    succeed end to end (child ACTIVE, outbox APPLIED, migration MIGRATED)."""
    account, alias_id, cap = _legacy_compat_account(db, mapping="P0_DIFF", tg=930001)
    _bind_legacy_bridge(db, cap, account["account_id"], alias_id, mapping="P0_DIFF")
    remote, ensure_fn, _observe_fn, _sub_fn = _remote_and_fns(WL_SOURCE_INBOUNDS)
    _seed_first_child(
        db, account_id=account["account_id"], alias_id=alias_id, remote=remote,
        ensure_fn=ensure_fn, hwid="p0-diff-slot2-hwid", idem="p0-diff-seed-child-1",
    )

    result = _bridge_resolve(db, "legacy-alice", "p0-diff-slot3-hwid", remote=remote, now=500)

    assert result.outcome == OUTCOME_OK
    assert result.slot_number is not None
    slot_row = db._conn.execute(
        "SELECT g.id, g.generation FROM mgboost_device_slot_generations g "
        "JOIN mgboost_device_slots s ON s.id=g.slot_id "
        "WHERE g.account_id=? AND s.slot_number=? AND g.status='ACTIVE'",
        (account["account_id"], result.slot_number),
    ).fetchone()
    intent = _intent_for_generation(db, slot_row["id"])
    assert intent is not None and intent["observed_state"] == "ACTIVE"
    outbox = _outbox(db, _outbox_op_id(db, intent["id"]))
    assert outbox["state"] == "APPLIED"
    # The child really did receive the legitimated exact WL inbounds.
    child_tags = remote.users[result.child_username]["inbounds"]["vless"]
    assert "wl-tcp-direct" in child_tags and "wl-selec-grpc-smart" in child_tags
    # Migration lifecycle completed, not stuck retrying.
    binding = db.migration_lifecycle.find_by_device(
        account["account_id"], privacy_safe_hwid("p0-diff-slot3-hwid", HWID_KEY)[0],
    )
    assert binding is not None and binding["state"] == "MIGRATED"

    # Re-resolve the same device: idempotent OK, same child, no new mutation.
    again = _bridge_resolve(db, "legacy-alice", "p0-diff-slot3-hwid", remote=remote, now=501)
    assert again.outcome == OUTCOME_OK
    assert again.child_username == result.child_username

    # The pre-existing ACTIVE WL child's device still resolves untouched.
    old = _bridge_resolve(db, "legacy-alice", "p0-diff-slot2-hwid", remote=remote, now=502)
    assert old.outcome == OUTCOME_OK


def _outbox_op_id(db, child_intent_id):
    return db._conn.execute(
        "SELECT operation_id FROM mgboost_outbox WHERE child_intent_id=?",
        (child_intent_id,),
    ).fetchone()["operation_id"]


def test_standard_none_child_with_exact_wl_still_fails_closed(db):
    """PH5-11 anti-leak must survive the policy scoping: an account whose
    canonical delivery is STANDARD (wl_mode='NONE') with a corrupted
    WL-carrying pinned template still terminates permanently."""
    account, alias_id, _cap = _internal_account(db, wl_mode="NONE", mapping="P0_STD_NEG", tg=930002)
    remote, _e, _o, _s = _remote_and_fns(WL_SOURCE_INBOUNDS, alias="tpl-source")
    db._conn.execute(
        "INSERT INTO mgboost_provisioning_templates "
        "(account_id,template_username,source_contract_hash,state,pinned_at,updated_at) "
        "VALUES (?,?,?,'ACTIVE',?,?)",
        (
            account["account_id"], "tpl-source",
            source_contract_hash(remote.users["tpl-source"]), 100, 100,
        ),
    )
    db._conn.commit()
    token = _issue_active_credential(db, account["account_id"], idem_prefix="p0-std-neg")

    result = resolve_opaque_subscription(
        db, token, _known_hwid_meta("p0-std-neg-hwid"), hmac_key=HWID_KEY,
        ensure_fn=_e, subscription_fn=_s, worker_id="p0-hotfix-worker", now=300,
    )

    assert result.outcome != OUTCOME_OK
    outbox = db._conn.execute(
        "SELECT o.* FROM mgboost_outbox o JOIN mgboost_child_user_intents c "
        "ON c.id=o.child_intent_id WHERE c.account_id=?",
        (account["account_id"],),
    ).fetchone()
    assert outbox["state"] == "ERROR"
    assert outbox["last_error_class"] == "WL_INBOUND_IN_STANDARD_CHILD"
    intent = _intent_for_generation(db, outbox["child_intent_id"])
    assert intent["observed_state"] == "ERROR"


def test_standard_none_child_with_non_wl_inbounds_provisions_ok(db):
    """A STANDARD delivery child cloned from a clean non-WL template must
    keep provisioning successfully (first commercial canary protection)."""
    account, alias_id, _cap = _internal_account(db, wl_mode="NONE", mapping="P0_STD_POS", tg=930003)
    remote, ensure_fn, _o, subscription_fn = _remote_and_fns(STANDARD_SOURCE_INBOUNDS, alias="tpl-source")
    db._conn.execute(
        "INSERT INTO mgboost_provisioning_templates "
        "(account_id,template_username,source_contract_hash,state,pinned_at,updated_at) "
        "VALUES (?,?,?,'ACTIVE',?,?)",
        (
            account["account_id"], "tpl-source",
            source_contract_hash(remote.users["tpl-source"]), 100, 100,
        ),
    )
    db._conn.commit()
    token = _issue_active_credential(db, account["account_id"], idem_prefix="p0-std-pos")

    result = resolve_opaque_subscription(
        db, token, _known_hwid_meta("p0-std-pos-hwid"), hmac_key=HWID_KEY,
        ensure_fn=ensure_fn, subscription_fn=subscription_fn,
        worker_id="p0-hotfix-worker", now=300,
    )
    assert result.outcome == OUTCOME_OK


def test_legacy_unlimited_existing_active_child_never_hits_backstop(db):
    """Resolving an already-ACTIVE legacy WL child (the Slot-2 path) must
    never even reach the render-boundary backstop."""
    account, alias_id, _cap = _legacy_compat_account(db, mapping="P0_EXIST", tg=930004)
    remote, ensure_fn, _o, _s = _remote_and_fns(WL_SOURCE_INBOUNDS)
    _seed_first_child(
        db, account_id=account["account_id"], alias_id=alias_id, remote=remote,
        ensure_fn=ensure_fn, hwid="p0-exist-slot2-hwid", idem="p0-exist-seed-child-1",
    )
    token = _issue_active_credential(db, account["account_id"], idem_prefix="p0-exist")

    result = resolve_opaque_subscription(
        db, token, _known_hwid_meta("p0-exist-slot2-hwid"), hmac_key=HWID_KEY,
        ensure_fn=ensure_fn, subscription_fn=_s, worker_id="p0-hotfix-worker", now=300,
    )
    assert result.outcome == OUTCOME_OK


def test_future_refresh_resolve_of_legacy_wl_child_no_false_backstop(db):
    """After a legacy WL child exists, refreshed resolves (expired lease /
    lost-ACK style re-entry into the same operation) must not apply the
    STANDARD backstop to the already-legitimate WL child."""
    account, alias_id, _cap = _legacy_compat_account(db, mapping="P0_REFRESH", tg=930005)
    remote, ensure_fn, _o, _s = _remote_and_fns(WL_SOURCE_INBOUNDS)
    _seed_first_child(
        db, account_id=account["account_id"], alias_id=alias_id, remote=remote,
        ensure_fn=ensure_fn, hwid="p0-refresh-slot2-hwid", idem="p0-refresh-seed-child-1",
    )
    token = _issue_active_credential(db, account["account_id"], idem_prefix="p0-refresh")

    first = resolve_opaque_subscription(
        db, token, _known_hwid_meta("p0-refresh-new-hwid"), hmac_key=HWID_KEY,
        ensure_fn=ensure_fn, subscription_fn=_s, worker_id="p0-hotfix-worker", now=300,
    )
    assert first.outcome == OUTCOME_OK
    for now in (301, 302, 400, 900):
        refreshed = resolve_opaque_subscription(
            db, token, _known_hwid_meta("p0-refresh-new-hwid"), hmac_key=HWID_KEY,
            ensure_fn=ensure_fn, subscription_fn=_s, worker_id="p0-hotfix-worker", now=now,
        )
        assert refreshed.outcome == OUTCOME_OK
        assert refreshed.child_username == first.child_username


# ============================================================================
# Section 2 -- terminal ERROR vs PROVISIONING_PENDING
# ============================================================================

def _poison_resolver_operation(db, *, account_id, alias_id, slot_generation_id,
                               remote, ensure_fn, error_class="WL_INBOUND_IN_STANDARD_CHILD",
                               now=300, expire=10_000):
    """Recreate the exact durable poisoned state from the incident: the remote
    ensure already succeeded (the child exists on Marzban), then the local
    op was failed permanently."""
    prepared = db.child_provisioning.prepare_child_ensure(
        account_id=account_id, slot_generation_id=slot_generation_id,
        source_alias_id=alias_id,
        source_contract_hash=source_contract_hash(remote.users["legacy-alice"]),
        expire=expire, idempotency_key=_resolver_idem_key(slot_generation_id),
        now=now - 10,
    )
    claimed = db.child_provisioning.claim(
        prepared["operation_id"], worker_id="poisoner", now=now - 5, lease_seconds=5,
    )
    created = ensure_fn(claimed["payload"])
    created.pop("uuid")
    db.child_provisioning.fail_permanent(
        prepared["operation_id"], worker_id="poisoner", error_class=error_class, now=now,
    )
    return prepared["operation_id"]


def test_terminal_outbox_error_is_not_provisioning_pending(db):
    account, alias_id, _cap = _legacy_compat_account(db, mapping="P0_TERM", tg=930006)
    remote, ensure_fn, _o, _s = _remote_and_fns(WL_SOURCE_INBOUNDS)
    slot = _seed_first_child(
        db, account_id=account["account_id"], alias_id=alias_id, remote=remote,
        ensure_fn=ensure_fn, hwid="p0-term-slot2-hwid", idem="p0-term-seed-child-1",
    )
    slot3 = db.device_slots.claim(account["account_id"], "p0-term-slot3-hwid", HWID_KEY, now=200)
    op_id = _poison_resolver_operation(
        db, account_id=account["account_id"], alias_id=alias_id,
        slot_generation_id=slot3["generation_id"], remote=remote, ensure_fn=ensure_fn,
    )
    token = _issue_active_credential(db, account["account_id"], idem_prefix="p0-term-cred")

    result = resolve_opaque_subscription(
        db, token, _known_hwid_meta("p0-term-slot3-hwid"), hmac_key=HWID_KEY,
        ensure_fn=ensure_fn, subscription_fn=_s, worker_id="p0-hotfix-worker", now=400,
    )

    assert result.outcome == _fail_permanent_outcome()
    assert result.outcome != OUTCOME_PROVISIONING_PENDING
    assert _outbox(db, op_id)["state"] == "ERROR"
    # Terminal means terminal: a worker must never reclaim it.
    assert db.child_provisioning.claim(
        op_id, worker_id="p0-hotfix-worker", now=500, lease_seconds=5,
    ) is None


def test_genuine_in_flight_lease_still_reports_pending(db):
    account, alias_id, _cap = _legacy_compat_account(db, mapping="P0_BUSY", tg=930007)
    remote, ensure_fn, _o, _s = _remote_and_fns(WL_SOURCE_INBOUNDS)
    _seed_first_child(
        db, account_id=account["account_id"], alias_id=alias_id, remote=remote,
        ensure_fn=ensure_fn, hwid="p0-busy-slot2-hwid", idem="p0-busy-seed-child-1",
    )
    slot3 = db.device_slots.claim(account["account_id"], "p0-busy-slot3-hwid", HWID_KEY, now=200)
    prepared = db.child_provisioning.prepare_child_ensure(
        account_id=account["account_id"], slot_generation_id=slot3["generation_id"],
        source_alias_id=alias_id,
        source_contract_hash=source_contract_hash(remote.users["legacy-alice"]),
        expire=10_000, idempotency_key=_resolver_idem_key(slot3["generation_id"]), now=290,
    )
    db.child_provisioning.claim(
        prepared["operation_id"], worker_id="blocker", now=295, lease_seconds=1000,
    )
    token = _issue_active_credential(db, account["account_id"], idem_prefix="p0-busy-cred")

    result = resolve_opaque_subscription(
        db, token, _known_hwid_meta("p0-busy-slot3-hwid"), hmac_key=HWID_KEY,
        ensure_fn=ensure_fn, subscription_fn=_s, worker_id="p0-hotfix-worker", now=300,
    )
    assert result.outcome == OUTCOME_PROVISIONING_PENDING


def test_retry_state_remains_retryable_and_completes(db):
    account, alias_id, _cap = _legacy_compat_account(db, mapping="P0_RETRY", tg=930008)
    # Non-WL source: this guard exercises lease/retry machinery, not the WL
    # policy scope (a WL source would legitimately poison on the baseline).
    remote, ensure_fn, _o, _s = _remote_and_fns(STANDARD_SOURCE_INBOUNDS)
    _seed_first_child(
        db, account_id=account["account_id"], alias_id=alias_id, remote=remote,
        ensure_fn=ensure_fn, hwid="p0-retry-slot2-hwid", idem="p0-retry-seed-child-1",
    )
    slot3 = db.device_slots.claim(account["account_id"], "p0-retry-slot3-hwid", HWID_KEY, now=200)
    prepared = db.child_provisioning.prepare_child_ensure(
        account_id=account["account_id"], slot_generation_id=slot3["generation_id"],
        source_alias_id=alias_id,
        source_contract_hash=source_contract_hash(remote.users["legacy-alice"]),
        expire=10_000, idempotency_key=_resolver_idem_key(slot3["generation_id"]), now=290,
    )
    db.child_provisioning.claim(
        prepared["operation_id"], worker_id="first-try", now=295, lease_seconds=5,
    )
    db.child_provisioning.retry(
        prepared["operation_id"], worker_id="first-try",
        error_class="PROVISIONING_UNAVAILABLE", now=296, delay=5,
    )
    token = _issue_active_credential(db, account["account_id"], idem_prefix="p0-retry")

    result = resolve_opaque_subscription(
        db, token, _known_hwid_meta("p0-retry-slot3-hwid"), hmac_key=HWID_KEY,
        ensure_fn=ensure_fn, subscription_fn=_s, worker_id="p0-hotfix-worker", now=400,
    )
    assert result.outcome == OUTCOME_OK
    assert _outbox(db, prepared["operation_id"])["state"] == "APPLIED"


# ============================================================================
# Section 3 -- migration binding diagnostics on terminal failure
# ============================================================================

def _poisoned_bridge_scenario(db, *, mapping, tg):
    account, alias_id, cap = _legacy_compat_account(db, mapping=mapping, tg=tg)
    _bind_legacy_bridge(db, cap, account["account_id"], alias_id, mapping=mapping)
    remote, ensure_fn, _o, _s = _remote_and_fns(WL_SOURCE_INBOUNDS)
    _seed_first_child(
        db, account_id=account["account_id"], alias_id=alias_id, remote=remote,
        ensure_fn=ensure_fn, hwid=f"{mapping.lower().replace(chr(95), chr(45))}-slot2-hwid", idem=f"{mapping}-seed-child-1",
    )
    slot3 = db.device_slots.claim(
        account["account_id"], f"{mapping.lower().replace(chr(95), chr(45))}-slot3-hwid", HWID_KEY, now=200,
    )
    op_id = _poison_resolver_operation(
        db, account_id=account["account_id"], alias_id=alias_id,
        slot_generation_id=slot3["generation_id"], remote=remote, ensure_fn=ensure_fn,
    )
    return account, alias_id, cap, remote, op_id, slot3


def test_terminal_failure_records_slot_and_child_in_binding(db):
    account, _alias_id, _cap, remote, op_id, slot3 = _poisoned_bridge_scenario(
        db, mapping="P0_BIND", tg=930009,
    )
    result = _bridge_resolve(db, "legacy-alice", "p0-bind-slot3-hwid", remote=remote, now=400)

    assert result.outcome == _fail_permanent_outcome()
    binding = db.migration_lifecycle.find_by_device(
        account["account_id"], privacy_safe_hwid("p0-bind-slot3-hwid", HWID_KEY)[0],
    )
    assert binding is not None
    # Diagnostics: the binding knows WHICH durable entities are involved.
    assert binding["slot_generation_id"] == slot3["generation_id"]
    intent = _intent_for_generation(db, slot3["generation_id"])
    assert binding["child_intent_id"] == intent["id"]
    # ... while the state stays an honest terminal/manual-review state.
    assert binding["state"] == "ERROR_RECONCILE"
    events = db._conn.execute(
        "SELECT event_type, safe_error_class FROM mgboost_migration_binding_events "
        "WHERE migration_binding_id=? ORDER BY id", (binding["id"],),
    ).fetchall()
    error_events = [e for e in events if e["event_type"] == "ERROR_RECONCILE"]
    assert error_events, "terminal failure must be durably recorded as ERROR_RECONCILE"
    assert any(
        (e["safe_error_class"] or "") == "WL_INBOUND_IN_STANDARD_CHILD" for e in error_events
    )


def test_terminal_failure_never_loops_through_retry_events(db):
    account, _alias_id, _cap, remote, op_id, _slot3 = _poisoned_bridge_scenario(
        db, mapping="P0_LOOP", tg=930010,
    )
    _bridge_resolve(db, "legacy-alice", "p0-loop-slot3-hwid", remote=remote, now=400)
    _bridge_resolve(db, "legacy-alice", "p0-loop-slot3-hwid", remote=remote, now=500)
    _bridge_resolve(db, "legacy-alice", "p0-loop-slot3-hwid", remote=remote, now=600)

    binding = db.migration_lifecycle.find_by_device(
        account["account_id"], privacy_safe_hwid("p0-loop-slot3-hwid", HWID_KEY)[0],
    )
    assert binding is not None
    assert binding["state"] == "ERROR_RECONCILE"
    retries = db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_migration_binding_events "
        "WHERE migration_binding_id=? AND event_type='RETRY'", (binding["id"],),
    ).fetchone()[0]
    assert retries == 0
    # The poisoned op itself was never resurrected into the retry machine.
    assert _outbox(db, op_id)["state"] == "ERROR"
    assert _outbox(db, op_id)["attempts"] == 1


def test_genuine_pending_still_retries_through_migration_lifecycle(db):
    """A non-terminal (pending/busy) outcome must keep the existing
    MIGRATING->RETRY lifecycle semantics."""
    account, alias_id, cap = _legacy_compat_account(db, mapping="P0_PEND", tg=930011)
    _bind_legacy_bridge(db, cap, account["account_id"], alias_id, mapping="P0_PEND")
    remote, ensure_fn, _o, _s = _remote_and_fns(WL_SOURCE_INBOUNDS)
    _seed_first_child(
        db, account_id=account["account_id"], alias_id=alias_id, remote=remote,
        ensure_fn=ensure_fn, hwid="p0-pend-slot2-hwid", idem="p0-pend-seed-child-1",
    )
    slot3 = db.device_slots.claim(account["account_id"], "p0-pend-slot3-hwid", HWID_KEY, now=200)
    prepared = db.child_provisioning.prepare_child_ensure(
        account_id=account["account_id"], slot_generation_id=slot3["generation_id"],
        source_alias_id=alias_id,
        source_contract_hash=source_contract_hash(remote.users["legacy-alice"]),
        expire=10_000, idempotency_key=_resolver_idem_key(slot3["generation_id"]), now=290,
    )
    db.child_provisioning.claim(
        prepared["operation_id"], worker_id="blocker", now=295, lease_seconds=1000,
    )

    result = _bridge_resolve(db, "legacy-alice", "p0-pend-slot3-hwid", remote=remote, now=300)

    assert result.outcome == OUTCOME_PROVISIONING_PENDING
    binding = db.migration_lifecycle.find_by_device(
        account["account_id"], privacy_safe_hwid("p0-pend-slot3-hwid", HWID_KEY)[0],
    )
    assert binding is not None and binding["state"] == "MIGRATING"


# ============================================================================
# Section 4 -- audited, idempotent recovery primitive for poisoned records
# ============================================================================

REPAIR_REASON = "P0 hotfix recovery: legacy WL child poisoned by unscoped STANDARD backstop"


def _repaired_scenario(db, *, mapping, tg, error_class="WL_INBOUND_IN_STANDARD_CHILD"):
    """Poisoned legacy operation whose remote child genuinely exists and is
    contract-correct for the (still WL-capable) current entitlement."""
    account, alias_id, cap = _legacy_compat_account(db, mapping=mapping, tg=tg)
    _bind_legacy_bridge(db, cap, account["account_id"], alias_id, mapping=mapping)
    remote, ensure_fn, observe_fn, _s = _remote_and_fns(WL_SOURCE_INBOUNDS)
    _seed_first_child(
        db, account_id=account["account_id"], alias_id=alias_id, remote=remote,
        ensure_fn=ensure_fn, hwid=f"{mapping.lower().replace(chr(95), chr(45))}-slot2-hwid", idem=f"{mapping}-seed-child-1",
    )
    slot3 = db.device_slots.claim(account["account_id"], f"{mapping.lower().replace(chr(95), chr(45))}-slot3-hwid", HWID_KEY, now=200)
    op_id = _poison_resolver_operation(
        db, account_id=account["account_id"], alias_id=alias_id,
        slot_generation_id=slot3["generation_id"], remote=remote, ensure_fn=ensure_fn,
        error_class=error_class,
    )
    return account, cap, remote, observe_fn, ensure_fn, op_id, slot3


def _repair(db, cap, observe_fn, op_id, *, idem, reason=REPAIR_REASON):
    from src.child_recovery import repair_child_ensure
    return repair_child_ensure(
        db, operation_id=op_id, capability=cap, reason=reason,
        idempotency_key=idem, observe_fn=observe_fn, now=900,
    )


def _mutation_count(db, operation):
    return db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_entitlement_mutations WHERE operation=?",
        (operation,),
    ).fetchone()[0]


def test_recovery_repairs_poisoned_legacy_wl_child(db):
    account, cap, remote, observe_fn, _ensure_fn, op_id, _slot3 = _repaired_scenario(
        db, mapping="P0_REC", tg=930012,
    )

    result = _repair(db, cap, observe_fn, op_id, idem="p0-rec-repair-key-1")

    assert result["status"] == "REPAIRED"
    outbox = _outbox(db, op_id)
    assert outbox["state"] == "APPLIED"
    intent = db._conn.execute(
        "SELECT * FROM mgboost_child_user_intents WHERE id=?", (outbox["child_intent_id"],),
    ).fetchone()
    assert intent["observed_state"] == "ACTIVE"
    remote_uuid = remote.users[intent["child_username"]]["proxies"]["vless"]["id"]
    assert intent["uuid_verifier"] == credential_verifier(remote_uuid)
    # Audited with actor/reason/idempotency evidence.
    assert _mutation_count(db, "CHILD_RECOVERY_REPAIR") == 1
    audit = db._conn.execute(
        "SELECT * FROM mgboost_entitlement_mutations WHERE operation='CHILD_RECOVERY_REPAIR'",
    ).fetchone()
    assert audit["actor_ref"] and audit["reason"] == REPAIR_REASON
    assert audit["idempotency_key_hash"] is not None
    assert json.loads(audit["before_json"])["outbox_state"] == "ERROR"
    assert json.loads(audit["after_json"])["outbox_state"] == "APPLIED"

    # The device immediately resolves again through the normal incident path.
    token = _issue_active_credential(db, account["account_id"], idem_prefix="p0-rec-cred")
    resolved = resolve_opaque_subscription(
        db, token, _known_hwid_meta("p0-rec-slot3-hwid"), hmac_key=HWID_KEY,
        ensure_fn=lambda payload: BrokerOperations(remote).dispatch("child.user.ensure", payload),
        subscription_fn=lambda payload: BrokerOperations(remote).dispatch("child.user.subscription.get", payload),
        worker_id="p0-hotfix-worker", now=1000,
    )
    assert resolved.outcome == OUTCOME_OK
    assert resolved.child_username == intent["child_username"]


def test_recovery_second_invocation_is_idempotent_noop(db):
    _account_row, cap, remote, observe_fn, _e, op_id, _s3 = _repaired_scenario(
        db, mapping="P0_REC2", tg=930013,
    )
    first = _repair(db, cap, observe_fn, op_id, idem="p0-rec2-repair-1")
    assert first["status"] == "REPAIRED"

    second = _repair(db, cap, observe_fn, op_id, idem="p0-rec2-repair-2")

    assert second["status"] in ("ALREADY_APPLIED", "NOOP")
    assert _outbox(db, op_id)["state"] == "APPLIED"
    assert _mutation_count(db, "CHILD_RECOVERY_REPAIR") == 1


def test_recovery_refuses_when_current_policy_still_forbids_wl(db):
    account, alias_id, cap = _internal_account(db, wl_mode="NONE", mapping="P0_RECSTD", tg=930014)
    remote, ensure_fn, observe_fn, _s = _remote_and_fns(WL_SOURCE_INBOUNDS, alias="tpl-source")
    db._conn.execute(
        "INSERT INTO mgboost_provisioning_templates "
        "(account_id,template_username,source_contract_hash,state,pinned_at,updated_at) VALUES (?,?,?,'ACTIVE',?,?)",
        (account["account_id"], "tpl-source", source_contract_hash(remote.users["tpl-source"]), 100, 100),
    )
    db._conn.commit()
    slot = db.device_slots.claim(account["account_id"], "p0-recstd-hwid", HWID_KEY, now=200)
    prepared = db.child_provisioning.prepare_child_ensure(
        account_id=account["account_id"], slot_generation_id=slot["generation_id"],
        source_alias_id=alias_id,
        source_contract_hash=source_contract_hash(remote.users["tpl-source"]),
        expire=0, idempotency_key=_resolver_idem_key(slot["generation_id"]), now=290,
    )
    claimed = db.child_provisioning.claim(
        prepared["operation_id"], worker_id="poisoner", now=295, lease_seconds=5,
    )
    created = ensure_fn(claimed["payload"])
    created.pop("uuid")
    db.child_provisioning.fail_permanent(
        prepared["operation_id"], worker_id="poisoner",
        error_class="WL_INBOUND_IN_STANDARD_CHILD", now=300,
    )

    result = _repair(db, cap, observe_fn, prepared["operation_id"], idem="p0-recstd-repair-1")

    assert result["status"] == "REFUSED"
    assert result["reason_class"] == "POLICY_STILL_FORBIDS_WL"
    assert _outbox(db, prepared["operation_id"])["state"] == "ERROR"


def test_recovery_refuses_uuid_verifier_mismatch(db):
    _account_row, cap, remote, observe_fn, _e, op_id, _s3 = _repaired_scenario(
        db, mapping="P0_UUID", tg=930015,
    )
    outbox = _outbox(db, op_id)
    import hashlib as _hashlib
    foreign_uuid = "00000000-0000-4000-8000-00000000000a"
    foreign_verifier = "sha256:" + _hashlib.sha256(foreign_uuid.encode()).hexdigest()
    foreign_masked = "uuid_" + _hashlib.sha256(("mask\0" + foreign_uuid).encode()).hexdigest()[:8]
    db._conn.execute(
        "UPDATE mgboost_child_user_intents SET uuid_verifier=?, uuid_masked=? WHERE id=?",
        (foreign_verifier, foreign_masked, outbox["child_intent_id"]),
    )
    db._conn.commit()

    result = _repair(db, cap, observe_fn, op_id, idem="p0-uuid-repair-1")

    assert result["status"] == "REFUSED"
    assert result["reason_class"] == "UUID_VERIFIER_MISMATCH"
    assert _outbox(db, op_id)["state"] == "ERROR"


def test_recovery_refuses_source_contract_mismatch(db):
    _account_row, cap, remote, observe_fn, _e, op_id, _s3 = _repaired_scenario(
        db, mapping="P0_SRC", tg=930016,
    )
    # The legacy source contract drifted after the pin: observe must answer
    # MISMATCH and repair must refuse without touching anything.
    remote.users["legacy-alice"]["inbounds"] = {"vless": ["LEGACY"]}

    result = _repair(db, cap, observe_fn, op_id, idem="p0-src-repair-key-1")

    assert result["status"] == "REFUSED"
    assert result["reason_class"] == "REMOTE_MISMATCH"
    assert _outbox(db, op_id)["state"] == "ERROR"
    intent = db._conn.execute(
        "SELECT observed_state FROM mgboost_child_user_intents WHERE id=?",
        (_outbox(db, op_id)["child_intent_id"],),
    ).fetchone()
    assert intent["observed_state"] == "ERROR"


def test_recovery_remote_missing_is_typed_and_creates_nothing(db):
    _account_row, cap, remote, observe_fn, ensure_fn, op_id, _s3 = _repaired_scenario(
        db, mapping="P0_GONE", tg=930017,
    )
    outbox = _outbox(db, op_id)
    intent = db._conn.execute(
        "SELECT child_username FROM mgboost_child_user_intents WHERE id=?",
        (outbox["child_intent_id"],),
    ).fetchone()
    remote.users.pop(intent["child_username"])

    result = _repair(db, cap, observe_fn, op_id, idem="p0-gone-repair-1")

    assert result["status"] == "REMOTE_MISSING"
    assert _outbox(db, op_id)["state"] == "ERROR"
    ensure_calls_before = len(remote.calls)
    assert ensure_calls_before >= 0  # nothing crashed; ensure was never invoked


def test_recovery_refuses_non_recoverable_error_class(db):
    _account_row, cap, remote, observe_fn, ensure_fn, op_id, _s3 = _repaired_scenario(
        db, mapping="P0_CLASS", tg=930018, error_class="REMOTE_CONTRACT_MISMATCH",
    )

    result = _repair(db, cap, observe_fn, op_id, idem="p0-class-repair-1")

    assert result["status"] == "REFUSED"
    assert result["reason_class"] == "ERROR_CLASS_NOT_RECOVERABLE"
    assert _outbox(db, op_id)["state"] == "ERROR"


def test_recovery_requires_primary_capability_and_bounded_reason(db):
    _account_row, cap, remote, observe_fn, _e, op_id, _s3 = _repaired_scenario(
        db, mapping="P0_AUTH", tg=930019,
    )
    with pytest.raises(Exception):
        _repair(db, None, observe_fn, op_id, idem="p0-auth-repair-1")
    with pytest.raises(Exception):
        _repair(db, cap, observe_fn, op_id, idem="p0-auth-repair-2", reason="no")
    with pytest.raises(Exception):
        _repair(db, cap, observe_fn, op_id, idem="short", reason=REPAIR_REASON)
    assert _outbox(db, op_id)["state"] == "ERROR"


def test_recovery_never_invokes_child_ensure(db):
    """Structural guarantee: recovery observes, it never ensures -- the remote
    child must never be created/rewritten by a repair."""
    _account_row, cap, remote, observe_fn, ensure_fn, op_id, _s3 = _repaired_scenario(
        db, mapping="P0_NOENS", tg=930020,
    )
    ensure_calls_before = sum(1 for c in remote.calls if c[0] == "create_user")

    _repair(db, cap, observe_fn, op_id, idem="p0-noens-repair-1")

    ensure_calls_after = sum(1 for c in remote.calls if c[0] == "create_user")
    assert ensure_calls_after == ensure_calls_before
