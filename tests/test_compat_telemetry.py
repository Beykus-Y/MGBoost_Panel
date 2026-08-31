import base64
import importlib
import io
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest


KEY = "ph3-07-test-hmac-key-material-at-least-32-bytes"
TOKEN = "raw-subscription-token-canary-never-store"
RAW_HWID = "raw-device-hwid-canary-123456"
UUID_CANARY = "12345678-1234-4234-9234-123456789abc"


@pytest.fixture
def telemetry_db(tmp_path, monkeypatch):
    data_dir = str(tmp_path)
    monkeypatch.setenv("DATA_DIR", data_dir)
    monkeypatch.setenv("COMPAT_TELEMETRY_HMAC_KEY", KEY)
    import src.config as config
    import src.database as database

    importlib.reload(config)
    importlib.reload(database)
    database.DB_PATH = str(tmp_path / "db.sqlite3")
    instance = database.Database()
    yield instance, database.DB_PATH
    instance._conn.close()


def metadata(*, hwid=RAW_HWID, client="Happ", version="3.1", platform="Android"):
    return {
        "device_id": hwid,
        "client_name": client,
        "client_version": version,
        "platform": platform,
    }


def test_schema_is_additive_idempotent_and_requires_ph3_02(telemetry_db):
    db, _ = telemetry_db
    from src.compat_telemetry_schema import (
        MIGRATION_ID,
        NEW_RUNTIME_TABLES,
        SCHEMA_CHECKSUM,
        apply_compat_telemetry_schema,
    )

    marker = db._conn.execute(
        "SELECT schema_checksum FROM mgboost_schema_migrations WHERE migration_id=?",
        (MIGRATION_ID,),
    ).fetchone()
    assert marker[0] == SCHEMA_CHECKSUM
    assert apply_compat_telemetry_schema(db._conn, now=2) is False
    assert all(
        db._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
        for table in NEW_RUNTIME_TABLES
    )

    isolated = sqlite3.connect(":memory:")
    isolated.execute(
        "CREATE TABLE mgboost_schema_migrations "
        "(migration_id TEXT PRIMARY KEY,schema_checksum TEXT NOT NULL,applied_at INTEGER NOT NULL)"
    )
    isolated.commit()
    with pytest.raises(RuntimeError, match="requires exact PH3-02"):
        apply_compat_telemetry_schema(isolated, now=1)
    assert isolated.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE name='mgboost_hwid_compat_daily'"
    ).fetchone()[0] == 0


@pytest.mark.parametrize(
    ("device_id", "client", "version", "category"),
    [
        (RAW_HWID, "Happ", "3.1", "SUPPORTED_HWID_PRESENT"),
        (None, "v2rayNG", "1.9", "HWID_MISSING"),
        ("bad", "Happ", "3.1", "HWID_UNSUPPORTED_OR_MALFORMED"),
        (None, None, None, "HWID_MISSING"),
    ],
)
def test_supported_missing_malformed_and_unknown_classification(
    device_id, client, version, category
):
    from src.compat_telemetry import build_observation

    result = build_observation(
        TOKEN,
        metadata(hwid=device_id, client=client, version=version, platform=None),
        KEY,
    )
    assert result.category == category
    assert result.client_name == (client.lower() if client else "unknown")
    assert result.client_version == (version.lower() if version else "unknown")
    assert result.client_ref.startswith("hmac-sha256:")
    assert TOKEN not in result.client_ref
    assert str(device_id) not in result.client_ref


def test_real_header_parser_marks_supported_and_malformed_without_changing_request_key():
    from src.device_headers import extract_device_metadata

    supported = extract_device_metadata(
        {"User-Agent": f"Happ/3.1/Android/{RAW_HWID}"}
    )
    assert supported["hwid_candidate_present"] is True
    assert supported["hwid_candidate_supported"] is True
    assert supported["request_key"].startswith("hwid:")

    malformed = extract_device_metadata(
        {"User-Agent": "Happ/3.1/Android", "X-HWID": "bad"}
    )
    assert malformed["hwid_candidate_present"] is True
    assert malformed["hwid_candidate_supported"] is False
    # Observe-only classification must not alter the permissive legacy key.
    assert malformed["request_key"].startswith("hwid:")


