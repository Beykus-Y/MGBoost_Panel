import importlib
import os
import tempfile

import pytest

from src.security import AdminSessionStore

PRIMARY = "wl-reset-admin-actor"
PRIMARY_LOGIN = "wl-reset-admin-login"


@pytest.fixture
def db(monkeypatch):
    tmp = tempfile.mkdtemp()
    monkeypatch.setenv("DATA_DIR", tmp)
    monkeypatch.setenv("PRIMARY_MGBOOST_ADMIN_ACTOR_ID", PRIMARY)
    monkeypatch.setenv("PRIMARY_MGBOOST_ADMIN_LOGIN", PRIMARY_LOGIN)
    import src.config as cfg
    import src.database as db_mod
    importlib.reload(cfg)
    importlib.reload(db_mod)
    db_mod.DB_PATH = os.path.join(tmp, "db.sqlite3")
    instance = db_mod.Database()
    yield instance
    instance._conn.close()


@pytest.fixture(autouse=True)
def seeded_catalog(db):
    from src.plan_catalog import seed_plan_catalog
    seed_plan_catalog(db.plan_catalog, now=100)
    return db


def _capability(db):
    _raw, session = AdminSessionStore().create(PRIMARY_LOGIN, "wl-period-reset-test-jwt")
    return db.primary_admin_authority.authorize_session(session)


def test_admin_reset_closes_old_period_and_creates_successor(db):
    account = db.accounts.create_account("DIRECT", now=1)
    purchase = db.subscription_renewal.apply_same_plan_purchase(
        account_id=account["id"], plan_code="WL", duration_days=30,
        payment_channel="TELEGRAM_STARS", mutation_source="DIRECT_PURCHASE",
        actor_type="TELEGRAM_USER", idempotency_key="reset-key-0001-xxx", now=1_000,
    )
    period = purchase["wl_periods"][0]
    period_id = db._conn.execute(
        "SELECT id FROM mgboost_wl_periods WHERE subscription_id=? AND sequence_no=?",
        (purchase["subscription_id"], period["sequence_no"]),
    ).fetchone()[0]
    # mark ACTIVE, as a live period would be by the time an admin resets it
    db._conn.execute("UPDATE mgboost_wl_periods SET status='ACTIVE' WHERE id=?", (period_id,))
    db._conn.commit()

    cap = _capability(db)
    result = db.wl_period_admin_reset.reset_period(
        capability=cap, period_id=period_id, reason="support ticket #123", now=50_000,
    )

    old_row = db._conn.execute("SELECT * FROM mgboost_wl_periods WHERE id=?", (period_id,)).fetchone()
    assert old_row["status"] == "CLOSED"
    # identity/quota fields untouched (PH5-02 immutability trigger would abort otherwise)
    assert old_row["starts_at"] == period["starts_at"]
    assert old_row["ends_at"] == period["ends_at"]

    successor = db._conn.execute(
        "SELECT * FROM mgboost_wl_periods WHERE id=?", (result["successor_period_id"],)
    ).fetchone()
    assert successor["status"] == "ACTIVE"
    assert successor["ends_at"] == old_row["ends_at"]  # never extends past original schedule
    assert successor["starts_at"] % 3600 == 0  # UTC-hour-aligned like every WL period
    assert successor["base_quota_bytes"] == old_row["base_quota_bytes"]
    assert successor["sequence_no"] == period["sequence_no"] + 1

    reset_row = db._conn.execute(
        "SELECT * FROM mgboost_wl_period_resets WHERE closed_period_id=?", (period_id,)
    ).fetchone()
    assert reset_row["successor_period_id"] == result["successor_period_id"]
    assert reset_row["reason"] == "support ticket #123"


def test_admin_reset_cannot_be_applied_twice_to_the_same_period(db):
    from src.wl_period_admin_reset import PeriodNotResettable

    account = db.accounts.create_account("DIRECT", now=1)
    purchase = db.subscription_renewal.apply_same_plan_purchase(
        account_id=account["id"], plan_code="WL", duration_days=30,
        payment_channel="TELEGRAM_STARS", mutation_source="DIRECT_PURCHASE",
        actor_type="TELEGRAM_USER", idempotency_key="reset-key-0002-xxx", now=1_000,
    )
    period_id = db._conn.execute(
        "SELECT id FROM mgboost_wl_periods WHERE subscription_id=?",
        (purchase["subscription_id"],),
    ).fetchone()[0]

    cap = _capability(db)
    db.wl_period_admin_reset.reset_period(capability=cap, period_id=period_id, reason="first reset", now=50_000)
    with pytest.raises(PeriodNotResettable):
        db.wl_period_admin_reset.reset_period(capability=cap, period_id=period_id, reason="second reset", now=60_000)


