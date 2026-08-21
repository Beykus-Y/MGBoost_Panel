import os
import sys
import tempfile
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


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


# --- no default tariff / disabled by default -------------------------------

def test_stars_tariffs_table_starts_empty(db):
    assert db.get_stars_tariffs() == []
    assert db.get_active_stars_tariffs() == []


def test_stars_enabled_defaults_off(db):
    assert db.get_setting("stars:enabled") is None
    assert db.get_setting("stars:enabled") != "1"


# --- tariff CRUD -------------------------------------------------------------

def test_create_and_list_tariff(db):
    t = db.save_stars_tariff({"name": "1 месяц", "duration_days": 30, "stars_price": 320, "active": True})
    assert t["id"]
    assert t["duration_days"] == 30
    assert db.get_stars_tariffs() == [t]
    assert db.get_active_stars_tariffs() == [t]


def test_inactive_tariff_excluded_from_active_list(db):
    t = db.save_stars_tariff({"name": "x", "duration_days": 10, "stars_price": 100, "active": False})
    assert db.get_active_stars_tariffs() == []
    assert db.get_stars_tariffs() == [t]


def test_toggle_and_delete_tariff(db):
    t = db.save_stars_tariff({"name": "x", "duration_days": 10, "stars_price": 100, "active": True})
    db.toggle_stars_tariff(t["id"], False)
    assert db.get_active_stars_tariffs() == []
    db.delete_stars_tariff(t["id"])
    assert db.get_stars_tariffs() == []


def test_update_tariff_by_id(db):
    t = db.save_stars_tariff({"name": "x", "duration_days": 10, "stars_price": 100})
    updated = db.save_stars_tariff({"id": t["id"], "name": "y", "duration_days": 20, "stars_price": 200})
    assert updated["id"] == t["id"]
    assert updated["name"] == "y"
    assert updated["duration_days"] == 20
    assert len(db.get_stars_tariffs()) == 1


# --- invoice creation / TTL --------------------------------------------------

def test_create_stars_invoice_sets_ttl_and_created_status(db):
    inv = db.create_stars_invoice(
        created_by_telegram_id=111, marzban_username="alice",
        tariff_id=1, tariff_name="1 month", duration_days=30, stars_price=320,
    )
    assert inv["status"] == "created"
    assert inv["expires_at"] == inv["created_at"] + 3600
    assert inv["payer_telegram_id"] is None


def test_invoice_creation_logs_audit_event(db):
    db.create_stars_invoice(
        created_by_telegram_id=111, marzban_username="alice",
        tariff_id=1, tariff_name="1 month", duration_days=30, stars_price=320,
    )
    events = db.get_audit_log(event_type="invoice_created")
    assert len(events) == 1
    assert events[0]["telegram_id"] == 111
    assert events[0]["marzban_username"] == "alice"


# --- tariff snapshot semantics -----------------------------------------------

def test_invoice_snapshot_survives_tariff_change(db):
    t = db.save_stars_tariff({"name": "1 месяц", "duration_days": 30, "stars_price": 320})
    inv = db.create_stars_invoice(
        created_by_telegram_id=1, marzban_username="alice",
        tariff_id=t["id"], tariff_name=t["name"], duration_days=t["duration_days"],
        stars_price=t["stars_price"],
    )
    db.save_stars_tariff({"id": t["id"], "name": "renamed", "duration_days": 999, "stars_price": 1})
    fresh = db.get_invoice(inv["id"])
    assert fresh["tariff_name"] == "1 месяц"
    assert fresh["duration_days"] == 30
    assert fresh["stars_price"] == 320


def test_invoice_snapshot_survives_tariff_deletion(db):
    t = db.save_stars_tariff({"name": "1 месяц", "duration_days": 30, "stars_price": 320})
    inv = db.create_stars_invoice(
        created_by_telegram_id=1, marzban_username="alice",
        tariff_id=t["id"], tariff_name=t["name"], duration_days=t["duration_days"],
        stars_price=t["stars_price"],
    )
    db.delete_stars_tariff(t["id"])
    fresh = db.get_invoice(inv["id"])
    assert fresh["tariff_name"] == "1 месяц"
    assert fresh["stars_price"] == 320


# --- state machine transitions -----------------------------------------------

def _make_invoice(db, **overrides):
    kwargs = dict(
        created_by_telegram_id=111, marzban_username="alice",
        tariff_id=1, tariff_name="1 month", duration_days=30, stars_price=320,
    )
    kwargs.update(overrides)
    return db.create_stars_invoice(**kwargs)