def test_repeated_requests_are_aggregated_without_raw_identifiers(telemetry_db):
    db, path = telemetry_db
    first = db.observe_hwid_compatibility(TOKEN, metadata(), now=100)
    second = db.observe_hwid_compatibility(TOKEN, metadata(), now=101)
    assert first.client_ref == second.client_ref

    row = db._conn.execute(
        "SELECT request_count,correlated_subject_count,repeat_request_count "
        "FROM mgboost_hwid_compat_daily"
    ).fetchone()
    assert tuple(row) == (2, 1, 1)
    subject = db._conn.execute(
        "SELECT request_count FROM mgboost_hwid_compat_subjects"
    ).fetchone()
    assert subject[0] == 2
    serialized = open(path, "rb").read()
    for forbidden in (TOKEN, RAW_HWID, UUID_CANARY, KEY):
        assert forbidden.encode() not in serialized


def test_concurrent_observations_are_atomic_across_connections(telemetry_db):
    db, path = telemetry_db
    from src.compat_telemetry import record_observation

    def write(_):
        return record_observation(
            path, TOKEN, metadata(), KEY, now=200, timeout_seconds=2
        ).category

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(write, range(40)))
    assert results == ["SUPPORTED_HWID_PRESENT"] * 40
    row = db._conn.execute(
        "SELECT request_count,correlated_subject_count,repeat_request_count "
        "FROM mgboost_hwid_compat_daily"
    ).fetchone()
    assert tuple(row) == (40, 1, 39)
    assert db._conn.execute(
        "SELECT request_count FROM mgboost_hwid_compat_subjects"
    ).fetchone()[0] == 40


def test_retention_removes_detail_at_30_and_rollup_at_60_days(telemetry_db):
    db, _ = telemetry_db
    from src.compat_telemetry import SECONDS_PER_DAY, cleanup_expired

    db.observe_hwid_compatibility(TOKEN, metadata(), now=100)
    deleted = cleanup_expired(
        db._conn.execute("PRAGMA database_list").fetchone()[2],
        now=31 * SECONDS_PER_DAY + 100,
    )
    assert deleted == {"detail_rows_deleted": 1, "rollup_rows_deleted": 0}
    assert db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_hwid_compat_subjects WHERE day_start=0"
    ).fetchone()[0] == 0
    assert db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_hwid_compat_daily WHERE day_start=0"
    ).fetchone()[0] == 1

    deleted = cleanup_expired(
        db._conn.execute("PRAGMA database_list").fetchone()[2],
        now=61 * SECONDS_PER_DAY + 100,
    )
    assert deleted == {"detail_rows_deleted": 0, "rollup_rows_deleted": 1}
    assert db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_hwid_compat_daily WHERE day_start=0"
    ).fetchone()[0] == 0


def test_retention_is_safe_before_schema_or_after_application_rollback(tmp_path):
    from src.compat_telemetry import cleanup_expired

    path = str(tmp_path / "older-app.sqlite3")
    sqlite3.connect(path).close()
    assert cleanup_expired(path, now=100) == {
        "detail_rows_deleted": 0,
        "rollup_rows_deleted": 0,
    }


def test_telemetry_creates_no_accounts_slots_generations_or_child_state(telemetry_db):
    db, _ = telemetry_db
    db.observe_hwid_compatibility(TOKEN, metadata(), now=100)
    for table in (
        "mgboost_accounts",
        "mgboost_device_slots",
        "mgboost_device_slot_generations",
    ):
        assert db._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    assert db._conn.execute("SELECT COUNT(*) FROM user_devices").fetchone()[0] == 0
    assert db._conn.execute("SELECT COUNT(*) FROM hwid_lock").fetchone()[0] == 0


