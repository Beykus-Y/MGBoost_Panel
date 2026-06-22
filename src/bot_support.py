import logging
import re
import time
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

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
            return "Подписка не привязана."
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


def setup_support_handlers(dp, db, marzban, node_states: dict | None = None, node_names: dict | None = None):
    try:
        from aiogram import F
        from aiogram.filters import CommandStart, StateFilter
        from aiogram.fsm.context import FSMContext
        from aiogram.fsm.state import State, StatesGroup
        from aiogram.types import (
            CallbackQuery,
            InlineKeyboardButton,
            InlineKeyboardMarkup,
            KeyboardButton,
            Message,
            ReplyKeyboardMarkup,
            ReplyKeyboardRemove,
        )
    except ImportError:
        logger.error("aiogram не установлен — поддержка не запустится")
        return

    class SupportStates(StatesGroup):
        waiting_link = State()
        in_dialog = State()
        waiting_human = State()

    def kb_main():
        return ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="📋 Моя подписка"), KeyboardButton(text="🆘 Позвать человека")]],
            resize_keyboard=True,
        )

    def kb_waiting():
        return ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="⬅️ Назад к боту")]],
            resize_keyboard=True,
        )

    def kb_no_link():
        return InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="У меня нет ссылки", callback_data="no_link")]]
        )

    @dp.message(CommandStart())
    async def cmd_start(message: Message, state: FSMContext):
        await state.clear()
        tg_user = db.get_tg_user(message.from_user.id)
        if tg_user:
            await state.set_state(SupportStates.in_dialog)
            await message.answer(
                f"С возвращением! Чем могу помочь?",
                reply_markup=kb_main(),
            )
        else:
            await state.set_state(SupportStates.waiting_link)
            await message.answer(
                "👋 Привет! Я помогу с вашей VPN-подпиской.\n\n"
                "Пришлите ссылку подписки для привязки аккаунта.\n"
                "Её можно найти в письме или у администратора.",
                reply_markup=kb_no_link(),
            )

    @dp.message(StateFilter(None))
    async def msg_no_state(message: Message, state: FSMContext):
        tg_user = db.get_tg_user(message.from_user.id)
        if tg_user:
            await state.set_state(SupportStates.in_dialog)
            await message.answer("Чем могу помочь?", reply_markup=kb_main())
        else:
            await state.set_state(SupportStates.waiting_link)
            await message.answer(
                "👋 Привет! Пришли ссылку подписки для привязки аккаунта.",
                reply_markup=kb_no_link(),
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
            admin_token = await _run_sync(marzban.get_admin_token_from_env)
            username = marzban.get_username_for_token(token)
            if not username:
                await message.answer("Ссылка не найдена в системе. Проверьте правильность.")
                return
        except Exception as e:
            logger.error(f"Ошибка поиска пользователя: {e}")
            await message.answer("Ошибка проверки подписки. Попробуйте позже.")
            return

        db.save_tg_user(message.from_user.id, username)
        await state.set_state(SupportStates.in_dialog)
        await message.answer(
            f"✅ Аккаунт привязан: {username}\n\nЧем могу помочь?",
            reply_markup=kb_main(),
        )

    @dp.message(SupportStates.in_dialog, F.text == "📋 Моя подписка")
    async def msg_my_subscription(message: Message, state: FSMContext):
        tg_user = db.get_tg_user(message.from_user.id)
        if not tg_user:
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
