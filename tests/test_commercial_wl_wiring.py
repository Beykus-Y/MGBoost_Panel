"""Commercial WL wiring: WL / EXTENDED / FAMILY 30/60d become purchasable
through the EXISTING canonical Stars signup/renewal backend.

The gate that this suite widens is deliberately channel-level
(``commercial_signup.assert_plan_sellable``); plan data, prices, device
limits, WL quotas, the PH5-02 renewal engine (immutable 30-day WL periods),
the template provisioning pipeline and the PH6-06/07/09 runtime already
support LIMITED plans and are NOT reimplemented here. This suite pins:

  * the exact 12-SKU sellable matrix (6 plans x 30/60d, approved prices,
    device limits, exact decimal-GB quotas);
  * packages (WL_PACKAGE_*) stay server-side unpurchasable (PH6-08 absent);
  * a brand-new customer's Stars signup creates ONE canonical account, a
    LIMITED subscription, and immutable WL periods (30d = 1 period,
    60d = exactly 2 sequential full-quota periods, no remainder carry);
  * same-plan renewal appends periods chronologically (never resets usage,
    mutates history, or overlaps); a different-plan purchase keeps failing
    closed (PH5-06 absent);
  * the per-account provisioning template for a WL plan is STANDARD
    membership + EXACT PH0-05 WL topology only, while the STANDARD profile
    itself can still never contain a WL tag; a BASIC template stays
    STANDARD-only;
  * the PH6 enforcement machine converges a fresh commercial LIMITED
    account WITHOUT any manual step: the first INCLUDED decision settles
    by observation against the account's pinned provisioning template
    (hash-verified, allowlist-filtered), quota exhaustion removes WL
    exactly once, and a renewal restores it from the frozen baseline;
  * the real aiogram dispatcher payment path (pre_checkout +
    successful_payment) routes a WL signup invoice through capture_paid.
"""

import asyncio
import importlib
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tests.test_commercial_signup import (
    STANDARD_TAGS, BrokerBacked, _capture, _run_signup,
)
from tests.test_child_provisioning import HWID_KEY, PRIMARY, PRIMARY_LOGIN
from tests.test_marzban_broker import FakeMarzban
from tests.test_opaque_resolver import _get_sub, _known_hwid_meta
from tests.test_wl_enforcement import (
    NON_WL_A, NON_WL_B, WL_A, WL_B, WlBackedClient, _burn_quota,
    _inbounds_of, _modify_count, _ok_observer,
)
from src.wl_topology import WL_INBOUND_TAGS

GB = 1_000_000_000


@pytest.fixture
def db(monkeypatch):
    tmp = tempfile.mkdtemp()
    monkeypatch.setenv("DATA_DIR", tmp)
    monkeypatch.setenv("PUBLIC_HOST", "sub.example.test")
    monkeypatch.setenv("PRIMARY_MGBOOST_ADMIN_ACTOR_ID", PRIMARY)
    monkeypatch.setenv("PRIMARY_MGBOOST_ADMIN_LOGIN", PRIMARY_LOGIN)
    monkeypatch.setenv("DEVICE_SLOT_HMAC_KEY", HWID_KEY.split("hmac-sha256:")[-1] + "padpad")
    import src.config as config
    import src.database as database
    importlib.reload(config)
    importlib.reload(database)
    database.DB_PATH = os.path.join(tmp, "db.sqlite3")
    instance = database.Database()
    from src.plan_catalog import seed_plan_catalog
    seed_plan_catalog(instance.plan_catalog, now=1)
    instance.delivery_routing.ensure_defaults(now=1)
    for tag in STANDARD_TAGS:
        instance.delivery_routing.apply_host_change(
            None, profile_code="STANDARD", inbound_tag=tag, operation="ADD",
            reason="test baseline membership", idempotency_key=f"test-seed:{tag}:0001",
            observed_live_tags=STANDARD_TAGS, now=1, system_actor=True,
        )
    yield instance
    instance._conn.close()


