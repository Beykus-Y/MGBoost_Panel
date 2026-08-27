"""PH5-11 first commercial STANDARD signup flow.

Covers the launch-critical matrix end to end: the exact six-sellable-SKU
gate, WL/EXTENDED/FAMILY rejection, server-authoritative callback handling,
the first new-customer DIRECT account purchase (self-service, no legacy
dependency), retry/concurrent-callback single-account guarantees, same-plan
renewal, different-plan refusal, crash durability between capture and
apply, the system-owned provisioning template (idempotent convergence,
drift STOP, outage recoverability, corrupted-routing fail-closed), the
first-device child bootstrap with per-slot independent UUIDs, opaque
credential initial issuance / lost-delivery recovery / no-rotation, and the
Telegram buy UX handlers.
"""

import asyncio
import importlib
import json
import os
import sys
import tempfile
import threading
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tests.test_child_provisioning import HWID_KEY, PRIMARY, PRIMARY_LOGIN
from tests.test_marzban_broker import FakeMarzban
from tests.test_opaque_resolver import _get_sub, _known_hwid_meta


STANDARD_TAGS = ("grpc-direct", "tcp-smart", "vless-ws-cdn")

EXPECTED_SELLABLE = {
    ("BASIC", 30): 99, ("BASIC", 60): 169,
    ("BASIC_PLUS", 30): 139, ("BASIC_PLUS", 60): 199,
    ("BASIC_PRO", 30): 169, ("BASIC_PRO", 60): 249,
}


@pytest.fixture
def db(monkeypatch):
    tmp = tempfile.mkdtemp()
    monkeypatch.setenv("DATA_DIR", tmp)
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


class BrokerBacked:
    """Marzban facade whose semantics go through the REAL BrokerOperations
    dispatch (legacy.user.get / legacy.user.create), so template creation is
    validated by the production contract code, and the ensure/subscription
    broker ops used by the resolver stay real."""

    def __init__(self):
        self.remote = FakeMarzban()
        self.remote.get_sub = _get_sub.__get__(self.remote, FakeMarzban)
        original_create = self.remote.create_user

        def create_with_sub_url(payload, token=None):
            created = original_create(payload, token=None)
            self.remote.users[created["username"]]["subscription_url"] = (
                f"/sub/{created['username']}-token"
            )
            created["subscription_url"] = self.remote.users[created["username"]]["subscription_url"]
            return created

        self.remote.create_user = create_with_sub_url

    def _dispatch(self, operation, payload):
        from src.broker_operations import BrokerOperations
        return BrokerOperations(self.remote).dispatch(operation, payload)

    def get_user(self, username, token=None):
        return self._dispatch("legacy.user.get", {"username": username})

    def create_user(self, payload, token=None):
        return self._dispatch("legacy.user.create", {"user": payload})

    def ensure_fn(self, payload):
        return self._dispatch("child.user.ensure", payload)

    def subscription_fn(self, payload):
        return self._dispatch("child.user.subscription.get", payload)


@pytest.fixture
def broker():
    return BrokerBacked()


def _capture(db, invoice, telegram_id, *, charge_id="charge-1", amount=None, now=110):
    return db.stars_purchases.capture_paid(
        invoice["id"], charge_id=charge_id, provider_charge_id="prov-1",
        payer_telegram_id=telegram_id, currency="XTR",
        amount=amount if amount is not None else invoice["stars_price"], now=now,
    )


def _run_signup(db, broker, telegram_id, plan_code="BASIC", duration_days=30, *, now=100):
    invoice = db.stars_purchases.create_invoice(
        telegram_id=telegram_id, plan_code=plan_code, duration_days=duration_days,
        ttl_seconds=3600, now=now,
    )
    outcome = _capture(db, invoice, telegram_id, charge_id=f"charge-{invoice['id']}", now=now + 10)
    assert outcome == "paid"
    applied = db.stars_purchases.apply_paid_invoice(invoice["id"], now=now + 20)
    account = db.accounts.get_active_account_by_telegram_id(telegram_id)
    return invoice, account, applied


# --- catalog gate ---------------------------------------------------------------

def test_first_rollout_sellable_sku_matrix_is_exact(db):
    sellable = db.stars_purchases.sellable_catalog()
    got = {(item["plan_code"], item["duration_days"]): item["amount"] for item in sellable}
    assert got == EXPECTED_SELLABLE
    limits = {item["plan_code"]: item["device_limit"] for item in sellable}
    assert limits == {"BASIC": 3, "BASIC_PLUS": 6, "BASIC_PRO": 12}
    assert len(sellable) == 6


@pytest.mark.parametrize("plan_code", ["WL", "EXTENDED", "FAMILY"])
def test_non_standard_plans_rejected_for_brand_new_customers(db, plan_code):
    from src.commercial_signup import PlanNotSellable
    with pytest.raises(PlanNotSellable):
        db.stars_purchases.create_invoice(
            telegram_id=777000, plan_code=plan_code, duration_days=30,
            ttl_seconds=3600, now=100,
        )
    assert db._conn.execute("SELECT COUNT(*) FROM stars_invoices").fetchone()[0] == 0


@pytest.mark.parametrize("plan_code", ["WL", "EXTENDED", "FAMILY"])
def test_non_standard_plans_rejected_for_existing_accounts(db, plan_code):
    from src.commercial_signup import PlanNotSellable
    account = db.accounts.create_account("DIRECT", now=1)
    db.accounts.link_telegram_owner(account["id"], 888000, provenance="DIRECT_BIND", actor="t", now=1)
    with pytest.raises(PlanNotSellable):
        db.stars_purchases.create_invoice(
            telegram_id=888000, plan_code=plan_code, duration_days=30,
            ttl_seconds=3600, now=100,
        )


# --- signup invoice lifecycle ----------------------------------------------------

def test_signup_invoice_creates_nothing_until_payment(db):
    invoice = db.stars_purchases.create_invoice(
        telegram_id=555000111, plan_code="BASIC", duration_days=30, ttl_seconds=3600, now=100,
    )
    assert invoice["invoice_kind"] == "CANONICAL_SIGNUP"
    assert invoice["account_id"] is None
    assert invoice["stars_price"] == 99  # server-resolved from the active catalog
    assert db._conn.execute("SELECT COUNT(*) FROM mgboost_accounts").fetchone()[0] == 0


