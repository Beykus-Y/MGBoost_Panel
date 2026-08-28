import logging
import re
import time
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

SQLITE_MAX_INTEGER = (1 << 63) - 1

_GB_DECIMAL = 1_000_000_000


def wl_quota_line(item: dict) -> str:
    """Human-readable WL quota for one catalog SKU. A 60-day SKU is ALWAYS
    described per 30-day period (never as a doubled total) -- each 30-day
    WL period carries its own full quota and remainder is never carried."""
    quota_bytes = item.get("wl_quota_bytes") or 0
    if not quota_bytes:
        return ""
    gb = quota_bytes // _GB_DECIMAL
    days = int(item["duration_days"])
    if days <= 30:
        return f"Трафик: {gb} GB на {days} дн.\n"
    return (
        f"Трафик: {gb} GB каждые 30 дней "
        f"({days // 30} периода по {gb} GB)\n"
    )


class PaymentDurabilityError(BaseException):
    """Stop polling before an unpersisted payment update is confirmed.

    This deliberately derives from BaseException: aiogram catches ordinary
    Exceptions and treats the update as processed. BotRunner uses sequential
    polling so this escapes before getUpdates advances its offset.
    """


def _parse_stars_invoice_id(payload) -> int | None:
    if not isinstance(payload, str) or not re.fullmatch(r"[1-9][0-9]*", payload):
        return None
    try:
        value = int(payload)
    except (ValueError, OverflowError):
        return None
    return value if value <= SQLITE_MAX_INTEGER else None

_SUB_TOKEN_RE = re.compile(r"https?://[^/]+/sub/([^/\s?#]+)")

SYSTEM_PROMPT = """Ты — помощник поддержки VPN-сервиса MGBoost. Помогаешь обычным людям, не программистам.

Правила общения:
- Пиши просто, как живой человек. Без технических терминов и портянок.
- Короткие сообщения. Максимум 3–4 предложения на ответ.
- Не давай сразу 5 советов — спроси уточняющий вопрос и дай один конкретный шаг.
- Не упоминай TCP/UDP, TLS, порты и прочую техническую лабуду — пользователь этого не знает.
- Если нужна инфа — используй инструменты (подписка, статус нод, история).
- Если не можешь помочь или вопрос сложный — передай оператору через escalate_to_human.
- Отвечай только по теме VPN и подписки. На остальное — "не по теме, но могу помочь с VPN".

Стиль: дружелюбно, коротко, по делу. Как будто помогает живой человек, а не робот."""


def parse_sub_link(text: str) -> str | None:
    m = _SUB_TOKEN_RE.search(text.strip())
    return m.group(1) if m else None


def format_subscription(user: dict) -> str:
    username = user.get("username", "?")
    status = user.get("status", "unknown")
    data_limit = user.get("data_limit") or 0
    used = user.get("used_traffic") or 0
    expire = user.get("expire")

    status_ru = {
        "active": "✅ Активна",
        "expired": "❌ Истекла",
        "disabled": "⛔ Отключена",
        "limited": "⚠️ Лимит трафика",
        "on_hold": "⏸ На паузе",
    }.get(status, status)

    if data_limit == 0:
        traffic_str = f"Использовано: {_fmt_bytes(used)} / ∞"
    else:
        traffic_str = f"Использовано: {_fmt_bytes(used)} / {_fmt_bytes(data_limit)}"

    expire_str = ""
    if expire:
        dt = datetime.fromtimestamp(expire, tz=timezone.utc)
        expire_str = f"\nДата окончания: {dt.strftime('%d.%m.%Y')}"

    return (
        f"👤 Пользователь: {username}\n"
        f"Статус: {status_ru}\n"
        f"{traffic_str}{expire_str}"
    )


def _fmt_bytes(b: int) -> str:
    if b == 0:
        return "0 B"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if b < 1024:
            return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} PB"


def _canonical_subscription_summary(db, telegram_id: int) -> str | None:
    """Render the canonical (``mgboost_accounts``) subscription state for an
    ACTIVE account with a non-revoked OWNER telegram identity, or None when
    the id resolves to no such account. Same resolver as Stars signup and
    renewal (``AccountStore.get_active_account_by_telegram_id``) -- a legacy
    ``tg_users`` link is deliberately not consulted, and possession of a
    subscription URL/HWID/username never counts as ownership here."""
    account = db.accounts.get_active_account_by_telegram_id(telegram_id)
    if account is None:
        return None
    ent = db.entitlements.calculate(account_id=int(account["id"]))
    sub = (ent or {}).get("subscription") or {}
    plan = ((ent or {}).get("plan") or {})
    expire = sub.get("effective_expiry")
    expire_str = ""
    if expire:
        dt = datetime.fromtimestamp(int(expire), tz=timezone.utc)
        expire_str = f"\nДата окончания: {dt.strftime('%d.%m.%Y')}"
    return (
        f"👤 Тариф: {plan.get('display_name') or plan.get('code') or '—'}\n"
        f"Статус: {sub.get('effective_status') or '—'}{expire_str}\n\n"
        "Ваша ссылка подписки была отправлена вам после оплаты. "
        "Если вы её потеряли — отправьте /newsub для перевыпуска."
    )


DEFAULT_FAQ = """## Частые проблемы

**Не обновляется подписка**
Открой приложение → найди свою подписку → нажми "Обновить" или значок обновления. Если не помогает — удали подписку и добавь заново по той же ссылке.

**Таймаут при подключении / не подключается**
Попробуй сменить сервер — выбери другую страну в приложении. Обычно помогает. Если все серверы не работают — возможно, проблема с интернетом или провайдер блокирует.

**Медленный интернет через VPN**
Смени сервер на ближайший географически. Если не помогает — напиши нам.

**Закончился трафик**
Используй кнопку "Моя подписка" чтобы проверить остаток. Для продления — обратись к оператору.

**Приложение вылетает / глючит**
Переустанови приложение. Рекомендуемые: Hiddify (iOS/Android), V2RayNG (Android)."""


def build_ai_messages(user_message: str, history: list, system: str = SYSTEM_PROMPT) -> list:
    messages = [{"role": "system", "content": system}]
    relevant = [m for m in history if m["role"] in ("user", "ai")][-10:]
    for m in relevant:
        role = "assistant" if m["role"] == "ai" else "user"
        messages.append({"role": role, "content": m["text"]})
    messages.append({"role": "user", "content": user_message})
    return messages


def get_tools() -> list:
    return [
        {
            "type": "function",
            "function": {
                "name": "get_subscription_info",
                "description": "Получить информацию о подписке пользователя: статус, трафик, срок действия.",
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_nodes_status",
                "description": "Получить текущий статус всех VPN-нод (онлайн/офлайн).",
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_ticket_history",
                "description": "Получить историю прошлых обращений пользователя в поддержку.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "limit": {
                            "type": "integer",
                            "description": "Количество тикетов (по умолчанию 3)",
                        }
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "escalate_to_human",
                "description": "Передать диалог живому оператору, если AI не может решить проблему.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "reason": {
                            "type": "string",
                            "description": "Причина передачи оператору",
                        }
                    },
                    "required": ["reason"],
                },
            },
        },
    ]


