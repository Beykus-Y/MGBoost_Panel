import logging
import re
import time
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

SQLITE_MAX_INTEGER = (1 << 63) - 1

_GB_DECIMAL = 1_000_000_000


def _wl_quota_text(quota_bytes: int, duration_days: int) -> str:
    """Human-readable WL quota for one catalog SKU. A 60-day SKU is ALWAYS
    described per 30-day period (never as a doubled total) -- each 30-day
    WL period carries its own full quota and remainder is never carried.
    Single formatting source for the tariff screens AND the invoice
    description (the two copies drifted before the UX redesign)."""
    gb = quota_bytes // _GB_DECIMAL
    days = int(duration_days)
    if days <= 30:
        return f"{gb} GB на {days} дн."
    return (
        f"{gb} GB каждые 30 дней "
        f"({days // 30} периода по {gb} GB)"
    )


def wl_quota_line(item: dict) -> str:
    quota_bytes = item.get("wl_quota_bytes") or 0
    if not quota_bytes:
        return ""
    return f"Трафик: {_wl_quota_text(quota_bytes, item['duration_days'])}\n"


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


def _fmt_date(ts: int) -> str:
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%d.%m.%Y")


def _fmt_gb(num_bytes) -> int:
    return (num_bytes or 0) // _GB_DECIMAL


# --- subscription card rendering (UI redesign slice C1) ---------------------
# English effective_status values are the engine's contract (OPD-16 requires
# one display rule); the Russian presentation lives here, in the UI layer.

_CANONICAL_STATUS_RU = {
    "ACTIVE": ("🟢", "Подписка активна"),
    "UNLIMITED": ("🟢", "Подписка активна (бессрочно)"),
    "EXPIRED": ("🔴", "Подписка истекла"),
    "NONE": ("⚪️", "Подписки пока нет"),
}

_LEGACY_STATUS_RU = {
    "active": "🟢 Подписка активна",
    "expired": "🔴 Подписка истекла",
    "disabled": "⛔ Подписка отключена",
    "limited": "⚠️ Лимит трафика исчерпан",
    "on_hold": "⏸ Подписка на паузе",
}


def render_subscription_card(card: dict, *, now: int | None = None) -> str:
    """Текст карточки подписки: тариф, срок, устройства, WL-остаток — то,
    что пользователь должен понимать с одного взгляда. Никакой сырой
    технической терминологии; нет данных — нет строки."""
    now = int(time.time()) if now is None else int(now)
    lines: list[str] = []
    devices = card.get("devices") or {}

    if card["cohort"] == "canonical":
        status = card["status"]
        if status == "EXPIRED" and card.get("expiry"):
            lines.append(f"🔴 Подписка истекла {_fmt_date(card['expiry'])}")
        else:
            icon, label = _CANONICAL_STATUS_RU.get(status, ("⚪️", status))
            lines.append(f"{icon} {label}")
        if card.get("plan_name"):
            plan_line = f"Тариф: {card['plan_name']}"
            if devices.get("limit"):
                plan_line += f" · до {devices['limit']} устройств"
            lines.append(plan_line)
        if not card.get("unlimited") and card.get("expiry"):
            days_left = max(0, (int(card["expiry"]) - now) // 86400)
            lines.append(f"Действует до: {_fmt_date(card['expiry'])} (осталось {days_left} дн.)")
        wl = card.get("wl")
        if wl:
            if wl["mode"] == "UNLIMITED":
                lines.append("WL: безлимит")
            elif wl.get("quota_bytes"):
                wl_line = f"WL: {_fmt_gb(wl.get('consumed_bytes'))} / {_fmt_gb(wl['quota_bytes'])} GB"
                if wl.get("period_ends_at"):
                    wl_line += f" · текущий период до {_fmt_date(wl['period_ends_at'])}"
                lines.append(wl_line)
        if status == "NONE":
            lines.append("Купить подписку — кнопка «🛒 Купить / Продлить» ниже.")
    else:
        lines.append(_LEGACY_STATUS_RU.get(card["status"], f"⚪️ Статус: {card['status']}"))
        if card.get("marzban_username"):
            lines.append(f"Пользователь: {card['marzban_username']}")
        if card.get("traffic_used") is not None:
            limit = card.get("traffic_limit")
            used_str = f"Использовано: {_fmt_bytes(card['traffic_used'])} / {_fmt_bytes(limit) if limit else '∞'}"
            lines.append(used_str)
        if card.get("expiry"):
            lines.append(f"Дата окончания: {_fmt_date(card['expiry'])}")

    if devices.get("mode") == "UNLIMITED":
        if devices.get("active") is not None:
            lines.append(f"Устройства: без лимита (активных: {devices['active']})")
    elif devices.get("active") is not None and devices.get("limit"):
        lines.append(f"Устройства: {devices['active']} из {devices['limit']} активных")
    return "\n".join(lines)


def _canonical_subscription_summary(db, telegram_id: int) -> str | None:
    """Compact canonical entitlement summary for the SUPPORT AI TOOL
    (`get_subscription_info`), not a user-facing screen -- the user-facing
    surface is `render_subscription_card`. Same resolver as Stars signup
    and renewal (``AccountStore.get_active_account_by_telegram_id``) -- a
    legacy ``tg_users`` link is deliberately not consulted, and possession
    of a subscription URL/HWID/username never counts as ownership here."""
    account = db.accounts.get_active_account_by_telegram_id(telegram_id)
    if account is None:
        return None
    ent = db.entitlements.calculate(account_id=int(account["id"]))
    sub = (ent or {}).get("subscription") or {}
    plan = ((ent or {}).get("plan") or {})
    expire = sub.get("effective_expiry")
    expire_str = ""
    if expire:
        expire_str = f"\nДата окончания: {_fmt_date(expire)}"
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
        return "AI-ассистент недоступен. Нажмите «🆘 Поддержка»."

    import json as _json
    import aiohttp

    headers = {
        "Authorization": f"Bearer {api_key}",
        "X-Title": "MGBoost Support",
    }
    from .config import subscription_base_url
    public_base = subscription_base_url()
    if public_base is not None:
        headers["HTTP-Referer"] = public_base

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


# --- FSM + shared keyboards -------------------------------------------------
# Hoisted to module level so notify_ticket_closed (invoked from the admin
# route, outside the dispatcher) can rebuild the main keyboard and reset the
# user's state after a ticket is closed.

try:
    from aiogram.fsm.state import State, StatesGroup

    class SupportStates(StatesGroup):
        waiting_link = State()
        in_dialog = State()
        waiting_human = State()
        waiting_promo_code = State()
except ImportError:  # pragma: no cover - aiogram optional at import time
    SupportStates = None

_FSM_STORAGE = None  # set by setup_support_handlers; consumed by notify_ticket_closed


def _reply_markup(markup_cls, buttons):
    return markup_cls(keyboard=buttons, resize_keyboard=True)


def kb_main():
    """Главное меню: 5 кнопок, единые для всех когорт. «🔗 Ссылка» — не
    отдельный раздел, а inline-кнопка внутри карточки подписки."""
    try:
        from aiogram.types import KeyboardButton, ReplyKeyboardMarkup
    except ImportError:  # pragma: no cover
        return None
    return _reply_markup(ReplyKeyboardMarkup, [
        [KeyboardButton(text="📱 Моя подписка"), KeyboardButton(text="🛒 Купить / Продлить")],
        [KeyboardButton(text="💻 Устройства"), KeyboardButton(text="🎟 Промокод")],
        [KeyboardButton(text="🆘 Поддержка")],
    ])


def kb_new_user():
    try:
        from aiogram.types import KeyboardButton, ReplyKeyboardMarkup
    except ImportError:  # pragma: no cover
        return None
    return _reply_markup(ReplyKeyboardMarkup, [
        [KeyboardButton(text="🛒 Купить / Продлить")],
        [KeyboardButton(text="🎟 Ввести промокод")],
    ])


def kb_new_user_inline():
    """Inline-меню initial onboarding («С чего начнём?»): покупка, промокод,
    привязка существующей ссылки. Один строитель для /start и для возвратов
    из promo flow, чтобы три входа онбординга не разъезжались."""
    try:
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    except ImportError:  # pragma: no cover
        return None
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🛒 Выбрать тариф", callback_data="buy_open"),
            InlineKeyboardButton(text="🎟 Ввести промокод", callback_data="start_promo"),
        ],
        [InlineKeyboardButton(text="У меня уже есть подписка", callback_data="start_link")],
    ])


