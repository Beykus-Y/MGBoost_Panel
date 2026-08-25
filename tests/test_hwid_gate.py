"""PH3-04 deterministic HWID fail-closed compatibility gate.

`src/hwid_gate.py` is dormant (no legacy route imports it) and reuses the
existing PH3-02 `DeviceSlotStore.claim` primitive verbatim -- these tests
prove the new compatibility/HWID-presence layer in front of it is
fail-closed, idempotent, and introduces no new provisioning path, no
ownership coupling and no caller-controlled slot/generation/account
override.
"""

import threading

import pytest

from src import hwid_gate
from src.device_slots import DeviceSlotStore

from tests.test_device_slots import HWID_KEY, _account_with_plan, db


SUPPORTED_CLIENT = {"client_name": "happ", "client_version": "3.26.3", "platform": "android"}
UNSUPPORTED_CLIENT = {"client_name": "unheard-of-client", "client_version": "1.0", "platform": "android"}
KNOWN_MISSING_CLIENT = {"client_name": "streisand", "client_version": "48", "platform": "darwin"}


def _evaluate(db, account_id, raw_hwid, *, client=None, present=True, supported=True, now=100):
    client = client or SUPPORTED_CLIENT
    return hwid_gate.evaluate(
        slots=db.device_slots, account_id=account_id,
        hwid_candidate_present=present, hwid_candidate_supported=supported,
        raw_hwid=raw_hwid, hmac_key=HWID_KEY, now=now, **client,
    )


# --- compatibility ----------------------------------------------------------

def test_exact_supported_client_version_platform_allows_evaluation(db):
    account, _sub = _account_with_plan(db, limit=3)
    decision = _evaluate(db, account["id"], "device-one")
    assert decision.decision == hwid_gate.DECISION_ASSIGN_FREE_SLOT
    assert decision.allowed


def test_same_client_unknown_version_denied(db):
    account, _sub = _account_with_plan(db, limit=3)
    decision = _evaluate(
        db, account["id"], "device-one",
        client={"client_name": "happ", "client_version": "0.0.1", "platform": "android"},
    )
    assert decision.decision == hwid_gate.DECISION_DENY_UNSUPPORTED_CLIENT
    assert not decision.allowed


def test_supported_client_wrong_platform_denied(db):
    account, _sub = _account_with_plan(db, limit=3)
    decision = _evaluate(
        db, account["id"], "device-one",
        client={"client_name": "happ", "client_version": "3.26.3", "platform": "ios"},
    )
    assert decision.decision == hwid_gate.DECISION_DENY_UNSUPPORTED_CLIENT


def test_unknown_client_denied(db):
    account, _sub = _account_with_plan(db, limit=3)
    decision = _evaluate(db, account["id"], "device-one", client=UNSUPPORTED_CLIENT)
    assert decision.decision == hwid_gate.DECISION_DENY_UNSUPPORTED_CLIENT


def test_known_missing_hwid_family_denied_as_unsupported(db):
    account, _sub = _account_with_plan(db, limit=3)
    decision = _evaluate(db, account["id"], None, client=KNOWN_MISSING_CLIENT, present=False)
    assert decision.decision == hwid_gate.DECISION_DENY_UNSUPPORTED_CLIENT


def test_spoofed_header_claiming_a_supported_client_name_is_still_exact_match(db):
    """A caller can put any string in a header, but the registry match stays
    an exact (client, version, platform) lookup -- spoofing the label alone
    without a registry-listed tuple never becomes SUPPORTED."""
    account, _sub = _account_with_plan(db, limit=3)
    decision = _evaluate(
        db, account["id"], "device-one",
        client={"client_name": "happ-but-fake", "client_version": "3.26.3", "platform": "android"},
    )
    assert decision.decision == hwid_gate.DECISION_DENY_UNSUPPORTED_CLIENT


# --- missing / malformed HWID -----------------------------------------------

def test_missing_hwid_on_supported_client_denied(db):
    account, _sub = _account_with_plan(db, limit=3)
    decision = _evaluate(db, account["id"], None, present=False, supported=False)
    assert decision.decision == hwid_gate.DECISION_DENY_MISSING_HWID