async def execute_tool(
    name: str, args: dict, *, db, marzban, telegram_id: int,
    node_states: dict, node_names: dict
) -> str:
    if name == "get_subscription_info":
        tg_user = db.get_tg_user(telegram_id)
        if not tg_user:
            # canary hotfix: canonical DIRECT owner without a legacy link -- report
            # the canonical entitlement instead of claiming no subscription.
            try:
                summary = _canonical_subscription_summary(db, telegram_id)
            except Exception as e:
                logger.error(f"execute_tool get_subscription_info canonical: {e}")
                return "Не удалось получить информацию о подписке."
            return summary or "Подписка не привязана."
        try:
            admin_token = await _run_sync(marzban.get_admin_token_from_env)
            user_info = await _run_sync(marzban.get_user, tg_user["marzban_username"], admin_token)
            return format_subscription(user_info)
        except Exception as e:
            logger.error(f"execute_tool get_subscription_info: {e}")
            return "Не удалось получить информацию о подписке."

    if name == "get_nodes_status":
        if not node_states:
            return "Статус нод ещё не загружен."
        lines = []
        for nid, state in node_states.items():
            icon = "🟢" if state.get("up") else "🔴"
            n_name = node_names.get(nid, str(nid))
            lines.append(f"{icon} {n_name}")
        return "\n".join(lines) if lines else "Ноды не найдены."

    if name == "get_ticket_history":
        limit = int(args.get("limit", 3))
        tickets = db.list_tickets(status="closed", limit=limit)
        user_tickets = [t for t in tickets if t["telegram_id"] == telegram_id]
        if not user_tickets:
            return "История обращений пуста."
        parts = []
        for t in user_tickets[:limit]:
            msgs = db.get_ticket_messages(t["id"], limit=10)
            summary = " / ".join(
                m["text"][:60] for m in msgs if m["role"] in ("user", "ai")
            )
            parts.append(f"Тикет #{t['id']}: {summary}")
        return "\n\n".join(parts)

    if name == "escalate_to_human":
        reason = args.get("reason", "")
        tg_user = db.get_tg_user(telegram_id)
        username = tg_user["marzban_username"] if tg_user else None
        ticket = db.get_open_ticket(telegram_id)
        if ticket:
            db.update_ticket_status(ticket["id"], "waiting_human")
            if reason:
                db.add_ticket_message(ticket["id"], "ai", f"[AI передал оператору: {reason}]")
        else:
            tid = db.create_ticket(telegram_id, marzban_username=username, status="waiting_human")
            if reason:
                db.add_ticket_message(tid, "ai", f"[AI передал оператору: {reason}]")
        return f"Передаю оператору: {reason}"

    return f"Неизвестный инструмент: {name}"


async def ask_openrouter_with_tools(
    api_key: str, model: str, messages: list, tools: list, *,
    db, marzban, telegram_id: int, node_states: dict, node_names: dict,
    max_tool_rounds: int = 3,
) -> str:
    if not api_key:
        return "AI-ассистент недоступен. Нажмите «🆘 Позвать человека»."

    import json as _json
    import aiohttp

    headers = {
        "Authorization": f"Bearer {api_key}",
        "HTTP-Referer": "https://sub.beykus.fun",
        "X-Title": "MGBoost Support",
    }

    current_messages = list(messages)

    async with aiohttp.ClientSession() as session:
        for _ in range(max_tool_rounds):
            try:
                payload = {"model": model, "messages": current_messages}
                if tools:
                    payload["tools"] = tools
                    payload["tool_choice"] = "auto"

                resp = await session.post(
                    "https://openrouter.ai/api/v1/chat/completions",
                    headers=headers,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=30),
                )
                data = await resp.json()
            except Exception as e:
                logger.error(f"OpenRouter request error: {e}")
                return "AI-ассистент временно недоступен."

            choice = data.get("choices", [{}])[0]
            finish_reason = choice.get("finish_reason", "stop")
            msg = choice.get("message", {})

            if finish_reason != "tool_calls" or not msg.get("tool_calls"):
                return msg.get("content") or "Не удалось получить ответ."

            current_messages.append(msg)

            for tc in msg["tool_calls"]:
                fn_name = tc["function"]["name"]
                try:
                    fn_args = _json.loads(tc["function"].get("arguments", "{}"))
                except Exception:
                    fn_args = {}

                tool_result = await execute_tool(
                    fn_name, fn_args,
                    db=db, marzban=marzban, telegram_id=telegram_id,
                    node_states=node_states, node_names=node_names,
                )
                current_messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": tool_result,
                })

    return "AI не смог завершить ответ. Попробуйте ещё раз."


async def ask_openrouter(api_key: str, model: str, messages: list) -> str:
    return await ask_openrouter_with_tools(
        api_key, model, messages, tools=[],
        db=None, marzban=None, telegram_id=0,
        node_states={}, node_names={},
    )


def build_system_prompt(db) -> str:
    faq = db.get_setting("bot:support_faq") if db else None
    if faq:
        return SYSTEM_PROMPT + "\n\n" + faq
    return SYSTEM_PROMPT + "\n\n" + DEFAULT_FAQ


