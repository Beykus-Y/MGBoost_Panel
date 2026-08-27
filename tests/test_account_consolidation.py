"""PH7-13 account consolidation (merge/supersession) -- DL-057.

Covers: schema/checksum/triggers, resolver coverage (legacy bridge +
Telegram grace-registration bind), no-physical-alias-copy, display_name
fallback, merge apply replay, reversal, cycle/self/conflicting merge
rejection, close preconditions, genesis Revoke->Free, CLOSED account
cannot retain an ACTIVE generation, the exact D3->D6 legacy-compat
transition (and its refusals), concurrent/stale-CAS safety, survivor
identity/credential/subscription stability, immutable history staying
attributed to the original account, and admin read-model canonicalization
-- using the exact Megochel scenario (two independent legacy accounts, one
absorbed, one survivor) as the running example throughout.
"""

from __future__ import annotations

import importlib
import os
import tempfile
import threading

import pytest

from src.account_consolidation import (
    AbsorbedNotClosed,
    AccountConsolidationError,
    AccountNotFound,
    ActiveGenerationExists,
    ClosedAccountError,
    MergeChainConflict,
    MergeConflict,
    MergeNotFound,
    NonTerminalChildExists,
    SelfMergeError,
    SurvivorNotActive,
    TelegramOwnerStillActive,
    close_account,
    create_merge,
    get_display_name,
    resolve_account_id,
    reverse_merge,
    set_display_name,
    reopen_account,
)
from src.account_consolidation_schema import (
    MIGRATION_ID,
    SCHEMA_CHECKSUM,
    apply_account_consolidation_schema,
)
from src import child_lifecycle
from src.broker_operations import BrokerOperations
from src.child_contract import source_contract_hash
from src.admin_read_models import account_detail, account_summaries
from src.legacy_grace_registration import bind_telegram_after_registration
from src.legacy_paid_compat import (
    DeviceLimitDecreaseRefused,
    NotLegacyCompatPlan,
    ensure_legacy_paid_compat_entitlement,
    increase_device_limit,
)
from src.security import AdminSessionStore

from tests.test_child_provisioning import HWID_KEY, PRIMARY, PRIMARY_LOGIN
from tests.test_marzban_broker import FakeMarzban


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
    _raw, session = AdminSessionStore().create(PRIMARY_LOGIN, "consolidation-test-jwt")
    return db.primary_admin_authority.authorize_session(session)


def _direct_account(
    db, capability, *, username, tg=None, ownership_evidence="ABSENT",
    decision_ref="dl-057-test",
):
    account = db.direct_enrollment.enroll_direct_account(
        capability=capability, legacy_username=username, decision_ref=decision_ref,
        ownership_evidence=ownership_evidence, telegram_id=tg,
        alias_provenance="EVIDENCE_PROVEN", legacy_status="ACTIVE", legacy_expiry=None,
        observed_device_count=0, observed_hwid_count=0, evidence={"source": "test"},
        idempotency_key=f"enroll-{username}-op", now=100,
    )
    db.direct_enrollment.record_owner_attested_legacy_payment(
        db, capability=capability, account_id=account["account_id"], decision_ref=decision_ref,
        attestation_note="Owner attests historical direct payment, details unknown",
        evidence={"source": "test"}, now=100,
    )
    ensure_legacy_paid_compat_entitlement(
        db, capability=capability, account_id=account["account_id"],
        decision_ref=decision_ref, now=100,
    )
    return account


def _primary_alias_id(db, account_id):
    row = db._conn.execute(
        "SELECT id FROM mgboost_legacy_account_aliases WHERE account_id=? AND alias_role='PRIMARY'",
        (account_id,),
    ).fetchone()
    return row["id"]


def _provision_child(db, *, account_id, alias_id, hwid, mapping, legacy_username):
    remote = FakeMarzban()
    remote.users[legacy_username] = remote.users.pop("alice")
    remote.users[legacy_username]["username"] = legacy_username
    slot = db.device_slots.claim(account_id, hwid, HWID_KEY, now=100)
    request_hash = source_contract_hash(remote.users[legacy_username])
    prepared = db.child_provisioning.prepare_child_ensure(
        account_id=account_id, slot_generation_id=slot["generation_id"],
        source_alias_id=alias_id, source_contract_hash=request_hash, expire=0,
        idempotency_key=f"consolidation-fixture-{mapping}", now=100,
    )
    claimed = db.child_provisioning.claim(
        prepared["operation_id"], worker_id="fixture-worker", now=101, lease_seconds=5,
    )
    created = BrokerOperations(remote).dispatch("child.user.ensure", claimed["payload"])
    child_uuid = created.pop("uuid")
    db.child_provisioning.acknowledge(
        prepared["operation_id"], worker_id="fixture-worker", outcome=created["outcome"],
        child_uuid=child_uuid, remote_result=created, now=102,
    )
    return {
        "slot": slot, "child_intent_id": prepared["child_intent_id"], "remote": remote,
        "child_username": prepared["child_username"],
    }


def _revoke_and_free(db, *, account_id, child_intent_id, remote, now_base):
    prepared = db.child_lifecycle.prepare_revoke(
        account_id=account_id, old_child_intent_id=child_intent_id,
        reason="account merge consolidation", idempotency_key="dl057-revoke-step---", now=now_base,
    )
    child_lifecycle.process_revoke(
        db, prepared["operation_id"], worker_id="fixture-worker",
        revoke_fn=lambda payload: BrokerOperations(remote).dispatch("child.user.revoke", payload),
        now=now_base,
    )
    free_prepared = db.child_lifecycle.prepare_free(
        account_id=account_id, old_child_intent_id=child_intent_id,
        reason="account merge consolidation", idempotency_key="dl057-free-step-----",
        now=now_base + 1,
    )
    child_lifecycle.process_free(
        db, free_prepared["operation_id"], worker_id="fixture-worker", now=now_base + 1,
    )


