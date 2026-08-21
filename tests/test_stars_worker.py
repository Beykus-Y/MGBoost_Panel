import asyncio
import os
import sys
import tempfile
import time
from urllib.error import HTTPError

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


@pytest.fixture(autouse=True)
def _reset_marzban_locks():
    """marzban_user_locks is a process-wide singleton; asyncio.Lock objects
    bind to the event loop they're first used on. Since each test below
    calls asyncio.run() (a fresh loop each time), stale locks from a
    previous test's loop must be cleared or a later test reusing the same
    username would hit 'bound to a different event loop'."""
    from src.marzban_lock import marzban_user_locks
    marzban_user_locks._locks.clear()
    yield
    marzban_user_locks._locks.clear()


@pytest.fixture
def db():
    tmp = tempfile.mkdtemp()
    os.environ["DATA_DIR"] = tmp
    from importlib import reload
    import src.config as cfg
    reload(cfg)
    cfg.DATA_DIR = tmp
    import src.database as db_mod
    reload(db_mod)
    db_mod.DB_PATH = os.path.join(tmp, "db.sqlite3")
    instance = db_mod.Database()
    yield instance
    instance._conn.close()


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text))


class FakeMarzban:
    """In-memory Marzban stand-in. `users` maps username -> user dict.
    `modify_calls` records every modify_user invocation for assertions.
    `explode_modify` / `explode_get` let a test force a transient error."""

    def __init__(self, users=None):
        self.users = users or {}
        self.modify_calls = []
        self.get_calls = []
        self.explode_modify = None
        self.explode_get = None

    def get_admin_token_from_env(self):
        return "tok"

    def get_user(self, username, admin_token):
        self.get_calls.append(username)
        if self.explode_get:
            raise self.explode_get
        if username not in self.users:
            raise HTTPError(url=None, code=404, msg="not found", hdrs=None, fp=None)
        return dict(self.users[username])

    def modify_user(self, username, payload, admin_token):
        self.modify_calls.append((username, dict(payload)))
        if self.explode_modify:
            raise self.explode_modify
        self.users[username].update(payload)
        return dict(self.users[username])


def _make_paid_invoice(db, username="alice", duration_days=30, stars_price=320, payer=222):
    inv = db.create_stars_invoice(
        created_by_telegram_id=111, marzban_username=username,
        tariff_id=1, tariff_name="1 month", duration_days=duration_days, stars_price=stars_price,
    )
    db.mark_invoice_paid(inv["id"], f"charge-{inv['id']}", None, payer_telegram_id=payer, total_amount=stars_price)
    return db.get_invoice(inv["id"])


# --- eligibility ---------------------------------------------------------

def test_check_stars_eligibility_unlimited_refused():
    from src.stars import _check_stars_eligibility
    ok, reason = _check_stars_eligibility({"expire": 0, "status": "active"})
    assert ok is False and reason == "unlimited"
    ok, reason = _check_stars_eligibility({"expire": None, "status": "active"})
    assert ok is False and reason == "unlimited"


def test_check_stars_eligibility_disabled_refused():
    from src.stars import _check_stars_eligibility
    for status in ("disabled", "limited", "on_hold"):
        ok, reason = _check_stars_eligibility({"expire": 12345, "status": status})
        assert ok is False
        assert reason == f"status_{status}"


def test_check_stars_eligibility_active_or_expired_allowed():
    from src.stars import _check_stars_eligibility
    ok, _ = _check_stars_eligibility({"expire": 12345, "status": "active"})
    assert ok is True
    ok, _ = _check_stars_eligibility({"expire": 12345, "status": "expired"})
    assert ok is True


# --- 3-case recovery logic, exercised directly (crash-safety tests) ------

