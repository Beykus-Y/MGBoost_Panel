"""Telegram Stars payment durability worker + shared eligibility rule.

Implements the two-phase commit-then-apply flow and the 3-case recovery
comparison from the Phase 2 design doc (§3/§4). Lives inside the bot
thread's asyncio event loop, started alongside bot_monitor._monitor_loop
(see bot_monitor.run_all), polling every RETRY_INTERVAL_SECONDS with a fast
first-attempt path triggered via an asyncio.Event.
"""
import asyncio
import logging
import time
from urllib.error import HTTPError

from .marzban_lock import marzban_user_locks
from .parent_sync import run_account_sync_cycle
from .stars_purchase import StarsPurchaseError

logger = logging.getLogger(__name__)

MAX_APPLY_ATTEMPTS = 5
RETRY_INTERVAL_SECONDS = 30


async def _run_sync(func, *args):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, func, *args)


def _is_not_found(exc: Exception) -> bool:
    return isinstance(exc, HTTPError) and exc.code == 404


def _check_stars_eligibility(user: dict) -> tuple[bool, str]:
    """Single source of truth for Stars purchase eligibility (§2/§8):
    - expire in (None, 0) -> unlimited, never offered.
    - status in (disabled, limited, on_hold) -> refused, contact support.
    - status in (active, expired) with a positive expire -> eligible.
    Never inspects/derives `status` from anything but Marzban's own value —
    this function never changes state, it only reads."""
    expire = user.get("expire")
    if expire in (None, 0):
        return False, "unlimited"
    status = user.get("status")
    if status in ("disabled", "limited", "on_hold"):
        return False, f"status_{status}"
    if status in ("active", "expired"):
        return True, ""
    return False, f"status_{status}"


# --- operator/user notifications ------------------------------------------

async def notify_admin_stuck_payment(bot, db, row: dict):
    """Mirrors bot_support._notify_admin's exact pattern: read
    bot:admin_tg_id, send_message, never let a notification failure mask
    the underlying (already durably recorded) DB state."""
    if bot is None or db is None or row is None:
        return
    admin_tg_id = db.get_setting("bot:admin_tg_id")
    if not admin_tg_id:
        return
    status = row.get("status")
    reason = {
        "apply_failed_user_missing": "пользователь Marzban не найден",
        "apply_retry_exhausted": "исчерпаны попытки применить платёж",
        "manual_review": row.get("manual_review_reason") or "требуется проверка",
    }.get(status, status)
    try:
        await bot.send_message(
            int(admin_tg_id),
            "⭐️ Проблемный платёж (Stars)\n"
            f"Счёт #{row.get('id')} — {row.get('marzban_username')}\n"
            f"Статус: {status}\nПричина: {reason}",
        )
    except Exception as e:
        logger.warning(f"Не удалось уведомить admin о проблемном платеже: {e}")


async def notify_user_extended(bot, row: dict):
    payer = row.get("payer_telegram_id")
    if bot is None or not payer:
        return
    try:
        await bot.send_message(
            int(payer),
            f"✅ Подписка продлена на {row.get('duration_days')} дн. Спасибо за оплату!",
        )
    except Exception as e:
        logger.warning(f"Не удалось уведомить пользователя о продлении: {e}")


# --- apply-worker -----------------------------------------------------------

def _exhaust_before_network(db, row: dict, notifications: list | None) -> bool:
    """Honor a durable retry budget before starting any new remote call."""
    if int(row.get("apply_attempts") or 0) < MAX_APPLY_ATTEMPTS:
        return False
    if db.mark_invoice_apply_failed(row["id"], "retry_exhausted"):
        if notifications is not None:
            notifications.append(("admin", db.get_invoice(row["id"])))
    return True


async def _commit_plan(bot, db, marzban, admin_token, row: dict,
                       notifications: list | None = None) -> dict | None:
    """Step 2a: paid -> plan_committed. Returns the refreshed row if a plan
    was committed (or already existed), None if the row was routed
    elsewhere (manual_review/apply_failed) or should be retried later."""
    invoice_id = row["id"]
    username = row["marzban_username"]
    if _exhaust_before_network(db, row, notifications):
        return None
    try:
        user = await _run_sync(marzban.get_user, username, admin_token)
    except Exception as e:
        if _is_not_found(e):
            if db.mark_invoice_apply_failed(invoice_id, "user_missing"):
                if notifications is not None:
                    notifications.append(("admin", db.get_invoice(invoice_id)))
            return None
        db.record_apply_attempt_failure(invoice_id, str(e), MAX_APPLY_ATTEMPTS)
        fresh = db.get_invoice(invoice_id)
        if fresh and fresh["status"] == "apply_retry_exhausted":
            if notifications is not None:
                notifications.append(("admin", fresh))
        return None

    ok, reason = _check_stars_eligibility(user)
    if not ok:
        full_reason = f"eligibility_changed_after_payment: {reason}"
        if db.mark_invoice_manual_review(invoice_id, reason=full_reason):
            if notifications is not None:
                notifications.append(("admin", db.get_invoice(invoice_id)))
        return None

    base_expire_observed = int(user.get("expire") or 0)
    now = int(time.time())
    target_expire = max(base_expire_observed, now) + int(row["duration_days"]) * 86400

    committed = db.commit_apply_plan(invoice_id, base_expire_observed=base_expire_observed, target_expire=target_expire)
    if not committed:
        return None
    return db.get_invoice(invoice_id)