class _DB:
    def __init__(self, *, observer_error=None):
        self.observer_error = observer_error
        self.device_checks = []
        self.requests = []

    def observe_hwid_compatibility(self, token, device_metadata):
        if self.observer_error:
            raise self.observer_error

    def check_device_access(self, username, token, device_metadata, *, hwid_hmac_key=None):
        self.device_checks.append((username, token, device_metadata))
        return False, None

    def log_request(self, *args):
        self.requests.append(args)

    def get_setting(self, _key):
        return None

    def get_node_filter(self, _username):
        return None

    def get_extra_configs(self):
        return []

    def get_per_user_configs(self, _username):
        return []

    def get_hysteria_traffic(self, _token):
        return 0, 0


class _Client:
    def __init__(self, body):
        self.body = body

    def get_sub(self, token, extra_headers=None):
        return self.body, {"Profile-Title": "same"}

    def get_username_for_token(self, token):
        return "legacy-user"


class _Handler:
    def __init__(self, db, headers):
        self.headers = headers
        self.client_address = ("198.51.100.10", 1)
        self.server = type("Server", (), {"db": db})()
        self.wfile = io.BytesIO()
        self.status = None
        self.response_headers = []

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.response_headers.append((key, value))

    def end_headers(self):
        pass


def test_telemetry_db_and_logging_outage_are_fail_open_and_body_exact(monkeypatch, caplog):
    from src.routes import sub as route

    original = base64.b64encode(
        f"vless://{UUID_CANARY}@vpn.example:443?type=tcp#One".encode()
    )
    db = _DB(observer_error=RuntimeError(f"{TOKEN}/{RAW_HWID}/{UUID_CANARY}"))
    monkeypatch.setattr(route, "_client", _Client(original))
    handler = _Handler(
        db,
        {"User-Agent": f"Happ/3.1/Android/{RAW_HWID}"},
    )
    route.handle_sub(handler, TOKEN)
    assert handler.status == 200
    assert handler.wfile.getvalue() == original
    assert len(db.device_checks) == 1
    assert len(db.requests) == 1
    logs = caplog.text
    assert "RuntimeError" in logs
    for forbidden in (TOKEN, RAW_HWID, UUID_CANARY):
        assert forbidden not in logs

    monkeypatch.setattr(route.logger, "warning", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("log down")))
    second = _Handler(db, {"User-Agent": "v2rayNG/1.9/Android"})
    route.handle_sub(second, TOKEN)
    assert second.status == 200
    assert second.wfile.getvalue() == original
    assert second.server.db.device_checks == db.device_checks


def test_missing_and_malformed_hwid_remain_permissive(monkeypatch):
    from src.routes import sub as route

    original = base64.b64encode(
        f"vless://{UUID_CANARY}@vpn.example:443?type=tcp#One".encode()
    )
    monkeypatch.setattr(route, "_client", _Client(original))
    for headers in (
        {"User-Agent": "v2rayNG/1.9/Android"},
        {"User-Agent": "Happ/3.1/Android", "X-HWID": "bad"},
        {"User-Agent": "unknown"},
    ):
        db = _DB()
        handler = _Handler(db, headers)
        route.handle_sub(handler, TOKEN)
        assert handler.status == 200
        assert handler.wfile.getvalue() == original


def test_report_is_aggregate_only(telemetry_db):
    db, path = telemetry_db
    db.observe_hwid_compatibility(TOKEN, metadata(), now=100)
    db.observe_hwid_compatibility(TOKEN, metadata(), now=101)
    db.observe_hwid_compatibility("missing-token", metadata(hwid=None), now=102)

    from scripts.report_ph3_07_compatibility import build_report

    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        report = build_report(connection, now=200, days=30)
    finally:
        connection.close()
    assert report["requests"] == 3
    assert report["supported_hwid_percent"] == 66.67
    assert report["future_fail_closed_unsafe_percent"] == 33.33
    assert report["raw_identifiers_emitted"] is False
    serialized = repr(report)
    for forbidden in (TOKEN, RAW_HWID, KEY, "hmac-sha256:"):
        assert forbidden not in serialized
