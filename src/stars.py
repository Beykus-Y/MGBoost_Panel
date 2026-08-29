"""Telegram Stars payment durability worker + shared eligibility rule.

Implements the two-phase commit-then-apply flow and the 3-case recovery
comparison from the Phase 2 design doc (§3/§4). Lives inside the bot
thread's asyncio event loop, started alongside bot_monitor._monitor_loop
(see bot_monitor.run_all), polling every RETRY_INTERVAL_SECONDS with a fast
first-attempt path triggered via an asyncio.Event.
"""
import asyncio
import logging
import secrets
import time
from urllib.error import HTTPError

from .marzban_lock import marzban_user_locks
from .parent_sync import run_account_sync_cycle
from .stars_purchase import StarsPurchaseError

logger = logging.getLogger(__name__)

MAX_APPLY_ATTEMPTS = 5
RETRY_INTERVAL_SECONDS = 30


def _opaque_subscription_enabled() -> bool:
    from .config import OPAQUE_SUBSCRIPTION_ENABLED
    return bool(OPAQUE_SUBSCRIPTION_ENABLED)


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


async def _notify_admin_signup_issue(bot, db, text: str):
    """Generic admin alert for signup-side manual-review states, mirroring
    notify_admin_stuck_payment's failure-honesty (a failed notification must
    never mask the already-durable DB state)."""
    if bot is None or db is None:
        return
    admin_tg_id = db.get_setting("bot:admin_tg_id")
    if not admin_tg_id:
        return
    try:
        await bot.send_message(int(admin_tg_id), f"🛒 Проблемный commercial signup:\n{text}")
    except Exception as e:
        logger.warning(f"Не удалось уведомить admin о signup-проблеме: {e}")


# --- PH5-11 signup delivery (initial opaque credential) ----------------------

def _active_credential_row(db, account_id: int):
    return db._conn.execute(
        "SELECT id, generation FROM mgboost_subscription_credentials "
        "WHERE account_id=? AND status='ACTIVE'", (int(account_id),),
    ).fetchone()


async def _deliver_signup_credential(bot, db, row: dict):
    """Initial opaque-credential issuance for a freshly applied signup.

    Mirrors the crash-safe split sequencing of bot_support's
    ``_deliver_and_activate``: deliver the raw token first, activate only if
    delivery did not raise. Never rotates an existing ACTIVE credential --
    if one already exists (e.g. a prior attempt fully succeeded), the user
    is pointed at /newsub instead. A delivery failure leaves exactly the old
    state plus a harmless PENDING_DELIVERY row for the next attempt to
    abandon and retry."""
    account_id = int(row["account_id"])
    payer = row.get("payer_telegram_id")
    if bot is None or not payer:
        return
    if not _opaque_subscription_enabled():
        await _notify_admin_signup_issue(
            bot, db,
            f"signup invoice #{row['id']} applied, but OPAQUE_SUBSCRIPTION_ENABLED "
            f"is off -- credential for account #{account_id} was NOT issued",
        )
        return
    try:
        existing = await _run_sync(_active_credential_row, db, account_id)
    except Exception as e:
        logger.error(f"signup credential pre-check failed for account {account_id}: {type(e).__name__}")
        return
    if existing is not None:
        try:
            await bot.send_message(
                int(payer),
                "🔗 Ссылка подписки уже была отправлена ранее. "
                "Если вы её потеряли — отправьте /newsub для перевыпуска.",
            )
        except Exception as e:
            logger.warning(f"signup credential hint delivery failed: {type(e).__name__}")
        return

    actor_ref = f"telegram:{int(payer)}"
    timestamp = int(time.time())
    op_key = f"{row['id']}:{timestamp}:{secrets.token_urlsafe(16)}"
    try:
        await _run_sync(
            lambda: db.subscription_credentials.abandon_pending(
                account_id=account_id, actor_ref=actor_ref,
                idempotency_key=f"ph5-11-signup-abandon:{op_key}", now=timestamp,
            )
        )
        prepared = await _run_sync(
            lambda: db.subscription_credentials.prepare(
                account_id=account_id, actor_ref=actor_ref,
                reason="commercial signup initial issuance",
                idempotency_key=f"ph5-11-signup-prepare:{op_key}", now=timestamp,
            )
        )
    except Exception as e:
        logger.error(f"signup credential prepare failed for account {account_id}: {type(e).__name__}")
        return
    delivered = False
    try:
        await bot.send_message(
            int(payer),
            "🔗 Ваша ссылка подписки:\n"
            f"https://sub.beykus.fun/{prepared['raw_token']}\n\n"
            "Сохраните её сейчас — повторно показать эту же ссылку сервер не сможет. "
            "Откройте её в приложении VPN, чтобы подключить устройство.",
        )
        delivered = True
    except Exception as e:
        logger.error(f"signup credential delivery failed for account {account_id}: {type(e).__name__}")
        await _notify_admin_signup_issue(
            bot, db,
            f"signup invoice #{row['id']} applied, but initial credential delivery to "
            f"account #{account_id} failed ({type(e).__name__}) -- recoverable via /newsub, "
            f"but the customer was not told to use it",
        )
    if delivered:
        try:
            await _run_sync(
                lambda: db.subscription_credentials.activate(
                    credential_id=prepared["id"], account_id=account_id,
                    expected_generation=prepared["generation"], actor_ref=actor_ref,
                    idempotency_key=f"ph5-11-signup-activate:{op_key}", now=timestamp,
                )
            )
        except Exception as e:
            logger.error(f"signup credential activation failed for account {account_id}: {type(e).__name__}")


