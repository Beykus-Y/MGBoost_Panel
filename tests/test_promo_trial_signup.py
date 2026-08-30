"""NEW USER TRIAL SIGNUP: self-service `TRIAL_GRANT` с `trial_class=WL_TRIAL`
бутстрапит совершенно новому Telegram-пользователю ровно один canonical
DIRECT account + OWNER identity + полноценный WL_TRIAL entitlement через
СУЩЕСТВУЮЩИЕ primitives (`direct_account_bootstrap`, `append_promo_wl_period`,
`device_slots`, `subscription_credentials`).

Инварианты под тестом:
* узкая policy: бутстрап разрешён ТОЛЬКО для trial_class=WL_TRIAL;
* ноль мусорных аккаунтов: любой отказ происходит до создания аккаунта;
* ноль финансовых строк: trial бесплатен (ADMIN_GRANT provenance);
* анти-абьюз: один trial на ownership identity на весь класс;
* crash/retry convergence по существующим idempotency-паттернам;
* PURCHASE_DISCOUNT / EXTEND_SUBSCRIPTION для нового пользователя остаются
  fail-closed."""
import importlib
import os
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from src.promo import PromoConflict, PromoError, PromoIneligible, PromoNotFound
from src.security import AdminSessionStore

PRIMARY = "owner:mgboost-primary:v1"
PRIMARY_LOGIN = "authenticated-primary-login"
HWID_KEY = b"ph3-02-test-key-material-at-least-32-bytes"


@pytest.fixture
def db(monkeypatch):
    tmp = tempfile.mkdtemp(prefix="promo-trial-signup-")
    monkeypatch.setenv("DATA_DIR", tmp)
    monkeypatch.setenv("PRIMARY_MGBOOST_ADMIN_ACTOR_ID", PRIMARY)
    monkeypatch.setenv("PRIMARY_MGBOOST_ADMIN_LOGIN", PRIMARY_LOGIN)
    import src.config as config
    import src.database as database
    importlib.reload(config)
    importlib.reload(database)
    database.DB_PATH = os.path.join(tmp, "db.sqlite3")
    instance = database.Database()
    from src.plan_catalog import seed_plan_catalog
    from src.promo import ensure_wl_trial_plan_version
    seed_plan_catalog(instance.plan_catalog, now=1)
    ensure_wl_trial_plan_version(instance.accounts, now=1)
    yield instance
    instance._conn.close()


def _capability(db):
    _raw, session = AdminSessionStore().create(PRIMARY_LOGIN, "test-server-jwt")
    return db.primary_admin_authority.authorize_session(session)


def _define(db, cap, *, code, effect_kind, trial_class=None, effect_params):
    return db.promo.create_definition(
        cap, code=code, effect_kind=effect_kind, trial_class=trial_class,
        effect_params=effect_params, reason="trial signup test definition",
        idempotency_key=f"promo-def-{code}-000000000001", now=1_000,
    )


def _trial_def(db, code, trial_class="WL_TRIAL"):
    return _define(db, _capability(db), code=code, effect_kind="TRIAL_GRANT",
                   trial_class=trial_class, effect_params={"days": 1})


def _redeem(db, code, telegram_id, key, now=5_000):
    return db.promo.redeem_for_telegram_user(
        code=code, telegram_id=telegram_id, idempotency_key=key, now=now,
    )


def _account_count(db):
    return db._conn.execute("SELECT COUNT(*) c FROM mgboost_accounts").fetchone()["c"]


def _identity_count(db, telegram_id):
    return db._conn.execute(
        "SELECT COUNT(*) c FROM mgboost_telegram_identities WHERE telegram_id=?",
        (telegram_id,),
    ).fetchone()["c"]


def _redemption_count(db):
    return db._conn.execute(
        "SELECT COUNT(*) c FROM mgboost_promo_redemptions"
    ).fetchone()["c"]


# --- 1-2: ровно один DIRECT account + одна OWNER identity ---------------------