def setup_support_handlers(dp, db, marzban, node_states: dict | None = None, node_names: dict | None = None,
                            stars_trigger=None):
    try:
        from aiogram import F
        from aiogram.enums import ChatType
        from aiogram.filters import Command, CommandStart, StateFilter
        from aiogram.fsm.context import FSMContext
        from aiogram.fsm.state import State, StatesGroup
        from aiogram.types import (
            CallbackQuery,
            InlineKeyboardButton,
            InlineKeyboardMarkup,
            KeyboardButton,
            LabeledPrice,
            Message,
            PreCheckoutQuery,
            ReplyKeyboardMarkup,
            ReplyKeyboardRemove,
        )
    except ImportError:
        logger.error("aiogram не установлен — поддержка не запустится")
        return

    from .stars import _check_stars_eligibility
    from .commercial_signup import SIGNUP_INVOICE_KIND

    _CANONICAL_INVOICE_KINDS = ("CANONICAL_PLAN", SIGNUP_INVOICE_KIND)

    class SupportStates(StatesGroup):
        waiting_link = State()
        in_dialog = State()
        waiting_human = State()

    def kb_main():
        return ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="📋 Моя подписка"), KeyboardButton(text="🆘 Позвать человека")],
                [KeyboardButton(text="🔧 Управление устройствами")],
                [KeyboardButton(text="🛒 Купить VPN"), KeyboardButton(text="⭐️ Продлить подписку")],
            ],
            resize_keyboard=True,
        )

    def kb_new_user():
        return ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="🛒 Купить VPN")]],
            resize_keyboard=True,
        )

    def kb_tariffs(tariffs: list):
        rows = [
            [InlineKeyboardButton(
                text=f"{t['display_name']} — {t['duration_days']} дн. — {t['amount']} ⭐️",
                callback_data=f"stars_buy:{t['plan_code']}:{t['duration_days']}",
            )]
            for t in tariffs
        ]
        return InlineKeyboardMarkup(inline_keyboard=rows)

    def kb_waiting():
        return ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="⬅️ Назад к боту")]],
            resize_keyboard=True,
        )

    async def _send_stars_invoice(bot, chat_id: int, invoice: dict):
        """Single canonical invoice sender: every field comes from the
        server-side invoice row (catalog snapshot), never from callback
        data. Single-chat invoice mode -- a forwarded copy gets a deep-link
        button, never another Pay button."""
        from .plan_catalog import GB_DECIMAL
        quota_row = db._conn.execute(
            "SELECT wl_quota_bytes FROM mgboost_plan_versions WHERE id=?",
            (invoice["plan_version_id"],),
        ).fetchone()
        quota_bytes = quota_row["wl_quota_bytes"] if quota_row else 0
        quota_text = ""
        if quota_bytes:
            gb = quota_bytes // GB_DECIMAL
            days = int(invoice["duration_days"])
            if days <= 30:
                quota_text = f" — {gb} GB на {days} дн."
            else:
                quota_text = f" — {gb} GB каждые 30 дней ({days // 30} периода по {gb} GB)"
        try:
            await bot.send_invoice(
                chat_id=chat_id,
                title=invoice["tariff_name"],
                description=f"{invoice['tariff_name']} — {invoice['duration_days']} дней{quota_text}",
                payload=str(invoice["id"]),
                provider_token="",
                currency="XTR",
                prices=[LabeledPrice(label=invoice["tariff_name"], amount=invoice["stars_price"])],
                start_parameter=f"stars_invoice_{invoice['id']}",
            )
        except Exception as e:
            logger.error(f"Не удалось отправить счёт: {e}")
            try:
                await bot.send_message(chat_id, "Не удалось создать счёт. Попробуйте позже.")
            except Exception:
                pass

    def kb_no_link():
        return InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="У меня нет ссылки", callback_data="no_link")]]
        )

    # Payment service updates must be registered before every FSM/state
    # catch-all. MemoryStorage is intentionally ephemeral, so after a bot
    # restart a legitimate successful_payment arrives in State(None).
    @dp.pre_checkout_query()
    async def on_pre_checkout(query: PreCheckoutQuery):
        payload = query.invoice_payload
        invoice_id = _parse_stars_invoice_id(payload)
        if invoice_id is None:
            await query.answer(ok=False, error_message="Неверные данные счёта.")
            return
        row = db.get_invoice(invoice_id)
        if row is None:
            await query.answer(ok=False, error_message="Счёт не найден.")
            return
        if row["status"] != "created":
            await query.answer(ok=False, error_message="Счёт уже недействителен.")
            return
        if query.currency != "XTR":
            await query.answer(ok=False, error_message="Неподдерживаемая валюта.")
            return
        if query.total_amount != row["stars_price"]:
            await query.answer(ok=False, error_message="Сумма не совпадает.")
            return
        # Payable interval is explicitly created_at <= now < expires_at.
        if int(time.time()) >= row["expires_at"]:
            await query.answer(ok=False, error_message="Счёт истёк, создайте новый.")
            return
        if row.get("invoice_kind") in _CANONICAL_INVOICE_KINDS:
            try:
                db.stars_purchases.validate_invoice_for_checkout(
                    invoice_id, query.from_user.id, now=int(time.time())
                )
            except Exception:
                await query.answer(ok=False, error_message="Счёт больше недействителен. Обратитесь в поддержку.")
                return
            await query.answer(ok=True)
            return
        # Telegram charges after this acknowledgement.  Re-read the exact
        # target user through the authenticated service boundary immediately
        # before accepting payment, so a broker/Marzban outage or an account
        # that became ineligible cannot create a newly-paid entitlement that
        # we already know we cannot safely apply.  If availability disappears
        # after this point, successful_payment is still captured durably and
        # the apply worker retries as before.
        try:
            if marzban is None:
                raise RuntimeError("Marzban service is unavailable")
            admin_token = await _run_sync(marzban.get_admin_token_from_env)
            user_info = await _run_sync(
                marzban.get_user, row["marzban_username"], admin_token
            )
            eligible, _reason = _check_stars_eligibility(user_info)
            if not eligible:
                await query.answer(
                    ok=False,
                    error_message="Подписка сейчас недоступна для продления.",
                )
                return
        except Exception as exc:
            logger.warning("Stars pre-checkout rejected: entitlement service unavailable: %s", exc)
            await query.answer(
                ok=False,
                error_message="Сервис продления временно недоступен. Попробуйте позже.",
            )
            return
        await query.answer(ok=True)

    async def capture_orphan_payment(message: Message, reason: str):
        sp = message.successful_payment
        try:
            row, created = db.record_stars_orphan_payment(
                telegram_payment_charge_id=sp.telegram_payment_charge_id,
                provider_payment_charge_id=sp.provider_payment_charge_id,
                payer_telegram_id=message.from_user.id,
                currency=sp.currency,
                total_amount=sp.total_amount,
                invoice_payload=sp.invoice_payload,
                reason=reason,
            )
        except Exception as exc:
            logger.critical("Could not durably capture Stars payment as orphan", exc_info=True)
            raise PaymentDurabilityError("Stars payment was not durably captured") from exc
        if created:
            await _notify_admin_orphan_payment(message.bot, db, sp, message.from_user.id)
        return row, created

    @dp.message(F.successful_payment)
    async def on_successful_payment(message: Message, state: FSMContext):
        sp = message.successful_payment
        payer_telegram_id = message.from_user.id
        payload = sp.invoice_payload
        invoice_id = _parse_stars_invoice_id(payload)
        if invoice_id is None:
            await capture_orphan_payment(message, "invalid_invoice_payload")
            return

        try:
            row = db.get_invoice(invoice_id)
        except Exception as exc:
            logger.critical("Could not read Stars invoice before payment capture", exc_info=True)
            raise PaymentDurabilityError("Stars payment was not durably captured") from exc
        if row is None:
            await capture_orphan_payment(message, "invoice_not_found")
            return

        if row["status"] != "created":
            if row.get("telegram_payment_charge_id") == sp.telegram_payment_charge_id:
                return  # ordinary duplicate delivery of the same payment
            await capture_orphan_payment(
                message, f"invoice_not_payable:{row['status']}"
            )
            return

        if row.get("invoice_kind") in _CANONICAL_INVOICE_KINDS:
            try:
                outcome = db.stars_purchases.capture_paid(
                    invoice_id, charge_id=sp.telegram_payment_charge_id,
                    provider_charge_id=sp.provider_payment_charge_id,
                    payer_telegram_id=payer_telegram_id, currency=sp.currency,
                    amount=sp.total_amount,
                )
            except Exception as exc:
                logger.critical("Could not durably capture canonical Stars payment", exc_info=True)
                raise PaymentDurabilityError("Stars payment was not durably captured") from exc
            if outcome == "paid":
                db.log_audit_event(
                    "payment_successful", telegram_id=payer_telegram_id,
                    marzban_username=row["marzban_username"],
                    metadata={"invoice_id": invoice_id, "total_amount": sp.total_amount,
                              "charge_id": sp.telegram_payment_charge_id, "canonical": True},
                )
                await message.answer("Оплата получена! Применяем подписку…")
                if stars_trigger is not None:
                    stars_trigger.set()
            elif outcome == "manual_review":
                await notify_admin_stuck_payment(message.bot, db, db.get_invoice(invoice_id))
            return

        mismatch_reason = None
        if sp.currency != "XTR" or sp.total_amount != row["stars_price"]:
            mismatch_reason = "amount_or_currency_mismatch"
        elif payer_telegram_id != row["created_by_telegram_id"]:
            # Phase 2 MVP invoices are private, single-chat invoices. Shared
            # Marzban subscriptions remain supported because each linked TG
            # account can create its own invoice through the menu.
            mismatch_reason = "unexpected_payer_for_single_chat_invoice"

        if mismatch_reason:
            try:
                ok = db.mark_invoice_paid_but_ambiguous(
                    invoice_id,
                    charge_id=sp.telegram_payment_charge_id,
                    provider_payment_charge_id=sp.provider_payment_charge_id,
                    payer_telegram_id=payer_telegram_id,
                    total_amount=sp.total_amount,
                    currency=sp.currency,
                    reason=mismatch_reason,
                )
            except Exception as exc:
                logger.critical("Could not durably capture ambiguous Stars payment", exc_info=True)
                raise PaymentDurabilityError("Stars payment was not durably captured") from exc
            if ok:
                from .stars import notify_admin_stuck_payment
                await notify_admin_stuck_payment(message.bot, db, db.get_invoice(invoice_id))
            else:
                try:
                    fresh = db.get_invoice(invoice_id)
                except Exception as exc:
                    logger.critical("Could not verify ambiguous Stars payment capture", exc_info=True)
                    raise PaymentDurabilityError("Stars payment capture could not be verified") from exc
                if not fresh or fresh.get("telegram_payment_charge_id") != sp.telegram_payment_charge_id:
                    await capture_orphan_payment(message, "invoice_changed_during_payment_capture")
            return

        try:
            ok = db.mark_invoice_paid(
                invoice_id,
                telegram_payment_charge_id=sp.telegram_payment_charge_id,
                provider_payment_charge_id=sp.provider_payment_charge_id,
                payer_telegram_id=payer_telegram_id,
                total_amount=sp.total_amount,
                currency=sp.currency,
            )
        except Exception as exc:
            logger.critical("Could not durably capture Stars payment", exc_info=True)
            raise PaymentDurabilityError("Stars payment was not durably captured") from exc
        if ok:
            await message.answer("Оплата получена! Продлеваем подписку…")
            if stars_trigger is not None:
                stars_trigger.set()
            return

        try:
            fresh = db.get_invoice(invoice_id)
        except Exception as exc:
            logger.critical("Could not verify Stars payment capture", exc_info=True)
            raise PaymentDurabilityError("Stars payment capture could not be verified") from exc
        if not fresh or fresh.get("telegram_payment_charge_id") != sp.telegram_payment_charge_id:
            await capture_orphan_payment(message, "invoice_changed_during_payment_capture")
    # --- PH5-11 first-rollout purchase UX («Купить VPN») ---------------------
    # Callback data carries ONLY plan_code + duration_days. Every price,
    # name, device limit and the invoice itself are re-resolved server-side
    # from the active immutable catalog inside create_invoice -- a tampered
    # callback can never select another plan, version or price.

    def _sellable_tariffs():
        if db.get_setting("stars:enabled") != "1":
            return None
        return db.stars_purchases.sellable_catalog()

    def _plan_summary(tariffs, plan_code):
        items = [t for t in tariffs if t["plan_code"] == plan_code]
        if not items:
            return None
        items.sort(key=lambda t: t["duration_days"])
        first = items[0]
        return {
            "plan_code": plan_code, "display_name": first["display_name"],
            "device_limit": first["device_limit"], "items": items,
        }

    @dp.message(F.text == "🛒 Купить VPN")
    async def msg_buy_vpn(message: Message, state: FSMContext):
        await state.clear()
        tariffs = _sellable_tariffs()
        if not tariffs:
            await message.answer("Покупка через Telegram Stars временно недоступна, обратитесь к оператору.")
            return
        seen, plans = set(), []
        for t in tariffs:
            if t["plan_code"] not in seen:
                seen.add(t["plan_code"])
                plans.append(_plan_summary(tariffs, t["plan_code"]))
        rows = [[InlineKeyboardButton(
            text=f"{p['display_name']} — до {p['device_limit']} устройств",
            callback_data=f"buy_plan:{p['plan_code']}",
        )] for p in plans]
        rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="buy_cancel")])
        await message.answer(
            "Выберите тариф:\n\n"
            "Все тарифы включают стандартный набор серверов. "
            "WL, Расширенный и Семейный дополнительно включают WL-серверы "
            "с лимитом трафика на 30-дневный период.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )

    @dp.callback_query(F.data == "buy_cancel")
    async def cb_buy_cancel(call: CallbackQuery):
        await call.answer()
        try:
            await call.message.edit_text("Покупка отменена.")
        except Exception:
            pass

    @dp.callback_query(F.data.startswith("buy_plan:"))
    async def cb_buy_plan(call: CallbackQuery):
        await call.answer()
        plan_code = call.data.split(":", 1)[1]
        tariffs = _sellable_tariffs()
        if not tariffs:
            await call.message.answer("Покупка временно недоступна, обратитесь к оператору.")
            return
        summary = _plan_summary(tariffs, plan_code)
        if summary is None:
            await call.message.answer("Этот тариф сейчас недоступен для покупки.")
            return
        rows = [[InlineKeyboardButton(
            text=f"{item['duration_days']} дн. — {item['amount']} ⭐️",
            callback_data=f"buy_dur:{plan_code}:{item['duration_days']}",
        )] for item in summary["items"]]
        rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="buy_cancel")])
        quota_note = wl_quota_line(summary["items"][0])
        await call.message.edit_text(
            f"Тариф «{summary['display_name']}» — до {summary['device_limit']} устройств.\n"
            + (quota_note + "\n" if quota_note else "")
            + "Выберите срок:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )

    @dp.callback_query(F.data.startswith("buy_dur:"))
    async def cb_buy_duration(call: CallbackQuery):
        await call.answer()
        try:
            _, plan_code, raw_duration = call.data.split(":", 2)
            duration_days = int(raw_duration)
        except (IndexError, ValueError):
            await call.message.answer("Неверный тариф.")
            return
        tariffs = _sellable_tariffs()
        if not tariffs:
            await call.message.answer("Покупка временно недоступна, обратитесь к оператору.")
            return
        summary = _plan_summary(tariffs, plan_code)
        item = next(
            (i for i in (summary or {}).get("items", []) if i["duration_days"] == duration_days),
            None,
        )
        if item is None:
            await call.message.answer("Этот тариф сейчас недоступен для покупки.")
            return
        await call.message.edit_text(
            f"Вы оформляете:\n\n"
            f"Тариф: {item['display_name']}\n"
            f"Срок: {item['duration_days']} дн.\n"
            f"Устройств: до {item['device_limit']}\n"
            + wl_quota_line(item)
            + f"Стоимость: {item['amount']} ⭐️\n\n"
            "Оплата через Telegram Stars.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(
                    text=f"Оплатить {item['amount']} ⭐️",
                    callback_data=f"buy_pay:{plan_code}:{duration_days}",
                ),
                InlineKeyboardButton(text="❌ Отмена", callback_data="buy_cancel"),
            ]]),
        )

    @dp.callback_query(F.data.startswith("buy_pay:"))
    async def cb_buy_pay(call: CallbackQuery, state: FSMContext):
        await call.answer()
        try:
            _, plan_code, raw_duration = call.data.split(":", 2)
            duration_days = int(raw_duration)
        except (IndexError, ValueError):
            await call.message.answer("Неверный тариф.")
            return
        if db.get_setting("stars:enabled") != "1":
            await call.message.answer("Покупка временно недоступна.")
            return
        try:
            invoice = db.stars_purchases.create_invoice(
                telegram_id=call.from_user.id, plan_code=plan_code, duration_days=duration_days,
                ttl_seconds=3600,
            )
        except Exception as exc:
            logger.info("Canonical Stars invoice rejected: %s", type(exc).__name__)
            await call.message.answer("Этот тариф сейчас нельзя оформить автоматически. Обратитесь к оператору.")
            return
        try:
            await call.message.edit_text("Счёт готов — оплатите его ниже. ⬇️")
        except Exception:
            pass
        await _send_stars_invoice(call.message.bot, call.from_user.id, invoice)


    @dp.message(CommandStart())
    async def cmd_start(message: Message, state: FSMContext):
        await state.clear()
        parts = (getattr(message, "text", None) or "").split(maxsplit=1)
        forwarded_invoice_link = len(parts) == 2 and parts[1].startswith("stars_invoice_")
        tg_user = db.get_tg_user(message.from_user.id)
        # canary hotfix: a CANONICAL_SIGNUP customer owns a canonical DIRECT
        # account with a non-revoked OWNER telegram identity but no legacy
        # tg_users row. The linked/unlinked decision must consult the same
        # canonical resolver as Stars signup/renewal/admin views --
        # a missing legacy link must never demote a real owner to onboarding.
        account = db.accounts.get_active_account_by_telegram_id(message.from_user.id)
        if tg_user or account:
            await state.set_state(SupportStates.in_dialog)
            await message.answer(
                ("Этот счёт нельзя оплатить из пересланного сообщения. "
                 "Создайте новый через «⭐️ Продлить подписку»."
                 if forwarded_invoice_link else "С возвращением! Чем могу помочь?"),
                reply_markup=kb_main(),
            )
        else:
            await state.set_state(SupportStates.waiting_link)
            await message.answer(
                (("Пересланный счёт оплатить нельзя — после покупки создайте новый через меню.\n\n")
                 if forwarded_invoice_link else "")
                + "👋 Привет! Я помогу с вашей VPN-подпиской.\n\n"
                "Здесь можно купить подписку или прислать существующую ссылку для привязки аккаунта.",
                reply_markup=kb_new_user(),
            )

    @dp.message(StateFilter(None), ~F.successful_payment)
    async def msg_no_state(message: Message, state: FSMContext):
        tg_user = db.get_tg_user(message.from_user.id)
        # canary hotfix: same canonical resolver as cmd_start -- a canonical DIRECT
        # owner must land in the normal dialog, not in binding onboarding.
        account = db.accounts.get_active_account_by_telegram_id(message.from_user.id)
        if tg_user or account:
            await state.set_state(SupportStates.in_dialog)
            await message.answer("Чем могу помочь?", reply_markup=kb_main())
        else:
            await state.set_state(SupportStates.waiting_link)
            await message.answer(
                "👋 Привет! Пришлите ссылку подписки для привязки аккаунта "
                "или купите подписку через «🛒 Купить VPN».",
                reply_markup=kb_new_user(),
            )

    @dp.callback_query(F.data == "no_link")
    async def cb_no_link(call: CallbackQuery, state: FSMContext):
        await call.answer()
        db.create_ticket(call.from_user.id, status="new_user")
        await state.set_state(SupportStates.waiting_link)
        await call.message.answer(
            "Понял! Заявка создана.\n\n"
            "Для получения подписки напишите администратору или опишите здесь свой вопрос — "
            "мы поможем.\n\nКак только получите ссылку — пришлите её сюда.",
        )

    @dp.message(SupportStates.waiting_link)
    async def msg_waiting_link(message: Message, state: FSMContext):
        token = parse_sub_link(message.text or "")
        if not token:
            await message.answer(
                "Не могу распознать ссылку. Попробуйте ещё раз или нажмите кнопку ниже.",
                reply_markup=kb_no_link(),
            )
            return
        try:
            username = marzban.get_username_for_token(token)
            if not username:
                await message.answer("Ссылка не найдена в системе. Проверьте правильность.")
                return
        except Exception as e:
            logger.error(f"Ошибка поиска пользователя: {e}")
            await message.answer("Ошибка проверки подписки. Попробуйте позже.")
            return

        db.save_tg_user(message.from_user.id, username)

        # PH4-05 grace campaign: if this legacy username was bootstrapped
        # into a grace-cohort account (ABSENT ownership, no Telegram claim
        # made at bootstrap time -- see legacy_grace_registration.py), this
        # is the exact moment real evidence first exists. Reuses the same
        # ambiguity bar enroll_direct_account(PROVEN) already enforces and
        # the existing link_telegram_owner primitive; never an automatic
        # rebind, never resolves an ambiguous mapping on its own. A
        # username with no bootstrapped account (the ordinary case for
        # every non-cohort user) is a silent no-op ('NO_ACCOUNT') -- zero
        # behavior change for anyone outside the grace campaign.
        try:
            from .legacy_grace_registration import bind_telegram_after_registration
            bind_telegram_after_registration(
                db, legacy_username=username, telegram_id=message.from_user.id,
                actor="mgboost-bot-grace-registration",
            )
        except Exception as exc:
            logger.warning(f"grace telegram bind skipped error_type={type(exc).__name__}")

        await state.set_state(SupportStates.in_dialog)
        await message.answer(
            f"✅ Аккаунт привязан: {username}\n\nЧем могу помочь?",
            reply_markup=kb_main(),
        )

    @dp.message(SupportStates.in_dialog, F.text == "📋 Моя подписка")
    async def msg_my_subscription(message: Message, state: FSMContext):
        tg_user = db.get_tg_user(message.from_user.id)
        if not tg_user:
            # PH5-11 + canary hotfix: a commercial signup customer owns a canonical
            # account but no legacy link -- show the canonical subscription
            # state instead of the legacy binding prompt.
            try:
                summary = _canonical_subscription_summary(db, message.from_user.id)
            except Exception as e:
                logger.error(f"Ошибка получения канонической подписки: {type(e).__name__}")
                await message.answer("Не удалось получить информацию о подписке.")
                return
            if summary is not None:
                await safe_answer(message, summary)
                return
            await state.set_state(SupportStates.waiting_link)
            await message.answer("Нужно сначала привязать подписку.", reply_markup=kb_no_link())
            return
        try:
            admin_token = await _run_sync(marzban.get_admin_token_from_env)
            user_info = await _run_sync(marzban.get_user, tg_user["marzban_username"], admin_token)
            await safe_answer(message, format_subscription(user_info))
        except Exception as e:
            logger.error(f"Ошибка получения подписки: {e}")
            await message.answer("Не удалось получить информацию о подписке.")

    # PH4-04 corrective fix: a bare repeat of /newsub must NEVER silently
    # rotate an already-ACTIVE credential -- that is a destructive action
    # (the old URL stops working immediately) and needs an explicit,
    # separate confirmation step. `_reissue_in_progress` is a tiny
    # in-memory per-account guard against a fast double-tap on the confirm
    # button producing two rotations (aiogram doesn't itself deduplicate
    # distinct callback_query ids for two real taps).
    _reissue_in_progress: set[int] = set()

    def _issue_new_credential(account_id: int, actor_ref: str, reason: str) -> dict:
        import secrets
        timestamp = int(time.time())
        op_key = f"{account_id}:{timestamp}:{secrets.token_urlsafe(16)}"
        db.subscription_credentials.abandon_pending(
            account_id=account_id, actor_ref=actor_ref,
            idempotency_key=f"bot-newsub-abandon-v1:{op_key}", now=timestamp,
        )
        prepared = db.subscription_credentials.prepare(
            account_id=account_id, actor_ref=actor_ref, reason=reason,
            idempotency_key=f"bot-newsub-v1:{op_key}", now=timestamp,
        )
        return {"prepared": prepared, "op_key": op_key, "timestamp": timestamp}

    def _activate_new_credential(account_id: int, actor_ref: str, prepared: dict, op_key: str, timestamp: int):
        return db.subscription_credentials.activate(
            credential_id=prepared["id"], account_id=account_id,
            expected_generation=prepared["generation"], actor_ref=actor_ref,
            idempotency_key=f"bot-newsub-v1:{op_key}:activate", now=timestamp,
        )

    async def _deliver_and_activate(message_or_call, account_id: int, actor_ref: str, prep: dict) -> bool:
        """Delivers the raw token, then activates only if delivery did not
        raise -- same crash-safe sequencing as `issue_or_reissue_credential`,
        just split across async Telegram calls."""
        prepared = prep["prepared"]
        try:
            await message_or_call.answer(
                "🔗 Ваша новая ссылка подписки:\n"
                f"https://sub.beykus.fun/{prepared['raw_token']}\n\n"
                "Сохраните её сейчас — повторно показать эту же ссылку сервер не сможет. "
                "Если понадобится новая — используйте перевыпуск."
            )
        except Exception as e:
            logger.error(f"Ошибка доставки opaque credential: {type(e).__name__}")
            return False
        try:
            await _run_sync(_activate_new_credential, account_id, actor_ref, prepared, prep["op_key"], prep["timestamp"])
        except Exception as e:
            logger.error(f"Ошибка активации opaque credential: {type(e).__name__}")
            return False
        return True

    def _active_credential(account_id: int) -> dict | None:
        return db._conn.execute(
            "SELECT id, generation FROM mgboost_subscription_credentials "
            "WHERE account_id=? AND status='ACTIVE'", (account_id,),
        ).fetchone()

    @dp.message(Command("newsub"), F.chat.type == ChatType.PRIVATE)
    async def msg_new_opaque_subscription(message: Message, state: FSMContext):
        """PH4-04: reviewed-account-only opaque URL issuance. Deliberately a
        hidden command, not a keyboard button shown to every legacy user --
        only accounts already linked as the canonical Telegram OWNER
        (`mgboost_telegram_identities`, PROVEN ownership, never mere
        possession of a legacy link) can use it. Private chat only -- never
        a group/channel. A bare repeat while a credential is already ACTIVE
        never rotates it -- see `cb_newsub_confirm`/`cb_newsub_do` below."""
        from .config import OPAQUE_SUBSCRIPTION_ENABLED
        if not OPAQUE_SUBSCRIPTION_ENABLED:
            await message.answer("Эта функция пока недоступна.")
            return
        account = await _run_sync(db.accounts.get_account_for_telegram, message.from_user.id)
        if account is None:
            await message.answer(
                "Новая ссылка подписки доступна только для проверенных аккаунтов. "
                "Если вы уже наш клиент — обратитесь к администратору."
            )
            return
        account_id = account["id"]

        existing = await _run_sync(_active_credential, account_id)
        if existing is not None:
            await message.answer(
                "🔗 У вас уже есть активная ссылка подписки.\n\n"
                "В целях безопасности сервер не хранит её открытый текст и не может "
                "показать её повторно.\n\n"
                "Если вы потеряли ссылку, её можно перевыпустить. После перевыпуска "
                "старая ссылка перестанет работать, но ваши устройства и VPN "
                "credentials останутся прежними.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(
                        text="🔄 Перевыпустить ссылку",
                        callback_data=f"newsub_confirm:{account_id}",
                    ),
                ]]),
            )
            return

        actor_ref = f"telegram:{message.from_user.id}"
        try:
            prep = await _run_sync(_issue_new_credential, account_id, actor_ref, "Telegram /newsub initial issuance")
        except Exception as e:
            logger.error(f"Ошибка подготовки opaque credential: {type(e).__name__}")
            await message.answer("Не удалось выпустить ссылку. Попробуйте позже.")
            return
        await _deliver_and_activate(message, account_id, actor_ref, prep)

    @dp.callback_query(F.data.startswith("newsub_confirm:"))
    async def cb_newsub_confirm(call: CallbackQuery):
        await call.answer()
        if call.message is None or call.message.chat.type != ChatType.PRIVATE:
            return
        try:
            account_id = int(call.data.split(":", 1)[1])
        except (IndexError, ValueError):
            return
        account = await _run_sync(db.accounts.get_account_for_telegram, call.from_user.id)
        if account is None or account["id"] != account_id:
            return
        try:
            await call.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        await call.message.answer(
            "⚠️ Старая ссылка подписки перестанет работать сразу.\n"
            "Устройства и их VPN credentials не изменятся.\n\n"
            "Перевыпустить?",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="✅ Перевыпустить", callback_data=f"newsub_do:{account_id}"),
                InlineKeyboardButton(text="❌ Отмена", callback_data=f"newsub_cancel:{account_id}"),
            ]]),
        )

    @dp.callback_query(F.data.startswith("newsub_cancel:"))
    async def cb_newsub_cancel(call: CallbackQuery):
        await call.answer()
        if call.message is None or call.message.chat.type != ChatType.PRIVATE:
            return
        try:
            account_id = int(call.data.split(":", 1)[1])
        except (IndexError, ValueError):
            return
        account = await _run_sync(db.accounts.get_account_for_telegram, call.from_user.id)
        if account is None or account["id"] != account_id:
            return
        try:
            await call.message.edit_text("Отменено. Действующая ссылка подписки не изменилась.")
        except Exception:
            pass

    @dp.callback_query(F.data.startswith("newsub_do:"))
    async def cb_newsub_do(call: CallbackQuery):
        await call.answer()
        if call.message is None or call.message.chat.type != ChatType.PRIVATE:
            return
        try:
            account_id = int(call.data.split(":", 1)[1])
        except (IndexError, ValueError):
            return
        account = await _run_sync(db.accounts.get_account_for_telegram, call.from_user.id)
        if account is None or account["id"] != account_id:
            return
        if account_id in _reissue_in_progress:
            return  # a fast double-tap on the confirm button -- ignore, idempotent by design
        _reissue_in_progress.add(account_id)
        try:
            try:
                await call.message.edit_reply_markup(reply_markup=None)
            except Exception:
                pass
            actor_ref = f"telegram:{call.from_user.id}"
            try:
                prep = await _run_sync(_issue_new_credential, account_id, actor_ref, "Telegram /newsub confirmed reissue")
            except Exception as e:
                logger.error(f"Ошибка подготовки opaque credential: {type(e).__name__}")
                await call.message.answer("Не удалось перевыпустить ссылку. Попробуйте позже.")
                return
            await _deliver_and_activate(call.message, account_id, actor_ref, prep)
        finally:
            _reissue_in_progress.discard(account_id)

    @dp.message(SupportStates.in_dialog, F.text == "🔧 Управление устройствами")
    async def msg_manage_devices(message: Message, state: FSMContext):
        tg_user = db.get_tg_user(message.from_user.id)
        if not tg_user and db.accounts.get_active_account_by_telegram_id(message.from_user.id):
            # canary hotfix: a canonical DIRECT owner is linked, but the LK
            # management deep link is keyed by a legacy marzban username
            # they do not have -- never push them into the waiting_link
            # binding loop (their opaque URL does not match the parser).
            await message.answer(
                "⚙️ Управление устройствами для вашего аккаунта пока доступно "
                "через поддержку. Опишите ваш запрос здесь — поможем.",
            )
            return
        if not tg_user:
            await state.set_state(SupportStates.waiting_link)
            await message.answer("Нужно сначала привязать подписку.", reply_markup=kb_no_link())
            return

        link = await _build_management_link(db, marzban, message.from_user.id, tg_user["marzban_username"])
        if link is None:
            await message.answer(
                "⚠️ Управление устройствами временно недоступно из-за ошибки конфигурации сервера. "
                "Мы уже знаем о проблеме — попробуйте позже или обратитесь к оператору.",
            )
            return
        await message.answer(
            "🔧 Ссылка для управления устройствами (действует 15 минут, одноразовая):\n\n"
            f"{link}\n\n"
            "Откройте её в браузере, чтобы переименовывать или отключать устройства. "
            "Если ссылка истечёт — запросите новую здесь же.",
        )

    @dp.message(SupportStates.in_dialog, F.text == "⭐️ Продлить подписку")
    async def msg_stars_menu(message: Message, state: FSMContext):
        tg_user = db.get_tg_user(message.from_user.id)
        account = db.accounts.get_active_account_by_telegram_id(message.from_user.id)
        if not tg_user and not account:
            await state.set_state(SupportStates.waiting_link)
            await message.answer("Нужно сначала привязать подписку.", reply_markup=kb_no_link())
            return

        if db.get_setting("stars:enabled") != "1":
            await message.answer("Продление через Stars временно недоступно, обратитесь к оператору.")
            return

        tariffs = db.stars_purchases.sellable_catalog()
        if not tariffs:
            await message.answer("Продление через Stars временно недоступно, обратитесь к оператору.")
            return
        if not account:
            await message.answer("Для покупки через Stars нужна подтверждённая учётная запись. Обратитесь к оператору.")
            return
        current = db._conn.execute(
            "SELECT pv.plan_code,s.status FROM mgboost_subscriptions s JOIN mgboost_plan_versions pv "
            "ON pv.id=s.current_plan_version_id WHERE s.account_id=? ORDER BY s.id DESC LIMIT 1",
            (account["id"],),
        ).fetchone()
        if current:
            if current["status"] == "UNLIMITED":
                await message.answer("У вас безлимитный тариф — покупка через Stars недоступна.")
                return
            tariffs = [item for item in tariffs if item["plan_code"] == current["plan_code"]]
        if not tariffs:
            await message.answer("Смена тарифа оформляется через поддержку. Продление через Stars сейчас недоступно.")
            return
        await message.answer("Выберите срок продления:", reply_markup=kb_tariffs(tariffs))

    @dp.callback_query(F.data.startswith("stars_buy:"))
    async def cb_stars_buy(call: CallbackQuery, state: FSMContext):
        await call.answer()
        tg_user = db.get_tg_user(call.from_user.id)
        if not tg_user and not db.accounts.get_active_account_by_telegram_id(call.from_user.id):
            await call.message.answer("Нужно сначала привязать подписку.")
            return

        if db.get_setting("stars:enabled") != "1":
            await call.message.answer("Продление через Stars временно недоступно.")
            return

        try:
            _, plan_code, raw_duration = call.data.split(":", 2)
            duration_days = int(raw_duration)
        except (IndexError, ValueError):
            await call.message.answer("Неверный тариф.")
            return
        try:
            invoice = db.stars_purchases.create_invoice(
                telegram_id=call.from_user.id, plan_code=plan_code, duration_days=duration_days,
                ttl_seconds=3600,
            )
        except Exception as exc:
            logger.info("Canonical Stars invoice rejected: %s", type(exc).__name__)
            await call.message.answer("Этот тариф сейчас нельзя оформить автоматически. Обратитесь к оператору.")
            return

        await _send_stars_invoice(call.message.bot, call.from_user.id, invoice)

    @dp.message(SupportStates.in_dialog, F.text == "🆘 Позвать человека")
    async def msg_call_human(message: Message, state: FSMContext):
        tg_user = db.get_tg_user(message.from_user.id)
        username = tg_user["marzban_username"] if tg_user else None
        ticket = db.get_open_ticket(message.from_user.id)
        if ticket:
            db.update_ticket_status(ticket["id"], "waiting_human")
        else:
            db.create_ticket(message.from_user.id, marzban_username=username, status="waiting_human")
        await state.set_state(SupportStates.waiting_human)
        await message.answer(
            "Оператор скоро подключится. Опишите ваш вопрос, и я передам его.",
            reply_markup=kb_waiting(),
        )
        await _notify_admin(message.bot, db, message.from_user.id, username)

    @dp.message(SupportStates.waiting_human)
    async def msg_waiting_human(message: Message, state: FSMContext):
        if message.text == "⬅️ Назад к боту":
            await state.set_state(SupportStates.in_dialog)
            await message.answer("Вернулись в диалог с ботом.", reply_markup=kb_main())
            return
        ticket = db.get_open_ticket(message.from_user.id)
        if ticket:
            db.add_ticket_message(ticket["id"], "user", message.text or "")
        await message.answer("Ваш вопрос передан оператору, ожидайте ответа.")

    @dp.message(SupportStates.in_dialog, F.photo | F.document | F.video)
    async def msg_in_dialog_media(message: Message, state: FSMContext):
        tg_user = db.get_tg_user(message.from_user.id)
        username = tg_user["marzban_username"] if tg_user else None
        ticket = db.get_open_ticket(message.from_user.id)
        if not ticket:
            ticket_id = db.create_ticket(message.from_user.id, marzban_username=username, status="waiting_human")
        else:
            ticket_id = ticket["id"]
            db.update_ticket_status(ticket_id, "waiting_human")

        media_type = "фото" if message.photo else ("видео" if message.video else "файл")
        caption = message.caption or ""
        db.add_ticket_message(ticket_id, "user", f"[{media_type}]{': ' + caption if caption else ''}")

        await state.set_state(SupportStates.waiting_human)
        await message.answer(
            f"Получил {media_type}! Передаю оператору — он скоро ответит.",
            reply_markup=kb_waiting(),
        )
        await _notify_admin(message.bot, db, message.from_user.id, username)

    @dp.message(SupportStates.waiting_human, F.photo | F.document | F.video)
    async def msg_waiting_human_media(message: Message, state: FSMContext):
        ticket = db.get_open_ticket(message.from_user.id)
        if ticket:
            media_type = "фото" if message.photo else ("видео" if message.video else "файл")
            caption = message.caption or ""
            db.add_ticket_message(ticket["id"], "user", f"[{media_type}]{': ' + caption if caption else ''}")
        await message.answer("Получил, оператор посмотрит.")

    @dp.message(SupportStates.in_dialog)
    async def msg_in_dialog(message: Message, state: FSMContext):
        if not message.text:
            return
        api_key = db.get_setting("bot:openrouter_api_key")
        model = db.get_setting("bot:openrouter_model", "openai/gpt-4o-mini")
        support_enabled = db.get_setting("bot:support_enabled", "1")

        tg_user = db.get_tg_user(message.from_user.id)
        username = tg_user["marzban_username"] if tg_user else None

        ticket = db.get_open_ticket(message.from_user.id)
        if not ticket:
            ticket_id = db.create_ticket(message.from_user.id, marzban_username=username, status="open")
        else:
            ticket_id = ticket["id"]
        db.add_ticket_message(ticket_id, "user", message.text)

        if not api_key or support_enabled == "0":
            await message.answer(
                "AI-ассистент недоступен. Нажмите «🆘 Позвать человека» для связи с оператором."
            )
            return

        history = db.get_ticket_messages(ticket_id, limit=20)
        ai_messages = build_ai_messages(message.text, history[:-1], system=build_system_prompt(db))
        current_states = node_states if node_states is not None else {}
        current_names = node_names if node_names is not None else {}
        reply = await ask_openrouter_with_tools(
            api_key, model, ai_messages, get_tools(),
            db=db, marzban=marzban, telegram_id=message.from_user.id,
            node_states=current_states, node_names=current_names,
        )
        db.add_ticket_message(ticket_id, "ai", reply)
        await safe_answer(message, reply)

        ticket_after = db.get_open_ticket(message.from_user.id)
        if ticket_after and ticket_after["status"] == "waiting_human":
            await state.set_state(SupportStates.waiting_human)
            await message.answer(
                "Передаю вас оператору, ожидайте.",
                reply_markup=kb_waiting(),
            )
            await _notify_admin(message.bot, db, message.from_user.id, username)