@pytest.fixture
def broker():
    return BrokerBacked()


def _full_wl_delivery():
    return sorted(set(STANDARD_TAGS) | set(WL_INBOUND_TAGS))


def _subscription_plan(db, account_id):
    return db._conn.execute(
        "SELECT s.status,s.current_expiry,pv.plan_code,pv.wl_mode,pv.wl_quota_bytes,"
        "pv.wl_period_days,pv.device_limit "
        "FROM mgboost_subscriptions s JOIN mgboost_plan_versions pv "
        "ON pv.id=s.current_plan_version_id WHERE s.account_id=? ORDER BY s.id DESC LIMIT 1",
        (account_id,),
    ).fetchone()


def _periods(db, account_id):
    return [dict(r) for r in db._conn.execute(
        "SELECT * FROM mgboost_wl_periods WHERE account_id=? ORDER BY sequence_no",
        (account_id,),
    ).fetchall()]


def _assert_chronology(periods, *, period_days=30):
    """Sequential, contiguous, UTC-hour-aligned, full-quota, no overlap/gap."""
    assert periods
    span = period_days * 86400
    for i, period in enumerate(periods):
        assert period["quota_mode"] == "LIMITED"
        assert period["ends_at"] - period["starts_at"] == span
        assert period["starts_at"] % 3600 == 0
        if i:
            assert period["starts_at"] == periods[i - 1]["ends_at"]


# --- catalog / sellability ---------------------------------------------------------

def test_sellable_catalog_is_the_exact_twelve_sku_matrix(db):
    from src.plan_catalog import STARS_PRICES
    sellable = db.stars_purchases.sellable_catalog()
    got = {(item["plan_code"], item["duration_days"]): item["amount"] for item in sellable}
    assert got == STARS_PRICES
    assert len(sellable) == 12
    limits = {item["plan_code"]: item["device_limit"] for item in sellable}
    assert limits == {
        "BASIC": 3, "BASIC_PLUS": 6, "BASIC_PRO": 12,
        "WL": 3, "EXTENDED": 6, "FAMILY": 12,
    }
    quotas = {item["plan_code"]: item["wl_quota_bytes"] for item in sellable}
    assert quotas == {
        "BASIC": None, "BASIC_PLUS": None, "BASIC_PRO": None,
        "WL": 100 * GB, "EXTENDED": 150 * GB, "FAMILY": 150 * GB,
    }
    modes = {item["plan_code"]: item["wl_mode"] for item in sellable}
    assert modes == {
        "BASIC": "NONE", "BASIC_PLUS": "NONE", "BASIC_PRO": "NONE",
        "WL": "LIMITED", "EXTENDED": "LIMITED", "FAMILY": "LIMITED",
    }


@pytest.mark.parametrize("plan_code,duration,price", [
    ("WL", 30, 199), ("WL", 60, 349),
    ("EXTENDED", 30, 249), ("EXTENDED", 60, 399),
    ("FAMILY", 30, 299), ("FAMILY", 60, 449),
])
def test_all_six_wl_skus_are_sellable_for_brand_new_customers(db, plan_code, duration, price):
    invoice = db.stars_purchases.create_invoice(
        telegram_id=777000, plan_code=plan_code, duration_days=duration,
        ttl_seconds=3600, now=100,
    )
    assert invoice["invoice_kind"] == "CANONICAL_SIGNUP"
    assert invoice["stars_price"] == price
    assert invoice["account_id"] is None