def test_fresh_trial_bootstrap_exactly_one_account_and_owner(db):
    _trial_def(db, "TRIALA1")
    result = _redeem(db, "TRIALA1", 610001, "trial-signup-a1-00000000001")

    assert result["status"] == "REDEEMED"
    assert result["already_applied"] is False
    assert _account_count(db) == 1
    assert _identity_count(db, 610001) == 1
    account = db.accounts.get_active_account_by_telegram_id(610001)
    assert account is not None and account["account_source"] == "DIRECT"
    identity = db._conn.execute(
        "SELECT * FROM mgboost_telegram_identities WHERE telegram_id=?", (610001,),
    ).fetchone()
    assert identity["role"] == "OWNER"
    assert identity["revoked_at"] is None
    assert identity["provenance"] == "DIRECT_BIND"
    # provisioning wiring: PRIMARY template alias + review + template job
    alias = db._conn.execute(
        "SELECT * FROM mgboost_legacy_account_aliases WHERE account_id=?",
        (account["id"],),
    ).fetchone()
    assert alias["alias_role"] == "PRIMARY"
    assert alias["legacy_username"] == f"tpl-{account['public_id']}"
    assert alias["ownership_provenance"] == "EVIDENCE_PROVEN"
    review = db._conn.execute(
        "SELECT * FROM mgboost_direct_account_reviews WHERE account_id=?",
        (account["id"],),
    ).fetchone()
    assert review["ownership_evidence"] == "PROVEN"
    job = db._conn.execute(
        "SELECT * FROM mgboost_admin_grant_template_jobs WHERE account_id=?",
        (account["id"],),
    ).fetchone()
    assert job is not None and job["state"] == "PENDING"
    assert job["decision_ref"].startswith("promo-trial-v1:")


# --- 3-7: канонический WL_TRIAL контракт --------------------------------------

def test_wl_trial_entitlement_contract_shape(db):
    _trial_def(db, "TRIALA2")
    result = _redeem(db, "TRIALA2", 610002, "trial-signup-a2-00000000001")
    effect = result["effect_result"]

    account = db.accounts.get_active_account_by_telegram_id(610002)
    subscription = db._conn.execute(
        "SELECT s.*, pv.plan_code, pv.device_limit_mode, pv.device_limit, "
        "pv.wl_mode, pv.wl_quota_bytes, pv.billing_required "
        "FROM mgboost_subscriptions s JOIN mgboost_plan_versions pv "
        "ON pv.id=s.current_plan_version_id WHERE s.account_id=?",
        (account["id"],),
    ).fetchone()
    # 3: именно WL_TRIAL
    assert subscription["plan_code"] == "WL_TRIAL"
    assert subscription["status"] == "ACTIVE"
    assert subscription["billing_required"] == 0
    # 4: duration = 1 день
    assert effect["days"] == 1
    assert subscription["current_expiry"] - effect["anchor"] == 86400
    # 5: device_limit = 1
    assert subscription["device_limit_mode"] == "LIMITED"
    assert subscription["device_limit"] == 1
    # 6: wl_mode = LIMITED
    assert subscription["wl_mode"] == "LIMITED"
    # 7: quota = 10 GB decimal
    assert effect["quota_bytes"] == 10_000_000_000
    period = db._conn.execute(
        "SELECT * FROM mgboost_wl_periods WHERE account_id=?", (account["id"],),
    ).fetchone()
    assert period["base_quota_bytes"] == 10_000_000_000
    assert period["quota_mode"] == "LIMITED"
    assert period["ends_at"] - period["starts_at"] == 86400
    term = db._conn.execute(
        "SELECT * FROM mgboost_subscription_terms WHERE subscription_id=?",
        (subscription["id"],),
    ).fetchone()
    assert term["billing_required_snapshot"] == 0
    assert term["wl_quota_bytes_snapshot"] == 10_000_000_000