def kb_waiting():
    try:
        from aiogram.types import KeyboardButton, ReplyKeyboardMarkup
    except ImportError:  # pragma: no cover
        return None
    return _reply_markup(ReplyKeyboardMarkup, [
        [KeyboardButton(text="⬅️ Назад к боту")],
    ])


def kb_promo_cancel():
    try:
        from aiogram.types import KeyboardButton, ReplyKeyboardMarkup
    except ImportError:  # pragma: no cover
        return None
    return _reply_markup(ReplyKeyboardMarkup, [
        [KeyboardButton(text="❌ Отмена")],
    ])


def setup_support_handlers(dp, db, marzban, node_states: dict | None = None, node_names: dict | None = None,
                            stars_trigger=None):
    try:
        from aiogram import F
        from aiogram.enums import ChatType
        from aiogram.filters import Command, CommandStart, StateFilter
        from aiogram.fsm.context import FSMContext
        from aiogram.types import (
            CallbackQuery,
            InlineKeyboardButton,
            InlineKeyboardMarkup,
            LabeledPrice,
            Message,
            PreCheckoutQuery,
        )
    except ImportError:
        logger.error("aiogram не установлен — поддержка не запустится")
        return

    global _FSM_STORAGE
    _FSM_STORAGE = getattr(dp, "storage", None)

    from .stars import _check_stars_eligibility
    from .commercial_signup import SIGNUP_INVOICE_KIND
    from .entitlement_read_model import build_subscription_card

    _CANONICAL_INVOICE_KINDS = ("CANONICAL_PLAN", SIGNUP_INVOICE_KIND)

    # Owner rule (bot UX redesign): a legacy-linked customer without a
    # canonical account must never silently receive a SECOND paid
    # subscription through a CANONICAL_SIGNUP invoice — two parallel paid
    # subscriptions for one person means billing/device/support chaos.
    LEGACY_SECOND_SUBSCRIPTION_GUARD = (
        "У вас уже есть подписка, оформленная по ссылке. Вторая отдельная "
        "подписка на тот же аккаунт не нужна — напишите нам, и мы поможем "
        "оформить перенос или продление."
    )

    def _support_button_markup():
        return InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🆘 Написать в поддержку", callback_data="call_human"),
        ]])

    def kb_no_link():
        return InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="У меня нет ссылки", callback_data="no_link")]]
        )

    def _buy_cancel_markup():
        return InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="❌ Отмена", callback_data="buy_cancel"),
        ]])

    async def _send_stars_invoice(bot, chat_id: int, invoice: dict):
        """Single canonical invoice sender: every field comes from the
        server-side invoice row (catalog snapshot), never from callback
        data. Single-chat invoice mode -- a forwarded copy gets a deep-link
        button, never another Pay button."""
        quota_row = db._conn.execute(
            "SELECT wl_quota_bytes FROM mgboost_plan_versions WHERE id=?",
            (invoice["plan_version_id"],),
        ).fetchone()
        quota_bytes = quota_row["wl_quota_bytes"] if quota_row else 0
        quota_text = ""
        if quota_bytes:
            quota_text = f" — {_wl_quota_text(quota_bytes, invoice['duration_days'])}"
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
            if row.get("invoice_kind") == SIGNUP_INVOICE_KIND:
                # Telegram captures Stars only after this acknowledgement.
                # Do not accept a signup payment if its post-payment opaque
                # subscription URL cannot be delivered safely.
                from .config import subscription_base_url
                if subscription_base_url() is None:
                    logger.error("Stars signup pre-checkout rejected: PUBLIC_HOST is invalid or missing")
                    await query.answer(
                        ok=False,
                        error_message="Покупка временно недоступна. Попробуйте позже.",
                    )
                    return
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

    # --- Единая воронка «🛒 Купить / Продлить» -------------------------------
    # Callback data carries ONLY plan_code + duration_days. Every price,
    # name, device limit and the invoice itself are re-resolved server-side
    # from the active immutable catalog inside create_invoice -- a tampered
    # callback can never select another plan, version or price.
    #
    # One funnel for purchase AND renewal (the pre-redesign «⭐️ Продлить
    # подписку» had its own stars_buy callback graph with no confirmation
    # step). An existing canonical account is only ever offered its CURRENT
    # plan (create_invoice locks the plan; plan change awaits PH5-06), so
    # the funnel never shows a buyable tariff the backend cannot fulfil.

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

    def _buy_catalog_view(tariffs):
        seen, plans = set(), []
        for t in tariffs:
            if t["plan_code"] not in seen:
                seen.add(t["plan_code"])
                plans.append(_plan_summary(tariffs, t["plan_code"]))
        rows = [[InlineKeyboardButton(
            text=(f"{p['display_name']} — до {p['device_limit']} устройств "
                  f"— от {min(i['amount'] for i in p['items'])}⭐"),
            callback_data=f"buy_plan:{p['plan_code']}",
        )] for p in plans]
        rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="buy_cancel")])
        text = (
            "Выберите тариф:\n\n"
            "Все тарифы включают стандартный набор серверов. "
            "WL, Расширенный и Семейный дополнительно включают WL-серверы "
            "с лимитом трафика на 30-дневный период."
        )
        return text, InlineKeyboardMarkup(inline_keyboard=rows)

    def _current_subscription_row(account_id: int):
        return db._conn.execute(
            "SELECT pv.plan_code,pv.display_name,pv.device_limit,pv.wl_quota_bytes,s.status "
            "FROM mgboost_subscriptions s JOIN mgboost_plan_versions pv "
            "ON pv.id=s.current_plan_version_id WHERE s.account_id=? "
            "ORDER BY s.id DESC LIMIT 1",
            (account_id,),
        ).fetchone()

    def _plan_renew_view(tariffs, account):
        """(text, markup) for an existing canonical account: only the
        current plan's durations are buyable; everything else routes to
        support instead of failing after the user has decided to pay."""
        current = _current_subscription_row(account["id"])
        if current is not None and current["status"] == "UNLIMITED":
            return "У вас безлимитный тариф — покупка через Stars недоступна.", None
        if current is None:
            # An account without a subscription row buys from the catalog
            # (its first purchase IS the signup/CREATE grant).
            return _buy_catalog_view(tariffs)
        items = [t for t in tariffs if t["plan_code"] == current["plan_code"]]
        if not items:
            return ("Смена тарифа оформляется через поддержку. "
                    "Продление через Stars сейчас недоступно."), None
        quota_note = wl_quota_line(items[0])
        rows = [[InlineKeyboardButton(
            text=f"{item['duration_days']} дн. — {item['amount']} ⭐️",
            callback_data=f"buy_dur:{current['plan_code']}:{item['duration_days']}",
        )] for item in items]
        rows.append([InlineKeyboardButton(text="🔄 Сменить тариф", callback_data="change_plan")])
        rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="buy_cancel")])
        text = (
            f"Ваш тариф: {current['display_name']} — до {current['device_limit']} устройств.\n"
            + (quota_note + "\n" if quota_note else "")
            + "Выберите срок продления:"
        )
        return text, InlineKeyboardMarkup(inline_keyboard=rows)

    def _change_plan_view():
        text = ("Смена тарифа пока оформляется через поддержку: поможем "
                "подобрать вариант и перейти без потери оплаченных дней.")
        markup = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🆘 Написать в поддержку", callback_data="call_human")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="buy_back_plan")],
        ])
        return text, markup

    async def _send_buy_entry(target, telegram_id: int, *, edit: bool = False):
        """Entry point shared by the reply button, the card's «➕ Продлить»
        and the promo success screen: catalog for brand-new users, current
        plan for canonical accounts, the second-subscription guard for
        legacy-linked customers."""
        tg_user = db.get_tg_user(telegram_id)
        account = db.accounts.get_active_account_by_telegram_id(telegram_id)
        if account is not None:
            tariffs = _sellable_tariffs()
            if not tariffs:
                text, markup = "Продление через Stars временно недоступно, обратитесь к оператору.", None
            else:
                text, markup = _plan_renew_view(tariffs, account)
        elif tg_user is not None:
            text, markup = LEGACY_SECOND_SUBSCRIPTION_GUARD, _support_button_markup()
        else:
            tariffs = _sellable_tariffs()
            if not tariffs:
                text, markup = "Покупка через Telegram Stars временно недоступна, обратитесь к оператору.", None
            else:
                text, markup = _buy_catalog_view(tariffs)
        if edit:
            try:
                await target.edit_text(text, reply_markup=markup)
                return
            except Exception:
                pass
        await target.answer(text, reply_markup=markup)

    # NOTE: no state.clear() here — a PURCHASE_DISCOUNT promo reservation
    # lives in FSM data and must survive into cb_buy_pay's invoice creation.
    @dp.message(F.text.in_({"🛒 Купить / Продлить", "🛒 Купить VPN", "⭐️ Продлить подписку"}))
    async def msg_buy_vpn(message: Message, state: FSMContext):
        # Registered above every FSM catch-all so the purchase journey stays
        # reachable from ANY state — including after a restart wiped
        # MemoryStorage. Old button labels are kept as aliases: Telegram
        # caches reply keyboards client-side, so stale keyboards must never
        # dead-end.
        await _send_buy_entry(message, message.from_user.id)

    @dp.callback_query(F.data == "buy_cancel")
    async def cb_buy_cancel(call: CallbackQuery):
        await call.answer()
        try:
            await call.message.edit_text("Покупка отменена.")
        except Exception:
            pass

    @dp.callback_query(F.data == "buy_back_plan")
    async def cb_buy_back_plan(call: CallbackQuery):
        """Настоящий Back: возвращает на предыдущий экран воронки. До
        редизайна кнопка «⬅️ Назад» на шаге срока вызывала buy_cancel и
        отменяла всю покупку."""
        await call.answer()
        await _send_buy_entry(call.message, call.from_user.id, edit=True)

    @dp.callback_query(F.data == "change_plan")
    async def cb_change_plan(call: CallbackQuery):
        await call.answer()
        text, markup = _change_plan_view()
        await call.message.edit_text(text, reply_markup=markup)

    @dp.callback_query(F.data == "buy_open")
    async def cb_buy_open(call: CallbackQuery):
        await call.answer()
        await _send_buy_entry(call.message, call.from_user.id)

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
        rows.append([InlineKeyboardButton(text="⬅️ К тарифам", callback_data="buy_back_plan")])
        rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="buy_cancel")])
        quota_note = wl_quota_line(summary["items"][0])
        await call.message.edit_text(
            f"Тариф «{summary['display_name']}» — до {summary['device_limit']} устройств.\n"
            + (quota_note + "\n" if quota_note else "")
            + "Выберите срок:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )

    async def _show_buy_confirmation(call: CallbackQuery, plan_code: str, duration_days: int,
                                     state: FSMContext = None):
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
        promo_line = ""
        state_data = {}
        try:
            if state is not None and hasattr(state, "get_data"):
                state_data = await state.get_data()
        except AttributeError:
            pass  # non-FSM transport (tests): no reservation context
        if state_data.get("promo_reservation_id"):
            promo_line = "\n🎟 Промокод: скидка будет учтена в счёте."
        await call.message.edit_text(
            f"Вы оформляете:\n\n"
            f"Тариф: {item['display_name']}\n"
            f"Срок: {item['duration_days']} дн.\n"
            f"Устройств: до {item['device_limit']}\n"
            + wl_quota_line(item)
            + f"Стоимость: {item['amount']} ⭐️\n\n"
            "Оплата через Telegram Stars."
            + promo_line,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(
                    text=f"Оплатить {item['amount']} ⭐️",
                    callback_data=f"buy_pay:{plan_code}:{duration_days}",
                ),
                InlineKeyboardButton(text="❌ Отмена", callback_data="buy_cancel"),
            ]]),
        )

    @dp.callback_query(F.data.startswith("buy_dur:"))
    async def cb_buy_duration(call: CallbackQuery, state: FSMContext = None):
        await call.answer()
        try:
            _, plan_code, raw_duration = call.data.split(":", 2)
            duration_days = int(raw_duration)
        except (IndexError, ValueError):
            await call.message.answer("Неверный тариф.")
            return
        await _show_buy_confirmation(call, plan_code, duration_days, state)

    @dp.callback_query(F.data.startswith("stars_buy:"))
    async def cb_legacy_stars_buy(call: CallbackQuery, state: FSMContext = None):
        """Compatibility for inline keyboards sent before the unified
        funnel. A stale tap is acknowledged and lands on the modern explicit
        confirmation step; it never recreates the removed direct-pay graph."""
        await call.answer()
        try:
            _, plan_code, raw_duration = call.data.split(":", 2)
            duration_days = int(raw_duration)
        except (IndexError, ValueError):
            await call.message.answer("Этот старый экран больше не актуален. Откройте покупку заново.")
            return
        if (db.get_tg_user(call.from_user.id) is not None
                and db.accounts.get_active_account_by_telegram_id(call.from_user.id) is None):
            await call.message.answer(LEGACY_SECOND_SUBSCRIPTION_GUARD, reply_markup=_support_button_markup())
            return
        await _show_buy_confirmation(call, plan_code, duration_days, state)

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
        if (db.get_tg_user(call.from_user.id) is not None
                and db.accounts.get_active_account_by_telegram_id(call.from_user.id) is None):
            # Defense in depth: stale inline keyboards from before the guard
            # existed must not mint a signup invoice for a legacy customer.
            await call.message.answer(LEGACY_SECOND_SUBSCRIPTION_GUARD, reply_markup=_support_button_markup())
            return
        promo_reservation_id = None
        try:
            state_data = await state.get_data() if hasattr(state, "get_data") else {}
            promo_reservation_id = state_data.get("promo_reservation_id")
        except AttributeError:
            pass  # non-FSM transport (tests): no reservation context
        try:
            invoice = await _run_sync(lambda: db.stars_purchases.create_invoice(
                telegram_id=call.from_user.id, plan_code=plan_code, duration_days=duration_days,
                ttl_seconds=3600, promo_redemption_id=promo_reservation_id,
            ))
        except Exception as exc:
            logger.info("Canonical Stars invoice rejected: %s", type(exc).__name__)
            await call.message.answer("Этот тариф сейчас нельзя оформить автоматически. Обратитесь к оператору.")
            return
        if promo_reservation_id is not None:
            await state.update_data(promo_reservation_id=None)
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
            if forwarded_invoice_link:
                await message.answer(
                    "Этот счёт нельзя оплатить из пересланного сообщения. "
                    "Создайте новый через «🛒 Купить / Продлить».",
                    reply_markup=kb_main(),
                )
                return
            greeting = "С возвращением! Чем могу помочь?"
            card = await _resolve_card_quiet(message.from_user.id)
            if card is not None:
                # Карточка как приветствие: статус/тариф/срок видны сразу,
                # без отдельного нажатия. Reply-клавиатура не переотправляется
                # — Telegram сохраняет её на клиенте.
                greeting = "С возвращением!\n\n" + render_subscription_card(card)
            await message.answer(greeting, reply_markup=kb_main())
        else:
            await state.set_state(SupportStates.waiting_link)
            prefix = ("Пересланный счёт оплатить нельзя — после покупки создайте новый через меню.\n\n"
                      if forwarded_invoice_link else "")
            await message.answer(
                prefix + "👋 Привет! Это бот MGBoost — VPN-подписка.\n\n"
                "• «🛒 Выбрать тариф» — купить подписку.\n"
                "• «🎟 Ввести промокод» — применить промокод.\n"
                "• Уже пользуетесь нашей ссылкой — нажмите «У меня уже есть "
                "подписка» и пришлите её сюда, чтобы привязать аккаунт.",
                reply_markup=kb_new_user(),
            )
            await message.answer("С чего начнём?", reply_markup=kb_new_user_inline())

    @dp.callback_query(F.data == "start_link")
    async def cb_start_link(call: CallbackQuery, state: FSMContext):
        await call.answer()
        await state.set_state(SupportStates.waiting_link)
        await call.message.answer(
            "Пришлите ссылку подписки одним сообщением — привяжу аккаунт.",
            reply_markup=kb_no_link(),
        )

    # --- Initial-onboarding promo ingress --------------------------------------
    # Кнопка «🎟 Ввести промокод» в онбординге ведёт в ТОТ ЖЕ promo flow,
    # что и «🎟 Промокод» главного меню: то же состояние waiting_promo_code,
    # тот же `_promo_redeem_message`. Ни второго FSM, ни второго redemption
    # path здесь нет -- только ранний доступ к существующему.

    async def _enter_promo_state(target_message, state: FSMContext):
        """Единый вход в промо-ввод: одно FSM-состояние и один промпт для
        главного меню и для onboarding-входа."""
        await state.set_state(SupportStates.waiting_promo_code)
        await target_message.answer(
            "Введите промокод одним сообщением:",
            reply_markup=kb_promo_cancel(),
        )

    @dp.callback_query(F.data == "start_promo")
    async def cb_start_promo(call: CallbackQuery, state: FSMContext):
        await call.answer()
        await _enter_promo_state(call.message, state)

    @dp.message(StateFilter(SupportStates.waiting_link),
                F.text.in_({"🎟 Промокод", "🎟 Ввести промокод"}))
    async def msg_new_user_promo(message: Message, state: FSMContext):
        """Reply-близнец cb_start_promo. Зарегистрирован ДО msg_waiting_link,
        чтобы метка промокода (в том числе «🎟 Промокод» со старой
        закэшированной клавиатуры главного меню) не упиралась в
        «не могу распознать ссылку»."""
        await _enter_promo_state(message, state)

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
                "👋 Привет! Пришлите ссылку подписки одним сообщением "
                "или купите подписку через «🛒 Купить / Продлить».",
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

    @dp.message(StateFilter(SupportStates.waiting_link))
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

    # --- Карточка подписки ----------------------------------------------------
    # Один рендер для обеих когорт. Повторное «📱 Моя подписка» шлёт свежую
    # карточку новым сообщением: edit по сохранённому message_id требует
    # durable-хранилища, которого у MemoryStorage нет (dedup — отдельная
    # косметическая задача). Явная inline-кнопка «🔄 Обновить» правит своё
    # же сообщение — ей message_id известен без FSM.

    async def _resolve_card_quiet(telegram_id: int):
        """Card resolver for callbacks/start: any failure degrades to None
        (the caller falls back to a plain greeting), never raises."""
        tg_user = db.get_tg_user(telegram_id)
        account = db.accounts.get_active_account_by_telegram_id(telegram_id)
        legacy_info = None
        if account is None and tg_user is not None and marzban is not None:
            try:
                admin_token = await _run_sync(marzban.get_admin_token_from_env)
                legacy_info = await _run_sync(
                    marzban.get_user, tg_user["marzban_username"], admin_token
                )
            except Exception as e:
                logger.warning(f"card: legacy marzban lookup failed: {type(e).__name__}")
                return None
        try:
            return build_subscription_card(
                db, telegram_id=telegram_id,
                legacy_user=tg_user, legacy_marzban_user=legacy_info,
            )
        except Exception as e:
            logger.error(f"card: build failed: {type(e).__name__}")
            return None

    def _card_keyboard(card: dict):
        first_row = []
        if card["cohort"] == "canonical":
            # «🔗 Ссылка» — только canonical: opaque-ссылку нельзя показать
            # повторно, а legacy-ссылку бот не хранит (только username).
            first_row.append(InlineKeyboardButton(text="🔗 Ссылка", callback_data="sub_link"))
        first_row.append(InlineKeyboardButton(text="➕ Продлить", callback_data="sub_renew"))
        return InlineKeyboardMarkup(inline_keyboard=[
            first_row,
            [
                InlineKeyboardButton(text="💻 Устройства", callback_data="sub_devices"),
                InlineKeyboardButton(text="🔄 Обновить", callback_data="sub_refresh"),
            ],
        ])

    @dp.message(StateFilter(SupportStates.in_dialog), F.text.in_({"📱 Моя подписка", "📋 Моя подписка"}))
    async def msg_my_subscription(message: Message, state: FSMContext):
        tg_user = db.get_tg_user(message.from_user.id)
        account = db.accounts.get_active_account_by_telegram_id(message.from_user.id)
        if tg_user is None and account is None:
            await state.set_state(SupportStates.waiting_link)
            await message.answer("Нужно сначала привязать подписку.", reply_markup=kb_no_link())
            return
        legacy_info = None
        if account is None:
            try:
                admin_token = await _run_sync(marzban.get_admin_token_from_env)
                legacy_info = await _run_sync(
                    marzban.get_user, tg_user["marzban_username"], admin_token
                )
            except Exception as e:
                logger.error(f"Ошибка получения подписки: {e}")
                await message.answer("Не удалось получить информацию о подписке.")
                return
        try:
            card = build_subscription_card(
                db, telegram_id=message.from_user.id,
                legacy_user=tg_user, legacy_marzban_user=legacy_info,
            )
        except Exception as e:
            logger.error(f"Ошибка построения карточки: {type(e).__name__}")
            await message.answer("Не удалось получить информацию о подписке.")
            return
        if card is None:
            await state.set_state(SupportStates.waiting_link)
            await message.answer("Нужно сначала привязать подписку.", reply_markup=kb_no_link())
            return
        await message.answer(render_subscription_card(card), reply_markup=_card_keyboard(card))

    @dp.callback_query(F.data == "sub_refresh")
    async def cb_sub_refresh(call: CallbackQuery):
        await call.answer()
        card = await _resolve_card_quiet(call.from_user.id)
        if card is None:
            await call.message.answer("Не удалось обновить карточку. Попробуйте позже.")
            return
        try:
            await call.message.edit_text(
                render_subscription_card(card), reply_markup=_card_keyboard(card),
            )
        except Exception:
            await call.message.answer(
                render_subscription_card(card), reply_markup=_card_keyboard(card),
            )

    @dp.callback_query(F.data == "sub_renew")
    async def cb_sub_renew(call: CallbackQuery):
        await call.answer()
        await _send_buy_entry(call.message, call.from_user.id)

    @dp.callback_query(F.data == "sub_open")
    async def cb_sub_open(call: CallbackQuery):
        """Inline target for post-payment delivery messages from stars.py."""
        await call.answer()
        card = await _resolve_card_quiet(call.from_user.id)
        if card is None:
            await call.message.answer("Чем могу помочь?", reply_markup=kb_main())
            return
        await call.message.answer(render_subscription_card(card), reply_markup=_card_keyboard(card))

    @dp.callback_query(F.data == "sub_devices")
    async def cb_sub_devices(call: CallbackQuery, state: FSMContext):
        await call.answer()
        await _devices_entry(call.message, call.from_user.id, state)

    # --- 🔗 Ссылка (Э2) --------------------------------------------------------
    # Экран поверх карточки И тело скрытой команды /newsub: одна и та же
    # логика. Первая ссылка выпускается сразу; перевыпуск активной — только
    # через явное подтверждение (rotation уничтожает старую ссылку).

    async def _link_entry(target, telegram_id: int):
        from .config import OPAQUE_SUBSCRIPTION_ENABLED, subscription_base_url
        if not OPAQUE_SUBSCRIPTION_ENABLED:
            await target.answer("Эта функция пока недоступна.")
            return
        if subscription_base_url() is None:
            logger.error(
                "Configuration error: PUBLIC_HOST is not set — cannot build "
                "an opaque subscription link. Set PUBLIC_HOST to fix this."
            )
            await target.answer("Не удалось выпустить ссылку. Попробуйте позже.")
            return
        account = await _run_sync(db.accounts.get_account_for_telegram, telegram_id)
        if account is None:
            await target.answer(
                "Новая ссылка подписки доступна только для проверенных аккаунтов. "
                "Если вы уже наш клиент — обратитесь к администратору."
            )
            return
        account_id = account["id"]

        existing = await _run_sync(_active_credential, account_id)
        if existing is not None:
            await target.answer(
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

        actor_ref = f"telegram:{telegram_id}"
        try:
            prep = await _run_sync(_issue_new_credential, account_id, actor_ref, "Telegram initial issuance")
        except Exception as e:
            logger.error(f"Ошибка подготовки opaque credential: {type(e).__name__}")
            await target.answer("Не удалось выпустить ссылку. Попробуйте позже.")
            return
        await _deliver_and_activate(target, account_id, actor_ref, prep)

    @dp.callback_query(F.data == "sub_link")
    async def cb_sub_link(call: CallbackQuery):
        await call.answer()
        await _link_entry(call.message, call.from_user.id)

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
        from .config import subscription_base_url
        base = subscription_base_url()
        if base is None:
            logger.error(
                "Configuration error: PUBLIC_HOST is not set — cannot deliver "
                "an opaque subscription link."
            )
            return False
        try:
            await message_or_call.answer(
                "🔗 Ваша новая ссылка подписки:\n"
                f"{base}/{prepared['raw_token']}\n\n"
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
        """PH4-04: reviewed-account-only opaque URL issuance — the body is
        shared with the card's «🔗 Ссылка» button (`_link_entry`). Hidden
        command, not a keyboard button shown to every legacy user -- only
        accounts already linked as the canonical Telegram OWNER
        (`mgboost_telegram_identities`, PROVEN ownership, never mere
        possession of a legacy link) can use it. Private chat only. A bare
        repeat while a credential is already ACTIVE never rotates it --
        see `cb_newsub_confirm`/`cb_newsub_do` below."""
        await _link_entry(message, message.from_user.id)

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
                prep = await _run_sync(_issue_new_credential, account_id, actor_ref, "Telegram confirmed reissue")
            except Exception as e:
                logger.error(f"Ошибка подготовки opaque credential: {type(e).__name__}")
                await call.message.answer("Не удалось перевыпустить ссылку. Попробуйте позже.")
                return
            await _deliver_and_activate(call.message, account_id, actor_ref, prep)
        finally:
            _reissue_in_progress.discard(account_id)

    # --- 💻 Устройства (Э5) -----------------------------------------------------
    # Canonical: счётчик через DeviceSlotStore (реальная новая модель).
    # Web-LK кнопку canonical НЕ обещаем: LK работает на legacy-модели
    # устройств, управление canonical-слотами им не доказано — поэтому
    # только поддержка. Legacy: счётчик legacy-модели + одноразовая ссылка.

    async def _devices_entry(target, telegram_id: int, state=None):
        tg_user = db.get_tg_user(telegram_id)
        account = db.accounts.get_active_account_by_telegram_id(telegram_id)
        if account is not None:
            try:
                capacity = db.device_slots.get_capacity_state(int(account["id"]))
            except Exception as e:
                logger.warning(f"devices: capacity unavailable: {type(e).__name__}")
                await target.answer(
                    "Не удалось получить устройства. Попробуйте позже или обратитесь в поддержку.",
                    reply_markup=_support_button_markup(),
                )
                return
            mode = capacity.get("limit_mode")
            active = capacity.get("active_count") or 0
            if mode == "UNLIMITED":
                text = f"Количество устройств не ограничено. Активных устройств: {active}."
            elif mode == "NONE":
                text = "Подписка ещё не активна — устройства появятся после покупки подписки."
            else:
                text = f"Активные устройства: {active} из {capacity.get('effective_limit')}."
            text += ("\n\nПереименовать или отключить устройство пока можно "
                     "через поддержку: напишите нам — сделаем быстро.")
            await target.answer(text, reply_markup=_support_button_markup())
            return
        if tg_user is None:
            if state is not None:
                await state.set_state(SupportStates.waiting_link)
            await target.answer("Нужно сначала привязать подписку.", reply_markup=kb_no_link())
            return
        link = await _build_management_link(db, marzban, telegram_id, tg_user["marzban_username"])
        if link is None:
            await target.answer(
                "⚠️ Управление устройствами временно недоступно из-за ошибки конфигурации сервера. "
                "Мы уже знаем о проблеме — попробуйте позже или обратитесь к оператору.",
            )
            return
        try:
            counts = db.get_active_device_counts([tg_user["marzban_username"]])
            active = int(counts.get(tg_user["marzban_username"], 0))
        except Exception as e:
            logger.warning(f"devices: legacy count unavailable: {type(e).__name__}")
            active = None
        try:
            limit = int(db.get_device_limit(tg_user["marzban_username"]))
        except Exception as e:
            logger.warning(f"devices: legacy limit unavailable: {type(e).__name__}")
            limit = None
        counter = ""
        if active is not None and limit is not None:
            counter = f"Активные устройства: {active} из {limit}.\n\n"
        await target.answer(
            f"💻 {counter}Ссылка для управления устройствами (действует 15 минут, одноразовая):\n\n"
            f"{link}\n\n"
            "Откройте её в браузере, чтобы переименовывать или отключать устройства. "
            "Если ссылка истечёт — запросите новую здесь же.",
        )

    @dp.message(StateFilter(SupportStates.in_dialog), F.text.in_({"💻 Устройства", "🔧 Управление устройствами"}))
    async def msg_manage_devices(message: Message, state: FSMContext):
        await _devices_entry(message, message.from_user.id, state)

    # --- 🎟 Промокод (Э4) -------------------------------------------------------

    def _is_onboarding_user(telegram_id: int) -> bool:
        """True, пока у пользователя нет ни legacy-ссылки, ни canonical
        account — то есть он всё ещё в initial onboarding."""
        return (db.get_tg_user(telegram_id) is None
                and db.accounts.get_active_account_by_telegram_id(telegram_id) is None)

    async def _promo_cancel_landing(message: Message, state: FSMContext):
        """Back/cancel из промо-ввода. Существующий пользователь возвращается
        в главный диалог; новый — в свой initial onboarding: НЕ в меню
        активного пользователя (kb_main обещает экраны, которых у него нет),
        НЕ в purchase flow и НЕ в AI-диалог."""
        if not _is_onboarding_user(message.from_user.id):
            await state.set_state(SupportStates.in_dialog)
            await message.answer("Чем могу помочь?", reply_markup=kb_main())
            return
        await state.set_state(SupportStates.waiting_link)
        await message.answer(
            "Действие отменено. Выберите, с чего начать:",
            reply_markup=kb_new_user(),
        )
        await message.answer("С чего начнём?", reply_markup=kb_new_user_inline())

    async def _promo_exit_landing(message: Message, state: FSMContext, text: str):
        """Финальный экран промо-потока (успех или отказ) — та же когортная
        развилка, что и у cancel: onboarding-пользователь не выпадает в
        in_dialog/kb_main."""
        if not _is_onboarding_user(message.from_user.id):
            await state.set_state(SupportStates.in_dialog)
            await message.answer(text, reply_markup=kb_main())
            return
        await state.set_state(SupportStates.waiting_link)
        await message.answer(text, reply_markup=kb_new_user())

    @dp.message(StateFilter(SupportStates.in_dialog), F.text.in_({"🎟 Промокод", "🎟 Ввести промокод"}))
    async def msg_promo_menu(message: Message, state: FSMContext):
        """PH5-13 self-service promo redemption entry. Any later duplicate
        delivery of the SAME code message replays idempotently in
        `_promo_redeem_message` via the (chat_id, message_id) key -- the
        state machine here is UX only, never the race-safety mechanism."""
        if db.accounts.get_active_account_by_telegram_id(message.from_user.id) is None:
            await message.answer(
                "Промокоды применяются к подтверждённому аккаунту. "
                "Если вы уже наш клиент — обратитесь к администратору."
            )
            return
        await _enter_promo_state(message, state)

    async def _promo_reserve_for_purchase(message: Message, state: FSMContext, code: str):
        """PURCHASE_DISCOUNT: reserve now, discount lands on the NEXT Stars
        invoice created in this dialog (idempotent by the same per-event key
        rule as redemption)."""
        reservation_id = f"promo-reserve-v1:{message.chat.id}:{message.message_id}"
        from .promo import PromoConflict, PromoError, PromoIneligible, PromoNotFound
        try:
            result = await _run_sync(lambda: db.promo.reserve_purchase_for_telegram_user(
                code=code, telegram_id=message.from_user.id,
                ttl_seconds=3600, idempotency_key=reservation_id,
            ))
        except PromoNotFound:
            await _promo_exit_landing(
                message, state, "❌ Промокод не найден или не применим. Попробуйте другой.")
            return
        except PromoConflict:
            await _promo_exit_landing(
                message, state, "⚠️ Этот промокод уже был использован вами ранее.")
            return
        except (PromoIneligible, PromoError):
            await _promo_exit_landing(
                message, state, "❌ Не удалось применить промокод. Обратитесь к поддержке.")
            return
        await state.update_data(promo_reservation_id=result["redemption_id"])
        await state.set_state(SupportStates.in_dialog)
        # Кнопка ведёт прямо в воронку своего тарифа — не просим искать
        # кнопку в меню. Задержавшаяся reply-клавиатура промо-режима
        # подхватывается глобальным msg_stale_cancel.
        await message.answer(
            "✅ Скидка зафиксирована. Она применится к следующему счёту.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🛒 Открыть покупку", callback_data="buy_open"),
            ]]),
        )

    @dp.message(StateFilter(SupportStates.waiting_promo_code))
    async def _promo_redeem_message(message: Message, state: FSMContext):
        if message.text in ("⬅️ Назад к боту", "❌ Отмена"):
            await _promo_cancel_landing(message, state)
            return
        code = (message.text or "").strip()
        if not code:
            await message.answer("Отправьте промокод текстом одним сообщением.")
            return
        definition = db.promo.get_definition(code)
        if definition is not None and definition["effect_kind"] == "PURCHASE_DISCOUNT":
            await _promo_reserve_for_purchase(message, state, code)
            return
        # Deterministic per-event idempotency: Telegram redelivers the same
        # update with the same (chat_id, message_id), so a retry/timeout
        # replays the SAME redemption instead of applying the promo twice.
        # Deliberately NOT a random UUID -- that would defeat replay safety.
        idempotency_key = f"promo-redeem-v1:{message.chat.id}:{message.message_id}"
        from .promo import PromoConflict, PromoError, PromoIneligible, PromoNotFound
        try:
            result = await _run_sync(lambda: db.promo.redeem_for_telegram_user(
                code=code, telegram_id=message.from_user.id,
                idempotency_key=idempotency_key,
            ))
        except PromoNotFound:
            await _promo_exit_landing(
                message, state, "❌ Промокод не найден или больше не активен. Попробуйте другой.")
            return
        except PromoConflict:
            await _promo_exit_landing(
                message, state, "⚠️ Этот промокод уже был применён ранее.")
            return
        except PromoIneligible:
            await _promo_exit_landing(
                message, state, "❌ Промокод неприменим к вашему аккаунту. Уточните условия у поддержки.")
            return
        except PromoError:
            await _promo_exit_landing(
                message, state, "❌ Не удалось применить промокод. Попробуйте позже или обратитесь к поддержке.")
            return
        except Exception:
            logger.exception("promo redeem failed")
            await _promo_exit_landing(
                message, state, "❌ Не удалось применить промокод. Попробуйте позже.")
            return
        effect = result.get("effect_result") or {}
        days = effect.get("days")
        if result.get("already_applied"):
            await _promo_exit_landing(
                message, state, "ℹ️ Этот промокод уже был применён к вашему аккаунту.")
            return
        if definition["effect_kind"] == "TRIAL_GRANT":
            # NEW USER TRIAL SIGNUP success: это был canonical WL_TRIAL --
            # у пользователя теперь есть аккаунт и подписка, поэтому он
            # выходит из промо-потока полноценным пользователем (kb_main),
            # а его первая opaque-ссылка выпускается СРАЗУ существующим
            # безопасным flow (`_link_entry`): не надо искать скрытый
            # /newsub; уже активный credential без явного согласия не
            # вращается, а деградация выдачи не теряет сам trial.
            trial_text = (
                f"✅ Trial активирован: {days} дн., 1 устройство, "
                f"WL-трафик {_fmt_gb(effect.get('quota_bytes'))} GB."
            )
            new_expiry = effect.get("new_expiry")
            if new_expiry:
                trial_text += (
                    "\nПодписка действует до "
                    + time.strftime('%d.%m.%Y %H:%M UTC', time.gmtime(int(new_expiry)))
                    + "."
                )
            await state.set_state(SupportStates.in_dialog)
            await message.answer(trial_text)
            await _link_entry(message, message.from_user.id)
            await message.answer("Чем могу помочь?", reply_markup=kb_main())
            return
        if days:
            text = f"✅ Промокод применён: +{days} дн."
            new_expiry = effect.get("new_expiry")
            if new_expiry:
                text += f"\nПодписка действует до {time.strftime('%d.%m.%Y %H:%M UTC', time.gmtime(int(new_expiry)))}."
        else:
            text = "✅ Промокод применён."
        await _promo_exit_landing(message, state, text)

    # --- 🆘 Поддержка (Э6) ------------------------------------------------------

    async def _escalate_to_human(target_message, telegram_id: int, username: str | None):
        ticket = db.get_open_ticket(telegram_id)
        if ticket:
            db.update_ticket_status(ticket["id"], "waiting_human")
        else:
            db.create_ticket(telegram_id, marzban_username=username, status="waiting_human")
        await target_message.answer(
            "Оператор скоро подключится. Опишите ваш вопрос, и я передам его.",
            reply_markup=kb_waiting(),
        )
        await _notify_admin(target_message.bot, db, telegram_id, username)

    @dp.message(StateFilter(SupportStates.in_dialog), F.text == "🆘 Поддержка")
    async def msg_support(message: Message, state: FSMContext):
        """Единая точка входа поддержки: свободный текст = ассистент,
        живой оператор — явная кнопка (и команда-алиас)."""
        await message.answer(
            "Напишите ваш вопрос прямо сюда следующим сообщением — я отвечу. "
            "Если понадобится живой оператор — позову: кнопка ниже.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🆘 Позвать оператора", callback_data="call_human"),
            ]]),
        )

    @dp.message(StateFilter(SupportStates.in_dialog), F.text == "🆘 Позвать человека")
    async def msg_call_human(message: Message, state: FSMContext):
        tg_user = db.get_tg_user(message.from_user.id)
        await state.set_state(SupportStates.waiting_human)
        await _escalate_to_human(message, message.from_user.id, tg_user["marzban_username"] if tg_user else None)

    @dp.callback_query(F.data == "call_human")
    async def cb_call_human(call: CallbackQuery, state: FSMContext):
        await call.answer()
        tg_user = db.get_tg_user(call.from_user.id)
        await state.set_state(SupportStates.waiting_human)
        await _escalate_to_human(call.message, call.from_user.id, tg_user["marzban_username"] if tg_user else None)

    @dp.message(StateFilter(SupportStates.waiting_human))
    async def msg_waiting_human(message: Message, state: FSMContext):
        if message.text == "⬅️ Назад к боту":
            await state.set_state(SupportStates.in_dialog)
            await message.answer("Чем могу помочь?", reply_markup=kb_main())
            return
        ticket = db.get_open_ticket(message.from_user.id)
        if ticket:
            db.add_ticket_message(ticket["id"], "user", message.text or "")
        await message.answer("Ваш вопрос передан оператору, ожидайте ответа.")

    @dp.message(StateFilter(SupportStates.in_dialog), F.text.in_({"❌ Отмена", "⬅️ Назад к боту"}))
    async def msg_stale_cancel(message: Message, state: FSMContext):
        """Keyboard re-sync: stale mode-keyboard buttons («❌ Отмена» после
        промо-экрана, «⬅️ Назад к боту») возвращают в главный диалог с
        kb_main вместо того, чтобы утекать в AI-тикеты."""
        await message.answer("Чем могу помочь?", reply_markup=kb_main())

    @dp.message(StateFilter(SupportStates.in_dialog), F.photo | F.document | F.video)
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

    @dp.message(StateFilter(SupportStates.waiting_human), F.photo | F.document | F.video)
    async def msg_waiting_human_media(message: Message, state: FSMContext):
        ticket = db.get_open_ticket(message.from_user.id)
        if ticket:
            media_type = "фото" if message.photo else ("видео" if message.video else "файл")
            caption = message.caption or ""
            db.add_ticket_message(ticket["id"], "user", f"[{media_type}]{': ' + caption if caption else ''}")
        await message.answer("Получил, оператор посмотрит.")

    @dp.message(StateFilter(SupportStates.in_dialog))
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
                "AI-ассистент недоступен. Нажмите «🆘 Поддержка» для связи с оператором."
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
    """Ticket closed by the operator. Beyond the notification this resets
    the user's FSM to the main dialog: pre-redesign the user stayed in
    waiting_human forever, every further message was silently swallowed by
    the ticket handler under a misleading «вопрос передан оператору»
    acknowledgement, and the reply keyboard never came back."""
    storage = state_storage or _FSM_STORAGE
    if storage is not None and SupportStates is not None:
        try:
            from aiogram.fsm.context import FSMContext
            from aiogram.fsm.storage.base import StorageKey
            key = StorageKey(bot_id=bot.id, chat_id=int(telegram_id), user_id=int(telegram_id))
            state = FSMContext(storage=storage, key=key)
            await state.set_state(SupportStates.in_dialog)
        except Exception as e:
            logger.warning(f"Не удалось вернуть FSM после закрытия тикета: {type(e).__name__}")
    try:
        await bot.send_message(
            int(telegram_id),
            "✅ Вопрос закрыт. Если остались вопросы — просто напишите сюда!",
            reply_markup=kb_main(),
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
    from .config import subscription_base_url

    base = subscription_base_url()
    if base is None:
        logger.error(
            "Configuration error: PUBLIC_HOST is invalid or not set — cannot build a "
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
    return f"{base}/lk/#mgmt={code}"