def test_package_skus_stay_server_side_unpurchasable(db):
    from src.commercial_signup import PlanNotSellable
    sellable = {item["plan_code"] for item in db.stars_purchases.sellable_catalog()}
    assert not any(code.startswith("WL_PACKAGE") for code in sellable)
    for package_code in ("WL_PACKAGE_50_GB", "WL_PACKAGE_100_GB", "WL_PACKAGE_250_GB", "WL_PACKAGE_500_GB"):
        with pytest.raises(PlanNotSellable):
            db.stars_purchases.create_invoice(
                telegram_id=777000, plan_code=package_code, duration_days=30,
                ttl_seconds=3600, now=100,
            )
    assert db._conn.execute("SELECT COUNT(*) FROM stars_invoices").fetchone()[0] == 0
    assert db._conn.execute("SELECT COUNT(*) FROM mgboost_accounts").fetchone()[0] == 0


# --- signup: canonical LIMITED entitlement + immutable periods ----------------------

def test_wl_30d_signup_creates_limited_entitlement_and_one_period(db, broker):
    _invoice, account, applied = _run_signup(db, broker, 555000111, "WL", 30, now=100)
    sub = _subscription_plan(db, account["id"])
    assert sub["plan_code"] == "WL" and sub["wl_mode"] == "LIMITED"
    assert sub["device_limit"] == 3 and sub["wl_quota_bytes"] == 100 * GB
    assert sub["wl_period_days"] == 30
    assert sub["current_expiry"] == applied["new_expiry"]
    periods = _periods(db, account["id"])
    assert len(periods) == 1
    assert periods[0]["base_quota_bytes"] == 100 * GB
    _assert_chronology(periods)
    # The first (only) period covers the whole purchased term, hour-aligned.
    assert periods[0]["starts_at"] == 0 and periods[0]["ends_at"] == 30 * 86400


@pytest.mark.parametrize("plan_code,quota", [("WL", 100 * GB), ("FAMILY", 150 * GB)])
def test_wl_60d_signup_creates_exactly_two_full_quota_periods(db, broker, plan_code, quota):
    _invoice, account, applied = _run_signup(db, broker, 555000111, plan_code, 60, now=100)
    sub = _subscription_plan(db, account["id"])
    assert sub["wl_mode"] == "LIMITED"
    assert sub["current_expiry"] == applied["new_expiry"] == 120 + 60 * 86400
    periods = _periods(db, account["id"])
    assert len(periods) == 2
    _assert_chronology(periods)
    # Each period carries its OWN full quota; period B can never spend
    # period A's usage, and no merged 60-day bucket exists.
    for period in periods:
        assert period["base_quota_bytes"] == quota
    assert periods[0]["id"] != periods[1]["id"]
    assert applied["wl_periods"] == [
        {"sequence_no": p["sequence_no"], "starts_at": p["starts_at"], "ends_at": p["ends_at"]}
        for p in periods
    ]


def test_wl_renewal_30d_plus_30d_appends_without_touching_history(db, broker):
    _invoice1, account, _applied1 = _run_signup(db, broker, 555000111, "WL", 30, now=100)
    before = _periods(db, account["id"])
    invoice2, account2, applied2 = _run_signup(db, broker, 555000111, "WL", 30, now=200_000)
    assert account2["id"] == account["id"]
    after = _periods(db, account["id"])
    assert len(after) == 2
    # History is byte-identical; the renewal appends, never rewrites.
    assert after[0] == before[0]
    _assert_chronology(after)
    assert applied2["wl_periods"] == [
        {"sequence_no": after[1]["sequence_no"], "starts_at": after[1]["starts_at"],
         "ends_at": after[1]["ends_at"]},
    ]
    operation = db._conn.execute(
        "SELECT applied_operation FROM mgboost_stars_purchase_applications WHERE invoice_id=?",
        (invoice2["id"],),
    ).fetchone()
    assert operation["applied_operation"] == "RENEW"


def test_wl_renewal_30d_plus_60d_appends_two_more_periods(db, broker):
    _invoice1, account, _applied1 = _run_signup(db, broker, 555000111, "WL", 30, now=100)
    invoice2, _account2, applied2 = _run_signup(db, broker, 555000111, "WL", 60, now=200_000)
    assert applied2["already_applied"] is False
    periods = _periods(db, account["id"])
    assert len(periods) == 3
    _assert_chronology(periods)
    assert all(p["base_quota_bytes"] == 100 * GB for p in periods)