def test_checkout_is_personal_to_the_invoice_creator(db):
    invoice = db.stars_purchases.create_invoice(
        telegram_id=555000111, plan_code="BASIC", duration_days=30, ttl_seconds=3600, now=100,
    )
    with pytest.raises(Exception):
        db.stars_purchases.validate_invoice_for_checkout(invoice["id"], 555000999, now=101)
    db.stars_purchases.validate_invoice_for_checkout(invoice["id"], 555000111, now=101)


def test_capture_confirms_payment_creates_exactly_one_direct_account(db):
    invoice = db.stars_purchases.create_invoice(
        telegram_id=555000111, plan_code="BASIC", duration_days=30, ttl_seconds=3600, now=100,
    )
    assert _capture(db, invoice, 555000111) == "paid"
    account = db.accounts.get_active_account_by_telegram_id(555000111)
    assert account is not None and account["account_source"] == "DIRECT"
    # Fill-once binding on the invoice row.
    bound = db._conn.execute(
        "SELECT account_id FROM stars_invoices WHERE id=?", (invoice["id"],)
    ).fetchone()[0]
    assert bound == account["id"]
    # Infrastructure-owned template alias, PROVEN telegram owner link.
    alias = db._conn.execute(
        "SELECT legacy_username, alias_role, ownership_provenance FROM mgboost_legacy_account_aliases "
        "WHERE account_id=?", (account["id"],),
    ).fetchone()
    assert alias["alias_role"] == "PRIMARY"
    assert alias["legacy_username"] == f"tpl-{account['public_id']}"
    identity = db._conn.execute(
        "SELECT provenance FROM mgboost_telegram_identities WHERE account_id=? AND revoked_at IS NULL",
        (account["id"],),
    ).fetchone()
    assert identity["provenance"] == "DIRECT_BIND"
    # A durable template job exists before any remote call.
    jobs = db.commercial_signup.pending_template_jobs()
    assert [job["account_id"] for job in jobs] == [account["id"]]
    # Evidence row references the freshly created account.
    evidence = db._conn.execute(
        "SELECT account_id FROM mgboost_stars_payment_evidence WHERE invoice_id=?", (invoice["id"],)
    ).fetchone()
    assert evidence["account_id"] == account["id"]


def test_capture_with_wrong_amount_is_manual_review_without_account(db):
    invoice = db.stars_purchases.create_invoice(
        telegram_id=555000111, plan_code="BASIC", duration_days=30, ttl_seconds=3600, now=100,
    )
    assert _capture(db, invoice, 555000111, amount=1) == "manual_review"
    assert db._conn.execute("SELECT COUNT(*) FROM mgboost_accounts").fetchone()[0] == 0
    assert db.get_invoice(invoice["id"])["status"] == "manual_review"


def test_duplicate_successful_callback_is_a_duplicate_not_a_second_account(db):
    invoice, account, _applied = _run_signup(db, BrokerBacked(), 555000111)
    again = _capture(db, invoice, 555000111, charge_id="charge-1", now=130)
    assert again == "duplicate"
    assert db._conn.execute("SELECT COUNT(*) FROM mgboost_accounts").fetchone()[0] == 1
    assert db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_stars_payment_evidence"
    ).fetchone()[0] == 1


def test_concurrent_capture_races_converge_to_one_account(db):
    invoice = db.stars_purchases.create_invoice(
        telegram_id=555000111, plan_code="BASIC", duration_days=30, ttl_seconds=3600, now=100,
    )
    outcomes = []
    lock = threading.Lock()

    def worker():
        result = _capture(db, invoice, 555000111, charge_id="charge-1", now=110 + len(outcomes))
        with lock:
            outcomes.append(result)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert outcomes.count("paid") == 1
    assert set(outcomes) <= {"paid", "duplicate", "manual_review"}
    assert db._conn.execute("SELECT COUNT(*) FROM mgboost_accounts").fetchone()[0] == 1
    assert db._conn.execute("SELECT COUNT(*) FROM mgboost_entitlement_mutations").fetchone()[0] == 0


def test_owner_link_lock_scope_prevents_orphan_account_race(db, monkeypatch):
    """Deterministic reproduction of an independent-review P1: two DIFFERENT
    signup invoices for the SAME brand-new telegram_id (e.g. the payer
    opened checkout twice before paying either). Before the fix,
    `link_telegram_owner()` ran AFTER `ensure_signup_account()` released the
    shared process lock, so a second concurrent capture for the OTHER
    invoice could observe "no OWNER yet" and create its own orphan account
    before the first call claimed ownership -- permanently stuck (every
    retry hits IdentityConflict), wasting a real infrastructure template job
    on a telegram-ownerless account. This test forces the exact interleaving
    deterministically instead of hoping a real race lands: it pauses
    invoice A's owner-link call mid-flight and, while paused, starts invoice
    B's capture on another thread. With the fix, B must block on the shared
    lock (proven by a bounded join) instead of running to completion and
    minting a second account."""
    telegram_id = 555000222
    invoice_a = db.stars_purchases.create_invoice(
        telegram_id=telegram_id, plan_code="BASIC", duration_days=30, ttl_seconds=3600, now=100,
    )
    invoice_b = db.stars_purchases.create_invoice(
        telegram_id=telegram_id, plan_code="BASIC_PLUS", duration_days=30, ttl_seconds=3600, now=100,
    )

    reached = threading.Event()
    release = threading.Event()
    real_link = db.accounts.link_telegram_owner
    state = {"paused_once": False}

    def paused_link(*args, **kwargs):
        if not state["paused_once"]:
            state["paused_once"] = True
            reached.set()
            release.wait(timeout=5)
        return real_link(*args, **kwargs)

    monkeypatch.setattr(db.accounts, "link_telegram_owner", paused_link)

    result_a, result_b = {}, {}

    thread_a = threading.Thread(target=lambda: result_a.__setitem__(
        "outcome", _capture(db, invoice_a, telegram_id, charge_id=f"charge-{invoice_a['id']}", now=110)
    ))
    thread_a.start()
    assert reached.wait(timeout=5), "owner-link call was never reached"

    thread_b = threading.Thread(target=lambda: result_b.__setitem__(
        "outcome", _capture(db, invoice_b, telegram_id, charge_id=f"charge-{invoice_b['id']}", now=110)
    ))
    thread_b.start()
    # While A is paused mid-owner-link, B must still be blocked on the same
    # process lock, not free to resolve-or-create its own account.
    thread_b.join(timeout=0.5)
    assert thread_b.is_alive(), (
        "capture of invoice B ran to completion while invoice A's owner "
        "link was still pending -- the shared lock no longer spans "
        "account-creation + owner-link, reopening the orphan-account race"
    )
    assert db._conn.execute("SELECT COUNT(*) FROM mgboost_accounts").fetchone()[0] == 1

    release.set()
    thread_a.join(timeout=5)
    thread_b.join(timeout=5)

    assert {result_a.get("outcome"), result_b.get("outcome")} <= {"paid", "manual_review"}
    accounts = db._conn.execute("SELECT id FROM mgboost_accounts").fetchall()
    assert len(accounts) == 1, "a second, independently-created account was orphaned by the race"
    account_id = accounts[0]["id"]
    for invoice in (invoice_a, invoice_b):
        bound = db._conn.execute(
            "SELECT account_id FROM stars_invoices WHERE id=?", (invoice["id"],)
        ).fetchone()["account_id"]
        assert bound == account_id
    owners = db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_telegram_identities "
        "WHERE telegram_id=? AND role='OWNER' AND revoked_at IS NULL",
        (telegram_id,),
    ).fetchone()[0]
    assert owners == 1
    jobs = db.commercial_signup.pending_template_jobs()
    assert {job["account_id"] for job in jobs} == {account_id}


