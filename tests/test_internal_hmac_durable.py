import io
import json
import os
import threading
import time

import pytest


class Handler:
    def __init__(self, db, *, method="GET", path="/internal/v1/status", body=b"", headers=None):
        self.command = method
        self.path = path
        self._body = body
        self.rfile = io.BytesIO(body)
        self.wfile = io.BytesIO()
        self.headers = {"Content-Length": str(len(body)), **(headers or {})}
        self.server = type("Server", (), {"db": db})()
        self.status = None
        self.response_headers = []

    def send_response(self, status):
        self.status = status

    def send_header(self, name, value):
        self.response_headers.append((name, value))

    def end_headers(self):
        pass

    def json(self):
        return json.loads(self.wfile.getvalue())


@pytest.fixture
def database_pair(tmp_path, monkeypatch):
    from src import database as database_module

    path = tmp_path / "db.sqlite3"
    monkeypatch.setattr(database_module, "DB_PATH", str(path))
    first = database_module.Database()
    second = database_module.Database()
    yield first, second
    first._conn.close()
    second._conn.close()


@pytest.fixture(autouse=True)
def internal_key(monkeypatch):
    from src import security

    monkeypatch.setattr(security, "INTERNAL_API_KEY", "test-key-at-least-32-bytes-long")
    monkeypatch.setattr(security, "INTERNAL_API_ALLOWED_SKEW_SECONDS", 300)
    monkeypatch.setattr(security, "INTERNAL_API_IDEMPOTENCY_TTL_SECONDS", 604800)
    monkeypatch.setattr(security, "INTERNAL_API_REQUIRE_V2_MUTATIONS", False)


def signed_handler(db, *, nonce, method="GET", path="/internal/v1/status", body=b"", version="1", key=""):
    from src.security import build_internal_signature

    timestamp = str(int(time.time()))
    signature = build_internal_signature(
        method, path, timestamp, nonce, body,
        version=version, idempotency_key=key,
    )
    headers = {
        "X-Filin-Timestamp": timestamp,
        "X-Filin-Nonce": nonce,
        "X-Filin-Signature": signature,
    }
    if version != "1":
        headers["X-Filin-Signature-Version"] = version
    if key:
        headers["X-Filin-Idempotency-Key"] = key
    return Handler(db, method=method, path=path, body=body, headers=headers)


def test_nonce_replay_is_blocked_by_another_database_connection_and_after_restart(database_pair):
    from src.security import require_internal_auth

    first, second = database_pair
    nonce = "nonce_restart_1234567890"
    assert require_internal_auth(signed_handler(first, nonce=nonce)) is True
    replay = signed_handler(second, nonce=nonce)
    assert require_internal_auth(replay) is False
    assert replay.status == 409
    assert replay.json()["error"] == "Replay detected"