def test_existing_60d_renewal_keeps_chronology_contiguous(db, broker):
    _invoice1, account, _applied1 = _run_signup(db, broker, 555000111, "FAMILY", 60, now=100)
    # Renew while still active: anchor = current expiry, no overlap/gap.
    _invoice2, _account2, _applied2 = _run_signup(db, broker, 555000111, "FAMILY", 60, now=200_000)
    periods = _periods(db, account["id"])
    assert len(periods) == 4
    _assert_chronology(periods)
    assert all(p["base_quota_bytes"] == 150 * GB for p in periods)


def test_duplicate_payment_callback_and_apply_replay_converge(db, broker):
    invoice, account, _applied = _run_signup(db, broker, 555000111, "WL", 30, now=100)
    # Duplicate successful_payment delivery of the same charge.
    outcome = _capture(db, invoice, 555000111, charge_id=f"charge-{invoice['id']}", now=150)
    assert outcome == "duplicate"
    assert db._conn.execute("SELECT COUNT(*) FROM mgboost_accounts").fetchone()[0] == 1
    assert db._conn.execute("SELECT COUNT(*) FROM mgboost_subscriptions").fetchone()[0] == 1
    assert len(_periods(db, account["id"])) == 1
    # Worker replay of the same paid invoice is a no-op.
    replay = db.stars_purchases.apply_paid_invoice(invoice["id"], now=160)
    assert replay["already_applied"] is True
    assert len(_periods(db, account["id"])) == 1


def test_existing_basic_account_cannot_buy_wl_sku_fail_closed(db, broker):
    _invoice1, _account, _applied = _run_signup(db, broker, 555000111, "BASIC", 30, now=100)
    from src.stars_purchase import PlanChangeRequired
    with pytest.raises(PlanChangeRequired):
        db.stars_purchases.create_invoice(
            telegram_id=555000111, plan_code="WL", duration_days=30,
            ttl_seconds=3600, now=500,
        )
    assert db._conn.execute("SELECT COUNT(*) FROM stars_invoices").fetchone()[0] == 1
    assert _subscription_plan(db, _account["id"])["plan_code"] == "BASIC"


# --- first-device bootstrap / delivery profile --------------------------------------

def test_basic_signup_template_stays_standard_only(db, broker):
    _invoice, account, result = _run_signup(db, broker, 555000111, "BASIC", 30, now=100)
    result = db.commercial_signup.ensure_template_for_account(account["id"], marzban=broker, now=200)
    assert result["state"] == "READY"
    template = broker.get_user(f"tpl-{account['public_id']}")
    assert sorted(template["inbounds"]["vless"]) == sorted(STANDARD_TAGS)
    membership = db.delivery_routing.membership("STANDARD")
    assert not (set(membership) & set(WL_INBOUND_TAGS))


@pytest.mark.parametrize("plan_code", ["WL", "EXTENDED", "FAMILY"])
def test_wl_signup_template_is_standard_plus_exact_wl_topology(db, broker, plan_code):
    _invoice, account, _applied = _run_signup(db, broker, 555000111, plan_code, 30, now=100)
    result = db.commercial_signup.ensure_template_for_account(account["id"], marzban=broker, now=200)
    assert result["state"] == "READY"
    template = broker.get_user(f"tpl-{account['public_id']}")
    assert sorted(template["inbounds"]["vless"]) == _full_wl_delivery()
    # The STANDARD profile itself is still untouched by WL wiring.
    membership = db.delivery_routing.membership("STANDARD")
    assert sorted(membership) == sorted(STANDARD_TAGS)
    # The pinned hash really pins the STANDARD+WL contract.
    from src.child_contract import source_contract_hash
    pinned = db.commercial_signup.template_for_account(account["id"])
    assert pinned["source_contract_hash"] == source_contract_hash(template)


