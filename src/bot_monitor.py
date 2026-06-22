import asyncio
import logging
import threading
from datetime import datetime, timezone, time as dtime

logger = logging.getLogger(__name__)

CHECK_INTERVAL = 60


def is_node_up(status: str) -> bool:
    return status == "connected"


def in_quiet_hours(quiet_hours: list) -> bool:
    if not quiet_hours:
        return False
    now = datetime.now(timezone.utc).time().replace(second=0, microsecond=0)
    for window in quiet_hours:
        h_from, m_from = map(int, window["from"].split(":"))
        h_to, m_to = map(int, window["to"].split(":"))
        if dtime(h_from, m_from) <= now <= dtime(h_to, m_to):
            return True
    return False


def get_display_name(node: dict, db) -> str:
    if db:
        setting = db.get_node_setting(node["id"])
        if setting and setting.get("node_name"):
            return setting["node_name"]
    return node["name"]


def compute_changes(nodes: list, states: dict, db) -> list:
    changes = []
    now = datetime.now(timezone.utc)
    for node in nodes:
        nid = node["id"]
        up = is_node_up(node["status"])
        prev = states.get(nid)
        states[nid] = {"up": up, "last_check": now}
        if prev is None:
            continue
        if prev["up"] and not up:
            quiet = in_quiet_hours(db.get_node_quiet_hours(nid) if db else [])
            changes.append({"node": node, "went_down": True, "came_up": False, "in_quiet": quiet})
        elif not prev["up"] and up:
            changes.append({"node": node, "went_down": False, "came_up": True, "in_quiet": False})
    return changes


def format_status(nodes: list, states: dict, names: dict) -> str:
    if not nodes:
        return "Список нод пуст."
    lines = ["📡 *Статус нод VPN*\n"]
    for node in nodes:
        nid = node["id"]
        name = names.get(nid) or node["name"]
        state = states.get(nid)
        if state is None:
            icon, time_str = "⏳", "ещё не проверялась"
        else:
            icon = "🟢" if state["up"] else "🔴"
            time_str = state["last_check"].strftime("%H:%M:%S")
        lines.append(f"{icon} {name} — {time_str}")
    updated_at = datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M:%S UTC")
    lines.append(f"\n🕐 Обновлено: {updated_at}")
    return "\n".join(lines)


async def _delete_after(bot, channel_id, *message_ids: int, delay: int = 600):
    await asyncio.sleep(delay)
    for mid in message_ids:
        try:
            await bot.delete_message(chat_id=channel_id, message_id=mid)
        except Exception as e:
            logger.warning(f"Не удалось удалить сообщение {mid}: {e}")


async def _monitor_loop(bot, channel_id: str, db, marzban, stop_event: threading.Event,
                        shared_states: dict | None = None, shared_names: dict | None = None):
    states: dict = {}
    down_ids: dict = {}

    saved_id = db.get_setting("bot:pinned_message_id") if db else None
    pinned: list = [int(saved_id)] if saved_id else []

    async def tick():
        try:
            loop = asyncio.get_event_loop()
            admin_token = await loop.run_in_executor(None, marzban.get_admin_token_from_env)
            nodes = await loop.run_in_executor(None, marzban.get_nodes, admin_token)
        except Exception as e:
            logger.warning(f"Не удалось получить ноды от Marzban: {e}")
            return

        names = {n["id"]: get_display_name(n, db) for n in nodes}
        if shared_states is not None:
            shared_states.clear()
            shared_states.update(states)
        if shared_names is not None:
            shared_names.clear()
            shared_names.update(names)
        changes = compute_changes(nodes, states, db)

        for ch in changes:
            node = ch["node"]
            name = names[node["id"]]
            if ch["went_down"]:
                if ch["in_quiet"]:
                    logger.info(f"Нода упала (тихие часы, без алерта): {name}")
                else:
                    logger.warning(f"Нода упала: {name}")
                    msg = await bot.send_message(channel_id, f"⚠️ Нода {name} недоступна")
                    down_ids[node["id"]] = msg.message_id
            elif ch["came_up"]:
                logger.info(f"Нода восстановлена: {name}")
                up_msg = await bot.send_message(channel_id, f"✅ Нода {name} восстановлена")
                down_mid = down_ids.pop(node["id"], None)
                ids_to_delete = [up_msg.message_id] + ([down_mid] if down_mid else [])
                asyncio.create_task(_delete_after(bot, channel_id, *ids_to_delete, delay=600))

        status_text = format_status(nodes, states, names)
        pinned_id = pinned[0] if pinned else None
        if pinned_id:
            try:
                await bot.edit_message_text(chat_id=channel_id, message_id=pinned_id,
                                            text=status_text, parse_mode="Markdown")
            except Exception as e:
                logger.warning(f"Не удалось отредактировать закреп: {e}")
        else:
            msg = await bot.send_message(channel_id, status_text, parse_mode="Markdown")
            pinned[0:] = [msg.message_id]
            if db:
                db.set_setting("bot:pinned_message_id", str(msg.message_id))
            try:
                pin_msg = await bot.pin_chat_message(channel_id, msg.message_id, disable_notification=True)
                await bot.delete_message(channel_id, pin_msg.message_id)
            except Exception:
                pass

    logger.info("Инициализация состояния нод...")
    await tick()
    logger.info(f"Мониторинг запущен, канал: {channel_id}, интервал: {CHECK_INTERVAL}с")

    while not stop_event.is_set():
        await asyncio.sleep(CHECK_INTERVAL)
        if stop_event.is_set():
            break
        await tick()


async def _wait_stop(stop_event: threading.Event):
    loop = asyncio.get_event_loop()
    while not stop_event.is_set():
        await asyncio.sleep(1)


async def run_all(bot_token: str, channel_id: str, proxy_url: str | None, db, marzban,
                  stop_event: threading.Event, bot_ref: list | None = None):
    try:
        from aiogram import Bot, Dispatcher
        from aiogram.client.session.aiohttp import AiohttpSession
        from aiogram.fsm.storage.memory import MemoryStorage
    except ImportError:
        logger.error("aiogram не установлен — бот не запустится")
        return

    from .bot_support import setup_support_handlers

    if proxy_url:
        proxy = proxy_url.replace("socks5h://", "socks5://")
        session = AiohttpSession(proxy=proxy)
    else:
        session = None

    bot = Bot(token=bot_token, session=session) if session else Bot(token=bot_token)

    if bot_ref is not None:
        bot_ref.append(bot)

    shared_states: dict = {}
    shared_names: dict = {}

    dp = Dispatcher(storage=MemoryStorage())
    setup_support_handlers(dp, db, marzban, node_states=shared_states, node_names=shared_names)

    monitor_task = asyncio.create_task(
        _monitor_loop(bot, channel_id, db, marzban, stop_event,
                      shared_states=shared_states, shared_names=shared_names)
    )
    stop_watcher = asyncio.create_task(_wait_stop(stop_event))

    try:
        await asyncio.gather(
            dp.start_polling(bot, handle_signals=False),
            monitor_task,
            stop_watcher,
            return_exceptions=True,
        )
    finally:
        await dp.stop_polling()
        try:
            await bot.session.close()
        except Exception:
            pass
        if bot_ref:
            bot_ref.clear()
