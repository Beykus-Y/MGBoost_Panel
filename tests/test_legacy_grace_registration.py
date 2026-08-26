"""PH4-05 mass-grace-campaign registration glue: account bootstrap without
Telegram evidence, post-registration binding (incl. ambiguous/conflict),
and shared-UTC-boundary cohort start (incl. duplicate/partial/retry)."""

import importlib
import os
import tempfile

import pytest

from src.legacy_grace_registration import (
    bind_telegram_after_registration,
    bootstrap_grace_subject,
    start_grace_cohort,
)
from src.legacy_grace_schema import GRACE_PERIOD_SECONDS
from src.security import AdminSessionStore

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
    yield instance
    instance._conn.close()


def _capability(db):
    _raw, session = AdminSessionStore().create(PRIMARY_LOGIN, "grace-registration-test-jwt")
    return db.primary_admin_authority.authorize_session(session)


def _tg_link(db, telegram_id, username, *, now=1000):
    """Mirrors `db.save_tg_user()` -- `tg_users.telegram_id` is the PK, so
    one Telegram user can only ever point at one username at a time."""
    db._conn.execute(
        "INSERT OR REPLACE INTO tg_users (telegram_id, marzban_username, registered_at) "
        "VALUES (?,?,?)",
        (telegram_id, username, now),
    )
    db._conn.commit()


def _bootstrap(db, cap, username, *, now=1000, key_suffix=""):
    return bootstrap_grace_subject(
        db, capability=cap, legacy_username=username, legacy_status="ACTIVE",
        legacy_expiry=None, observed_device_count=1, observed_hwid_count=1,
        decision_ref="mass-grace-campaign-2026-08-26",
        payment_decision_ref="owner-attested-legacy-external-payment-2026",
        payment_attestation_note="Historical direct payment, no invented amount/date.",
        payment_evidence={"source": "owner-decision-2026-08-26"},
        idempotency_key=f"grace-bootstrap-v1:{username}{key_suffix}", now=now,
    )


# --- bootstrap without Telegram evidence ------------------------------------

def test_bootstrap_creates_account_with_no_telegram_claim(db):
    cap = _capability(db)
    result = _bootstrap(db, cap, "client047")
    account_id = result["account_id"]
    owner = db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_telegram_identities WHERE account_id=? AND role='OWNER'",
        (account_id,),
    ).fetchone()[0]
    assert owner == 0
    account = db.accounts.get_account(account_id)
    assert account["account_source"] == "DIRECT"
    assert result["subscription"]["status"] == "ACTIVE"


def test_bootstrap_is_idempotent_per_username(db):
    cap = _capability(db)
    first = _bootstrap(db, cap, "client048")
    second = _bootstrap(db, cap, "client048")
    assert first["account_id"] == second["account_id"]


def test_bootstrap_creates_no_bridge_binding_and_no_migration_state(db):
    cap = _capability(db)
    result = _bootstrap(db, cap, "client049")
    account_id = result["account_id"]
    assert db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_legacy_bridge_bindings WHERE account_id=?", (account_id,),
    ).fetchone()[0] == 0
    assert db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_migration_bindings WHERE account_id=?", (account_id,),
    ).fetchone()[0] == 0


def test_bootstrap_then_grace_start_succeeds_with_no_telegram(db):
    """The core architectural claim this module exists to prove: grace
    never waits on Telegram identity."""
    cap = _capability(db)
    result = _bootstrap(db, cap, "client050")
    row = db.legacy_grace.start(
        account_id=result["account_id"], cohort_ref="PH4-05-TEST-COHORT", capability=cap,
        reason="mass grace campaign", idempotency_key="grace-start-key-client050", now=5000,
    )
    assert row["current_end_at"] == 5000 + GRACE_PERIOD_SECONDS


# --- Telegram binding after registration ------------------------------------

def test_bind_no_account_is_a_safe_noop(db):
    result = bind_telegram_after_registration(
        db, legacy_username="never-bootstrapped", telegram_id=111, actor="test",
    )
    assert result == "NO_ACCOUNT"


def test_bind_single_registration_succeeds(db):
    cap = _capability(db)
    result = _bootstrap(db, cap, "client051")
    account_id = result["account_id"]
    _tg_link(db, 222, "client051")
    outcome = bind_telegram_after_registration(
        db, legacy_username="client051", telegram_id=222, actor="bot", now=6000,
    )
    assert outcome == "BOUND"
    owner_id = db._conn.execute(
        "SELECT telegram_id FROM mgboost_telegram_identities WHERE account_id=? AND role='OWNER'",
        (account_id,),
    ).fetchone()[0]
    assert owner_id == 222


def test_bind_is_idempotent_on_retry(db):
    cap = _capability(db)
    _bootstrap(db, cap, "client052")
    _tg_link(db, 333, "client052")
    first = bind_telegram_after_registration(db, legacy_username="client052", telegram_id=333, actor="bot")
    second = bind_telegram_after_registration(db, legacy_username="client052", telegram_id=333, actor="bot")
    assert first == "BOUND"
    assert second == "ALREADY_BOUND"


def test_bind_ambiguous_multiple_telegram_ids_never_auto_resolved(db):
    cap = _capability(db)
    _bootstrap(db, cap, "client053")
    _tg_link(db, 444, "client053")
    _tg_link(db, 555, "client053")
    outcome = bind_telegram_after_registration(db, legacy_username="client053", telegram_id=444, actor="bot")
    assert outcome == "AMBIGUOUS"
    owner_count = db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_telegram_identities WHERE role='OWNER'"
    ).fetchone()[0]
    assert owner_count == 0