def test_wl_template_survives_outage_and_converges_idempotently(db, broker):
    _invoice, account, _applied = _run_signup(db, broker, 555000111, "EXTENDED", 30, now=100)

    class DownMarzban:
        def get_user(self, username, token=None):
            raise RuntimeError("broker down")

        def create_user(self, payload, token=None):
            raise RuntimeError("broker down")

    with pytest.raises(RuntimeError):
        db.commercial_signup.ensure_template_for_account(account["id"], marzban=DownMarzban(), now=200)
    assert db.commercial_signup.pending_template_jobs()[0]["account_id"] == account["id"]
    first = db.commercial_signup.ensure_template_for_account(account["id"], marzban=broker, now=300)
    assert first["state"] == "READY"
    rerun = db.commercial_signup.ensure_template_for_account(account["id"], marzban=broker, now=310)
    assert rerun["state"] == "READY" and rerun["already_pinned"] is True
    template = broker.get_user(f"tpl-{account['public_id']}")
    assert sorted(template["inbounds"]["vless"]) == _full_wl_delivery()


# --- PH6 runtime: the purchased LIMITED account is served automatically -------------

def _provision_first_child(db, broker, account, *, hwid="wl-wiring-hw-1"):
    prepared = db.subscription_credentials.prepare(
        account_id=account["id"], actor_ref="worker", reason="signup initial",
        idempotency_key=f"cred-prepare-{hwid}", now=300,
    )
    db.subscription_credentials.activate(
        credential_id=prepared["id"], account_id=account["id"],
        expected_generation=prepared["generation"], actor_ref="worker",
        idempotency_key=f"cred-activate-{hwid}", now=300,
    )
    from src.opaque_resolver import resolve_opaque_subscription
    from src.config import DEVICE_SLOT_HMAC_KEY
    result = resolve_opaque_subscription(
        db, prepared["raw_token"], _known_hwid_meta(hwid) | {"device_id": "device-one"},
        hmac_key=DEVICE_SLOT_HMAC_KEY,
        ensure_fn=broker.ensure_fn, subscription_fn=broker.subscription_fn,
        worker_id="wl-wiring-worker", now=400,
    )
    child = db._conn.execute(
        "SELECT child_username,id FROM mgboost_child_user_intents WHERE account_id=? "
        "ORDER BY id DESC LIMIT 1", (account["id"],),
    ).fetchone()
    assert child is not None
    return result, child["child_username"], child["id"]


def _fresh_usage_lease(db, *, now):
    db._conn.execute(
        "UPDATE mgboost_wl_usage_collector_lease SET last_run_started_at=?,"
        "last_run_completed_at=?,last_run_outcome=?,last_run_error_class=NULL WHERE id=1",
        (now - 5, now, "OK"),
    )
    db._conn.commit()