async def _notify_signup_applied(bot, db, row: dict):
    """Post-apply user notification for a signup invoice: welcome text plus
    the initial subscription link delivery (CREATE) or the ordinary renewal
    text (a second purchase of the same plan on the now-existing account)."""
    if bot is None:
        return
    operation = "RENEW"
    try:
        applied = db._conn.execute(
            "SELECT applied_operation FROM mgboost_stars_purchase_applications WHERE invoice_id=?",
            (int(row["id"]),),
        ).fetchone()
        if applied is not None:
            operation = applied["applied_operation"]
    except Exception as e:
        logger.warning(f"signup applied-operation lookup failed: {type(e).__name__}")
    if operation == "CREATE":
        payer = row.get("payer_telegram_id")
        try:
            await bot.send_message(
                int(payer),
                f"🎉 Подписка «{row.get('tariff_name')}» активирована "
                f"на {row.get('duration_days')} дн. Спасибо за покупку!",
            )
        except Exception as e:
            logger.warning(f"signup welcome notification failed: {type(e).__name__}")
        await _deliver_signup_credential(bot, db, row)
    else:
        await notify_user_extended(bot, row)


async def _process_admin_grant_template_jobs(db, marzban, bot):
    """PH7-14 counterpart to `_process_signup_template_jobs`, for accounts
    bootstrapped by `AdminGrantStore` (no invoice to key a
    `mgboost_signup_template_jobs` row on). Same convergence loop, same
    remote provisioning call (`ensure_template_for_account` -- reused
    unchanged; it already falls back to the account's own current
    subscription's `wl_mode` when no PH5-11 job row exists, which is
    exactly this case), a separate queue only."""
    jobs = db.admin_grants.pending_template_jobs() if hasattr(db, "admin_grants") else []
    for job in jobs:
        account_id = int(job["account_id"])
        try:
            result = await _run_sync(
                lambda: db.commercial_signup.ensure_template_for_account(
                    account_id, marzban=marzban,
                )
            )
        except Exception as exc:
            logger.warning(
                "admin-grant template provisioning retry for account %s: %s",
                account_id, type(exc).__name__,
            )
            await _run_sync(
                lambda: db.admin_grants.record_template_result(
                    account_id, state="PENDING", error_class=type(exc).__name__,
                )
            )
            continue
        state = result.get("state")
        if state == "READY":
            await _run_sync(
                lambda: db.admin_grants.record_template_result(account_id, state="READY")
            )
        else:
            error_class = result.get("error_class") or "template_unknown"
            await _run_sync(
                lambda: db.admin_grants.record_template_result(
                    account_id, state="MANUAL_REVIEW", error_class=error_class,
                )
            )
            await _notify_admin_signup_issue(
                bot, db,
                f"admin-grant template job for account #{account_id} -> MANUAL_REVIEW "
                f"({error_class})",
            )


async def _process_signup_template_jobs(db, marzban, bot):
    """Drive the durable PH5-11 template-provisioning jobs to convergence.

    The paid entitlement never depends on this: account, subscription and
    credential are already durable; the template only unlocks first-device
    bootstrap. Failures stay PENDING (bounded by the job's own retries in
    later ticks); poison states become MANUAL_REVIEW with an admin alert."""
    jobs = db.commercial_signup.pending_template_jobs() if hasattr(db, "commercial_signup") else []
    for job in jobs:
        account_id = int(job["account_id"])
        try:
            result = await _run_sync(
                lambda: db.commercial_signup.ensure_template_for_account(
                    account_id, marzban=marzban,
                )
            )
        except Exception as exc:
            logger.warning(
                "signup template provisioning retry for account %s: %s",
                account_id, type(exc).__name__,
            )
            await _run_sync(
                lambda: db.commercial_signup.record_template_result(
                    account_id, state="PENDING", error_class=type(exc).__name__,
                )
            )
            continue
        state = result.get("state")
        if state == "READY":
            await _run_sync(
                lambda: db.commercial_signup.record_template_result(
                    account_id, state="READY",
                )
            )
        else:
            error_class = result.get("error_class") or "template_unknown"
            await _run_sync(
                lambda: db.commercial_signup.record_template_result(
                    account_id, state="MANUAL_REVIEW", error_class=error_class,
                )
            )
            await _notify_admin_signup_issue(
                bot, db,
                f"template job for account #{account_id} -> MANUAL_REVIEW "
                f"({error_class})",
            )


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
        if fresh.get("invoice_kind") == "CANONICAL_SIGNUP":
            await _notify_signup_applied(bot, db, fresh)
        else:
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
    await _process_signup_template_jobs(db, marzban, bot)
    await _process_admin_grant_template_jobs(db, marzban, bot)
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