def test_apply_grants_subscription_and_schedules_template_job(db, broker):
    invoice, account, applied = _run_signup(db, broker, 555000111)
    assert applied["already_applied"] is False
    assert applied["new_expiry"] == 100 + 20 + 30 * 86400
    assert db.get_invoice(invoice["id"])["status"] == "canonical_applied"
    operation = db._conn.execute(
        "SELECT applied_operation FROM mgboost_stars_purchase_applications WHERE invoice_id=?",
        (invoice["id"],),
    ).fetchone()
    assert operation["applied_operation"] == "CREATE"
    assert db.commercial_signup.pending_template_jobs()[0]["account_id"] == account["id"]


def test_crash_between_capture_and_apply_is_fully_durable(db, broker):
    invoice = db.stars_purchases.create_invoice(
        telegram_id=555000111, plan_code="BASIC", duration_days=30, ttl_seconds=3600, now=100,
    )
    assert _capture(db, invoice, 555000111) == "paid"
    # Simulate a process crash: a fresh Database over the same on-disk file.
    import src.database as database
    fresh = database.Database()
    try:
        assert fresh.stars_purchases.pending_invoices()[0]["id"] == invoice["id"]
        fresh.stars_purchases.apply_paid_invoice(invoice["id"], now=200)
        assert fresh._conn.execute(
            "SELECT COUNT(*) FROM mgboost_subscriptions"
        ).fetchone()[0] == 1
    finally:
        fresh._conn.close()


def test_second_purchase_of_same_plan_is_renewal_on_the_same_account(db, broker):
    _invoice1, _account, _applied1 = _run_signup(db, broker, 555000111, now=100)
    invoice2, account2, applied2 = _run_signup(db, broker, 555000111, now=200_000)
    assert db._conn.execute("SELECT COUNT(*) FROM mgboost_accounts").fetchone()[0] == 1
    operation = db._conn.execute(
        "SELECT applied_operation FROM mgboost_stars_purchase_applications WHERE invoice_id=?",
        (invoice2["id"],),
    ).fetchone()
    assert operation["applied_operation"] == "RENEW"
    assert applied2["new_expiry"] > _applied1["new_expiry"]


def test_different_plan_purchase_is_controlled_rejection(db, broker):
    _invoice1, _account, _applied = _run_signup(db, broker, 555000111)
    from src.stars_purchase import PlanChangeRequired
    with pytest.raises(PlanChangeRequired):
        db.stars_purchases.create_invoice(
            telegram_id=555000111, plan_code="BASIC_PLUS", duration_days=30,
            ttl_seconds=3600, now=500,
        )


# --- system-owned provisioning template -------------------------------------------

def _prepare_child_ready_state(db, broker, telegram_id):
    invoice, account, _applied = _run_signup(db, broker, telegram_id)
    result = db.commercial_signup.ensure_template_for_account(account["id"], marzban=broker, now=200)
    db.commercial_signup.record_template_result(account["id"], state="READY", now=201)
    return invoice, account, result


def test_template_provisioning_matches_profile_and_is_idempotent(db, broker):
    _invoice, account, result = _prepare_child_ready_state(db, broker, 555000111)
    assert result["state"] == "READY" and result["already_pinned"] is False
    template = broker.get_user(f"tpl-{account['public_id']}")
    assert sorted(template["inbounds"]["vless"]) == sorted(STANDARD_TAGS)
    assert template["proxies"]["vless"]["flow"] == "xtls-rprx-vision"
    assert int(template["expire"] or 0) == 0
    rerun = db.commercial_signup.ensure_template_for_account(account["id"], marzban=broker, now=210)
    assert rerun["state"] == "READY" and rerun["already_pinned"] is True
    pinned = db.commercial_signup.template_for_account(account["id"])
    assert pinned["source_contract_hash"] == result["source_contract_hash"]


def test_template_remote_drift_is_manual_review_never_silent_repin(db, broker):
    _invoice, account, first = _prepare_child_ready_state(db, broker, 555000111)
    username = f"tpl-{account['public_id']}"
    broker.remote.users[username]["inbounds"]["vless"] = sorted(STANDARD_TAGS) + ["de-tcp-smart"]
    drifted = db.commercial_signup.ensure_template_for_account(account["id"], marzban=broker, now=220)
    assert drifted["state"] == "MANUAL_REVIEW"
    assert drifted["error_class"] == "template_contract_drift"
    pinned = db.commercial_signup.template_for_account(account["id"])
    assert pinned["source_contract_hash"] == first["source_contract_hash"]


