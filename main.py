#!/usr/bin/env python3
from src.config import LISTEN_HOST, LISTEN_PORT
from src.database import Database
from src.marzban import MarzbanClient
from src.server import Server
from src.bot_runner import BotRunner

if __name__ == "__main__":
    db = Database()
    db.migrate_from_json()

    marzban = MarzbanClient()

    def make_bot_runner():
        return BotRunner.from_db(db, marzban=marzban)

    bot_runner = make_bot_runner()
    if bot_runner:
        bot_runner.start()
        proxy_state = "enabled" if bot_runner.proxy_url else "disabled"
        print(f"[Bot] Запущен в фоне, Telegram proxy: {proxy_state}")
    else:
        print("[Bot] Telegram bot not started; check bot configuration")

    server = Server(db)
    server.run(LISTEN_HOST, LISTEN_PORT, bot_runner=bot_runner, bot_runner_factory=make_bot_runner)