def test_commercial_wl_account_converges_disable_and_restore_through_ph6_runtime(db, broker):
    from src.wl_enforcement import run_wl_enforcement_cycle
    _invoice, account, _applied = _run_signup(db, broker, 555000111, "EXTENDED", 30, now=100)
    _result = db.commercial_signup.ensure_template_for_account(account["id"], marzban=broker, now=200)
    db.commercial_signup.record_template_result(account["id"], state="READY", now=201)
    _sub, child_username, child_intent_id = _provision_first_child(db, broker, account)

    # Born INCLUDED: the first enforcement cycle settles the fresh account by
    # observation against its pinned template -- zero remote mutations, no
    # ERROR_RECONCILE, WL delivery intact (STANDARD + exact WL).
    _fresh_usage_lease(db, now=900)
    summary = run_wl_enforcement_cycle(
        db=db, service_marzban=WlBackedClient(broker.remote), worker_id="wl-wiring",
        now=1000, topology_observer=_ok_observer(),
    )
    state = db.wl_enforcement.get_state(account["id"])
    assert state is not None and state["state"] == "ACTIVE"
    assert summary["accounts_error_reconcile"] == 0
    assert sorted(_inbounds_of(broker.remote, child_username)) == _full_wl_delivery()
    assert _modify_count(broker.remote, child_username) == 0

    # Quota exhaustion (150 GB plan, burn 160 GB) removes the WL inbounds
    # exactly once -- through the SAME engine, no purchase-path involvement.
    period_id = _periods(db, account["id"])[0]["id"]
    _burn_quota(db, account_id=account["id"], child_intent_id=child_intent_id,
                period_id=period_id, total_bytes=160 * GB,
                collected_at=2000 - 100)
    _fresh_usage_lease(db, now=2000)
    summary = run_wl_enforcement_cycle(
        db=db, service_marzban=WlBackedClient(broker.remote), worker_id="wl-wiring",
        now=2000, topology_observer=_ok_observer(),
    )
    state = db.wl_enforcement.get_state(account["id"])
    assert state["state"] == "DISABLED"
    assert sorted(_inbounds_of(broker.remote, child_username)) == sorted(STANDARD_TAGS)
    assert summary["accounts_disabled"] == 1

    # Renewal (+30d) opens a fresh full-quota period; the SAME runtime
    # restores WL from the frozen baseline (never guessed). The restore can
    # only happen once the NEW period is the current one (old period exhausted).
    invoice2, _account2, applied2 = _run_signup(db, broker, 555000111, "EXTENDED", 30, now=3000)
    assert applied2["already_applied"] is False
    periods = _periods(db, account["id"])
    assert len(periods) == 2
    _assert_chronology(periods)
    assert periods[1]["base_quota_bytes"] == 150 * GB
    restore_now = periods[1]["starts_at"] + 100
    _fresh_usage_lease(db, now=restore_now - 10)
    summary = run_wl_enforcement_cycle(
        db=db, service_marzban=WlBackedClient(broker.remote), worker_id="wl-wiring",
        now=restore_now, topology_observer=_ok_observer(),
    )
    state = db.wl_enforcement.get_state(account["id"])
    assert state["state"] == "ACTIVE"
    assert summary["accounts_enabled"] == 1
    assert sorted(_inbounds_of(broker.remote, child_username)) == _full_wl_delivery()


# --- real dispatcher payment path (bot_support) -------------------------------------

class FakeUser:
    def __init__(self, uid):
        self.id = uid


class FakeBot:
    def __init__(self):
        self.invoices = []
        self.messages = []

    async def send_invoice(self, **kwargs):
        self.invoices.append(kwargs)

    async def send_message(self, chat_id, text, **kwargs):
        self.messages.append((chat_id, text))


class FakeCallMessage:
    def __init__(self, bot, uid):
        self.bot = bot
        self.uid = uid
        self.edits = []
        self.answers = []

    async def answer(self, text, reply_markup=None, **kw):
        self.answers.append(text)

    async def edit_text(self, text, reply_markup=None, **kw):
        self.edits.append((text, reply_markup))


class FakeCall:
    def __init__(self, uid, data, bot=None):
        self.from_user = FakeUser(uid)
        self.data = data
        self.message = FakeCallMessage(bot or FakeBot(), uid)
        self.answered = False

    async def answer(self):
        self.answered = True


class FakeState:
    async def clear(self):
        pass

    async def set_state(self, s):
        pass


class FakePreCheckoutQuery:
    def __init__(self, invoice_payload, currency, total_amount, uid=555000111):
        self.invoice_payload = invoice_payload
        self.currency = currency
        self.total_amount = total_amount
        self.from_user = FakeUser(uid)
        self.answers = []

    async def answer(self, ok, error_message=None):
        self.answers.append((ok, error_message))