def test_case1_live_equals_target_finalizes_without_second_marzban_call(db):
    """Simulates a crash between a successful modify_user and the local
    'applied' commit: on the next attempt, live_expire already == target.
    The recovery comparison must finalize WITHOUT calling modify_user
    again."""
    from src.stars import _resolve_plan

    row = _make_paid_invoice(db)
    db.commit_apply_plan(row["id"], base_expire_observed=1000, target_expire=1000 + 30 * 86400)
    row = db.get_invoice(row["id"])

    marzban = FakeMarzban(users={"alice": {"username": "alice", "expire": row["target_expire"], "status": "active"}})

    async def explode(*a, **kw):
        raise AssertionError("modify_user must not be called when live_expire already == target")
    marzban.modify_user = explode  # any call fails the test

    bot = FakeBot()
    asyncio.run(_resolve_plan(bot, db, marzban, "tok", row))

    final = db.get_invoice(row["id"])
    assert final["status"] == "applied"
    assert final["applied_expire"] == row["target_expire"]


def test_case2_live_equals_base_attempts_modify_and_applies(db):
    from src.stars import _resolve_plan

    row = _make_paid_invoice(db)
    db.commit_apply_plan(row["id"], base_expire_observed=1000, target_expire=1000 + 30 * 86400)
    row = db.get_invoice(row["id"])

    marzban = FakeMarzban(users={"alice": {"username": "alice", "expire": 1000, "status": "active"}})
    bot = FakeBot()
    asyncio.run(_resolve_plan(bot, db, marzban, "tok", row))

    final = db.get_invoice(row["id"])
    assert final["status"] == "applied"
    assert final["applied_expire"] == row["target_expire"]
    assert marzban.modify_calls == [("alice", {"expire": row["target_expire"]})]


def test_case3_ambiguous_live_value_routes_to_manual_review_no_write(db):
    """live_expire matches neither base nor target — e.g. a second writer
    (Filin) touched expire in between. Must never guess, must never call
    modify_user."""
    from src.stars import _resolve_plan

    row = _make_paid_invoice(db)
    db.commit_apply_plan(row["id"], base_expire_observed=1000, target_expire=1000 + 30 * 86400)
    row = db.get_invoice(row["id"])

    marzban = FakeMarzban(users={"alice": {"username": "alice", "expire": 555555, "status": "active"}})

    async def explode(*a, **kw):
        raise AssertionError("modify_user must not be called on an ambiguous comparison")
    marzban.modify_user = explode

    bot = FakeBot()
    asyncio.run(_resolve_plan(bot, db, marzban, "tok", row))

    final = db.get_invoice(row["id"])
    assert final["status"] == "manual_review"
    assert "live_expire_mismatch" in final["manual_review_reason"]


def test_case2_transient_failure_retried_not_manual_review(db):
    from src.stars import _resolve_plan

    row = _make_paid_invoice(db)
    db.commit_apply_plan(row["id"], base_expire_observed=1000, target_expire=1000 + 30 * 86400)
    row = db.get_invoice(row["id"])

    marzban = FakeMarzban(users={"alice": {"username": "alice", "expire": 1000, "status": "active"}})
    marzban.explode_modify = ConnectionError("timeout")

    bot = FakeBot()
    asyncio.run(_resolve_plan(bot, db, marzban, "tok", row))

    final = db.get_invoice(row["id"])
    assert final["status"] == "plan_committed"  # stays, retried next tick
    assert final["apply_attempts"] == 1


def test_max_apply_attempts_exhausts_to_retry_exhausted(db):
    from src.stars import _resolve_plan, MAX_APPLY_ATTEMPTS

    row = _make_paid_invoice(db)
    db.commit_apply_plan(row["id"], base_expire_observed=1000, target_expire=1000 + 30 * 86400)

    marzban = FakeMarzban(users={"alice": {"username": "alice", "expire": 1000, "status": "active"}})
    marzban.explode_modify = ConnectionError("still down")
    bot = FakeBot()

    for _ in range(MAX_APPLY_ATTEMPTS):
        row = db.get_invoice(row["id"])
        asyncio.run(_resolve_plan(bot, db, marzban, "tok", row))

    final = db.get_invoice(row["id"])
    assert final["status"] == "apply_retry_exhausted"


