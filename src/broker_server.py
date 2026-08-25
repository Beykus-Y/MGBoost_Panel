"""Loopback-only authenticated HTTP server for typed Marzban operations."""

import hashlib
import hmac
import json
import logging
import socketserver
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.error import HTTPError, URLError

from .broker_operations import safe_upstream_error
from .broker_protocol import (
    BROKER_CONTENT_TYPE,
    BROKER_MAX_BODY_BYTES,
    BROKER_OPERATIONS,
    ReplayGuard,
    authenticate_broker_request,
    validate_loopback_host,
    validate_shared_key,
)
from .http_utils import DEFAULT_SECURITY_HEADERS


logger = logging.getLogger(__name__)


class BoundedThreadingHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, *args, max_workers=16, **kwargs):
        self._worker_slots = __import__("threading").BoundedSemaphore(max_workers)
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        self._worker_slots.acquire()
        try:
            super().process_request(request, client_address)
        except Exception:
            self._worker_slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._worker_slots.release()


class BrokerApplication:
    def __init__(
        self, operations, *, shared_key: str, client_id: str = "mgboost-main",
        allowed_skew_seconds: int = 30, replay_guard=None, client_policies=None,
    ):
        self.operations = operations
        self.shared_key = validate_shared_key(shared_key)
        if not client_id or len(client_id) > 64:
            raise ValueError("invalid broker client id")
        self.client_id = client_id
        self.allowed_skew_seconds = max(5, min(int(allowed_skew_seconds), 300))
        self.replay_guard = replay_guard or ReplayGuard()
        self.client_policies = None
        if client_policies is not None:
            policies = {}
            for policy_client, policy in client_policies.items():
                if not policy_client or len(policy_client) > 64:
                    raise ValueError("invalid broker policy client id")
                key = validate_shared_key(policy["shared_key"])
                allowed = frozenset(policy["allowed_operations"])
                if not allowed.issubset(BROKER_OPERATIONS):
                    raise ValueError("broker policy contains unknown operation")
                policies[policy_client] = (key, allowed)
            if not policies:
                raise ValueError("broker client policies cannot be empty")
            self.client_policies = policies

    def authenticate(self, handler, body: bytes):
        client_id = (handler.headers.get("X-MGBoost-Client") or "").strip()
        if self.client_policies is not None:
            policy = self.client_policies.get(client_id)
            if policy is None:
                return False, 401, "Missing or invalid broker authentication"
            shared_key, _allowed = policy
            return authenticate_broker_request(
                headers=handler.headers, method=handler.command,
                path=handler.path, body=body, expected_client_id=client_id,
                shared_key=shared_key,
                allowed_skew_seconds=self.allowed_skew_seconds,
                replay_guard=self.replay_guard,
            )
        return authenticate_broker_request(
            headers=handler.headers,
            method=handler.command,
            path=handler.path,
            body=body,
            expected_client_id=self.client_id,
            shared_key=self.shared_key,
            allowed_skew_seconds=self.allowed_skew_seconds,
            replay_guard=self.replay_guard,
        )

    def authorize_operation(self, client_id: str, operation: str) -> bool:
        if self.client_policies is None:
            return True
        policy = self.client_policies.get(client_id)
        return bool(policy and operation in policy[1])

    def client_key(self, client_id: str) -> bytes:
        if self.client_policies is None:
            return self.shared_key
        return self.client_policies[client_id][0]

    def target_ref(self, data) -> str | None:
        if not isinstance(data, dict):
            return None
        username = data.get("username")
        if username is None:
            username = data.get("child_username")
        if username is None and isinstance(data.get("user"), dict):
            username = data["user"].get("username")
        if not isinstance(username, str) or not username:
            return None
        return hmac.new(self.shared_key, username.encode("utf-8"), hashlib.sha256).hexdigest()[:16]


class BrokerHandler(BaseHTTPRequestHandler):
    server_version = "MGBoostBroker"
    sys_version = ""

    def version_string(self):
        return self.server_version

    def log_message(self, _format, *_args):
        return

    def _json(self, status: int, payload):
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        for name, value in DEFAULT_SECURITY_HEADERS.items():
            if name.lower() != "cache-control":
                self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/healthz":
            self._json(200, {"status": "ok"})
        else:
            self._json(404, {"error": "Not found"})

    def do_POST(self):
        started = time.monotonic()
        content_type = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self._json(400, {"error": "Invalid Content-Length"})
            return
        if content_type != BROKER_CONTENT_TYPE or length < 0:
            self._json(415, {"error": "Expected application/json"})
            return
        if length > BROKER_MAX_BODY_BYTES:
            self._json(413, {"error": "Request too large"})
            return
        body = self.rfile.read(length)
        ok, status, message = self.server.app.authenticate(self, body)
        if not ok:
            self._json(status, {"error": message})
            return
        prefix = "/v1/operations/"
        operation = self.path[len(prefix):] if self.path.startswith(prefix) else ""
        if operation not in BROKER_OPERATIONS or "/" in operation:
            self._json(404, {"error": "Unknown broker operation"})
            return
        actor = (self.headers.get("X-MGBoost-Client") or "").strip()
        if not self.server.app.authorize_operation(actor, operation):
            self._json(403, {"error": "Broker operation is not permitted"})
            return
        try:
            data = json.loads(body or b"{}")
            result = self.server.app.operations.dispatch(operation, data)
            self._json(200, {"result": result})
            outcome = "ok"
        except (ValueError, TypeError):
            self._json(400, {"error": "Invalid operation payload"})
            outcome = "invalid"
        except HTTPError as exc:
            upstream_status, upstream_message = safe_upstream_error(exc)
            self._json(upstream_status, {"error": upstream_message})
            outcome = f"upstream_{upstream_status}"
        except (URLError, TimeoutError, ConnectionError):
            self._json(503, {"error": "Marzban unavailable"})
            outcome = "unavailable"
        except Exception:
            logger.exception("broker operation failed")
            self._json(502, {"error": "Marzban request failed"})
            outcome = "error"
        target = None
        try:
            target = self.server.app.target_ref(data)
        except Exception:
            pass
        logger.info(
            "broker operation=%s actor=%s target_ref=%s outcome=%s duration_ms=%d",
            operation,
            actor,
            target or "-",
            outcome,
            int((time.monotonic() - started) * 1000),
        )


def build_broker_server(host: str, port: int, app: BrokerApplication, *, max_workers: int = 16):
    validate_loopback_host(host)
    # Port 0 is useful for isolated tests (the kernel selects an ephemeral
    # loopback port); production broker_main always supplies an explicit port.
    if not 0 <= int(port) <= 65535:
        raise ValueError("invalid broker port")
    server = BoundedThreadingHTTPServer((host, int(port)), BrokerHandler, max_workers=max_workers)
    server.app = app
    return server