def _megochel_pair(db, cap):
    """The running example: PC (absorbed, no Telegram, no real device yet)
    and Android (survivor, real Telegram owner, real credential)."""
    absorbed = _direct_account(db, cap, username="MegochelPC")
    survivor = _direct_account(
        db, cap, username="MegochelAndroid", tg=1623120036, ownership_evidence="PROVEN",
    )
    credential = db.subscription_credentials.prepare(
        account_id=survivor["account_id"], actor_ref="telegram:1623120036",
        reason="Telegram /newsub initial issuance", idempotency_key="megochel-cred-prepare-1",
        now=100,
    )
    db.subscription_credentials.activate(
        credential_id=credential["id"], account_id=survivor["account_id"], expected_generation=1,
        actor_ref="telegram:1623120036", idempotency_key="megochel-cred-activate-1", now=100,
    )
    return absorbed, survivor


# --- schema -------------------------------------------------------------

def test_schema_applied_and_idempotent(db):
    row = db._conn.execute(
        "SELECT schema_checksum FROM mgboost_schema_migrations WHERE migration_id=?",
        (MIGRATION_ID,),
    ).fetchone()
    assert row["schema_checksum"] == SCHEMA_CHECKSUM
    assert apply_account_consolidation_schema(db._conn) is False


def test_merge_identity_columns_are_immutable(db):
    cap = _capability(db)
    absorbed, survivor = _megochel_pair(db, cap)
    close_account(
        db, capability=cap, account_id=absorbed["account_id"],
        decision_ref="dl-057", reason="merged into survivor",
    )
    merge = create_merge(
        db, capability=cap, absorbed_account_id=absorbed["account_id"],
        survivor_account_id=survivor["account_id"], decision_ref="dl-057",
        reason="same real person",
    )
    with pytest.raises(Exception):
        db._conn.execute(
            "UPDATE mgboost_account_merges SET survivor_account_id=? WHERE id=?",
            (absorbed["account_id"], merge["id"]),
        )
    with pytest.raises(Exception):
        db._conn.execute("DELETE FROM mgboost_account_merges WHERE id=?", (merge["id"],))
    with pytest.raises(Exception):
        db._conn.execute(
            "UPDATE mgboost_account_merge_events SET reason='tampered' WHERE merge_id=?",
            (merge["id"],),
        )


def test_display_name_identity_is_immutable_only_revoked_at_is_mutable(db):
    cap = _capability(db)
    _absorbed, survivor = _megochel_pair(db, cap)
    set_display_name(
        db, capability=cap, account_id=survivor["account_id"], display_name="Megochel",
        decision_ref="dl-057",
    )
    row = db._conn.execute(
        "SELECT id FROM mgboost_account_display_names WHERE account_id=?",
        (survivor["account_id"],),
    ).fetchone()
    with pytest.raises(Exception):
        db._conn.execute(
            "UPDATE mgboost_account_display_names SET display_name='Other' WHERE id=?",
            (row["id"],),
        )
    with pytest.raises(Exception):
        db._conn.execute("DELETE FROM mgboost_account_display_names WHERE id=?", (row["id"],))


# --- resolver coverage ----------------------------------------------------

def test_resolve_account_id_passthrough_when_no_merge(db):
    cap = _capability(db)
    absorbed, _survivor = _megochel_pair(db, cap)
    assert resolve_account_id(db, absorbed["account_id"]) == absorbed["account_id"]


def test_legacy_bridge_resolves_absorbed_username_to_survivor(db):
    cap = _capability(db)
    absorbed, survivor = _megochel_pair(db, cap)
    alias_id = _primary_alias_id(db, absorbed["account_id"])
    db.legacy_bridge.create_binding(
        capability=cap, account_id=absorbed["account_id"], legacy_alias_id=alias_id,
        enabled=True, decision_ref="dl-057", now=100,
    )
    assert db.legacy_bridge.resolve_account_for_legacy_username(
        "MegochelPC"
    ) == absorbed["account_id"]

    close_account(
        db, capability=cap, account_id=absorbed["account_id"],
        decision_ref="dl-057", reason="merged",
    )
    create_merge(
        db, capability=cap, absorbed_account_id=absorbed["account_id"],
        survivor_account_id=survivor["account_id"], decision_ref="dl-057",
        reason="same real person",
    )
    # DL-057: a real device reconnecting on the absorbed legacy username now
    # transparently lands on the survivor -- never on the closed account.
    assert db.legacy_bridge.resolve_account_for_legacy_username(
        "MegochelPC"
    ) == survivor["account_id"]
    # the alias row itself is completely untouched
    alias = db._conn.execute(
        "SELECT account_id FROM mgboost_legacy_account_aliases WHERE legacy_username=?",
        ("MegochelPC",),
    ).fetchone()
    assert alias["account_id"] == absorbed["account_id"]