def test_trial_cannot_switch_to_a_later_wl_trial_plan_version(db):
    """A later row with the same plan_code must not become the free-trial
    entitlement merely because it sorts later than the reviewed v1 row."""
    db.accounts.create_plan_version({
        "plan_code": "WL_TRIAL", "version": 2, "display_name": "WL Trial v2",
        "plan_kind": "COMMERCIAL", "billing_required": False,
        "device_limit_mode": "LIMITED", "device_limit": 1,
        "wl_mode": "LIMITED", "wl_quota_bytes": 10_000_000_000,
        "wl_period_days": 1,
        "terms": {"catalog": "unreviewed-wl-trial-v2", "device_limit": 1, "wl_quota_gb": 10},
    }, now=2)
    _trial_def(db, "TRIALA2V")
    result = _redeem(db, "TRIALA2V", 6100021, "trial-signup-a2v-000000001")
    version = db._conn.execute(
        "SELECT pv.version FROM mgboost_subscriptions s JOIN mgboost_plan_versions pv "
        "ON pv.id=s.current_plan_version_id WHERE s.account_id=?", (result["account_id"],),
    ).fetchone()["version"]
    assert version == 1


# --- 8: ноль финансовых строк --------------------------------------------------

def test_trial_is_free_zero_financial_rows(db):
    _trial_def(db, "TRIALA3")
    _redeem(db, "TRIALA3", 610003, "trial-signup-a3-00000000001")

    assert db._conn.execute("SELECT COUNT(*) c FROM stars_invoices").fetchone()["c"] == 0
    assert db._conn.execute(
        "SELECT COUNT(*) c FROM mgboost_manual_payment_records").fetchone()["c"] == 0
    mutation = db._conn.execute(
        "SELECT * FROM mgboost_entitlement_mutations "
        "WHERE operation='PROMO_TRIAL_GRANT'"
    ).fetchone()
    assert mutation is not None
    assert mutation["payment_channel"] == "ADMIN_GRANT"
    assert mutation["actor_type"] == "TELEGRAM_USER"
    assert mutation["external_reference"] is None


# --- 9-12: отказы НИКОГДА не создают аккаунт -----------------------------------

def test_invalid_code_creates_no_account(db):
    with pytest.raises(PromoNotFound):
        _redeem(db, "NOSUCH1", 610004, "trial-signup-invalid-0000001")
    assert _account_count(db) == 0
    assert _identity_count(db, 610004) == 0
    assert _redemption_count(db) == 0


def test_disabled_code_creates_no_account(db):
    cap = _capability(db)
    _define(db, cap, code="TRIALDIS", effect_kind="TRIAL_GRANT",
            trial_class="WL_TRIAL", effect_params={"days": 1})
    db.promo.disable_definition(cap, code="TRIALDIS", reason="trial disabled by test",
                                now=2_000)
    with pytest.raises(PromoNotFound):
        _redeem(db, "TRIALDIS", 610005, "trial-signup-dis-0000000001")
    assert _account_count(db) == 0
    assert _identity_count(db, 610005) == 0
    assert _redemption_count(db) == 0


def test_non_wl_trial_class_stays_fail_closed_no_account(db):
    _trial_def(db, "TRIALOTHR", trial_class="SOME_OTHER_CLASS")
    with pytest.raises(PromoNotFound):
        _redeem(db, "TRIALOTHR", 610006, "trial-signup-oth-0000000001")
    assert _account_count(db) == 0
    assert _identity_count(db, 610006) == 0
    assert _redemption_count(db) == 0


def test_malformed_wl_trial_definition_cannot_bootstrap(db):
    """`WL_TRIAL` is not a label permitting author-selected free terms."""
    _define(db, _capability(db), code="TRIALBAD", effect_kind="TRIAL_GRANT",
            trial_class="WL_TRIAL", effect_params={"days": 2})
    with pytest.raises(PromoNotFound, match="exact WL_TRIAL"):
        _redeem(db, "TRIALBAD", 6100061, "trial-signup-malformed-00001")
    assert _account_count(db) == 0
    assert _identity_count(db, 6100061) == 0
    assert _redemption_count(db) == 0


def test_purchase_discount_new_user_fail_closed_no_account(db):
    _define(db, _capability(db), code="DISCNEW", effect_kind="PURCHASE_DISCOUNT",
            effect_params={"discount_percent": 50})
    # store-level: PURCHASE_DISCOUNT не проходим через redeem_* (PromoError);
    # bot-level fail-closed (PromoNotFound из reservation) закреплён в
    # test_bot_onboarding_promo.py::test_new_user_promo_fail_closed_...
    with pytest.raises(PromoError):
        _redeem(db, "DISCNEW", 610007, "trial-signup-disc-0000000001")
    assert _account_count(db) == 0
    assert db._conn.execute(
        "SELECT COUNT(*) c FROM mgboost_promo_redemptions "
        "WHERE status='RESERVED'"
    ).fetchone()["c"] == 0


