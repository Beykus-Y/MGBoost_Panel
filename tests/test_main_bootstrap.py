"""Production entrypoint bootstrap ordering, without opening real services."""

from __future__ import annotations

import runpy
from pathlib import Path


def test_main_seeds_wl_trial_after_legacy_migration(monkeypatch):
    import src.bot_runner as bot_runner
    import src.database as database
    import src.promo as promo
    import src.server as server
    import src.service_marzban as service_marzban

    events = []

    class FakeDatabase:
        accounts = object()

        def migrate_from_json(self):
            events.append("migrate")

    class FakeMarzban:
        def assert_credential_boundary(self):
            events.append("credential-boundary")

    class FakeBotRunner:
        @classmethod
        def from_db(cls, _db, *, marzban):
            assert isinstance(marzban, FakeMarzban)
            events.append("bot-runner")
            return None

    class FakeServer:
        def __init__(self, _db):
            events.append("server")

        def run(self, _host, _port, *, bot_runner, bot_runner_factory):
            assert bot_runner is None
            assert callable(bot_runner_factory)
            events.append("run")

    def seed(accounts):
        assert accounts is FakeDatabase.accounts
        assert events == ["migrate"]
        events.append("seed")

    monkeypatch.setattr(database, "Database", FakeDatabase)
    monkeypatch.setattr(promo, "ensure_wl_trial_plan_version", seed)
    monkeypatch.setattr(service_marzban, "ServiceMarzbanClient", FakeMarzban)
    monkeypatch.setattr(bot_runner, "BotRunner", FakeBotRunner)
    monkeypatch.setattr(server, "Server", FakeServer)

    runpy.run_path(str(Path(__file__).parents[1] / "main.py"), run_name="__main__")
    assert events == ["migrate", "seed", "credential-boundary", "bot-runner", "server", "run"]