def test_malformed_hwid_on_supported_client_denied(db):
    account, _sub = _account_with_plan(db, limit=3)
    decision = _evaluate(db, account["id"], "!!!", present=True, supported=False)
    assert decision.decision == hwid_gate.DECISION_DENY_MALFORMED_HWID


# --- slot resolution ---------------------------------------------------------

def test_known_hwid_resolves_to_same_slot_and_generation(db):
    account, _sub = _account_with_plan(db, limit=3)
    first = _evaluate(db, account["id"], "device-one")
    second = _evaluate(db, account["id"], "device-one")
    assert first.decision == hwid_gate.DECISION_ASSIGN_FREE_SLOT
    assert second.decision == hwid_gate.DECISION_KNOWN_SLOT
    assert (second.slot_number, second.generation) == (first.slot_number, first.generation)


def test_repeated_known_request_is_idempotent_no_new_generation(db):
    account, _sub = _account_with_plan(db, limit=3)
    _evaluate(db, account["id"], "device-one")
    before = db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_device_slot_generations WHERE account_id=?",
        (account["id"],),
    ).fetchone()[0]
    for _ in range(3):
        _evaluate(db, account["id"], "device-one")
    after = db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_device_slot_generations WHERE account_id=?",
        (account["id"],),
    ).fetchone()[0]
    assert before == after == 1


def test_unknown_hwid_with_free_slot_assigns_one_slot(db):
    account, _sub = _account_with_plan(db, limit=3)
    decision = _evaluate(db, account["id"], "device-new")
    assert decision.decision == hwid_gate.DECISION_ASSIGN_FREE_SLOT
    count = db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_device_slot_generations WHERE account_id=? AND status='ACTIVE'",
        (account["id"],),
    ).fetchone()[0]
    assert count == 1


@pytest.mark.parametrize("limit", [3, 6, 12])
def test_exact_paid_baseline_limits(db, limit):
    account, _sub = _account_with_plan(db, limit=limit)
    for i in range(limit):
        decision = _evaluate(db, account["id"], f"device-{i}")
        assert decision.decision == hwid_gate.DECISION_ASSIGN_FREE_SLOT
    full = _evaluate(db, account["id"], "device-overflow")
    assert full.decision == hwid_gate.DECISION_DENY_SLOT_LIMIT


def test_internal_unlimited_uses_technical_cap(db):
    account, _sub = _account_with_plan(
        db, source="INTERNAL", plan_kind="INTERNAL", limit=None, limit_mode="UNLIMITED",
    )
    for i in range(20):
        decision = _evaluate(db, account["id"], f"device-{i}")
        assert decision.decision == hwid_gate.DECISION_ASSIGN_FREE_SLOT


def test_full_capacity_denies_without_evicting_existing_device(db):
    account, _sub = _account_with_plan(db, limit=3)
    for i in range(3):
        _evaluate(db, account["id"], f"device-{i}")
    before = db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_device_slot_generations WHERE account_id=? AND status='ACTIVE'",
        (account["id"],),
    ).fetchone()[0]
    denied = _evaluate(db, account["id"], "device-new")
    after = db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_device_slot_generations WHERE account_id=? AND status='ACTIVE'",
        (account["id"],),
    ).fetchone()[0]
    assert denied.decision == hwid_gate.DECISION_DENY_SLOT_LIMIT
    assert before == after == 3
    # The original three devices must still resolve to their own slots.
    first_again = _evaluate(db, account["id"], "device-0")
    assert first_again.decision == hwid_gate.DECISION_KNOWN_SLOT


# --- concurrency --------------------------------------------------------------

def test_concurrent_same_hwid_converges_to_one_generation(db):
    account, _sub = _account_with_plan(db, limit=3)
    results = []
    barrier = threading.Barrier(2)

    def worker():
        barrier.wait(5)
        results.append(_evaluate(db, account["id"], "device-shared"))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    count = db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_device_slot_generations WHERE account_id=? AND status='ACTIVE'",
        (account["id"],),
    ).fetchone()[0]
    assert count == 1
    assert all(r.allowed for r in results)


