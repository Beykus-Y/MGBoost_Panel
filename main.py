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
    bot_runner = BotRunner.from_db(db, marzban=marzban)
    if bot_runner:
        bot_runner.start()
        print(f"[Bot] Запущен в фоне, канал: {bot_runner.channel_id}, прокси: {bot_runner.proxy_url}")
    else:
        print("[Bot] bot:token не задан в БД — мониторинг не запущен")

    server = Server(db)
    server.run(LISTEN_HOST, LISTEN_PORT)