def test_telegram_grace_registration_resolves_absorbed_username_gracefully(db):
    """DL-057 resolver-coverage finding: `bind_telegram_after_registration`
    resolved the alias's raw account_id and called `link_telegram_owner`
    directly -- against a CLOSED absorbed account that raises
    `AccountSchemaError`, not `IdentityConflict`, which this function did
    not catch. Canonicalizing through the survivor first fixes this and
    also produces the semantically correct outcome (the survivor already
    has this exact Telegram owner)."""
    cap = _capability(db)
    absorbed, survivor = _megochel_pair(db, cap)
    close_account(
        db, capability=cap, account_id=absorbed["account_id"],
        decision_ref="dl-057", reason="merged",
    )
    create_merge(
        db, capability=cap, absorbed_account_id=absorbed["account_id"],
        survivor_account_id=survivor["account_id"], decision_ref="dl-057",
        reason="same real person",
    )
    outcome = bind_telegram_after_registration(
        db, legacy_username="MegochelPC", telegram_id=1623120036, actor="test-bot",
    )
    assert outcome == "ALREADY_BOUND"
    outcome_other = bind_telegram_after_registration(
        db, legacy_username="MegochelPC", telegram_id=999999999, actor="test-bot",
    )
    assert outcome_other == "CONFLICT"
    # never raised, never created a second owner identity anywhere
    owners = db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_telegram_identities WHERE role='OWNER' AND revoked_at IS NULL"
    ).fetchone()[0]
    assert owners == 1


def test_admin_expiry_ops_refuse_a_closed_absorbed_account(db):
    cap = _capability(db)
    absorbed, survivor = _megochel_pair(db, cap)
    close_account(
        db, capability=cap, account_id=absorbed["account_id"],
        decision_ref="dl-057", reason="merged",
    )
    create_merge(
        db, capability=cap, absorbed_account_id=absorbed["account_id"],
        survivor_account_id=survivor["account_id"], decision_ref="dl-057",
        reason="same real person",
    )
    from src.subscription_admin_ops import AdminExpiryError

    with pytest.raises(AdminExpiryError, match="ACCOUNT_CLOSED"):
        db.subscription_admin_ops.preview(
            absorbed["account_id"], adjustment_kind="EXTEND_DAYS", value=7, now=200,
        )
    with pytest.raises(AdminExpiryError, match="ACCOUNT_CLOSED"):
        db.subscription_admin_ops.apply_adjustment(
            cap, account_id=absorbed["account_id"], adjustment_kind="EXTEND_DAYS", value=7,
            reason="attempted mutation on a closed absorbed account",
            idempotency_key="closed-account-expiry-attempt-1", now=200,
        )


# --- display_name fallback -------------------------------------------------

def test_display_name_fallback_none_then_set_then_canonical_in_read_models(db):
    cap = _capability(db)
    absorbed, survivor = _megochel_pair(db, cap)
    assert get_display_name(db, survivor["account_id"]) is None
    detail = account_detail(db, survivor["account_id"], now=200)
    assert detail["display_identity"]["display_name"] is None
    assert detail["display_identity"]["primary_alias"] == "MegochelAndroid"

    set_display_name(
        db, capability=cap, account_id=survivor["account_id"], display_name="Megochel",
        decision_ref="dl-057",
    )
    assert get_display_name(db, survivor["account_id"]) == "Megochel"
    detail = account_detail(db, survivor["account_id"], now=200)
    assert detail["display_identity"]["display_name"] == "Megochel"
    assert detail["display_identity"]["primary_alias"] == "MegochelAndroid"

    summaries = {row["id"]: row for row in account_summaries(db, now=200, include_technical=True)}
    assert summaries[survivor["account_id"]]["display_name"] == "Megochel"

    # idempotent replay of the same name
    result = set_display_name(
        db, capability=cap, account_id=survivor["account_id"], display_name="Megochel",
        decision_ref="dl-057",
    )
    assert result["already_applied"] is True
    assert db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_account_display_names WHERE account_id=? AND revoked_at IS NULL",
        (survivor["account_id"],),
    ).fetchone()[0] == 1

    # changing it revokes the old row and inserts a new one, never an UPDATE
    set_display_name(
        db, capability=cap, account_id=survivor["account_id"], display_name="Megochel V2",
        decision_ref="dl-057",
    )
    all_rows = db._conn.execute(
        "SELECT display_name,revoked_at FROM mgboost_account_display_names "
        "WHERE account_id=? ORDER BY id", (survivor["account_id"],),
    ).fetchall()
    assert [dict(row) for row in all_rows][0]["revoked_at"] is not None
    assert get_display_name(db, survivor["account_id"]) == "Megochel V2"


def test_display_name_refused_on_non_active_account(db):
    cap = _capability(db)
    absorbed, _survivor = _megochel_pair(db, cap)
    close_account(
        db, capability=cap, account_id=absorbed["account_id"],
        decision_ref="dl-057", reason="merged",
    )
    with pytest.raises(ClosedAccountError):
        set_display_name(
            db, capability=cap, account_id=absorbed["account_id"], display_name="X",
            decision_ref="dl-057",
        )


# --- close preconditions ---------------------------------------------------

def test_close_refuses_active_telegram_owner(db):
    cap = _capability(db)
    _absorbed, survivor = _megochel_pair(db, cap)
    with pytest.raises(TelegramOwnerStillActive):
        close_account(
            db, capability=cap, account_id=survivor["account_id"],
            decision_ref="dl-057", reason="attempted close with live owner",
        )