def test_template_outage_keeps_paid_entitlement_recoverable(db, broker):
    invoice, account, _applied = _run_signup(db, broker, 555000111)

    class DownMarzban:
        def get_user(self, username, token=None):
            raise RuntimeError("broker down")

        def create_user(self, payload, token=None):
            raise RuntimeError("broker down")

    with pytest.raises(RuntimeError):
        db.commercial_signup.ensure_template_for_account(account["id"], marzban=DownMarzban(), now=200)
    # The paid grant is durable and unaffected.
    assert db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_subscriptions WHERE account_id=?", (account["id"],)
    ).fetchone()[0] == 1
    assert db.commercial_signup.pending_template_jobs()[0]["account_id"] == account["id"]
    # Recovery converges once the broker is back.
    recovered = db.commercial_signup.ensure_template_for_account(account["id"], marzban=broker, now=300)
    assert recovered["state"] == "READY"


def test_first_device_bootstrap_without_any_legacy_dependency(db, broker):
    _invoice, account, _template = _prepare_child_ready_state(db, broker, 555000111)
    prepared = db.subscription_credentials.prepare(
        account_id=account["id"], actor_ref="worker", reason="signup initial",
        idempotency_key="cred-prepare-0000-0001", now=300,
    )
    db.subscription_credentials.activate(
        credential_id=prepared["id"], account_id=account["id"],
        expected_generation=prepared["generation"], actor_ref="worker",
        idempotency_key="cred-activate-0000001", now=300,
    )
    from src.opaque_resolver import OUTCOME_OK, resolve_opaque_subscription
    from src.config import DEVICE_SLOT_HMAC_KEY
    result = resolve_opaque_subscription(
        db, prepared["raw_token"], _known_hwid_meta("hw") | {"device_id": "device-one"},
        hmac_key=DEVICE_SLOT_HMAC_KEY,
        ensure_fn=broker.ensure_fn, subscription_fn=broker.subscription_fn,
        worker_id="signup-worker", now=400,
    )
    assert result.outcome == OUTCOME_OK
    child = broker.get_user(result.child_username)
    assert sorted(child["inbounds"]["vless"]) == sorted(STANDARD_TAGS)
    template = broker.get_user(f"tpl-{account['public_id']}")
    assert child["proxies"]["vless"]["id"] != template["proxies"]["vless"]["id"]
    # Exactly one occupied slot belongs to the real device -- the template
    # never occupies a customer slot.
    slots = db._conn.execute(
        "SELECT slot_number FROM mgboost_device_slots WHERE account_id=?", (account["id"],)
    ).fetchall()
    assert [s["slot_number"] for s in slots] == [1]


def test_second_device_gets_its_own_child_and_uuid(db, broker):
    _invoice, account, _template = _prepare_child_ready_state(db, broker, 555000111)
    prepared = db.subscription_credentials.prepare(
        account_id=account["id"], actor_ref="worker", reason="signup initial",
        idempotency_key="cred-prepare-0000-0002", now=300,
    )
    db.subscription_credentials.activate(
        credential_id=prepared["id"], account_id=account["id"],
        expected_generation=prepared["generation"], actor_ref="worker",
        idempotency_key="cred-activate-0000002", now=300,
    )
    from src.opaque_resolver import OUTCOME_OK, resolve_opaque_subscription
    from src.config import DEVICE_SLOT_HMAC_KEY
    first = resolve_opaque_subscription(
        db, prepared["raw_token"], _known_hwid_meta("hw") | {"device_id": "device-one"},
        hmac_key=DEVICE_SLOT_HMAC_KEY, ensure_fn=broker.ensure_fn,
        subscription_fn=broker.subscription_fn, worker_id="signup-worker", now=400,
    )
    second = resolve_opaque_subscription(
        db, prepared["raw_token"], _known_hwid_meta("hw") | {"device_id": "device-two"},
        hmac_key=DEVICE_SLOT_HMAC_KEY, ensure_fn=broker.ensure_fn,
        subscription_fn=broker.subscription_fn, worker_id="signup-worker", now=410,
    )
    assert first.outcome == OUTCOME_OK and second.outcome == OUTCOME_OK
    assert first.child_username != second.child_username
    assert first.slot_number != second.slot_number
    uuid_one = broker.get_user(first.child_username)["proxies"]["vless"]["id"]
    uuid_two = broker.get_user(second.child_username)["proxies"]["vless"]["id"]
    assert uuid_one != uuid_two


# --- worker-side notifications and credential delivery ----------------------------

class FakeBot:
    def __init__(self, fail_send=False):
        self.messages = []
        self.invoices = []
        self.fail_send = fail_send

    async def send_message(self, chat_id, text, **kwargs):
        if self.fail_send:
            raise RuntimeError("telegram down")
        self.messages.append((chat_id, text))

    async def send_invoice(self, chat_id, **kwargs):
        self.invoices.append((chat_id, kwargs))


def _invoice_row(db, invoice_id):
    return db.get_invoice(invoice_id)


def test_worker_signup_create_delivers_credential_once_and_activates(db, broker, monkeypatch):
    import src.config as config
    import src.stars as stars
    monkeypatch.setattr(config, "OPAQUE_SUBSCRIPTION_ENABLED", True, raising=False)
    invoice, account, _applied = _run_signup(db, broker, 555000111)
    row = _invoice_row(db, invoice["id"])
    bot = FakeBot()
    asyncio.run(stars._notify_signup_applied(bot, db, row))
    texts = [t for _, t in bot.messages]
    assert any("активирована" in t for t in texts)
    link_messages = [t for t in texts if "sub.beykus.fun/" in t]
    assert len(link_messages) == 1
    credential = db._conn.execute(
        "SELECT status, generation FROM mgboost_subscription_credentials WHERE account_id=?",
        (account["id"],),
    ).fetchone()
    assert credential["status"] == "ACTIVE"
    assert credential["generation"] == 1


def test_worker_lost_credential_delivery_stays_recoverable(db, broker, monkeypatch):
    import src.config as config
    import src.stars as stars
    monkeypatch.setattr(config, "OPAQUE_SUBSCRIPTION_ENABLED", True, raising=False)
    invoice, account, _applied = _run_signup(db, broker, 555000111)
    row = _invoice_row(db, invoice["id"])
    asyncio.run(stars._deliver_signup_credential(FakeBot(fail_send=True), db, row))
    pending = db._conn.execute(
        "SELECT status FROM mgboost_subscription_credentials WHERE account_id=?",
        (account["id"],),
    ).fetchone()
    assert pending["status"] == "PENDING_DELIVERY"
    assert db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_subscription_credentials WHERE account_id=? AND status='ACTIVE'",
        (account["id"],),
    ).fetchone()[0] == 0
    # Retry with a working delivery abandons the stale pending and converges.
    asyncio.run(stars._deliver_signup_credential(FakeBot(), db, row))
    states = [r["status"] for r in db._conn.execute(
        "SELECT status FROM mgboost_subscription_credentials WHERE account_id=?",
        (account["id"],),
    ).fetchall()]
    assert states.count("ACTIVE") == 1
    assert states.count("REVOKED") == 1  # the abandoned pending generation