def test_user_missing_at_resolve_is_terminal(db):
    from src.stars import _resolve_plan

    row = _make_paid_invoice(db)
    db.commit_apply_plan(row["id"], base_expire_observed=1000, target_expire=1000 + 30 * 86400)
    row = db.get_invoice(row["id"])

    marzban = FakeMarzban(users={})  # 404
    bot = FakeBot()
    asyncio.run(_resolve_plan(bot, db, marzban, "tok", row))

    final = db.get_invoice(row["id"])
    assert final["status"] == "apply_failed_user_missing"


# --- eligibility re-check at plan-commit (§4.2 step 2a) -------------------

def test_eligibility_changed_after_payment_routes_to_manual_review(db):
    """Invoice created while eligible; before plan-commit the account
    becomes ineligible (status flips to disabled). Must land in
    manual_review with the specific reason, status must never be written,
    and no Marzban mutation attempted."""
    from src.stars import _commit_plan

    row = _make_paid_invoice(db)
    marzban = FakeMarzban(users={"alice": {"username": "alice", "expire": 12345, "status": "disabled"}})

    async def explode(*a, **kw):
        raise AssertionError("modify_user must never be called from _commit_plan")
    marzban.modify_user = explode

    bot = FakeBot()
    result = asyncio.run(_commit_plan(bot, db, marzban, "tok", row))

    assert result is None
    final = db.get_invoice(row["id"])
    assert final["status"] == "manual_review"
    assert "eligibility_changed_after_payment" in final["manual_review_reason"]
    assert "status_disabled" in final["manual_review_reason"]
    # status was never touched — the fake Marzban's own record is unchanged
    assert marzban.users["alice"]["status"] == "disabled"
    assert marzban.modify_calls == []


def test_eligibility_changed_to_unlimited_routes_to_manual_review(db):
    from src.stars import _commit_plan

    row = _make_paid_invoice(db)
    marzban = FakeMarzban(users={"alice": {"username": "alice", "expire": 0, "status": "active"}})
    bot = FakeBot()
    result = asyncio.run(_commit_plan(bot, db, marzban, "tok", row))

    assert result is None
    final = db.get_invoice(row["id"])
    assert final["status"] == "manual_review"
    assert "unlimited" in final["manual_review_reason"]
    assert marzban.modify_calls == []


def test_commit_plan_happy_path(db):
    from src.stars import _commit_plan

    row = _make_paid_invoice(db, duration_days=10)
    now = int(time.time())
    marzban = FakeMarzban(users={"alice": {"username": "alice", "expire": now + 1000, "status": "active"}})
    bot = FakeBot()
    result = asyncio.run(_commit_plan(bot, db, marzban, "tok", row))

    assert result["status"] == "plan_committed"
    assert result["base_expire_observed"] == now + 1000
    assert result["target_expire"] == now + 1000 + 10 * 86400


def test_pre_plan_get_user_failures_exhaust_after_five_attempts(db):
    from src.stars import _tick, MAX_APPLY_ATTEMPTS

    row = _make_paid_invoice(db)
    marzban = FakeMarzban(users={"alice": {"expire": 1000, "status": "active"}})
    marzban.explode_get = ConnectionError("backend unavailable")
    bot = FakeBot()
    db.set_setting("bot:admin_tg_id", "999")

    async def scenario():
        for _ in range(MAX_APPLY_ATTEMPTS):
            await _tick(bot, db, marzban, "tok")
        assert db.get_invoice(row["id"])["status"] == "apply_retry_exhausted"
        # A sixth automatic tick must not even call get_user because the
        # terminal row is no longer selected by get_pending_apply_invoices.
        await _tick(bot, db, marzban, "tok")

    asyncio.run(scenario())
    final = db.get_invoice(row["id"])
    assert final["apply_attempts"] == MAX_APPLY_ATTEMPTS
    assert len(marzban.get_calls) == MAX_APPLY_ATTEMPTS
    assert marzban.modify_calls == []
    assert len(db.get_audit_log(event_type="payment_apply_retry_exhausted")) == 1
    assert len(bot.sent) == 1