def test_close_refuses_non_terminal_child_then_succeeds_after_revoke_free(db):
    cap = _capability(db)
    absorbed, survivor = _megochel_pair(db, cap)
    alias_id = _primary_alias_id(db, absorbed["account_id"])
    child = _provision_child(
        db, account_id=absorbed["account_id"], alias_id=alias_id,
        hwid="genesis-hwid-megochelpc", mapping="MEGOCHELPC", legacy_username="MegochelPC",
    )

    with pytest.raises(NonTerminalChildExists):
        close_account(
            db, capability=cap, account_id=absorbed["account_id"],
            decision_ref="dl-057", reason="attempted close with live child",
        )

    _revoke_and_free(
        db, account_id=absorbed["account_id"], child_intent_id=child["child_intent_id"],
        remote=child["remote"], now_base=300,
    )

    remote_user = child["remote"].users[child["child_username"]]
    assert remote_user["status"] == "disabled"

    generation = db._conn.execute(
        "SELECT status FROM mgboost_device_slot_generations WHERE id=?",
        (child["slot"]["generation_id"],),
    ).fetchone()
    assert generation["status"] == "RELEASED"
    slot = db._conn.execute(
        "SELECT desired_state,observed_state FROM mgboost_device_slots WHERE id=?",
        (child["slot"]["slot_id"],),
    ).fetchone()
    assert (slot["desired_state"], slot["observed_state"]) == ("FREE", "FREE")

    # no ACTIVE generation remains anywhere for this account -- close now succeeds
    active_generations = db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_device_slot_generations "
        "WHERE account_id=? AND status='ACTIVE'", (absorbed["account_id"],),
    ).fetchone()[0]
    assert active_generations == 0

    result = close_account(
        db, capability=cap, account_id=absorbed["account_id"],
        decision_ref="dl-057", reason="genesis child retired, safe to close",
    )
    assert result["status"] == "CLOSED"
    assert result["subscription_cancelled"] is True
    account = db.accounts.get_account(absorbed["account_id"])
    assert account["status"] == "CLOSED"
    subscription = db._conn.execute(
        "SELECT status FROM mgboost_subscriptions WHERE account_id=?",
        (absorbed["account_id"],),
    ).fetchone()
    assert subscription["status"] == "CANCELLED"


def test_close_refuses_active_generation_without_a_revoke(db):
    cap = _capability(db)
    absorbed, _survivor = _megochel_pair(db, cap)
    alias_id = _primary_alias_id(db, absorbed["account_id"])
    _provision_child(
        db, account_id=absorbed["account_id"], alias_id=alias_id,
        hwid="genesis-hwid-active-gen", mapping="ACTIVEGEN", legacy_username="MegochelPC",
    )
    # child intent itself is ACTIVE/ACTIVE here (no revoke attempted at all)
    with pytest.raises((NonTerminalChildExists, ActiveGenerationExists)):
        close_account(
            db, capability=cap, account_id=absorbed["account_id"],
            decision_ref="dl-057", reason="attempted close with active generation",
        )


def test_close_is_idempotent(db):
    cap = _capability(db)
    absorbed, _survivor = _megochel_pair(db, cap)
    close_account(db, capability=cap, account_id=absorbed["account_id"], decision_ref="dl-057", reason="r-close")
    result = close_account(
        db, capability=cap, account_id=absorbed["account_id"], decision_ref="dl-057", reason="r2-close",
    )
    assert result["already_applied"] is True
    assert result["status"] == "CLOSED"


def test_close_unknown_account_raises(db):
    cap = _capability(db)
    with pytest.raises(AccountNotFound):
        close_account(db, capability=cap, account_id=999999, decision_ref="dl-057", reason="r-close")


# --- merge apply / replay / reversal --------------------------------------

def test_merge_apply_replay_is_idempotent(db):
    cap = _capability(db)
    absorbed, survivor = _megochel_pair(db, cap)
    close_account(db, capability=cap, account_id=absorbed["account_id"], decision_ref="dl-057", reason="r-close")

    first = create_merge(
        db, capability=cap, absorbed_account_id=absorbed["account_id"],
        survivor_account_id=survivor["account_id"], decision_ref="dl-057", reason="same person",
    )
    assert first["already_applied"] is False
    second = create_merge(
        db, capability=cap, absorbed_account_id=absorbed["account_id"],
        survivor_account_id=survivor["account_id"], decision_ref="dl-057", reason="same person again",
    )
    assert second["already_applied"] is True
    assert second["id"] == first["id"]
    count = db._conn.execute("SELECT COUNT(*) FROM mgboost_account_merges").fetchone()[0]
    assert count == 1
    events = db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_account_merge_events WHERE merge_id=?", (first["id"],),
    ).fetchone()[0]
    assert events == 1