def test_extend_new_user_fail_closed_no_account(db):
    _define(db, _capability(db), code="EXTNEW", effect_kind="EXTEND_SUBSCRIPTION",
            effect_params={"days": 7})
    with pytest.raises(PromoNotFound):
        _redeem(db, "EXTNEW", 610008, "trial-signup-ext-00000000001")
    assert _account_count(db) == 0
    assert _identity_count(db, 610008) == 0


# --- 13: duplicate Telegram update converge ------------------------------------

def test_duplicate_same_idempotency_key_converges(db):
    _trial_def(db, "TRIALA4")
    key = "trial-signup-dup-0000000001"
    first = _redeem(db, "TRIALA4", 610009, key)
    replay = _redeem(db, "TRIALA4", 610009, key)

    assert first["already_applied"] is False
    assert replay["already_applied"] is True
    assert replay["redemption_id"] == first["redemption_id"]
    assert _account_count(db) == 1
    assert _identity_count(db, 610009) == 1
    assert _redemption_count(db) == 1
    assert db._conn.execute(
        "SELECT COUNT(*) c FROM mgboost_wl_periods").fetchone()["c"] == 1


# --- 14: конкурентные redemption ------------------------------------------------

def test_concurrent_redemptions_produce_one_account_and_one_redemption(db):
    _trial_def(db, "TRIALA5")
    barrier = threading.Barrier(2)

    def redeem(tag):
        barrier.wait()
        try:
            return _redeem(db, "TRIALA5", 610010,
                           f"trial-signup-conc-{tag}-0000001")
        except PromoError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(redeem, ["a", "b"]))

    succeeded = [r for r in results if r is not None]
    assert len(succeeded) == 1
    assert _account_count(db) == 1
    assert _identity_count(db, 610010) == 1
    assert _redemption_count(db) == 1
    assert db._conn.execute(
        "SELECT COUNT(*) c FROM mgboost_wl_periods").fetchone()["c"] == 1


def test_separate_db_connections_cannot_orphan_second_account(db):
    """The SQLite write transaction, rather than the process-local RLock,
    serializes two independently constructed Database instances."""
    _trial_def(db, "TRIALA5B")
    import src.database as database
    second = database.Database()
    barrier = threading.Barrier(2)

    def redeem(store, suffix):
        barrier.wait()
        try:
            return store.promo.redeem_for_telegram_user(
                code="TRIALA5B", telegram_id=6100101,
                idempotency_key=f"trial-signup-separate-{suffix}-00001", now=5_000,
            )
        except PromoError:
            return None

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(
                lambda pair: redeem(*pair), ((db, "a"), (second, "b")),
            ))
    finally:
        second._conn.close()

    assert len([result for result in results if result is not None]) == 1
    assert _account_count(db) == 1
    assert _identity_count(db, 6100101) == 1
    assert db._conn.execute(
        "SELECT COUNT(*) c FROM mgboost_legacy_account_aliases"
    ).fetchone()["c"] == 1
    assert db._conn.execute(
        "SELECT COUNT(*) c FROM mgboost_wl_periods"
    ).fetchone()["c"] == 1


# --- 15: анти-абьюз по trial_class, не по строке кода ---------------------------

def test_second_different_code_same_trial_class_rejected(db):
    """Отклонение должно прийти ИМЕННО от trial-class unique contract, а не
    от «есть активная подписка» -- поэтому второй код подаётся ПОСЛЕ
    истечения первого trial (now за пределами его срока)."""
    _trial_def(db, "TRIALA6")
    _trial_def(db, "TRIALB6")
    _redeem(db, "TRIALA6", 610011, "trial-signup-b6-a-0000000001")

    long_after_expiry = 5_000 + 3 * 86400
    with pytest.raises(PromoIneligible, match="already redeemed"):
        _redeem(db, "TRIALB6", 610011, "trial-signup-b6-b-0000000001",
                now=long_after_expiry)

    assert _account_count(db) == 1
    assert _redemption_count(db) == 1
    assert db._conn.execute(
        "SELECT COUNT(*) c FROM mgboost_wl_periods").fetchone()["c"] == 1