@pytest.mark.parametrize("stage", ["paid", "plan_committed"])
def test_persisted_five_attempts_after_restart_exhausts_without_network(db, stage):
    from src.stars import _tick, MAX_APPLY_ATTEMPTS

    row = _make_paid_invoice(db)
    if stage == "plan_committed":
        assert db.commit_apply_plan(row["id"], 1000, 2000)
    for attempt in range(MAX_APPLY_ATTEMPTS):
        assert db.record_apply_attempt_failure(row["id"], f"failure-{attempt}")

    marzban = FakeMarzban(users={
        "alice": {"username": "alice", "expire": 1000, "status": "active"}
    })
    bot = FakeBot()
    db.set_setting("bot:admin_tg_id", "999")

    asyncio.run(_tick(bot, db, marzban, "tok"))

    final = db.get_invoice(row["id"])
    assert final["status"] == "apply_retry_exhausted"
    assert final["apply_attempts"] == MAX_APPLY_ATTEMPTS
    assert marzban.get_calls == []
    assert marzban.modify_calls == []
    assert db.get_pending_apply_invoices() == []
    assert len(db.get_audit_log(event_type="payment_apply_retry_exhausted")) == 1
    assert len(bot.sent) == 1


def test_four_persisted_failures_allow_fifth_attempt_to_succeed(db):
    from src.stars import _tick, MAX_APPLY_ATTEMPTS

    row = _make_paid_invoice(db)
    for attempt in range(MAX_APPLY_ATTEMPTS - 1):
        assert db.record_apply_attempt_failure(row["id"], f"failure-{attempt}")

    now = int(time.time())
    marzban = FakeMarzban(users={
        "alice": {"username": "alice", "expire": now + 1000, "status": "active"}
    })

    asyncio.run(_tick(FakeBot(), db, marzban, "tok"))

    final = db.get_invoice(row["id"])
    assert final["status"] == "applied"
    assert final["apply_attempts"] == MAX_APPLY_ATTEMPTS - 1
    assert len(marzban.modify_calls) == 1


def test_notification_happens_after_username_lock_release(db):
    from src.marzban_lock import marzban_user_locks
    from src.stars import process_invoice_row

    now = int(time.time())
    row = _make_paid_invoice(db)
    marzban = FakeMarzban(users={
        "alice": {"username": "alice", "expire": now + 1000, "status": "active"}
    })

    class LockCheckingBot(FakeBot):
        async def send_message(self, chat_id, text, **kwargs):
            assert marzban_user_locks.get("alice").locked() is False
            await super().send_message(chat_id, text, **kwargs)

    asyncio.run(process_invoice_row(LockCheckingBot(), db, marzban, "tok", row))
    assert db.get_invoice(row["id"])["status"] == "applied"


# --- idempotency: duplicate successful_payment delivery -------------------

def test_duplicate_charge_id_is_safe_noop(db):
    inv = db.create_stars_invoice(
        created_by_telegram_id=111, marzban_username="alice",
        tariff_id=1, tariff_name="1 month", duration_days=30, stars_price=320,
    )
    ok1 = db.mark_invoice_paid(inv["id"], "dup-charge", None, payer_telegram_id=222, total_amount=320)
    ok2 = db.mark_invoice_paid(inv["id"], "dup-charge", None, payer_telegram_id=222, total_amount=320)
    assert ok1 is True
    assert ok2 is False
    # only one paid transition ever took effect
    assert db.get_invoice(inv["id"])["status"] == "paid"