def test_reversal_is_append_only_and_idempotent(db):
    cap = _capability(db)
    absorbed, survivor = _megochel_pair(db, cap)
    close_account(db, capability=cap, account_id=absorbed["account_id"], decision_ref="dl-057", reason="r-close")
    merge = create_merge(
        db, capability=cap, absorbed_account_id=absorbed["account_id"],
        survivor_account_id=survivor["account_id"], decision_ref="dl-057", reason="same person",
    )
    assert resolve_account_id(db, absorbed["account_id"]) == survivor["account_id"]

    reversed_row = reverse_merge(
        db, capability=cap, absorbed_account_id=absorbed["account_id"],
        decision_ref="dl-057-reversal", reason="reverted by mistake",
    )
    assert reversed_row["status"] == "REVERSED"
    assert reversed_row["id"] == merge["id"]
    # append-only: row still exists, never deleted
    assert db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_account_merges WHERE id=?", (merge["id"],),
    ).fetchone()[0] == 1
    assert resolve_account_id(db, absorbed["account_id"]) == absorbed["account_id"]

    again = reverse_merge(
        db, capability=cap, absorbed_account_id=absorbed["account_id"],
        decision_ref="dl-057-reversal", reason="already reversed",
    )
    assert again["already_applied"] is True

    # reversal alone does not reopen the account or resurrect the child
    account = db.accounts.get_account(absorbed["account_id"])
    assert account["status"] == "CLOSED"

    reopened = reopen_account(
        db, capability=cap, account_id=absorbed["account_id"],
        decision_ref="dl-057-reopen", reason="independence restored",
    )
    assert reopened["status"] == "ACTIVE"
    idempotent_reopen = reopen_account(
        db, capability=cap, account_id=absorbed["account_id"],
        decision_ref="dl-057-reopen", reason="already active",
    )
    assert idempotent_reopen["already_applied"] is True


def test_reverse_merge_not_found(db):
    cap = _capability(db)
    with pytest.raises(MergeNotFound):
        reverse_merge(
            db, capability=cap, absorbed_account_id=999999,
            decision_ref="dl-057", reason="no such merge",
        )


def test_reopen_refused_while_merge_active(db):
    cap = _capability(db)
    absorbed, survivor = _megochel_pair(db, cap)
    close_account(db, capability=cap, account_id=absorbed["account_id"], decision_ref="dl-057", reason="r-close")
    create_merge(
        db, capability=cap, absorbed_account_id=absorbed["account_id"],
        survivor_account_id=survivor["account_id"], decision_ref="dl-057", reason="same person",
    )
    with pytest.raises(AccountConsolidationError):
        reopen_account(
            db, capability=cap, account_id=absorbed["account_id"],
            decision_ref="dl-057", reason="attempted reopen while absorbed",
        )


# --- self/cycle/chain/conflict rejection -----------------------------------

def test_self_merge_rejected(db):
    cap = _capability(db)
    absorbed, _survivor = _megochel_pair(db, cap)
    close_account(db, capability=cap, account_id=absorbed["account_id"], decision_ref="dl-057", reason="r-close")
    with pytest.raises(SelfMergeError):
        create_merge(
            db, capability=cap, absorbed_account_id=absorbed["account_id"],
            survivor_account_id=absorbed["account_id"], decision_ref="dl-057", reason="self merge attempt",
        )


def test_merge_requires_absorbed_already_closed(db):
    cap = _capability(db)
    absorbed, survivor = _megochel_pair(db, cap)
    with pytest.raises(AbsorbedNotClosed):
        create_merge(
            db, capability=cap, absorbed_account_id=absorbed["account_id"],
            survivor_account_id=survivor["account_id"], decision_ref="dl-057",
            reason="forgot to close first",
        )


def test_merge_requires_survivor_active(db):
    cap = _capability(db)
    absorbed, survivor = _megochel_pair(db, cap)
    third = _direct_account(db, cap, username="MegochelThird")
    close_account(db, capability=cap, account_id=absorbed["account_id"], decision_ref="dl-057", reason="r-close")
    close_account(db, capability=cap, account_id=third["account_id"], decision_ref="dl-057", reason="r2-close")
    with pytest.raises(SurvivorNotActive):
        create_merge(
            db, capability=cap, absorbed_account_id=absorbed["account_id"],
            survivor_account_id=third["account_id"], decision_ref="dl-057",
            reason="survivor is closed too",
        )


def test_conflicting_survivor_for_already_merged_absorbed_is_rejected(db):
    cap = _capability(db)
    absorbed, survivor = _megochel_pair(db, cap)
    other = _direct_account(db, cap, username="SomeoneElse", tg=42, ownership_evidence="PROVEN")
    close_account(db, capability=cap, account_id=absorbed["account_id"], decision_ref="dl-057", reason="r-close")
    create_merge(
        db, capability=cap, absorbed_account_id=absorbed["account_id"],
        survivor_account_id=survivor["account_id"], decision_ref="dl-057", reason="same person",
    )
    with pytest.raises(MergeConflict):
        create_merge(
            db, capability=cap, absorbed_account_id=absorbed["account_id"],
            survivor_account_id=other["account_id"], decision_ref="dl-057",
            reason="attempted re-target to a different survivor",
        )


def test_chained_merge_via_absorbed_already_a_survivor_is_rejected(db):
    """A -> B already merged (B is now a survivor); B can never later be
    absorbed into C -- that would chain through the survivor side."""
    cap = _capability(db)
    a = _direct_account(db, cap, username="ChainA")
    b = _direct_account(db, cap, username="ChainB")
    c = _direct_account(db, cap, username="ChainC")
    close_account(db, capability=cap, account_id=a["account_id"], decision_ref="dl-057", reason="r-close")
    create_merge(
        db, capability=cap, absorbed_account_id=a["account_id"], survivor_account_id=b["account_id"],
        decision_ref="dl-057", reason="a into b",
    )
    close_account(db, capability=cap, account_id=b["account_id"], decision_ref="dl-057", reason="r2-close")
    with pytest.raises(MergeChainConflict):
        create_merge(
            db, capability=cap, absorbed_account_id=b["account_id"],
            survivor_account_id=c["account_id"], decision_ref="dl-057",
            reason="attempted chain b into c",
        )


