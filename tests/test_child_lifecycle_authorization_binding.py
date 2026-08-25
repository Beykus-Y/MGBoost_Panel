"""PH3-05 pre-canary security preflight: proves the server-bound lifecycle
authorization property before any production destructive gate runs.

Architecture note (verdict): the typed `child.user.revoke` broker operation
itself is stateless -- it has no MGBoost SQLite access at all (the broker
process deliberately never reads the panel's database or its .env, per the
PH1-05 isolation boundary; `mgboost-child-worker.service` and the panel both
authenticate to it as the same `mgboost-main` HMAC identity). Its own
request validation only proves internal self-consistency (`operation_id`
must be exactly `derive_lifecycle_operation_id(child_username, "REVOKE")`)
plus a live remote-state check (`uuid_verifier` must match the *current*
remote credential). This mirrors `child.user.ensure`/`observe` exactly --
none of the typed operations re-verify against the MGBoost DB from inside
the broker, because the broker is intentionally DB-less.

The actual "operation_id -> durable lifecycle row -> expected
account/slot/generation/child" binding lives one layer up, in
`src/child_lifecycle.py`: `process_revoke`/`process_rebind` never construct
a revoke payload from free-form input -- they always call
`ChildLifecycleStore.claim()` first, which only succeeds for an
operation_id that already exists as a real, non-terminal row in
`mgboost_child_lifecycle_operations`, and the payload dispatched to the
broker is then read verbatim from that DB row (or, for rebind's inner
revoke step, from the `old_child_intent_id` the *same* claimed row points
to). There is no code path in this module that lets a caller pick an
arbitrary child to revoke; the caller only ever supplies `operation_id`,
and everything else is resolved server-side from the DB.

Verdict: PASS -- consistent with, not weaker than, the existing PH3-03
trust model. No broker/orchestration code change was required.
"""

import pytest

from src.broker_operations import BrokerOperations
from src.child_contract import (
    credential_verifier,
    derive_lifecycle_operation_id,
    source_contract_hash,
)
from src import child_lifecycle
from src.child_lifecycle import ChildLifecycleError

from tests.test_child_provisioning import _account
from tests.test_child_lifecycle import _build_applied_child, _revoke_fn, db  # noqa: F401
from tests.test_marzban_broker import FakeMarzban


def test_claim_refuses_an_operation_id_that_was_never_prepared(db):
    """The broker payload for a revoke is only ever built from a claimed DB
    row -- an operation_id that was never prepared cannot be claimed at all,
    so `process_revoke` can never dispatch a payload for it."""
    fx = _build_applied_child(db)
    forged_operation_id = derive_lifecycle_operation_id(fx["child_username"], "REVOKE")
    # No prepare_revoke() call happened -- the row does not exist.
    assert db.child_lifecycle.claim(forged_operation_id, worker_id="attacker-worker", now=300) is None


def test_operation_from_a_different_account_cannot_be_claimed_for_this_child(db):
    fx_a = _build_applied_child(db, mapping="AUTH_A", tg=800001, alias="alice")
    fx_b = _build_applied_child(db, mapping="AUTH_B", tg=800002, alias="second-source")
    # Account B can never even prepare an operation against account A's
    # child intent -- the prepare-time account_id/child_intent ownership
    # check rejects it outright.
    with pytest.raises(ChildLifecycleError, match="does not belong"):
        db.child_lifecycle.prepare_revoke(
            account_id=fx_b["account"]["account_id"],
            old_child_intent_id=fx_a["child_intent_id"],
            reason="cross account probe", idempotency_key="auth-cross-account", now=300,
        )


def test_terminal_applied_operation_cannot_be_reclaimed_or_reused(db):
    fx = _build_applied_child(db)
    prepared = db.child_lifecycle.prepare_revoke(
        account_id=fx["account"]["account_id"], old_child_intent_id=fx["child_intent_id"],
        reason="terminal reuse probe", idempotency_key="auth-terminal-reuse", now=300,
    )
    child_lifecycle.process_revoke(
        db, prepared["operation_id"], worker_id="worker-one", revoke_fn=_revoke_fn(fx["remote"]), now=301,
    )
    # The operation is now APPLIED (terminal). No worker can claim it again,
    # regardless of how it is addressed.
    assert db.child_lifecycle.claim(prepared["operation_id"], worker_id="worker-two", now=999) is None


def test_stale_superseded_generation_child_revoke_is_safe_and_touches_nothing_new(db):
    """After a rebind, the old (now-RELEASED, superseded) generation's child
    intent was already disabled as part of that same durable operation.
    Rebind itself does not write a REVOKE-kind lifecycle row for that intent
    (its inline revoke is part of the REBIND row), so a later, independent
    prepare_revoke() against that same stale child intent is technically
    preparable -- but processing it must converge safely to the broker's own
    idempotent ALREADY_REVOKED path and perform zero further remote mutation
    or state change, never touching the live new generation's child."""
    fx = _build_applied_child(db)
    rebind_prepared = db.child_lifecycle.prepare_rebind(
        account_id=fx["account"]["account_id"], old_child_intent_id=fx["child_intent_id"],
        reason="create a stale generation", idempotency_key="auth-stale-gen-setup", now=300,
    )
    rebind_result = child_lifecycle.process_rebind(
        db, rebind_prepared["operation_id"], worker_id="worker-one",
        revoke_fn=_revoke_fn(fx["remote"]), new_raw_hwid="auth-stale-gen-new-hwid",
        hmac_key="lifecycle-auth-test-hwid-key-32-bytes!!", now=301,
    )
    new_intent_before = dict(db._conn.execute(
        "SELECT observed_state,uuid_verifier FROM mgboost_child_user_intents WHERE id=?",
        (rebind_result["new_child_intent_id"],),
    ).fetchone())

    stale_revoke = db.child_lifecycle.prepare_revoke(
        account_id=fx["account"]["account_id"], old_child_intent_id=fx["child_intent_id"],
        reason="stale generation probe", idempotency_key="auth-stale-gen-probe", now=310,
    )
    result = child_lifecycle.process_revoke(
        db, stale_revoke["operation_id"], worker_id="worker-two",
        revoke_fn=_revoke_fn(fx["remote"]), now=311,
    )
    assert result["state"] == "APPLIED"
    # The live, new generation's child intent must be completely untouched.
    new_intent_after = dict(db._conn.execute(
        "SELECT observed_state,uuid_verifier FROM mgboost_child_user_intents WHERE id=?",
        (rebind_result["new_child_intent_id"],),
    ).fetchone())
    assert new_intent_after == new_intent_before


def test_broker_still_rejects_a_verifier_that_does_not_match_live_remote_state():
    remote = FakeMarzban()
    child_username = "mgc_" + "a" * 26
    remote.users[child_username] = {
        "username": child_username, "expire": 0, "status": "active",
        "proxies": {"vless": {"id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", "flow": ""}},
        "inbounds": {"vless": ["LEGACY"]}, "data_limit": None,
    }
    payload = {
        "operation_id": derive_lifecycle_operation_id(child_username, "REVOKE"),
        "child_username": child_username,
        "uuid_verifier": credential_verifier("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),  # wrong
    }
    with pytest.raises(ValueError, match="verifier mismatch"):
        BrokerOperations(remote).dispatch("child.user.revoke", payload)


def test_derived_operation_ids_are_distinct_per_child_forever():
    a = derive_lifecycle_operation_id("mgc_" + "a" * 26, "REVOKE")
    b = derive_lifecycle_operation_id("mgc_" + "b" * 26, "REVOKE")
    assert a != b