class FakeSuccessfulPayment:
    def __init__(self, invoice_payload, currency="XTR", total_amount=199,
                 charge_id="wl-charge-1", provider_charge_id=None):
        self.invoice_payload = invoice_payload
        self.currency = currency
        self.total_amount = total_amount
        self.telegram_payment_charge_id = charge_id
        self.provider_payment_charge_id = provider_charge_id


class FakeSuccessfulPaymentMessage:
    def __init__(self, sp, payer_id, bot=None):
        self.successful_payment = sp
        self.from_user = FakeUser(payer_id)
        self.bot = bot or FakeBot()
        self.answers = []

    async def answer(self, text, **kw):
        self.answers.append(text)


@pytest.fixture
def buy_handlers(db):
    from aiogram import Dispatcher
    from src.bot_support import setup_support_handlers

    dp = Dispatcher()
    trigger = asyncio.Event()
    setup_support_handlers(dp, db, marzban=None, stars_trigger=trigger)

    def _handler(observer, name):
        for h in observer.handlers:
            if h.callback.__name__ == name:
                return h.callback
        raise AssertionError(f"handler {name} not registered")

    return {
        "buy": _handler(dp.message, "msg_buy_vpn"),
        "buy_plan": _handler(dp.callback_query, "cb_buy_plan"),
        "buy_duration": _handler(dp.callback_query, "cb_buy_duration"),
        "buy_pay": _handler(dp.callback_query, "cb_buy_pay"),
        "pre_checkout": _handler(dp.pre_checkout_query, "on_pre_checkout"),
        "successful_payment": _handler(dp.message, "on_successful_payment"),
        "trigger": trigger,
    }


def _enable_stars(db):
    db._conn.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES ('stars:enabled', '1')"
    )
    db._conn.commit()


def _kb_texts(markup):
    rows = getattr(markup, "inline_keyboard", None) or getattr(markup, "keyboard")
    return [[btn.text for btn in row] for row in rows]


def _kb_data(markup):
    rows = getattr(markup, "inline_keyboard", None)
    return [[btn.callback_data for btn in row] for row in rows]


def test_bot_buy_flow_offers_all_six_plans_including_wl_family(db, buy_handlers):
    _enable_stars(db)

    class Msg:
        def __init__(self, uid):
            self.from_user = FakeUser(uid)
            self.text = "🛒 Купить VPN"
            self.sent = []

        async def answer(self, text, reply_markup=None, **kw):
            self.sent.append((text, reply_markup))

    msg = Msg(555000111)
    asyncio.run(buy_handlers["buy"](msg, FakeState()))
    flat = [t for row in _kb_texts(msg.sent[0][1]) for t in row]
    assert any("Базовый" in t and "3" in t for t in flat)
    assert any("Базовый Плюс" in t and "6" in t for t in flat)
    assert any("Базовый Про" in t and "12" in t for t in flat)
    assert any("WL" in t and "3" in t for t in flat)
    assert any("Расширенный" in t and "6" in t for t in flat)
    assert any("Семейный" in t and "12" in t for t in flat)
    # Packages are not offered even though the package catalog is seeded.
    assert not any("GB" in t and "пакет" in t.lower() for t in flat)


def test_bot_wl_duration_screen_states_per_period_quota_not_double(db, buy_handlers):
    _enable_stars(db)
    call = FakeCall(555000111, "buy_plan:WL")
    asyncio.run(buy_handlers["buy_plan"](call))
    data_rows = _kb_data(call.message.edits[0][1])
    assert sorted(d for row in data_rows for d in row if d and d.startswith("buy_dur")) == [
        "buy_dur:WL:30", "buy_dur:WL:60",
    ]
    # 60d must read as per-period quota, never as a doubled total.
    call30 = FakeCall(555000111, "buy_dur:WL:30")
    asyncio.run(buy_handlers["buy_duration"](call30))
    text30, markup30 = call30.message.edits[0]
    assert "100 GB" in text30 and "до 3" in text30 and "199 ⭐️" in text30
    call60 = FakeCall(555000111, "buy_dur:WL:60")
    asyncio.run(buy_handlers["buy_duration"](call60))
    text60, _markup60 = call60.message.edits[0]
    assert "100 GB" in text60
    assert "каждые 30 дней" in text60
    assert "200 GB" not in text60
    assert "2 периода по 100 GB" in text60