def test_mark_invoice_paid_transition(db):
    inv = _make_invoice(db)
    ok = db.mark_invoice_paid(inv["id"], "charge1", None, payer_telegram_id=222, total_amount=320)
    assert ok is True
    row = db.get_invoice(inv["id"])
    assert row["status"] == "paid"
    assert row["payer_telegram_id"] == 222
    assert row["telegram_payment_charge_id"] == "charge1"


def test_mark_invoice_paid_is_idempotent_duplicate_charge(db):
    """A duplicate successful_payment delivery for the same charge id must
    be a safe no-op — never double-apply, never error loudly."""
    inv = _make_invoice(db)
    ok1 = db.mark_invoice_paid(inv["id"], "charge1", None, payer_telegram_id=222, total_amount=320)
    ok2 = db.mark_invoice_paid(inv["id"], "charge1", None, payer_telegram_id=222, total_amount=320)
    assert ok1 is True
    assert ok2 is False
    row = db.get_invoice(inv["id"])
    assert row["status"] == "paid"


def test_commit_apply_plan_requires_paid_status(db):
    inv = _make_invoice(db)
    # still 'created' — commit must fail
    assert db.commit_apply_plan(inv["id"], base_expire_observed=0, target_expire=1000) is False
    db.mark_invoice_paid(inv["id"], "c1", None, 222, 320)
    assert db.commit_apply_plan(inv["id"], base_expire_observed=0, target_expire=1000) is True
    row = db.get_invoice(inv["id"])
    assert row["status"] == "plan_committed"
    assert row["target_expire"] == 1000
    # second commit attempt must fail (already plan_committed)
    assert db.commit_apply_plan(inv["id"], base_expire_observed=0, target_expire=2000) is False
    row2 = db.get_invoice(inv["id"])
    assert row2["target_expire"] == 1000  # never recomputed


def test_mark_invoice_applied_requires_plan_committed(db):
    inv = _make_invoice(db)
    db.mark_invoice_paid(inv["id"], "c1", None, 222, 320)
    assert db.mark_invoice_applied(inv["id"], applied_expire=1000) is False
    db.commit_apply_plan(inv["id"], 0, 1000)
    assert db.mark_invoice_applied(inv["id"], applied_expire=1000) is True
    row = db.get_invoice(inv["id"])
    assert row["status"] == "applied"
    assert row["applied_expire"] == 1000


def test_mark_invoice_manual_review_from_paid_and_plan_committed(db):
    inv = _make_invoice(db)
    db.mark_invoice_paid(inv["id"], "c1", None, 222, 320)
    assert db.mark_invoice_manual_review(inv["id"], reason="eligibility_changed_after_payment: unlimited") is True
    row = db.get_invoice(inv["id"])
    assert row["status"] == "manual_review"
    assert "eligibility_changed_after_payment" in row["manual_review_reason"]

    inv2 = _make_invoice(db)
    db.mark_invoice_paid(inv2["id"], "c2", None, 222, 320)
    db.commit_apply_plan(inv2["id"], 0, 1000)
    assert db.mark_invoice_manual_review(inv2["id"], reason="live_expire_mismatch") is True
    assert db.get_invoice(inv2["id"])["status"] == "manual_review"


def test_mark_invoice_apply_failed_user_missing(db):
    inv = _make_invoice(db)
    db.mark_invoice_paid(inv["id"], "c1", None, 222, 320)
    assert db.mark_invoice_apply_failed(inv["id"], "user_missing") is True
    assert db.get_invoice(inv["id"])["status"] == "apply_failed_user_missing"


def test_mark_invoice_apply_failed_retry_exhausted(db):
    inv = _make_invoice(db)
    db.mark_invoice_paid(inv["id"], "c1", None, 222, 320)
    db.commit_apply_plan(inv["id"], 0, 1000)
    assert db.mark_invoice_apply_failed(inv["id"], "retry_exhausted") is True
    row = db.get_invoice(inv["id"])
    assert row["status"] == "apply_retry_exhausted"
    events = db.get_audit_log(event_type="payment_apply_retry_exhausted")
    assert len(events) == 1


def test_record_apply_attempt_failure_does_not_change_status(db):
    inv = _make_invoice(db)
    db.mark_invoice_paid(inv["id"], "c1", None, 222, 320)
    db.commit_apply_plan(inv["id"], 0, 1000)
    db.record_apply_attempt_failure(inv["id"], "timeout")
    row = db.get_invoice(inv["id"])
    assert row["status"] == "plan_committed"
    assert row["apply_attempts"] == 1
    assert row["last_apply_error"] == "timeout"