async def _resolve_plan(bot, db, marzban, admin_token, row: dict,
                        notifications: list | None = None):
    """Step 2b: resolve a plan_committed row via the 3-case recovery
    comparison (§4.3). Always re-fetches first — never trusts local memory
    of "I think the PUT already landed"."""
    invoice_id = row["id"]
    username = row["marzban_username"]
    if _exhaust_before_network(db, row, notifications):
        return
    try:
        user = await _run_sync(marzban.get_user, username, admin_token)
    except Exception as e:
        if _is_not_found(e):
            if db.mark_invoice_apply_failed(invoice_id, "user_missing"):
                if notifications is not None:
                    notifications.append(("admin", db.get_invoice(invoice_id)))
            return
        db.record_apply_attempt_failure(invoice_id, str(e), MAX_APPLY_ATTEMPTS)
        fresh = db.get_invoice(invoice_id)
        if fresh and fresh["status"] == "apply_retry_exhausted":
            if notifications is not None:
                notifications.append(("admin", fresh))
        return

    live_expire = int(user.get("expire") or 0)
    base = row["base_expire_observed"]
    target = row["target_expire"]

    # ---- CASE 1: already applied ----
    if live_expire == target:
        if db.mark_invoice_applied(invoice_id, applied_expire=target):
            db.log_audit_event(
                "subscription_extended", telegram_id=row["payer_telegram_id"],
                marzban_username=username,
                metadata={"invoice_id": invoice_id, "days": row["duration_days"],
                          "new_expire": target, "via": "recovery_match"},
            )
            if notifications is not None:
                notifications.append(("user", row))
        return

    # ---- CASE 2: nothing applied yet — safe to (re)attempt the SAME target ----
    if live_expire == base:
        try:
            await _run_sync(marzban.modify_user, username, {"expire": target}, admin_token)
        except Exception as e:
            db.record_apply_attempt_failure(invoice_id, str(e), MAX_APPLY_ATTEMPTS)
            fresh = db.get_invoice(invoice_id)
            if fresh and fresh["status"] == "apply_retry_exhausted":
                if notifications is not None:
                    notifications.append(("admin", fresh))
            return
        if db.mark_invoice_applied(invoice_id, applied_expire=target):
            db.log_audit_event(
                "subscription_extended", telegram_id=row["payer_telegram_id"],
                marzban_username=username,
                metadata={"invoice_id": invoice_id, "days": row["duration_days"],
                          "new_expire": target, "via": "direct_apply"},
            )
            if notifications is not None:
                notifications.append(("user", row))
        return

    # ---- CASE 3: ambiguous — neither base nor target. Do NOT guess. ----
    reason = f"live_expire_mismatch: expected base={base} or target={target}, got {live_expire}"
    if db.mark_invoice_manual_review(invoice_id, reason=reason):
        if notifications is not None:
            notifications.append(("admin", db.get_invoice(invoice_id)))


async def process_invoice_row(bot, db, marzban, admin_token, row: dict):
    """Full per-invoice processing for one worker-tick pass, holding the
    per-username lock across the entire fetch->commit->re-fetch->compare->
    write sequence (§5.2) so nothing else touching this marzban_username
    (in particular handle_internal_user_renew) can interleave partway
    through."""
    username = row["marzban_username"]
    lock = marzban_user_locks.get(username)
    notifications = []
    async with lock:
        current = db.get_invoice(row["id"])
        if current is not None and current["status"] == "paid":
            current = await _commit_plan(
                bot, db, marzban, admin_token, current, notifications=notifications
            )
        if current is not None and current["status"] == "plan_committed":
            await _resolve_plan(
                bot, db, marzban, admin_token, current, notifications=notifications
            )

    # Telegram I/O is never part of the shared Marzban read/decide/write
    # critical section. Durable DB state is already final before notifying.
    for kind, notification_row in notifications:
        if kind == "admin":
            await notify_admin_stuck_payment(bot, db, notification_row)
        else:
            await notify_user_extended(bot, notification_row)


