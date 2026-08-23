import json
from pathlib import Path
from urllib.error import URLError


RAW_TOKEN = "legacy-bearer-value-unchanged"


def test_token_reference_is_stable_idempotent_and_not_the_bearer():
    from src.sensitive import is_subscription_token_ref, subscription_token_ref

    reference = subscription_token_ref(RAW_TOKEN)
    assert reference != RAW_TOKEN
    assert is_subscription_token_ref(reference)
    assert subscription_token_ref(reference) == reference
    assert len(reference) == len("sha256:") + 64


def test_request_target_redacts_path_bearers_and_all_queries():
    from src.sensitive import redact_request_target

    assert redact_request_target(f"/sub/{RAW_TOKEN}") == "/sub/<redacted>"
    assert redact_request_target(f"/sub/{RAW_TOKEN}/info") == "/sub/<redacted>/info"
    assert redact_request_target(f"/lk/api/info?token={RAW_TOKEN}") == "/lk/api/info?<redacted>"
    assert RAW_TOKEN not in redact_request_target(f"/other?next={RAW_TOKEN}")


def test_server_request_log_and_urllib_error_never_emit_raw_token(capsys, monkeypatch):
    from src import marzban
    from src.server import _Handler

    handler = object.__new__(_Handler)
    handler.client_address = ("127.0.0.1", 12345)
    handler.command = "GET"
    handler.path = f"/sub/{RAW_TOKEN}?token={RAW_TOKEN}"
    handler.log_message('"%s" %s %s', f"GET {handler.path} HTTP/1.1", "502", "-")

    marzban._USERNAME_CACHE.clear()
    monkeypatch.setattr(
        marzban, "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            URLError(f"http://127.0.0.1:8000/sub/{RAW_TOKEN}/info")
        ),
    )
    assert marzban.MarzbanClient().get_username_for_token(RAW_TOKEN) is None
    output = capsys.readouterr().out
    assert RAW_TOKEN not in output
    assert "/sub/<redacted>?<redacted>" in output
    assert "URLError" in output


def test_new_database_writes_store_only_token_references(tmp_path, monkeypatch):
    from src import database as database_module
    from src.sensitive import subscription_token_ref

    monkeypatch.setattr(database_module, "DB_PATH", str(tmp_path / "db.sqlite3"))
    db = database_module.Database()
    metadata = {
        "request_key": "hwid:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "device_name": "Phone",
        "client_name": "Happ",
        "client_version": "1",
        "platform": "Android",
        "metadata": {},
    }
    try:
        db.log_request(RAW_TOKEN, "alice", "Happ", "192.0.2.1", metadata)
        assert db.check_device_access("alice", RAW_TOKEN, metadata) == (False, None)
        db.update_hysteria_stats(RAW_TOKEN, 10, 20)
        ref = subscription_token_ref(RAW_TOKEN)
        for table in ("sub_requests", "user_devices", "hysteria_stats"):
            values = [row[0] for row in db._conn.execute(f"SELECT token FROM {table}")]
            assert values == [ref]
            assert RAW_TOKEN not in values
        assert db.get_hysteria_traffic(RAW_TOKEN) == (10, 20)
        assert len(db.get_device_history(RAW_TOKEN)) == 1
        assert set(db.get_hysteria_stats()) == {ref}
    finally:
        db._conn.close()


def test_controlled_legacy_token_migration_preserves_usage_and_device_rows(tmp_path, monkeypatch):
    from src import database as database_module
    from src.sensitive import is_subscription_token_ref, subscription_token_ref

    monkeypatch.setattr(database_module, "DB_PATH", str(tmp_path / "db.sqlite3"))
    db = database_module.Database()
    now = 1_700_000_000
    ref = subscription_token_ref(RAW_TOKEN)
    try:
        db._conn.execute(
            "INSERT INTO sub_requests (token, username, timestamp) VALUES (?,?,?)",
            (RAW_TOKEN, "alice", now),
        )
        db._conn.execute(
            "INSERT INTO user_devices "
            "(username, token, request_key, is_active, first_seen, last_seen) "
            "VALUES (?,?,?,?,?,?)",
            ("alice", RAW_TOKEN, "hwid:legacy", 1, now, now),
        )
        db._conn.execute(
            "INSERT INTO hysteria_stats (token, upload, download) VALUES (?,?,?)",
            (RAW_TOKEN, 7, 11),
        )
        db._conn.execute(
            "INSERT INTO hysteria_stats (token, upload, download) VALUES (?,?,?)",
            (ref, 13, 17),
        )
        db._conn.commit()

        assert db.migrate_legacy_subscription_token_storage() == {
            "sub_requests": 1,
            "user_devices": 1,
            "hysteria_stats": 1,
        }
        for table in ("sub_requests", "user_devices", "hysteria_stats"):
            values = [row[0] for row in db._conn.execute(f"SELECT token FROM {table}")]
            assert values and all(is_subscription_token_ref(value) for value in values)
            assert RAW_TOKEN not in values
        assert db.get_hysteria_traffic(RAW_TOKEN) == (20, 28)
        assert len(db.get_device_history(RAW_TOKEN)) == 1
        assert db.migrate_legacy_subscription_token_storage() == {
            "sub_requests": 0,
            "user_devices": 0,
            "hysteria_stats": 0,
        }
    finally:
        db._conn.close()


def test_lk_frontend_moves_legacy_query_to_memory_and_uses_header():
    source = Path("frontend/assets/lk.js").read_text()
    assert "history.replaceState" in source
    assert "X-MGBoost-Subscription" in source
    assert "`?token=${encodeURIComponent(token)}`" not in source
    assert "location.href = `/lk/#token=" in source


def test_browser_subscription_page_has_no_referrer_links():
    source = Path("frontend/browser_page.html").read_text()
    assert source.count('target="_blank"') == source.count('rel="noreferrer noopener"')