def test_bot_dispatcher_payment_path_routes_wl_signup_invoice(db, buy_handlers):
    """Regression pin for the past P0 class: the real pre_checkout +
    successful_payment dispatcher path must treat a WL signup invoice as
    canonical (never fall through to the legacy Marzban branch)."""
    _enable_stars(db)
    bot = FakeBot()
    call = FakeCall(555000111, "buy_pay:WL:30", bot=bot)
    asyncio.run(buy_handlers["buy_pay"](call, FakeState()))
    invoice = db._conn.execute(
        "SELECT id, invoice_kind, stars_price, account_id FROM stars_invoices ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert invoice["invoice_kind"] == "CANONICAL_SIGNUP"
    assert invoice["account_id"] is None
    assert invoice["stars_price"] == 199

    pre_checkout = FakePreCheckoutQuery(str(invoice["id"]), "XTR", invoice["stars_price"])
    asyncio.run(buy_handlers["pre_checkout"](pre_checkout))
    assert pre_checkout.answers == [(True, None)], pre_checkout.answers

    sp = FakeSuccessfulPayment(str(invoice["id"]), total_amount=invoice["stars_price"])
    message = FakeSuccessfulPaymentMessage(sp, payer_id=555000111, bot=bot)
    asyncio.run(buy_handlers["successful_payment"](message, FakeState()))

    row = db.get_invoice(invoice["id"])
    assert row["status"] == "paid"
    assert row["account_id"] is not None
    account = db.accounts.get_account(row["account_id"])
    assert account is not None and account["status"] == "ACTIVE"
    assert buy_handlers["trigger"].is_set()
    applied = db.stars_purchases.apply_paid_invoice(invoice["id"], now=200)
    assert applied["wl_periods"], "WL signup must schedule immutable WL periods"
    sub = _subscription_plan(db, row["account_id"])
    assert sub["plan_code"] == "WL" and sub["wl_mode"] == "LIMITED"


def test_bot_buy_pay_rejects_package_sku_callback_server_side(db, buy_handlers):
    _enable_stars(db)
    bot = FakeBot()
    call = FakeCall(555000111, "buy_pay:WL_PACKAGE_100_GB:30", bot=bot)
    asyncio.run(buy_handlers["buy_pay"](call, FakeState()))
    assert any("нельзя оформить" in t for t in call.message.answers)
    assert db._conn.execute("SELECT COUNT(*) FROM stars_invoices").fetchone()[0] == 0
    assert db._conn.execute("SELECT COUNT(*) FROM mgboost_accounts").fetchone()[0] == 0


def test_bot_renewal_menu_for_wl_account_offers_only_wl_skus(db, buy_handlers, broker):
    _enable_stars(db)
    _invoice, _account, _applied = _run_signup(db, broker, 555000111, "WL", 30, now=100)

    class Msg:
        def __init__(self, uid):
            self.from_user = FakeUser(uid)
            self.text = "⭐️ Продлить подписку"
            self.sent = []

        async def answer(self, text, reply_markup=None, **kw):
            self.sent.append((text, reply_markup))

    msg = Msg(555000111)
    asyncio.run(buy_handlers["buy"](msg, FakeState()))  # sanity: buy flow stays available
    data = [d for row in _kb_data(msg.sent[0][1]) for d in row if d]
    # unified funnel: a WL account is offered ONLY its own plan's durations
    assert sorted(d for d in data if d.startswith("buy_dur:")) == [
        "buy_dur:WL:30", "buy_dur:WL:60",
    ]
    assert "change_plan" in data
    assert not any(d.startswith("buy_plan:") for d in data)
