#!/usr/bin/env python3
from src.config import LISTEN_HOST, LISTEN_PORT
from src.database import Database
from src.promo import ensure_wl_trial_plan_version
from src.service_marzban import ServiceMarzbanClient
from src.server import Server
from src.bot_runner import BotRunner

if __name__ == "__main__":
    db = Database()
    db.migrate_from_json()
    # The seed is versioned and idempotent.  Keep it in the application
    # bootstrap rather than Database() so isolated stores remain neutral.
    ensure_wl_trial_plan_version(db.accounts)

    marzban = ServiceMarzbanClient()
    marzban.assert_credential_boundary()

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