# --- 16: active subscription → отказ -------------------------------------------

def test_trial_rejected_for_active_subscription(db):
    _trial_def(db, "TRIALA7")
    account = db.accounts.create_account("DIRECT", now=1)
    db.accounts.link_telegram_owner(account["id"], 610012, provenance="DIRECT_BIND",
                                    actor="test", now=1)
    db.subscription_renewal.apply_same_plan_purchase(
        account_id=account["id"], plan_code="BASIC", duration_days=30,
        payment_channel="TELEGRAM_STARS", mutation_source="DIRECT_PURCHASE",
        actor_type="TEST", idempotency_key="trial-active-sub-key-00001", now=4_000,
    )
    with pytest.raises(PromoIneligible, match="active subscription"):
        _redeem(db, "TRIALA7", 610012, "trial-signup-a7-00000000001")
    assert _account_count(db) == 1  # существующий аккаунт, второго нет
    # BASIC -- STANDARD-план (wl_mode NONE): только его собственный оплаченный
    # term, никаких trial-периодов не добавлено
    assert db._conn.execute(
        "SELECT COUNT(*) c FROM mgboost_wl_periods WHERE account_id=?",
        (account["id"],),
    ).fetchone()["c"] == 0


# --- 17-18: crash после bootstrap / около apply boundary ------------------------

def test_failure_after_bootstrap_retry_converges_no_duplicate(db, monkeypatch):
    """A: apply упал после bootstrap+redemption(PENDING_APPLY) -> retry с тем
    же ключом сходится к ровно одному аккаунту/period'у, без дублей."""
    _trial_def(db, "TRIALA8")
    key = "trial-signup-a8-00000000001"
    original = db.promo._apply_effect
    monkeypatch.setattr(
        db.promo, "_apply_effect",
        lambda **kwargs: (_ for _ in ()).throw(PromoError("crash after bootstrap")),
    )
    with pytest.raises(PromoError):
        _redeem(db, "TRIALA8", 610013, key)
    # durable intent зафиксирован, эффект не применён
    assert _account_count(db) == 1
    row = db._conn.execute(
        "SELECT status FROM mgboost_promo_redemptions").fetchone()
    assert row["status"] == "PENDING_APPLY"
    assert db._conn.execute(
        "SELECT COUNT(*) c FROM mgboost_wl_periods").fetchone()["c"] == 0

    monkeypatch.setattr(db.promo, "_apply_effect", original)
    retry = _redeem(db, "TRIALA8", 610013, key)
    assert retry["status"] == "REDEEMED" and retry["already_applied"] is False
    assert _account_count(db) == 1
    assert _redemption_count(db) == 1
    assert db._conn.execute(
        "SELECT COUNT(*) c FROM mgboost_wl_periods").fetchone()["c"] == 1
    assert db._conn.execute(
        "SELECT status FROM mgboost_promo_redemptions").fetchone()["status"] == "REDEEMED"


def test_crash_between_bootstrap_and_redemption_retry_reuses_account(db):
    """D: crash сразу после bootstrap (аккаунт есть, redemption ещё нет) ->
    retry переиспользует ТОТ ЖЕ аккаунт, второй не создаётся."""
    from src.direct_account_bootstrap import ensure_direct_account

    _trial_def(db, "TRIALA9")
    # симуляция упавшей попытки: bootstrap дошёл до конца, redemption нет
    ensure_direct_account(
        db._conn, db._lock,
        telegram_id=610014, actor="telegram:610014",
        decision_ref="promo-trial-v1:crashsim0000000000000000000001", now=4_000,
        bootstrap_policy="PROMO_TRIAL",
    )
    assert _account_count(db) == 1

    result = _redeem(db, "TRIALA9", 610014, "trial-signup-a9-00000000001")
    assert result["status"] == "REDEEMED"
    assert _account_count(db) == 1
    assert _identity_count(db, 610014) == 1
    account = db.accounts.get_active_account_by_telegram_id(610014)
    assert result["account_id"] == account["id"]