async def safe_answer(message, text: str, **kwargs):
    try:
        await message.answer(text, parse_mode="Markdown", **kwargs)
    except Exception:
        try:
            await message.answer(text, **kwargs)
        except Exception as e:
            logger.error(f"safe_answer failed: {e}")


async def _notify_admin(bot, db, telegram_id: int, username: str | None):
    admin_tg_id = db.get_setting("bot:admin_tg_id")
    if not admin_tg_id:
        return
    try:
        name = username or f"tg:{telegram_id}"
        await bot.send_message(
            int(admin_tg_id),
            f"🆘 Пользователь {name} (tg_id: {telegram_id}) запросил оператора.",
        )
    except Exception as e:
        logger.warning(f"Не удалось уведомить admin: {e}")


async def _notify_admin_orphan_payment(bot, db, sp, payer_telegram_id: int):
    """A successful_payment arrived for an invoice_id we have no row for at
    all (should be unreachable given invoice_payload is always our own
    row id, but money has moved — never silently drop it)."""
    admin_tg_id = db.get_setting("bot:admin_tg_id")
    if not admin_tg_id:
        return
    try:
        await bot.send_message(
            int(admin_tg_id),
            "⚠️ Платёж Stars без соответствующего счёта в БД!\n"
            f"charge_id: {sp.telegram_payment_charge_id}\n"
            f"payer: {payer_telegram_id}, amount: {sp.total_amount}",
        )
    except Exception as e:
        logger.warning(f"Не удалось уведомить admin об orphan-платеже: {e}")


