import logging
import re
import time
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

_SUB_TOKEN_RE = re.compile(r"https?://[^/]+/sub/([^/\s?#]+)")

SYSTEM_PROMPT = (
    "Ты — AI-ассистент технической поддержки VPN-сервиса MGBoost. "
    "Отвечай коротко, по-русски, дружелюбно. "
    "Помогай пользователям с подключением, настройкой клиентов (Hiddify, V2RayNG, Clash), "
    "проблемами с подпиской и доступом к интернету через VPN. "
    "Если вопрос не связан с VPN или техподдержкой — мягко перенаправь. "
    "Не раскрывай технические детали сервера."
)


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


def build_ai_messages(user_message: str, history: list, system: str = SYSTEM_PROMPT) -> list:
    messages = [{"role": "system", "content": system}]
    relevant = [m for m in history if m["role"] in ("user", "ai")][-10:]
    for m in relevant:
        role = "assistant" if m["role"] == "ai" else "user"
        messages.append({"role": role, "content": m["text"]})
    messages.append({"role": "user", "content": user_message})
    return messages


async def ask_openrouter(api_key: str, model: str, messages: list) -> str:
    try:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            resp = await session.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "HTTP-Referer": "https://sub.beykus.fun",
                    "X-Title": "MGBoost Support",
                },
                json={"model": model, "messages": messages},
                timeout=aiohttp.ClientTimeout(total=30),
            )
            data = await resp.json()
            return data["choices"][0]["message"]["content"]
    except Exception as e:
        logger.error(f"OpenRouter error: {e}")
        return "Извините, AI-ассистент временно недоступен. Попробуйте позже или позвоните оператору."


def setup_support_handlers(dp, db, marzban):
    try:
        from aiogram import F
        from aiogram.filters import CommandStart
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
            await message.answer(format_subscription(user_info))
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
        messages = build_ai_messages(message.text, history[:-1])
        reply = await ask_openrouter(api_key, model, messages)
        db.add_ticket_message(ticket_id, "ai", reply)
        await message.answer(reply)


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
    try:
        await bot.send_message(telegram_id, f"💬 Оператор: {text}")
    except Exception as e:
        logger.warning(f"Не удалось доставить ответ оператора: {e}")


async def _run_sync(func, *args):
    import asyncio
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, func, *args)