def test_mark_invoice_paid_but_ambiguous(db):
    inv = _make_invoice(db)
    ok = db.mark_invoice_paid_but_ambiguous(
        inv["id"], charge_id="c1", payer_telegram_id=222, total_amount=5,
        reason="amount_or_currency_mismatch",
    )
    assert ok is True
    row = db.get_invoice(inv["id"])
    assert row["status"] == "manual_review"
    assert row["payer_telegram_id"] == 222
    assert row["telegram_payment_charge_id"] == "c1"
    assert row["manual_review_reason"] == "amount_or_currency_mismatch"


def test_resolve_manual_review_confirm_applied(db):
    inv = _make_invoice(db)
    db.mark_invoice_paid(inv["id"], "c1", None, 222, 320)
    db.commit_apply_plan(inv["id"], 0, 1000)
    db.mark_invoice_manual_review(inv["id"], reason="x")
    assert db.resolve_manual_review_confirm_applied(inv["id"], applied_expire=1234) is True
    row = db.get_invoice(inv["id"])
    assert row["status"] == "applied"
    assert row["applied_expire"] == 1234
    assert row["resolved_by_admin_at"] is not None


def test_resolve_manual_review_requeue(db):
    inv = _make_invoice(db)
    db.mark_invoice_paid(inv["id"], "c1", None, 222, 320)
    db.commit_apply_plan(inv["id"], 0, 1000)
    db.mark_invoice_manual_review(inv["id"], reason="x")
    assert db.resolve_manual_review_requeue(inv["id"]) is True
    row = db.get_invoice(inv["id"])
    assert row["status"] == "plan_committed"
    assert row["target_expire"] == 1000  # unchanged, same target retried


def test_mark_invoice_refunded_from_applied(db):
    inv = _make_invoice(db)
    db.mark_invoice_paid(inv["id"], "c1", None, 222, 320)
    db.commit_apply_plan(inv["id"], 0, 1000)
    db.mark_invoice_applied(inv["id"], applied_expire=1000)
    assert db.mark_invoice_refunded(inv["id"]) is True
    row = db.get_invoice(inv["id"])
    assert row["status"] == "refunded"
    events = db.get_audit_log(event_type="refund")
    assert len(events) == 1
    assert events[0]["telegram_id"] == 222


# --- payer-identity split -----------------------------------------------------

def test_created_by_and_payer_telegram_id_are_independent(db):
    """A gift/shared payment: invoice created for one telegram_id, paid by
    a different one — both must be recorded distinctly, and refund must
    read payer_telegram_id, never created_by_telegram_id."""
    inv = db.create_stars_invoice(
        created_by_telegram_id=111, marzban_username="alice",
        tariff_id=1, tariff_name="1 month", duration_days=30, stars_price=320,
    )
    db.mark_invoice_paid(inv["id"], "c1", None, payer_telegram_id=999, total_amount=320)
    row = db.get_invoice(inv["id"])
    assert row["created_by_telegram_id"] == 111
    assert row["payer_telegram_id"] == 999
    db.commit_apply_plan(inv["id"], 0, 1000)
    db.mark_invoice_applied(inv["id"], applied_expire=1000)
    db.mark_invoice_refunded(inv["id"])
    refund_events = db.get_audit_log(event_type="refund")
    assert refund_events[0]["telegram_id"] == 999  # payer, not creator


# --- get_pending_apply_invoices ordering -------------------------------------

def test_get_pending_apply_invoices_orders_by_username_then_id(db):
    inv_b1 = _make_invoice(db, marzban_username="bob")
    inv_a1 = _make_invoice(db, marzban_username="alice")
    inv_a2 = _make_invoice(db, marzban_username="alice")
    for inv in (inv_b1, inv_a1, inv_a2):
        db.mark_invoice_paid(inv["id"], f"c{inv['id']}", None, 1, 320)
    pending = db.get_pending_apply_invoices()
    usernames_in_order = [r["marzban_username"] for r in pending]
    assert usernames_in_order == sorted(usernames_in_order)
    alice_rows = [r for r in pending if r["marzban_username"] == "alice"]
    assert alice_rows[0]["id"] < alice_rows[1]["id"]


def test_get_pending_apply_invoices_excludes_terminal_states(db):
    inv = _make_invoice(db)
    db.mark_invoice_paid(inv["id"], "c1", None, 1, 320)
    db.commit_apply_plan(inv["id"], 0, 1000)
    db.mark_invoice_applied(inv["id"], applied_expire=1000)
    assert db.get_pending_apply_invoices() == []
