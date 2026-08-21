import asyncio
import logging
import threading


logger = logging.getLogger(__name__)


def build_proxy_url(host, port, user, password) -> str | None:
    if not host:
        return None
    return f"socks5h://{user}:{password}@{host}:{port}"


class BotRunner(threading.Thread):
    def __init__(self, bot_token: str, channel_id: str, proxy_url: str | None, db, marzban=None):
        super().__init__(daemon=True, name="bot-runner")
        self.bot_token = bot_token
        self.channel_id = channel_id
        self.proxy_url = proxy_url
        self.db = db
        self.marzban = marzban
        self._stop_event = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._bot_ref: list = []

    @classmethod
    def from_db(cls, db, marzban=None) -> "BotRunner | None":
        token = db.get_setting("bot:token")
        if not token:
            return None
        channel_id = db.get_setting("bot:channel_id", "@MGBoost_News")
        proxy_url = build_proxy_url(
            db.get_setting("bot:proxy_host"),
            db.get_setting("bot:proxy_port", "1080"),
            db.get_setting("bot:proxy_user", "socks"),
            db.get_setting("bot:proxy_pass", ""),
        )
        return cls(bot_token=token, channel_id=channel_id, proxy_url=proxy_url, db=db, marzban=marzban)

    @property
    def bot_instance(self):
        return self._bot_ref[0] if self._bot_ref else None

    def stop(self):
        self._stop_event.set()
        if self._loop and self._loop.is_running():
            # Wake the loop; run_all observes _stop_event and performs an
            # orderly polling/worker shutdown. Never stop run_until_complete
            # from underneath its main coroutine.
            self._loop.call_soon_threadsafe(lambda: None)

    def run(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._run_bot())
        except Exception:
            logger.exception("Bot runner stopped with an error")
        finally:
            pending = asyncio.all_tasks(self._loop)
            for task in pending:
                task.cancel()
            if pending:
                self._loop.run_until_complete(
                    asyncio.gather(*pending, return_exceptions=True)
                )
            self._loop.run_until_complete(self._loop.shutdown_asyncgens())
            self._loop.run_until_complete(self._loop.shutdown_default_executor())
            self._loop.close()
            self._loop = None
            self._bot_ref.clear()
            from .marzban_lock import marzban_user_locks
            try:
                marzban_user_locks.reset_after_loop_shutdown()
            except RuntimeError:
                logger.exception("Marzban lock registry was still in use at bot shutdown")

    async def _run_bot(self):
        from .bot_monitor import run_all
        await run_all(
            bot_token=self.bot_token,
            channel_id=self.channel_id,
            proxy_url=self.proxy_url,
            db=self.db,
            marzban=self.marzban,
            stop_event=self._stop_event,
            bot_ref=self._bot_ref,
        )