def test_concurrent_different_hwid_near_capacity_never_exceeds_limit(db):
    account, _sub = _account_with_plan(db, limit=3)
    _evaluate(db, account["id"], "device-0")
    _evaluate(db, account["id"], "device-1")
    results = []
    barrier = threading.Barrier(3)

    def worker(name):
        barrier.wait(5)
        results.append(_evaluate(db, account["id"], name))

    threads = [threading.Thread(target=worker, args=(f"device-race-{i}",)) for i in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    active = db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_device_slot_generations WHERE account_id=? AND status='ACTIVE'",
        (account["id"],),
    ).fetchone()[0]
    assert active == 3
    assert sum(1 for r in results if r.allowed) == 1
    assert sum(1 for r in results if r.decision == hwid_gate.DECISION_DENY_SLOT_LIMIT) == 2


# --- security: cross-account / copied HWID / caller-supplied slot -----------

def test_cross_account_hwid_is_denied_not_a_takeover(db):
    account_a, _ = _account_with_plan(db, limit=3, code="A")
    account_b, _ = _account_with_plan(db, limit=3, code="B")
    first = _evaluate(db, account_a["id"], "shared-copied-hwid")
    assert first.decision == hwid_gate.DECISION_ASSIGN_FREE_SLOT
    second = _evaluate(db, account_b["id"], "shared-copied-hwid")
    assert second.decision == hwid_gate.DECISION_DENY_CROSS_ACCOUNT_HWID
    # Account A's slot/generation must be completely unaffected.
    a_count = db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_device_slot_generations WHERE account_id=? AND status='ACTIVE'",
        (account_a["id"],),
    ).fetchone()[0]
    b_count = db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_device_slot_generations WHERE account_id=? AND status='ACTIVE'",
        (account_b["id"],),
    ).fetchone()[0]
    assert (a_count, b_count) == (1, 0)


def test_copied_hwid_within_same_account_resolves_to_same_slot(db):
    """Documented practical-identity limitation: within one account, a
    copied HWID value is indistinguishable from the original device. This
    is not claimed to be cryptographic uniqueness."""
    account, _sub = _account_with_plan(db, limit=3)
    original = _evaluate(db, account["id"], "device-original")
    copied = _evaluate(db, account["id"], "device-original")
    assert copied.decision == hwid_gate.DECISION_KNOWN_SLOT
    assert (copied.slot_number, copied.generation) == (original.slot_number, original.generation)


def test_evaluate_accepts_no_caller_supplied_slot_or_generation():
    import inspect

    params = set(inspect.signature(hwid_gate.evaluate).parameters)
    assert "slot_id" not in params
    assert "generation" not in params
    assert "child_username" not in params
    assert "child_uuid" not in params
    assert "telegram_id" not in params


def test_stale_generation_cannot_be_reactivated_via_gate(db):
    account, _sub = _account_with_plan(db, limit=3)
    claimed = _evaluate(db, account["id"], "device-one")
    released_slot_id = _slot_id(db, account["id"], claimed.slot_number)
    db.device_slots.release(
        account["id"], released_slot_id, claimed.generation,
        reason="test release", now=101,
    )
    # The released generation must stay RELEASED forever -- never reactivated.
    released_state = db._conn.execute(
        "SELECT status FROM mgboost_device_slot_generations "
        "WHERE slot_id=? AND generation=?", (released_slot_id, claimed.generation),
    ).fetchone()[0]
    assert released_state == "RELEASED"
    # A retry of the same old HWID is treated as a brand-new device candidate
    # (unknown to any ACTIVE generation) and gets a fresh assignment -- it can
    # never resolve back into the stale, released generation.
    stale_retry = _evaluate(db, account["id"], "device-one")
    assert stale_retry.decision == hwid_gate.DECISION_ASSIGN_FREE_SLOT
    assert stale_retry.slot_result == "CLAIMED"
    released_state_after = db._conn.execute(
        "SELECT status FROM mgboost_device_slot_generations "
        "WHERE slot_id=? AND generation=?", (released_slot_id, claimed.generation),
    ).fetchone()[0]
    assert released_state_after == "RELEASED"