# --- race: Stars apply-worker vs handle_internal_user_renew on the SAME
#     marzban_username, serialized via the shared per-username lock ------

def test_stars_worker_and_internal_renew_race_serialize_via_shared_lock(db):
    """Orchestrates the Stars apply-worker's process_invoice_row and a
    stand-in for handle_internal_user_renew's read-then-write critical
    section against the SAME username and the SAME FakeMarzban instance,
    both holding marzban_lock.marzban_user_locks.get('alice') for their
    full read-decide-write span, with an artificial delay to force the
    race window open. Asserts final `expire` reflects BOTH renewals
    (+30 Stars days, then +7 Filin days on top) rather than one clobbering
    the other."""
    from src.marzban_lock import marzban_user_locks
    from src.stars import process_invoice_row

    now = int(time.time())
    marzban = FakeMarzban(users={"alice": {"username": "alice", "expire": now + 1000, "status": "active"}})
    bot = FakeBot()

    row = _make_paid_invoice(db, duration_days=30)

    async def filin_renew(add_days):
        """Stand-in for handle_internal_user_renew's core logic, holding
        the SAME shared lock across its own get_user -> modify_user span
        (mirrors src/routes/internal.py's _lock_marzban_username usage)."""
        lock = marzban_user_locks.get("alice")
        async with lock:
            # Force the race window open: if the lock weren't actually
            # held/respected by the other coroutine, this sleep gives it
            # room to interleave.
            await asyncio.sleep(0.02)
            user = marzban.get_user("alice", "tok")
            base = max(int(user.get("expire") or 0), now)
            marzban.modify_user("alice", {"expire": base + add_days * 86400}, "tok")

    async def scenario():
        await asyncio.gather(
            process_invoice_row(bot, db, marzban, "tok", row),
            filin_renew(7),
        )

    asyncio.run(scenario())

    final_user = marzban.users["alice"]
    final_invoice = db.get_invoice(row["id"])

    # Both writers' effects must be present — no interleaved clobber.
    # Whichever ran first established a new baseline the second extended
    # from, so the final expire equals (initial +30d) +7d OR (initial +7d)
    # +30d — both equal initial + 37 days from `now` (since Stars extends
    # from base_expire_observed which will already reflect Filin's write if
    # Filin ran first, and vice versa).
    expected = (now + 1000) + 37 * 86400
    assert final_user["expire"] == expected
    # Stars invoice must have resolved cleanly either way (applied, or
    # manual_review if it happened to observe a genuinely ambiguous state —
    # but with the lock serializing the two writers there must be no
    # ambiguity: it must reach 'applied').
    assert final_invoice["status"] == "applied"


def test_different_usernames_do_not_serialize_in_worker(db):
    """Two different marzban_usernames must not block each other — assert
    both can be processed concurrently/interleaved with no unnecessary
    serialization."""
    from src.stars import process_invoice_row

    now = int(time.time())
    marzban = FakeMarzban(users={
        "alice": {"username": "alice", "expire": now + 1000, "status": "active"},
        "bob": {"username": "bob", "expire": now + 1000, "status": "active"},
    })
    bot = FakeBot()

    row_alice = _make_paid_invoice(db, username="alice")
    row_bob = _make_paid_invoice(db, username="bob")

    events = []
    orig_get_user = marzban.get_user

    def tracking_get_user(username, admin_token):
        events.append(f"{username}:get_start")
        result = orig_get_user(username, admin_token)
        events.append(f"{username}:get_end")
        return result
    marzban.get_user = tracking_get_user

    async def scenario():
        await asyncio.gather(
            process_invoice_row(bot, db, marzban, "tok", row_alice),
            process_invoice_row(bot, db, marzban, "tok", row_bob),
        )
    asyncio.run(scenario())

    both_applied = (
        db.get_invoice(row_alice["id"])["status"] == "applied"
        and db.get_invoice(row_bob["id"])["status"] == "applied"
    )
    assert both_applied