def test_chained_merge_via_survivor_being_absorbed_elsewhere_is_rejected(db):
    """A -> B merged, then reversed and reopened: A is ACTIVE again, but it
    has already played the absorbed role once (even if reversed) -- it must
    never be trusted as a survivor for a new merge (C -> A)."""
    cap = _capability(db)
    a = _direct_account(db, cap, username="ChainX")
    b = _direct_account(db, cap, username="ChainY")
    c = _direct_account(db, cap, username="ChainZ")
    close_account(db, capability=cap, account_id=a["account_id"], decision_ref="dl-057", reason="r-close")
    create_merge(
        db, capability=cap, absorbed_account_id=a["account_id"], survivor_account_id=b["account_id"],
        decision_ref="dl-057", reason="a into b",
    )
    reverse_merge(
        db, capability=cap, absorbed_account_id=a["account_id"], decision_ref="dl-057",
        reason="reverted this merge",
    )
    reopen_account(
        db, capability=cap, account_id=a["account_id"], decision_ref="dl-057",
        reason="restored independence",
    )
    close_account(db, capability=cap, account_id=c["account_id"], decision_ref="dl-057", reason="r2-close")
    with pytest.raises(MergeChainConflict):
        create_merge(
            db, capability=cap, absorbed_account_id=c["account_id"],
            survivor_account_id=a["account_id"], decision_ref="dl-057",
            reason="attempted chain c into a",
        )


# --- concurrent / stale CAS -------------------------------------------------

def test_concurrent_create_merge_converges_to_exactly_one_row(db):
    cap = _capability(db)
    absorbed, survivor = _megochel_pair(db, cap)
    close_account(db, capability=cap, account_id=absorbed["account_id"], decision_ref="dl-057", reason="r-close")

    errors = []

    def worker():
        try:
            create_merge(
                db, capability=cap, absorbed_account_id=absorbed["account_id"],
                survivor_account_id=survivor["account_id"], decision_ref="dl-057",
                reason="concurrent attempt",
            )
        except Exception as exc:  # noqa: BLE001 -- collected, not swallowed
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_account_merges WHERE absorbed_account_id=?",
        (absorbed["account_id"],),
    ).fetchone()[0] == 1


def test_stale_cas_mechanism_on_merge_row_rejects_a_mismatched_update(db):
    """`reverse_merge()`/`create_merge()` share the exact optimistic-CAS
    clause proven here directly: an `UPDATE ... WHERE id=? AND
    row_version=?` keyed on a now-stale version affects zero rows. The
    higher-level function itself always re-reads before its own CAS, so it
    stays correct regardless -- this test isolates and proves the guard
    clause those functions rely on is genuinely enforced by SQLite, not
    just assumed."""
    cap = _capability(db)
    absorbed, survivor = _megochel_pair(db, cap)
    close_account(db, capability=cap, account_id=absorbed["account_id"], decision_ref="dl-057", reason="r-close")
    merge = create_merge(
        db, capability=cap, absorbed_account_id=absorbed["account_id"],
        survivor_account_id=survivor["account_id"], decision_ref="dl-057", reason="same person",
    )
    stale_version = merge["row_version"]

    # A concurrent writer bumps the row first (a real, committed change).
    db._conn.execute(
        "UPDATE mgboost_account_merges SET updated_at=updated_at,row_version=row_version+1 WHERE id=?",
        (merge["id"],),
    )
    db._conn.commit()

    # The exact CAS clause the store relies on rejects the now-stale version.
    stale_update = db._conn.execute(
        "UPDATE mgboost_account_merges SET status='REVERSED',row_version=row_version+1 "
        "WHERE id=? AND row_version=?",
        (merge["id"], stale_version),
    )
    db._conn.commit()
    assert stale_update.rowcount == 0
    assert db._conn.execute(
        "SELECT status FROM mgboost_account_merges WHERE id=?", (merge["id"],),
    ).fetchone()["status"] == "ACTIVE"

    # reverse_merge() itself always re-reads first, so it stays correct.
    result = reverse_merge(
        db, capability=cap, absorbed_account_id=absorbed["account_id"],
        decision_ref="dl-057", reason="reverse after external bump",
    )
    assert result["status"] == "REVERSED"


def test_stale_cas_mechanism_on_subscription_row_rejects_a_mismatched_update(db):
    """Same guard clause `close_account()`'s subscription-cancel step
    relies on, proven directly for the same reason as the merge-row case
    above: `BEGIN IMMEDIATE` already makes a genuine external race
    impossible to observe mid-call (a second writer blocks on the file lock
    until commit/rollback), so this isolates the SQL CAS clause itself
    rather than trying to force an architecturally-prevented race."""
    cap = _capability(db)
    absorbed, _survivor = _megochel_pair(db, cap)
    sub = db._conn.execute(
        "SELECT id,row_version FROM mgboost_subscriptions WHERE account_id=?",
        (absorbed["account_id"],),
    ).fetchone()
    stale_version = sub["row_version"]

    db._conn.execute(
        "UPDATE mgboost_subscriptions SET row_version=row_version+1 WHERE id=?", (sub["id"],),
    )
    db._conn.commit()

    stale_update = db._conn.execute(
        "UPDATE mgboost_subscriptions SET status='CANCELLED',row_version=row_version+1 "
        "WHERE id=? AND account_id=? AND row_version=?",
        (sub["id"], absorbed["account_id"], stale_version),
    )
    db._conn.commit()
    assert stale_update.rowcount == 0
    assert db._conn.execute(
        "SELECT status FROM mgboost_subscriptions WHERE id=?", (sub["id"],),
    ).fetchone()["status"] != "CANCELLED"

    # close_account() itself always re-reads first, so it stays correct.
    result = close_account(
        db, capability=cap, account_id=absorbed["account_id"],
        decision_ref="dl-057", reason="close after external bump",
    )
    assert result["status"] == "CLOSED"
    assert result["subscription_cancelled"] is True