async def process_canonical_invoice_row(bot, db, row: dict):
    """Apply a paid PH5-05 invoice through the canonical renewal store.

    No live Marzban parent is inspected or mutated here.  The durable payment
    evidence has already passed payer/currency/amount/product checks; PH5-02
    supplies the invoice-scoped idempotent local grant.
    """
    try:
        result = await _run_sync(db.stars_purchases.apply_paid_invoice, row["id"])
    except StarsPurchaseError:
        fresh = db.get_invoice(row["id"])
        if fresh and fresh.get("status") == "manual_review":
            await notify_admin_stuck_payment(bot, db, fresh)
        return
    except Exception as exc:
        logger.error("canonical Stars apply failed for invoice %s: %s", row["id"], exc)
        return
    if not result.get("already_applied"):
        fresh = db.get_invoice(row["id"])
        db.log_audit_event(
            "subscription_extended", telegram_id=fresh.get("payer_telegram_id"),
            marzban_username=fresh.get("marzban_username"),
            metadata={"invoice_id": row["id"], "new_expire": result["new_expiry"],
                      "via": "canonical_ph5_05", "mutation_id": result["mutation_id"]},
        )
        await notify_user_extended(bot, fresh)


async def _sync_canonical_purchase_children(db, marzban):
    """Drive PH3-08's existing durable child-sync outbox for paid grants."""
    for job in db.stars_purchases.pending_sync_jobs():
        try:
            result = await _run_sync(lambda: run_account_sync_cycle(
                db, job["account_id"], sync_fn=marzban.sync_child_user_state,
                worker_id="stars-ph5-05", now=int(time.time()),
            ))
            # Only PH3-08's terminal aggregate state proves convergence.  A
            # PENDING/PARTIAL cycle can represent a leased or backoff-bound
            # retry, so it must remain recoverable rather than falsely mark
            # the paid Stars hand-off complete.
            state = (
                "SYNCED" if result["aggregate_state"] == "IN_SYNC" else
                "MANUAL_REVIEW" if result["aggregate_state"] == "MANUAL_REVIEW" or result["errored"] else
                "PENDING"
            )
            await _run_sync(lambda: db.stars_purchases.record_sync_result(job["invoice_id"], state=state))
        except Exception as exc:
            logger.warning("canonical Stars child sync retry for invoice %s: %s", job["invoice_id"], exc)
            await _run_sync(lambda: db.stars_purchases.record_sync_result(
                job["invoice_id"], state="PENDING", error_class=type(exc).__name__,
            ))


async def _tick(bot, db, marzban, admin_token):
    for row in db.stars_purchases.pending_invoices():
        await process_canonical_invoice_row(bot, db, row)
    await _sync_canonical_purchase_children(db, marzban)
    if not admin_token:
        return
    processed_usernames: set = set()
    for row in db.get_pending_apply_invoices():
        username = row["marzban_username"]
        if username in processed_usernames:
            # Head-of-line for this username is still unresolved this tick
            # (or already handled) — later rows stay parked, never reordered
            # past it (§5.1).
            continue
        processed_usernames.add(username)
        try:
            await process_invoice_row(bot, db, marzban, admin_token, row)
        except Exception as e:
            logger.error(f"stars apply-worker: unexpected error processing invoice {row.get('id')}: {e}")


async def apply_pending_payments_loop(bot, db, marzban, stop_event, trigger_event: asyncio.Event | None = None):
    """Periodic loop living inside BotRunner's asyncio event loop, exactly
    like bot_monitor._monitor_loop's 60s poll — plus an asyncio.Event
    fast-path so a fresh payment is picked up immediately instead of
    waiting up to RETRY_INTERVAL_SECONDS."""
    trigger_event = trigger_event if trigger_event is not None else asyncio.Event()
    while not stop_event.is_set():
        try:
            admin_token = await _run_sync(marzban.get_admin_token_from_env)
        except Exception as e:
            logger.warning(f"stars apply-worker: could not obtain Marzban admin token: {e}")
            admin_token = None

        try:
            await _tick(bot, db, marzban, admin_token)
        except Exception as e:
            logger.error(f"stars apply-worker: tick failed: {e}")

        trigger_event.clear()
        if stop_event.is_set():
            break
        try:
            await asyncio.wait_for(trigger_event.wait(), timeout=RETRY_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            pass