def test_worker_lost_credential_delivery_alerts_admin(db, broker, monkeypatch):
    """A failed initial delivery must not fail silently: the paying customer
    already saw 'activated!' and has no way to know /newsub exists, so the
    admin must be told (mirrors the OPAQUE_SUBSCRIPTION_ENABLED=off alert)."""
    import src.config as config
    import src.stars as stars
    monkeypatch.setattr(config, "OPAQUE_SUBSCRIPTION_ENABLED", True, raising=False)
    db.set_setting("bot:admin_tg_id", "999")
    invoice, account, _applied = _run_signup(db, broker, 555000111)
    row = _invoice_row(db, invoice["id"])

    class FailOnlyForCustomer(FakeBot):
        async def send_message(self, chat_id, text, **kwargs):
            if chat_id == 555000111:
                raise RuntimeError("telegram down for this customer")
            return await super().send_message(chat_id, text, **kwargs)

    bot = FailOnlyForCustomer()
    asyncio.run(stars._deliver_signup_credential(bot, db, row))
    admin_texts = [t for chat_id, t in bot.messages if chat_id == 999]
    assert any("delivery" in t for t in admin_texts), bot.messages
    pending = db._conn.execute(
        "SELECT status FROM mgboost_subscription_credentials WHERE account_id=?",
        (account["id"],),
    ).fetchone()
    assert pending["status"] == "PENDING_DELIVERY"


def test_worker_never_rotates_an_existing_active_credential(db, broker, monkeypatch):
    import src.config as config
    import src.stars as stars
    monkeypatch.setattr(config, "OPAQUE_SUBSCRIPTION_ENABLED", True, raising=False)
    invoice, account, _applied = _run_signup(db, broker, 555000111)
    prepared = db.subscription_credentials.prepare(
        account_id=account["id"], actor_ref="worker", reason="signup initial",
        idempotency_key="cred-prepare-0000-0009", now=300,
    )
    db.subscription_credentials.activate(
        credential_id=prepared["id"], account_id=account["id"],
        expected_generation=prepared["generation"], actor_ref="worker",
        idempotency_key="cred-activate-0000009", now=300,
    )
    row = _invoice_row(db, invoice["id"])
    bot = FakeBot()
    asyncio.run(stars._notify_signup_applied(bot, db, row))
    texts = [t for _, t in bot.messages]
    assert not any("sub.beykus.fun/" in t for t in texts)
    assert any("/newsub" in t for t in texts)
    credential = db._conn.execute(
        "SELECT id, generation FROM mgboost_subscription_credentials "
        "WHERE account_id=? AND status='ACTIVE'", (account["id"],),
    ).fetchone()
    assert credential["id"] == prepared["id"]
    assert credential["generation"] == 1


def test_worker_renewal_notification_uses_renewal_text(db, broker):
    _invoice1, _account, _applied1 = _run_signup(db, broker, 555000111, now=100)
    invoice2, _account2, _applied2 = _run_signup(db, broker, 555000111, now=200_000)
    bot = FakeBot()
    asyncio.run(stars_module_notify(db, bot, invoice2["id"]))
    texts = [t for _, t in bot.messages]
    assert any("продлена" in t for t in texts)
    assert not any("sub.beykus.fun/" in t for t in texts)


def stars_module_notify(db, bot, invoice_id):
    import src.stars as stars
    return stars._notify_signup_applied(bot, db, _invoice_row(db, invoice_id))


# --- bot purchase UX ---------------------------------------------------------------

class FakeUser:
    def __init__(self, uid):
        self.id = uid


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


@pytest.fixture
def buy_handlers(db):
    from aiogram import Dispatcher
    from src.bot_support import setup_support_handlers

    dp = Dispatcher()
    trigger = asyncio.Event()
    setup_support_handlers(dp, db, marzban=None, stars_trigger=trigger)
    return {
        "buy": _handler(dp.message, "msg_buy_vpn"),
        "buy_plan": _handler(dp.callback_query, "cb_buy_plan"),
        "buy_duration": _handler(dp.callback_query, "cb_buy_duration"),
        "buy_pay": _handler(dp.callback_query, "cb_buy_pay"),
        "stars_menu": _handler(dp.message, "msg_stars_menu"),
        "pre_checkout": _handler(dp.pre_checkout_query, "on_pre_checkout"),
        "successful_payment": _handler(dp.message, "on_successful_payment"),
        "trigger": trigger,
    }


def _handler(dp_observer, name):
    for h in dp_observer.handlers:
        if h.callback.__name__ == name:
            return h.callback
    raise AssertionError(f"handler {name} not registered")


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


def test_bot_buy_flow_shows_three_plans_with_device_counts(db, buy_handlers):
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
    texts = _kb_texts(msg.sent[0][1])
    flat = [t for row in texts for t in row]
    assert any("Базовый" in t and "3" in t for t in flat)
    assert any("Базовый Плюс" in t and "6" in t for t in flat)
    assert any("Базовый Про" in t and "12" in t for t in flat)
    assert not any("WL" in t for t in flat)


def test_bot_buy_flow_duration_and_confirmation_are_server_priced(db, buy_handlers):
    _enable_stars(db)
    call = FakeCall(555000111, "buy_plan:BASIC")
    asyncio.run(buy_handlers["buy_plan"](call))
    data_rows = _kb_data(call.message.edits[0][1])
    assert sorted(d for row in data_rows for d in row if d and d.startswith("buy_dur")) == [
        "buy_dur:BASIC:30", "buy_dur:BASIC:60",
    ]
    call2 = FakeCall(555000111, "buy_dur:BASIC:60")
    asyncio.run(buy_handlers["buy_duration"](call2))
    text, markup = call2.message.edits[0]
    assert "60 дн." in text and "169 ⭐️" in text and "до 3" in text
    assert "buy_pay:BASIC:60" in [d for row in _kb_data(markup) for d in row if d]