def test_nonce_consume_is_atomic_between_workers(database_pair):
    from src.security import require_internal_auth

    first, second = database_pair
    barrier = threading.Barrier(2)
    results = []

    def attempt(db):
        handler = signed_handler(db, nonce="nonce_concurrent_1234567890")
        barrier.wait()
        results.append((require_internal_auth(handler), handler.status))

    threads = [threading.Thread(target=attempt, args=(db,)) for db in (first, second)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert sorted(results) == [(False, 409), (True, None)]


def test_invalid_signature_does_not_consume_nonce(database_pair):
    from src.security import require_internal_auth

    first, _ = database_pair
    handler = signed_handler(first, nonce="nonce_invalidsig_1234567890")
    handler.headers["X-Filin-Signature"] = "0" * 64
    assert require_internal_auth(handler) is False
    assert handler.status == 403
    assert require_internal_auth(
        signed_handler(first, nonce="nonce_invalidsig_1234567890")
    ) is True


def test_replay_store_outage_fails_closed(monkeypatch):
    from src.security import require_internal_auth

    class BrokenDB:
        def consume_internal_nonce(self, *args, **kwargs):
            raise OSError("database unavailable")

    handler = signed_handler(BrokenDB(), nonce="nonce_storeoutage_1234567890")
    assert require_internal_auth(handler) is False
    assert handler.status == 503
    assert handler.json() == {"error": "Internal replay store is unavailable"}


def test_v1_mutation_contract_remains_compatible(database_pair):
    from src.security import require_internal_auth

    first, _ = database_pair
    handler = signed_handler(
        first, nonce="nonce_legacy_mutation_12345", method="POST",
        path="/internal/v1/users/alice/renew", body=b'{"add_days":7}',
    )
    assert require_internal_auth(handler) is True
    assert not hasattr(handler, "_internal_idempotency")

    short_nonce = signed_handler(
        first, nonce="legacy-1", method="GET", path="/internal/v1/status",
    )
    assert require_internal_auth(short_nonce) is True


def test_v2_completed_retry_and_key_conflict_do_not_reexecute(database_pair):
    from src.http_utils import json_response
    from src.security import require_internal_auth

    first, second = database_pair
    path = "/internal/v1/users/alice/renew"
    body = b'{"add_days":7}'
    key = "operation_key_123456789012345"
    initial = signed_handler(
        first, nonce="nonce_v2_initial_123456789", method="POST", path=path,
        body=body, version="2", key=key,
    )
    assert require_internal_auth(initial) is True
    json_response(initial, 200, {"ok": True})
    assert initial.status == 200

    retry = signed_handler(
        second, nonce="nonce_v2_retry_12345678901", method="POST", path=path,
        body=body, version="2", key=key,
    )
    assert require_internal_auth(retry) is False
    assert retry.status == 409
    assert retry.json()["details"]["original_status"] == 200

    conflict = signed_handler(
        second, nonce="nonce_v2_conflict_123456789", method="POST", path=path,
        body=b'{"add_days":30}', version="2", key=key,
    )
    assert require_internal_auth(conflict) is False
    assert conflict.status == 409
    assert "conflicts" in conflict.json()["error"]


def test_v2_pending_survives_restart_and_blocks_blind_retry(database_pair):
    from src.security import require_internal_auth

    first, second = database_pair
    key = "operation_pending_1234567890123"
    kwargs = {
        "method": "DELETE",
        "path": "/internal/v1/users/alice",
        "version": "2",
        "key": key,
    }
    initial = signed_handler(first, nonce="nonce_pending_initial_12345", **kwargs)
    assert require_internal_auth(initial) is True
    # Simulate a process crash before the HTTP response/acknowledgement.
    retry = signed_handler(second, nonce="nonce_pending_retry_123456", **kwargs)
    assert require_internal_auth(retry) is False
    assert retry.status == 409
    assert "pending reconciliation" in retry.json()["error"]


def test_v2_mutation_requires_valid_key_and_v2_can_be_enforced(database_pair, monkeypatch):
    from src import security

    first, _ = database_pair
    missing = signed_handler(
        first, nonce="nonce_v2_nokey_1234567890", method="POST",
        path="/internal/v1/users", body=b"{}", version="2",
    )
    assert security.require_internal_auth(missing) is False
    assert missing.status == 400

    monkeypatch.setattr(security, "INTERNAL_API_REQUIRE_V2_MUTATIONS", True)
    legacy = signed_handler(
        first, nonce="nonce_v1_enforced_123456789", method="DELETE",
        path="/internal/v1/users/alice",
    )
    assert security.require_internal_auth(legacy) is False
    assert legacy.status == 428


def test_idempotency_ack_failure_returns_503_and_keeps_pending(monkeypatch):
    from src.http_utils import json_response

    class BrokenDB:
        def complete_internal_idempotency(self, *args, **kwargs):
            raise OSError("disk unavailable")

    handler = Handler(BrokenDB())
    handler._internal_idempotency = {
        "key": "operation_ack_failure_123456",
        "request_hash": "f" * 64,
        "ttl_seconds": 60,
    }
    json_response(handler, 200, {"secret": "must-not-be-acknowledged"})
    assert handler.status == 503
    assert handler.json() == {"error": "Internal idempotency acknowledgement failed"}


def test_nonce_and_idempotency_stores_keep_only_hash_references(database_pair):
    from src.http_utils import json_response
    from src.security import require_internal_auth

    first, _ = database_pair
    nonce = "nonce_storage_reference_123456"
    key = "operation_storage_reference_1234"
    body = b'{"add_days":7}'
    handler = signed_handler(
        first, nonce=nonce, method="POST",
        path="/internal/v1/users/alice/renew", body=body,
        version="2", key=key,
    )
    assert require_internal_auth(handler) is True
    json_response(handler, 200, {"ok": True})

    stored_nonce = first._conn.execute(
        "SELECT nonce_hash, request_hash FROM internal_hmac_nonces"
    ).fetchone()
    stored_operation = first._conn.execute(
        "SELECT key_hash, request_hash, response_hash FROM internal_idempotency"
    ).fetchone()
    serialized = " ".join([*stored_nonce, *stored_operation])
    assert nonce not in serialized
    assert key not in serialized
    assert body.decode() not in serialized
    assert all(len(value) == 64 for value in stored_nonce)
    assert all(len(value) == 64 for value in stored_operation)


def test_expired_nonces_are_pruned_and_capacity_fails_closed(database_pair):
    first, _ = database_pair
    assert first.consume_internal_nonce(
        "nonce_expired_storage_12345", "a" * 64, now=10, ttl_seconds=1,
        max_rows=1,
    ) is True
    assert first.consume_internal_nonce(
        "nonce_after_expiry_1234567", "b" * 64, now=12, ttl_seconds=1,
        max_rows=1,
    ) is True
    with pytest.raises(RuntimeError, match="capacity"):
        first.consume_internal_nonce(
            "nonce_capacity_fail_123456", "c" * 64, now=12, ttl_seconds=1,
            max_rows=1,
        )