def test_bind_telegram_id_already_owns_a_different_account_conflicts(db):
    """One real Telegram user linking two distinct legacy usernames (their
    own second device's old username, say) must never silently merge a
    second account under the same owner via this path -- that is
    ownership-rebind (PH2-05) territory, untouched and not weakened here."""
    cap = _capability(db)
    _bootstrap(db, cap, "client054")
    _bootstrap(db, cap, "client055")
    _tg_link(db, 666, "client054")
    first = bind_telegram_after_registration(db, legacy_username="client054", telegram_id=666, actor="bot")
    assert first == "BOUND"
    _tg_link(db, 666, "client055")
    second = bind_telegram_after_registration(db, legacy_username="client055", telegram_id=666, actor="bot")
    assert second == "CONFLICT"


def test_bind_never_overwrites_an_existing_different_owner(db):
    cap = _capability(db)
    result = _bootstrap(db, cap, "client056")
    account_id = result["account_id"]
    db.accounts.link_telegram_owner(account_id, 777, provenance="DIRECT_BIND", actor="prior")
    _tg_link(db, 888, "client056")
    outcome = bind_telegram_after_registration(db, legacy_username="client056", telegram_id=888, actor="bot")
    assert outcome == "CONFLICT"
    owner_id = db._conn.execute(
        "SELECT telegram_id FROM mgboost_telegram_identities WHERE account_id=? AND role='OWNER'",
        (account_id,),
    ).fetchone()[0]
    assert owner_id == 777


def test_bind_after_grace_already_started_does_not_touch_grace_row(db):
    cap = _capability(db)
    result = _bootstrap(db, cap, "client057")
    account_id = result["account_id"]
    started = db.legacy_grace.start(
        account_id=account_id, cohort_ref="PH4-05-TEST-COHORT", capability=cap,
        reason="mass grace campaign", idempotency_key="grace-start-key-client057", now=9000,
    )
    _tg_link(db, 999, "client057")
    bind_telegram_after_registration(db, legacy_username="client057", telegram_id=999, actor="bot", now=9500)
    after = db.legacy_grace.find_by_account(account_id)
    assert after["started_at"] == started["started_at"]
    assert after["current_end_at"] == started["current_end_at"]
    assert after["revision"] == started["revision"]


# --- shared-UTC-boundary cohort start ---------------------------------------

def test_cohort_start_shares_exact_same_timestamp(db):
    cap = _capability(db)
    ids = [_bootstrap(db, cap, f"client{i}")["account_id"] for i in range(60, 65)]
    result = start_grace_cohort(
        db, capability=cap, account_ids=ids, cohort_ref="PH4-05-SHARED",
        reason="mass grace campaign shared boundary", cohort_start_at=12_345_000,
    )
    assert set(result["started"]) == set(ids)
    assert result["already_started"] == []
    assert result["failed"] == {}
    rows = [db.legacy_grace.find_by_account(i) for i in ids]
    assert {r["started_at"] for r in rows} == {12_345_000}
    assert {r["current_end_at"] for r in rows} == {12_345_000 + GRACE_PERIOD_SECONDS}


def test_cohort_start_duplicate_call_is_idempotent_no_restart(db):
    cap = _capability(db)
    ids = [_bootstrap(db, cap, f"client{i}")["account_id"] for i in range(70, 73)]
    start_grace_cohort(
        db, capability=cap, account_ids=ids, cohort_ref="PH4-05-DUP",
        reason="mass grace campaign", cohort_start_at=20_000_000,
    )
    second = start_grace_cohort(
        db, capability=cap, account_ids=ids, cohort_ref="PH4-05-DUP",
        reason="mass grace campaign retry", cohort_start_at=20_000_000,
    )
    assert second["started"] == []
    assert set(second["already_started"]) == set(ids)
    rows = [db.legacy_grace.find_by_account(i) for i in ids]
    assert {r["started_at"] for r in rows} == {20_000_000}


def test_cohort_partial_batch_then_retry_completes_the_rest(db):
    cap = _capability(db)
    ids = [_bootstrap(db, cap, f"client{i}")["account_id"] for i in range(80, 84)]
    first_batch = ids[:2]
    rest = ids[2:]
    start_grace_cohort(
        db, capability=cap, account_ids=first_batch, cohort_ref="PH4-05-PARTIAL",
        reason="mass grace campaign batch 1", cohort_start_at=30_000_000,
    )
    result = start_grace_cohort(
        db, capability=cap, account_ids=ids, cohort_ref="PH4-05-PARTIAL",
        reason="mass grace campaign retry, all members", cohort_start_at=30_000_000,
    )
    assert set(result["started"]) == set(rest)
    assert set(result["already_started"]) == set(first_batch)
    rows = [db.legacy_grace.find_by_account(i) for i in ids]
    assert {r["started_at"] for r in rows} == {30_000_000}


def test_cohort_start_after_process_restart_keeps_same_boundary(monkeypatch):
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
    cap = _capability(instance)
    ids = [_bootstrap(instance, cap, f"client{i}")["account_id"] for i in range(90, 92)]
    start_grace_cohort(
        instance, capability=cap, account_ids=ids, cohort_ref="PH4-05-RESTART",
        reason="mass grace campaign", cohort_start_at=40_000_000,
    )
    instance._conn.close()

    reopened = database.Database()
    rows = [reopened.legacy_grace.find_by_account(i) for i in ids]
    assert {r["started_at"] for r in rows} == {40_000_000}
    reopened._conn.close()