# --- D3 -> D6 legacy-compat device limit -----------------------------------

def test_d3_to_d6_changes_only_plan_version_never_expiry_status_or_second_subscription(db):
    cap = _capability(db)
    _absorbed, survivor = _megochel_pair(db, cap)
    account_id = survivor["account_id"]
    before = db._conn.execute(
        "SELECT id,status,current_expiry,row_version FROM mgboost_subscriptions WHERE account_id=?",
        (account_id,),
    ).fetchone()
    before_plan = db._conn.execute(
        "SELECT plan_code,device_limit,wl_mode FROM mgboost_plan_versions WHERE id="
        "(SELECT current_plan_version_id FROM mgboost_subscriptions WHERE account_id=?)",
        (account_id,),
    ).fetchone()
    assert before_plan["plan_code"] == "LEGACY_PAID_COMPAT_V1_D3"

    result = increase_device_limit(
        db, capability=cap, account_id=account_id, approved_extra_device_slots=3,
        decision_ref="dl-057", evidence={"trusted_user": True, "owner_decision": "megochel consolidation"},
        now=200,
    )
    assert result["already_applied"] is False
    assert result["id"] == before["id"]  # same subscription row, never a second one

    after = db._conn.execute(
        "SELECT id,status,current_expiry FROM mgboost_subscriptions WHERE account_id=?",
        (account_id,),
    ).fetchone()
    after_plan = db._conn.execute(
        "SELECT plan_code,device_limit,wl_mode FROM mgboost_plan_versions WHERE id="
        "(SELECT current_plan_version_id FROM mgboost_subscriptions WHERE account_id=?)",
        (account_id,),
    ).fetchone()
    assert after["id"] == before["id"]
    assert after["status"] == before["status"]
    assert after["current_expiry"] == before["current_expiry"]
    assert after_plan["plan_code"] == "LEGACY_PAID_COMPAT_V1_D6"
    assert after_plan["device_limit"] == 6
    assert after_plan["wl_mode"] == before_plan["wl_mode"] == "UNLIMITED"

    live_subs = db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_subscriptions WHERE account_id=? "
        "AND status IN ('PENDING','ACTIVE','DISABLED','UNLIMITED','UNKNOWN_LEGACY')",
        (account_id,),
    ).fetchone()[0]
    assert live_subs == 1

    # idempotent replay at the same target limit
    replay = increase_device_limit(
        db, capability=cap, account_id=account_id, approved_extra_device_slots=3,
        decision_ref="dl-057", evidence={"trusted_user": True}, now=201,
    )
    assert replay["already_applied"] is True


def test_increase_device_limit_refuses_decrease(db):
    cap = _capability(db)
    _absorbed, survivor = _megochel_pair(db, cap)
    increase_device_limit(
        db, capability=cap, account_id=survivor["account_id"], approved_extra_device_slots=3,
        decision_ref="dl-057", evidence={"note": "bump to D6 first"}, now=200,
    )
    with pytest.raises(DeviceLimitDecreaseRefused):
        increase_device_limit(
            db, capability=cap, account_id=survivor["account_id"], approved_extra_device_slots=1,
            decision_ref="dl-057", evidence={"note": "attempted decrease to D4"}, now=201,
        )


def test_increase_device_limit_refuses_non_legacy_compat_commercial_plan(db):
    cap = _capability(db)
    account = _direct_account(db, cap, username="CommercialUser", tg=555, ownership_evidence="PROVEN")
    plan = db.accounts.create_plan_version(
        {
            "plan_code": "REAL_COMMERCIAL", "version": 1, "display_name": "Real commercial",
            "plan_kind": "COMMERCIAL", "billing_required": True, "device_limit_mode": "LIMITED",
            "device_limit": 3, "wl_mode": "UNLIMITED", "wl_quota_bytes": None,
            "wl_period_days": None, "terms": {"schema": 1},
        },
        now=100,
    )
    db._conn.execute(
        "UPDATE mgboost_subscriptions SET current_plan_version_id=? WHERE account_id=?",
        (plan["id"], account["account_id"]),
    )
    db._conn.commit()
    with pytest.raises(NotLegacyCompatPlan):
        increase_device_limit(
            db, capability=cap, account_id=account["account_id"], approved_extra_device_slots=3,
            decision_ref="dl-057", evidence={"note": "should never apply to a billed plan"}, now=200,
        )


def test_increase_device_limit_requires_evidence(db):
    cap = _capability(db)
    _absorbed, survivor = _megochel_pair(db, cap)
    from src.legacy_paid_compat import LegacyPaidCompatError

    with pytest.raises(LegacyPaidCompatError):
        increase_device_limit(
            db, capability=cap, account_id=survivor["account_id"], approved_extra_device_slots=3,
            decision_ref="dl-057", evidence=None, now=200,
        )


# --- survivor identity stability / old history stays attributed -----------