async def notify_ticket_closed(bot, telegram_id: int, state_storage=None):
    try:
        from aiogram.fsm.storage.memory import MemoryStorage
        from aiogram.fsm.context import FSMContext
    except ImportError:
        return
    try:
        await bot.send_message(
            telegram_id,
            "✅ Тикет закрыт. Если остались вопросы — пишите!",
        )
    except Exception as e:
        logger.warning(f"Не удалось уведомить пользователя о закрытии тикета: {e}")


async def send_operator_reply(bot, telegram_id: int, text: str):
    msg = f"💬 Оператор: {text}"
    try:
        await bot.send_message(telegram_id, msg, parse_mode="Markdown")
    except Exception:
        try:
            await bot.send_message(telegram_id, msg)
        except Exception as e:
            logger.warning(f"Не удалось доставить ответ оператора: {e}")


async def _run_sync(func, *args):
    import asyncio
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, func, *args)


async def _build_management_link(db, marzban, telegram_id: int, username: str) -> str | None:
    """Build a one-time device-management deep link into the web LK for the
    given (telegram_id, username) binding. Never embeds a permanent bearer
    credential, and never carries the raw subscription token: the `mgmt`
    code is single-use and expires in ~15 minutes (see
    Database.create_mgmt_code). Once exchanged for a session cookie, the LK
    page's read-only info/usage/devices views *and* the mutating device
    actions all operate purely off that session (see
    _resolve_username_from_session / _require_mgmt_session in
    src/routes/lk.py) — so there is no need to reconstitute and transmit
    the user's subscription token here at all, unlike an earlier version of
    this function that did a Marzban admin-API reverse lookup
    (username -> subscription_url -> token) just to prefill the link.

    Returns None — never a broken or wrong-domain link — if PUBLIC_HOST is
    not configured. Callers must handle that by telling the user the
    feature is temporarily unavailable, not by falling back to a
    hardcoded domain."""
    from .config import PUBLIC_HOST

    if not PUBLIC_HOST:
        logger.error(
            "Configuration error: PUBLIC_HOST is not set — cannot build a "
            "device-management deep link. Set the PUBLIC_HOST environment "
            "variable (see .env.example) to fix this."
        )
        return None

    code = db.create_mgmt_code(telegram_id, username)
    # The one-time code is delivered via a URL *fragment*, never a query
    # string: fragments are never sent to the server in the HTTP request
    # (browser-local only), so the code can't leak into reverse-proxy
    # access logs, browser history entries that get synced/shared, or a
    # Referer header sent to a third-party resource loaded by the page.
    # The frontend (frontend/assets/lk.js) reads location.hash, POSTs the
    # code to /lk/api/mgmt/exchange in the request body, then immediately
    # clears the fragment via history.replaceState.
    return f"https://{PUBLIC_HOST}/lk/#mgmt={code}"