def test_bot_buy_pay_creates_signup_invoice_and_sends_catalog_invoice(db, buy_handlers):
    _enable_stars(db)
    bot = FakeBot()
    call = FakeCall(555000111, "buy_pay:BASIC:30", bot=bot)
    asyncio.run(buy_handlers["buy_pay"](call, FakeState()))
    invoice = db._conn.execute(
        "SELECT invoice_kind, account_id, stars_price FROM stars_invoices ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert invoice["invoice_kind"] == "CANONICAL_SIGNUP"
    assert invoice["account_id"] is None
    assert invoice["stars_price"] == 99
    assert len(bot.invoices) == 1
    assert bot.invoices[0][1]["prices"][0].amount == 99
    assert bot.invoices[0][1]["payload"] == str(
        db._conn.execute("SELECT id FROM stars_invoices ORDER BY id DESC LIMIT 1").fetchone()[0]
    )


def test_bot_buy_pay_rejects_tampered_plan_callback(db, buy_handlers):
    _enable_stars(db)
    bot = FakeBot()
    call = FakeCall(555000111, "buy_pay:WL:30", bot=bot)
    asyncio.run(buy_handlers["buy_pay"](call, FakeState()))
    assert any("нельзя оформить" in t for t in call.message.answers)
    assert db._conn.execute("SELECT COUNT(*) FROM stars_invoices").fetchone()[0] == 0
    assert db._conn.execute("SELECT COUNT(*) FROM mgboost_accounts").fetchone()[0] == 0


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
    def __init__(self, invoice_payload, currency="XTR", total_amount=99,
                 charge_id="signup-charge-1", provider_charge_id=None):
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


def test_bot_signup_purchase_survives_real_telegram_pre_checkout_and_payment(db, buy_handlers):
    """Regression for a real end-to-end defect: the plan-selection callback
    (cb_buy_pay) creates a CANONICAL_SIGNUP invoice, but on_pre_checkout and
    on_successful_payment only special-cased invoice_kind=='CANONICAL_PLAN'.
    A brand-new customer's pre_checkout_query fell through to the legacy
    branch, which calls marzban.get_user(row['marzban_username']) -- for a
    signup invoice marzban_username is the synthetic 'signup-<tg_id>'
    placeholder, not a real Marzban user, so pre_checkout always answered
    ok=False and Telegram would never even charge the customer. Even if it
    had, on_successful_payment would have routed the paid money through the
    legacy mark_invoice_paid() path instead of capture_paid(), leaving the
    invoice 'paid' with no bound account (later caught only by the worker
    as 'missing_signup_account'). This test drives the REAL dispatcher
    handlers (not the store layer directly) end to end."""
    _enable_stars(db)
    bot = FakeBot()
    call = FakeCall(555000111, "buy_pay:BASIC:30", bot=bot)
    asyncio.run(buy_handlers["buy_pay"](call, FakeState()))
    invoice = db._conn.execute(
        "SELECT id, invoice_kind, stars_price FROM stars_invoices ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert invoice["invoice_kind"] == "CANONICAL_SIGNUP"

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


def test_bot_signup_pre_checkout_rejects_tampered_amount(db, buy_handlers):
    _enable_stars(db)
    bot = FakeBot()
    call = FakeCall(555000111, "buy_pay:BASIC:30", bot=bot)
    asyncio.run(buy_handlers["buy_pay"](call, FakeState()))
    invoice = db._conn.execute(
        "SELECT id, stars_price FROM stars_invoices ORDER BY id DESC LIMIT 1"
    ).fetchone()
    pre_checkout = FakePreCheckoutQuery(str(invoice["id"]), "XTR", invoice["stars_price"] + 1)
    asyncio.run(buy_handlers["pre_checkout"](pre_checkout))
    assert pre_checkout.answers[0][0] is False
    assert db.get_invoice(invoice["id"])["status"] == "created"


def test_bot_renewal_menu_shows_only_sellable_same_plan(db, buy_handlers):
    _enable_stars(db)
    broker = BrokerBacked()
    _invoice, _account, _applied = _run_signup(db, broker, 555000111)

    class Msg:
        def __init__(self, uid):
            self.from_user = FakeUser(uid)
            self.text = "⭐️ Продлить подписку"
            self.sent = []

        async def answer(self, text, reply_markup=None, **kw):
            self.sent.append((text, reply_markup))

    msg = Msg(555000111)
    asyncio.run(buy_handlers["stars_menu"](msg, FakeState()))
    data = [d for row in _kb_data(msg.sent[0][1]) for d in row if d]
    assert data and all(d.startswith("stars_buy:BASIC:") for d in data)


def test_unrelated_existing_accounts_are_untouched_by_signup(db, broker):
    # A pre-existing reviewed-style account with its own alias.
    existing = db.accounts.create_account("DIRECT", now=1)
    db._conn.execute(
        "INSERT INTO mgboost_legacy_alias_groups (account_id,mapping_key,decision_ref,created_by_actor,created_at) "
        "VALUES (?,?,?,?,?)", (existing["id"], "k:existing", "test", "test", 1),
    )
    db._conn.execute(
        "INSERT INTO mgboost_legacy_account_aliases (account_id,legacy_username,alias_role,"
        "ownership_provenance,legacy_status,legacy_expiry,observed_device_count,observed_hwid_count,"
        "evidence_json,created_at) VALUES (?,?,'PRIMARY','EVIDENCE_PROVEN','ACTIVE',NULL,0,0,'{}',1)",
        (existing["id"], "existing-legacy-user"),
    )
    db._conn.commit()
    before_aliases = db._conn.execute("SELECT account_id, legacy_username FROM mgboost_legacy_account_aliases ORDER BY id").fetchall()

    _run_signup(db, broker, 555000111)

    after_aliases = db._conn.execute("SELECT account_id, legacy_username FROM mgboost_legacy_account_aliases ORDER BY id").fetchall()
    assert after_aliases[0]["legacy_username"] == "existing-legacy-user"
    assert len(after_aliases) == len(before_aliases) + 1
    assert after_aliases[0]["account_id"] == existing["id"]


# --- registration order: the buy entry must beat every FSM catch-all ---------

def _text_update(uid, text, update_id=1):
    from datetime import datetime, timezone
    from aiogram.types import Chat, Message, Update, User
    message = Message(
        message_id=10 + update_id,
        date=datetime.now(timezone.utc),
        chat=Chat(id=uid, type="private"),
        from_user=User(id=uid, is_bot=False, first_name="Buyer"),
        text=text,
    )
    return Update(update_id=update_id, message=message)


@pytest.mark.parametrize("fsm_state", [None, "SupportStates:waiting_link", "SupportStates:in_dialog"])
def test_buy_vpn_text_reaches_the_purchase_menu_from_any_state(db, fsm_state):
    """A brand-new customer lands in waiting_link after /start; the buy
    entry point must win over every FSM handler in EVERY state, or the
    purchase journey is unreachable exactly for the users it exists for."""
    from aiogram import Bot, Dispatcher
    from aiogram.fsm.storage.memory import MemoryStorage
    from src.bot_support import setup_support_handlers

    _enable_stars(db)

    async def scenario():
        session = StubTelegramSession.make()
        bot = Bot("123456:abcdefghijklmnopqrstuvwxyzABCDE", session=session)
        dp = Dispatcher(storage=MemoryStorage())
        setup_support_handlers(dp, db, marzban=None, stars_trigger=asyncio.Event())
        if fsm_state is not None:
            context = dp.fsm.get_context(bot=bot, chat_id=555000111, user_id=555000111)
            await context.set_state(fsm_state)
        await dp.feed_update(bot, _text_update(555000111, "🛒 Купить VPN"))
        await dp.storage.close()
        await bot.session.close()
        return session.methods

    methods = asyncio.run(scenario())
    sent_texts = [getattr(m, "text", "") for m in methods]
    assert any("Выберите тариф" in t for t in sent_texts), sent_texts
    # The plan buttons come from the sellable catalog only.
    reply_markup = next(
        getattr(m, "reply_markup", None) for m in methods
        if getattr(m, "reply_markup", None) is not None
    )
    data = [btn.callback_data for row in reply_markup.inline_keyboard for btn in row]
    plan_rows = [d for d in data if d and d.startswith("buy_plan:")]
    assert sorted(plan_rows) == ["buy_plan:BASIC", "buy_plan:BASIC_PLUS", "buy_plan:BASIC_PRO"]


class StubTelegramSession:
    """Small BaseSession-compatible factory used by dispatcher-level tests
    (same shape as tests/test_bot_support_stars.py)."""

    @staticmethod
    def make():
        from aiogram.client.session.base import BaseSession

        class Session(BaseSession):
            def __init__(self):
                super().__init__()
                self.methods = []

            async def close(self):
                pass

            async def make_request(self, bot, method, timeout=None):
                self.methods.append(method)
                return True

            async def stream_content(self, url, headers=None, timeout=30,
                                     chunk_size=65536, raise_for_status=True):
                if False:
                    yield b""

        return Session()


# --- canonical (PH5-05/PH5-11) refund gate: money-only, no product reversal ------
#
# Owner decision (2026-08-28): a Telegram Stars refund of a canonical invoice
# must be purely a money-state transition (stars_invoices.status only, via
# the existing refund_pending -> refunded/refund_unknown -> reconcile state
# machine already proven for legacy invoices). It must NEVER, by itself,
# touch the account/subscription/credential/child/template it already
# provisioned -- product reversal is a distinct, not-yet-built feature.
# Deliberately NOT extended to invoice status 'paid' (only the terminal
# 'canonical_applied' state): refunding before the canonical apply pipeline
# has run would race PH5-05/PH5-11's own apply logic in a way nobody has
# proven safe yet.

def _full_signup_with_device(db, broker, telegram_id, *, now=100):
    """Builds the full realistic chain a real canary purchase produces:
    paid+applied signup account, pinned system-owned template, one
    provisioned child/device slot, and an ACTIVE opaque credential --
    everything a money-only refund must leave untouched."""
    invoice, account, _applied = _run_signup(db, broker, telegram_id, now=now)
    db.commercial_signup.ensure_template_for_account(account["id"], marzban=broker, now=now + 100)
    db.commercial_signup.record_template_result(account["id"], state="READY", now=now + 101)
    prepared = db.subscription_credentials.prepare(
        account_id=account["id"], actor_ref="worker", reason="signup initial",
        idempotency_key=f"cred-prepare-refund-test-{invoice['id']}", now=now + 200,
    )
    db.subscription_credentials.activate(
        credential_id=prepared["id"], account_id=account["id"],
        expected_generation=prepared["generation"], actor_ref="worker",
        idempotency_key=f"cred-activate-refund-test-{invoice['id']}", now=now + 200,
    )
    from src.opaque_resolver import OUTCOME_OK, resolve_opaque_subscription
    from src.config import DEVICE_SLOT_HMAC_KEY
    result = resolve_opaque_subscription(
        db, prepared["raw_token"], _known_hwid_meta("hw") | {"device_id": f"device-refund-{invoice['id']}"},
        hmac_key=DEVICE_SLOT_HMAC_KEY, ensure_fn=broker.ensure_fn,
        subscription_fn=broker.subscription_fn, worker_id="signup-worker", now=now + 300,
    )
    assert result.outcome == OUTCOME_OK
    return invoice, account, result


def _product_snapshot(db, account_id):
    """Everything a money-only refund must leave byte-identical."""
    def _rows(sql):
        return [dict(r) for r in db._conn.execute(sql, (account_id,)).fetchall()]
    return {
        "account": _rows("SELECT * FROM mgboost_accounts WHERE id=?"),
        "subscription": _rows("SELECT * FROM mgboost_subscriptions WHERE account_id=?"),
        "credentials": _rows("SELECT * FROM mgboost_subscription_credentials WHERE account_id=?"),
        "template": _rows("SELECT * FROM mgboost_provisioning_templates WHERE account_id=?"),
        "child_intents": _rows("SELECT * FROM mgboost_child_user_intents WHERE account_id=?"),
        "device_slots": _rows("SELECT * FROM mgboost_device_slots WHERE account_id=?"),
        "application": _rows("SELECT * FROM mgboost_stars_purchase_applications WHERE account_id=?"),
        "evidence": _rows("SELECT * FROM mgboost_stars_payment_evidence WHERE account_id=?"),
    }


def test_canonical_signup_refund_success_is_money_only(db, broker):
    from tests.test_admin_stars_routes import FakeHandler, LoopRunner
    from src.routes.admin import handle_stars_payment_refund

    telegram_id = 555000900
    invoice, account, _device = _full_signup_with_device(db, broker, telegram_id)
    row = db.get_invoice(invoice["id"])
    assert row["status"] == "canonical_applied"
    before = _product_snapshot(db, account["id"])

    calls = []

    class Bot:
        async def refund_star_payment(self, user_id, charge_id):
            calls.append((user_id, charge_id))
            return True

    runner = LoopRunner(Bot())
    try:
        h = FakeHandler(db, bot_runner=runner)
        handle_stars_payment_refund(h, str(invoice["id"]))
        assert h._response_code == 200
    finally:
        runner.close()

    # Exactly one Telegram refundStarPayment call, with the invoice's own
    # payer/charge id -- never a guessed or reconstructed charge id.
    assert calls == [(telegram_id, row["telegram_payment_charge_id"])]

    after_row = db.get_invoice(invoice["id"])
    assert after_row["status"] == "refunded"
    assert after_row["refunded_at"] is not None
    assert after_row["telegram_payment_charge_id"] == row["telegram_payment_charge_id"]

    after = _product_snapshot(db, account["id"])
    assert after == before, (
        "a canonical refund must be money-only: no account/subscription/"
        "credential/child/template/device/application/evidence row may change"
    )
    assert after["account"][0]["status"] == "ACTIVE"
    assert after["subscription"][0]["status"] == "ACTIVE"
    assert after["credentials"][0]["status"] == "ACTIVE"

    # A second refund attempt on the now-refunded invoice must not repeat
    # the Telegram call and must not be silently accepted either.
    h2 = FakeHandler(db, bot_runner=LoopRunner(Bot()))
    handle_stars_payment_refund(h2, str(invoice["id"]))
    assert h2._response_code == 409
    assert len(calls) == 1


def test_canonical_signup_refund_concurrent_requests_make_one_telegram_call(db, broker):
    import threading
    from tests.test_admin_stars_routes import FakeHandler, LoopRunner
    from src.routes.admin import handle_stars_payment_refund

    invoice, _account, _device = _full_signup_with_device(db, broker, 555000901)
    calls = []

    class Bot:
        async def refund_star_payment(self, user_id, charge_id):
            calls.append((user_id, charge_id))
            await asyncio.sleep(0.05)
            return True

    runner = LoopRunner(Bot())
    results = []

    def worker():
        h = FakeHandler(db, bot_runner=runner)
        handle_stars_payment_refund(h, str(invoice["id"]))
        results.append(h._response_code)

    try:
        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        runner.close()

    assert len(calls) == 1
    assert results.count(200) == 1
    assert set(results) <= {200, 409}
    assert db.get_invoice(invoice["id"])["status"] == "refunded"


def test_canonical_signup_refund_timeout_reconciles_via_existing_path(db, broker, monkeypatch):
    from types import SimpleNamespace
    from tests.test_admin_stars_routes import FakeHandler, LoopRunner
    from src.routes import admin as admin_mod

    invoice, account, _device = _full_signup_with_device(db, broker, 555000902)
    row = db.get_invoice(invoice["id"])
    before = _product_snapshot(db, account["id"])

    class SlowBot:
        async def refund_star_payment(self, user_id, charge_id):
            await asyncio.sleep(1)
            return True

    runner = LoopRunner(SlowBot())
    monkeypatch.setattr(admin_mod, "REFUND_RESULT_TIMEOUT_SECONDS", 0.01)
    try:
        h = FakeHandler(db, bot_runner=runner)
        admin_mod.handle_stars_payment_refund(h, str(invoice["id"]))
        assert h._response_code == 202
        assert db.get_invoice(invoice["id"])["status"] == "refund_unknown"
    finally:
        runner.close()

    # Blind retry stays blocked while the outcome is unknown.
    blocked = FakeHandler(db, bot_runner=LoopRunner(SlowBot()))
    admin_mod.handle_stars_payment_refund(blocked, str(invoice["id"]))
    assert blocked._response_code == 409

    class ReconcileBot:
        async def get_star_transactions(self, offset=0, limit=100):
            return SimpleNamespace(transactions=[SimpleNamespace(
                id=row["telegram_payment_charge_id"],
                receiver=SimpleNamespace(user=SimpleNamespace(id=row["payer_telegram_id"])),
            )])

    runner2 = LoopRunner(ReconcileBot())
    try:
        h2 = FakeHandler(db, bot_runner=runner2)
        admin_mod.handle_stars_payment_reconcile_refund(h2, str(invoice["id"]))
        assert h2._response_code == 200
        final = db.get_invoice(invoice["id"])
        assert final["status"] == "refunded"
        assert final["refund_reconciled_at"] is not None
    finally:
        runner2.close()

    after = _product_snapshot(db, account["id"])
    assert after == before


def test_paid_canonical_signup_invoice_is_still_not_refundable(db):
    """Deliberate scope boundary: refund is enabled only from the terminal
    'canonical_applied' state, never from the intermediate 'paid' state --
    refunding before apply would race the PH5-05/PH5-11 apply pipeline."""
    from tests.test_admin_stars_routes import FakeHandler, LoopRunner
    from src.routes.admin import handle_stars_payment_refund

    invoice = db.stars_purchases.create_invoice(
        telegram_id=555000903, plan_code="BASIC", duration_days=30, ttl_seconds=3600, now=100,
    )
    outcome = db.stars_purchases.capture_paid(
        invoice["id"], charge_id="charge-paid-only", provider_charge_id="prov-1",
        payer_telegram_id=555000903, currency="XTR", amount=invoice["stars_price"], now=110,
    )
    assert outcome == "paid"
    assert db.get_invoice(invoice["id"])["status"] == "paid"
    assert db.begin_invoice_refund(invoice["id"]) is False

    class Bot:
        async def refund_star_payment(self, user_id, charge_id):
            raise AssertionError("must never call Telegram for a merely 'paid' canonical invoice")

    runner = LoopRunner(Bot())
    try:
        h = FakeHandler(db, bot_runner=runner)
        handle_stars_payment_refund(h, str(invoice["id"]))
        assert h._response_code == 409
    finally:
        runner.close()
    assert db.get_invoice(invoice["id"])["status"] == "paid"