def test_survivor_keeps_telegram_credential_and_subscription_id_unchanged(db):
    cap = _capability(db)
    absorbed, survivor = _megochel_pair(db, cap)
    before_owner = db._conn.execute(
        "SELECT id,telegram_id,linked_at FROM mgboost_telegram_identities "
        "WHERE account_id=? AND role='OWNER' AND revoked_at IS NULL", (survivor["account_id"],),
    ).fetchone()
    before_credential = db._conn.execute(
        "SELECT id,token_hash,status FROM mgboost_subscription_credentials WHERE account_id=?",
        (survivor["account_id"],),
    ).fetchone()
    before_sub_id = db._conn.execute(
        "SELECT id FROM mgboost_subscriptions WHERE account_id=?", (survivor["account_id"],),
    ).fetchone()["id"]

    close_account(db, capability=cap, account_id=absorbed["account_id"], decision_ref="dl-057", reason="r-close")
    create_merge(
        db, capability=cap, absorbed_account_id=absorbed["account_id"],
        survivor_account_id=survivor["account_id"], decision_ref="dl-057", reason="same person",
    )
    set_display_name(
        db, capability=cap, account_id=survivor["account_id"], display_name="Megochel",
        decision_ref="dl-057",
    )
    increase_device_limit(
        db, capability=cap, account_id=survivor["account_id"], approved_extra_device_slots=3,
        decision_ref="dl-057", evidence={"note": "trusted user"}, now=200,
    )

    after_owner = db._conn.execute(
        "SELECT id,telegram_id,linked_at FROM mgboost_telegram_identities "
        "WHERE account_id=? AND role='OWNER' AND revoked_at IS NULL", (survivor["account_id"],),
    ).fetchone()
    after_credential = db._conn.execute(
        "SELECT id,token_hash,status FROM mgboost_subscription_credentials WHERE account_id=?",
        (survivor["account_id"],),
    ).fetchone()
    after_sub_id = db._conn.execute(
        "SELECT id FROM mgboost_subscriptions WHERE account_id=?", (survivor["account_id"],),
    ).fetchone()["id"]

    assert dict(after_owner) == dict(before_owner)
    assert dict(after_credential) == dict(before_credential)
    assert after_sub_id == before_sub_id


def test_old_immutable_history_stays_attributed_to_the_original_account(db):
    cap = _capability(db)
    absorbed, survivor = _megochel_pair(db, cap)
    alias_id = _primary_alias_id(db, absorbed["account_id"])
    child = _provision_child(
        db, account_id=absorbed["account_id"], alias_id=alias_id,
        hwid="genesis-hwid-history", mapping="HISTORY", legacy_username="MegochelPC",
    )
    _revoke_and_free(
        db, account_id=absorbed["account_id"], child_intent_id=child["child_intent_id"],
        remote=child["remote"], now_base=300,
    )
    before_counts = {
        table: db._conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE account_id=?", (absorbed["account_id"],),
        ).fetchone()[0]
        for table in (
            "mgboost_legacy_account_aliases", "mgboost_child_user_intents",
            "mgboost_device_slot_generations", "mgboost_entitlement_mutations",
        )
    }
    assert before_counts["mgboost_legacy_account_aliases"] == 1
    assert before_counts["mgboost_child_user_intents"] == 1

    close_account(db, capability=cap, account_id=absorbed["account_id"], decision_ref="dl-057", reason="r-close")
    create_merge(
        db, capability=cap, absorbed_account_id=absorbed["account_id"],
        survivor_account_id=survivor["account_id"], decision_ref="dl-057", reason="same person",
    )

    after_counts = {
        table: db._conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE account_id=?", (absorbed["account_id"],),
        ).fetchone()[0]
        for table in before_counts
    }
    assert after_counts["mgboost_legacy_account_aliases"] == before_counts["mgboost_legacy_account_aliases"]
    assert after_counts["mgboost_child_user_intents"] == before_counts["mgboost_child_user_intents"]
    assert after_counts["mgboost_device_slot_generations"] == before_counts["mgboost_device_slot_generations"]
    # a genuinely new evidence row (the close mutation) is expected and correct --
    # the *meaning* to preserve is "nothing pre-existing was rewritten", not a
    # frozen literal row count across the whole account.
    assert after_counts["mgboost_entitlement_mutations"] > before_counts["mgboost_entitlement_mutations"]

    alias = db._conn.execute(
        "SELECT account_id,legacy_username FROM mgboost_legacy_account_aliases WHERE account_id=?",
        (absorbed["account_id"],),
    ).fetchone()
    assert alias["legacy_username"] == "MegochelPC"
    assert alias["account_id"] == absorbed["account_id"]


def test_merge_has_no_effect_on_an_unrelated_third_account(db):
    cap = _capability(db)
    absorbed, survivor = _megochel_pair(db, cap)
    unrelated = _direct_account(db, cap, username="Unrelated", tg=777, ownership_evidence="PROVEN")
    before = dict(db.accounts.get_account(unrelated["account_id"]))

    close_account(db, capability=cap, account_id=absorbed["account_id"], decision_ref="dl-057", reason="r-close")
    create_merge(
        db, capability=cap, absorbed_account_id=absorbed["account_id"],
        survivor_account_id=survivor["account_id"], decision_ref="dl-057", reason="same person",
    )
    set_display_name(
        db, capability=cap, account_id=survivor["account_id"], display_name="Megochel",
        decision_ref="dl-057",
    )

    after = dict(db.accounts.get_account(unrelated["account_id"]))
    assert after == before
    assert resolve_account_id(db, unrelated["account_id"]) == unrelated["account_id"]
    assert get_display_name(db, unrelated["account_id"]) is None