def _slot_id(db, account_id, slot_number):
    return db._conn.execute(
        "SELECT id FROM mgboost_device_slots WHERE account_id=? AND slot_number=?",
        (account_id, slot_number),
    ).fetchone()[0]


# --- reinstall ----------------------------------------------------------------

def test_reinstall_new_hwid_with_free_slot_gets_new_slot(db):
    account, _sub = _account_with_plan(db, limit=3)
    old = _evaluate(db, account["id"], "device-old-install")
    new = _evaluate(db, account["id"], "device-new-install-after-reinstall")
    assert new.decision == hwid_gate.DECISION_ASSIGN_FREE_SLOT
    assert new.slot_number != old.slot_number
    # Old slot/device is not touched.
    old_state = db._conn.execute(
        "SELECT status FROM mgboost_device_slot_generations WHERE account_id=? AND generation=? AND slot_number=?",
        (account["id"], old.generation, old.slot_number),
    ).fetchone()[0]
    assert old_state == "ACTIVE"


def test_reinstall_new_hwid_with_full_slots_gets_clear_refusal(db):
    account, _sub = _account_with_plan(
        db, source="INTERNAL", plan_kind="INTERNAL", limit=1, limit_mode="LIMITED",
    )
    _evaluate(db, account["id"], "device-old-install")
    denied = _evaluate(db, account["id"], "device-new-install-after-reinstall")
    assert denied.decision == hwid_gate.DECISION_DENY_SLOT_LIMIT
    # No automatic device replacement: old slot still active for the old HWID.
    still_known = _evaluate(db, account["id"], "device-old-install")
    assert still_known.decision == hwid_gate.DECISION_KNOWN_SLOT


# --- ownership boundary -------------------------------------------------------

def test_gate_never_touches_telegram_identity_tables(db):
    account, _sub = _account_with_plan(db, limit=3)
    before = list(db._conn.execute("SELECT * FROM mgboost_telegram_identities"))
    for i in range(3):
        _evaluate(db, account["id"], f"device-{i}")
    _evaluate(db, account["id"], "device-overflow")  # denied path too
    after = list(db._conn.execute("SELECT * FROM mgboost_telegram_identities"))
    assert before == after == []


def test_gate_never_creates_or_modifies_accounts(db):
    account, _sub = _account_with_plan(db, limit=3)
    before = db._conn.execute("SELECT COUNT(*) FROM mgboost_accounts").fetchone()[0]
    _evaluate(db, account["id"], "device-one")
    after = db._conn.execute("SELECT COUNT(*) FROM mgboost_accounts").fetchone()[0]
    assert before == after == 1


def test_gate_never_touches_child_intent_or_outbox_tables(db):
    account, _sub = _account_with_plan(db, limit=3)
    _evaluate(db, account["id"], "device-one")
    intents = db._conn.execute("SELECT COUNT(*) FROM mgboost_child_user_intents").fetchone()[0]
    outbox = db._conn.execute("SELECT COUNT(*) FROM mgboost_outbox").fetchone()[0]
    assert (intents, outbox) == (0, 0)


# --- privacy -------------------------------------------------------------------

def test_no_raw_hwid_in_db_after_gate_evaluations(db):
    account, _sub = _account_with_plan(db, limit=3)
    raw = "very-secret-raw-hwid-value-should-never-be-stored"
    _evaluate(db, account["id"], raw)
    dump = "\n".join(db._conn.iterdump())
    assert raw not in dump


def test_decision_object_carries_no_raw_hwid_or_account_secret(db):
    account, _sub = _account_with_plan(db, limit=3)
    raw = "another-secret-raw-hwid-value"
    decision = _evaluate(db, account["id"], raw)
    rendered = repr(decision)
    assert raw not in rendered