def test_admin_reset_refuses_an_already_closed_period(db):
    from src.wl_period_admin_reset import PeriodNotResettable

    account = db.accounts.create_account("DIRECT", now=1)
    purchase = db.subscription_renewal.apply_same_plan_purchase(
        account_id=account["id"], plan_code="WL", duration_days=30,
        payment_channel="TELEGRAM_STARS", mutation_source="DIRECT_PURCHASE",
        actor_type="TELEGRAM_USER", idempotency_key="reset-key-0003-xxx", now=1_000,
    )
    period_id = db._conn.execute(
        "SELECT id FROM mgboost_wl_periods WHERE subscription_id=?",
        (purchase["subscription_id"],),
    ).fetchone()[0]
    db._conn.execute("UPDATE mgboost_wl_periods SET status='CLOSED' WHERE id=?", (period_id,))
    db._conn.commit()

    cap = _capability(db)
    with pytest.raises(PeriodNotResettable):
        db.wl_period_admin_reset.reset_period(capability=cap, period_id=period_id, reason="x", now=50_000)


def test_admin_reset_requires_primary_admin_capability(db):
    from src.wl_period_admin_reset import PrimaryAdminRequired

    account = db.accounts.create_account("DIRECT", now=1)
    purchase = db.subscription_renewal.apply_same_plan_purchase(
        account_id=account["id"], plan_code="WL", duration_days=30,
        payment_channel="TELEGRAM_STARS", mutation_source="DIRECT_PURCHASE",
        actor_type="TELEGRAM_USER", idempotency_key="reset-key-0004-xxx", now=1_000,
    )
    period_id = db._conn.execute(
        "SELECT id FROM mgboost_wl_periods WHERE subscription_id=?",
        (purchase["subscription_id"],),
    ).fetchone()[0]

    with pytest.raises(PrimaryAdminRequired):
        db.wl_period_admin_reset.reset_period(capability=object(), period_id=period_id, reason="x", now=50_000)


def test_admin_reset_refuses_when_reset_time_at_or_past_period_end(db):
    from src.wl_period_admin_reset import PeriodNotResettable

    account = db.accounts.create_account("DIRECT", now=1)
    purchase = db.subscription_renewal.apply_same_plan_purchase(
        account_id=account["id"], plan_code="WL", duration_days=30,
        payment_channel="TELEGRAM_STARS", mutation_source="DIRECT_PURCHASE",
        actor_type="TELEGRAM_USER", idempotency_key="reset-key-0005-xxx", now=1_000,
    )
    period = purchase["wl_periods"][0]
    period_id = db._conn.execute(
        "SELECT id FROM mgboost_wl_periods WHERE subscription_id=?",
        (purchase["subscription_id"],),
    ).fetchone()[0]

    cap = _capability(db)
    with pytest.raises(PeriodNotResettable):
        db.wl_period_admin_reset.reset_period(
            capability=cap, period_id=period_id, reason="too late", now=period["ends_at"],
        )


def test_admin_reset_audit_rows_are_append_only(db):
    account = db.accounts.create_account("DIRECT", now=1)
    purchase = db.subscription_renewal.apply_same_plan_purchase(
        account_id=account["id"], plan_code="WL", duration_days=30,
        payment_channel="TELEGRAM_STARS", mutation_source="DIRECT_PURCHASE",
        actor_type="TELEGRAM_USER", idempotency_key="reset-key-0006-xxx", now=1_000,
    )
    period_id = db._conn.execute(
        "SELECT id FROM mgboost_wl_periods WHERE subscription_id=?",
        (purchase["subscription_id"],),
    ).fetchone()[0]
    cap = _capability(db)
    db.wl_period_admin_reset.reset_period(capability=cap, period_id=period_id, reason="x", now=50_000)

    with pytest.raises(Exception):
        db._conn.execute("UPDATE mgboost_wl_period_resets SET reason='changed'")
    with pytest.raises(Exception):
        db._conn.execute("DELETE FROM mgboost_wl_period_resets")