# --- 19: DB reopen / process-restart style --------------------------------------

def test_db_reopen_replays_without_duplicates(db):
    _trial_def(db, "TRIALB1")
    key = "trial-signup-b1-00000000001"
    first = _redeem(db, "TRIALB1", 610015, key)
    # reopen: тот же файл БД, новый Database()
    import src.database as database
    conn_path = database.DB_PATH
    db._conn.close()
    reopened = database.Database()
    try:
        replay = reopened.promo.redeem_for_telegram_user(
            code="TRIALB1", telegram_id=610015, idempotency_key=key, now=9_000,
        )
        assert replay["already_applied"] is True
        assert replay["redemption_id"] == first["redemption_id"]
        assert _account_count(reopened) == 1
        assert _identity_count(reopened, 610015) == 1
        assert _redemption_count(reopened) == 1
        assert reopened._conn.execute(
            "SELECT COUNT(*) c FROM mgboost_wl_periods").fetchone()["c"] == 1
    finally:
        reopened._conn.close()
        database.DB_PATH = conn_path


# --- 20-23: полный VPN journey ---------------------------------------------------

def _issue_first_credential(db, account_id):
    """Тот же паттерн, что bot `_link_entry`/`_issue_new_credential`."""
    prepared = db.subscription_credentials.prepare(
        account_id=account_id, actor_ref="telegram:610016",
        reason="trial first link issuance", idempotency_key="trial-cred-prep-00000001",
        now=6_000,
    )
    activated = db.subscription_credentials.activate(
        credential_id=prepared["id"], account_id=account_id,
        expected_generation=prepared["generation"], actor_ref="telegram:610016",
        idempotency_key="trial-cred-act-000000001", now=6_000,
    )
    return prepared, activated


def test_trial_user_obtains_opaque_credential(db):
    _trial_def(db, "TRIALB2")
    result = _redeem(db, "TRIALB2", 610016, "trial-signup-b2-00000000001")
    prepared, activated = _issue_first_credential(db, result["account_id"])

    assert activated["status"] == "ACTIVE"
    resolved = db.subscription_credentials.resolve(prepared["raw_token"], now=6_100)
    assert resolved is not None and resolved["account_id"] == result["account_id"]


def test_first_hwid_claims_canonical_slot_and_is_idempotent(db):
    _trial_def(db, "TRIALB3")
    result = _redeem(db, "TRIALB3", 610017, "trial-signup-b3-00000000001")
    account_id = result["account_id"]
    raw_hwid = "hwid-abc123device-610017"

    first = db.device_slots.claim(account_id, raw_hwid, HWID_KEY, now=6_000)
    assert first["result"] == "CLAIMED"
    assert first["slot_number"] == 1  # device_limit=1 -> ровно один слот

    second = db.device_slots.claim(account_id, raw_hwid, HWID_KEY, now=6_100)
    assert second["result"] == "EXISTING"
    assert second["slot_number"] == first["slot_number"]
    assert db._conn.execute(
        "SELECT COUNT(*) c FROM mgboost_device_slots WHERE account_id=?",
        (account_id,),
    ).fetchone()["c"] == 1

    from src.device_slots import CapacityReached
    with pytest.raises(CapacityReached):
        db.device_slots.claim(account_id, "hwid-other-device-610017", HWID_KEY, now=6_200)


def test_no_raw_token_or_hwid_anywhere_in_db(db):
    _trial_def(db, "TRIALB4")
    result = _redeem(db, "TRIALB4", 610018, "trial-signup-b4-00000000001")
    prepared, _activated = _issue_first_credential(db, result["account_id"])
    raw_hwid = "hwid-secret-raw-value-610018"
    db.device_slots.claim(result["account_id"], raw_hwid, HWID_KEY, now=6_000)

    # полная выгрузка всех TEXT значений БД: ни raw token, ни raw HWID
    dump = []
    tables = db._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    for table in tables:
        name = table["name"]
        for row in db._conn.execute(f"SELECT * FROM {name}"):
            dump.extend(str(value) for value in tuple(row))
    blob = "\n".join(dump)
    assert prepared["raw_token"] not in blob
    assert raw_hwid not in blob
