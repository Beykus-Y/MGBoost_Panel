"""Service-operation facade: typed localhost broker or explicit rollback direct mode."""

import json
import os
import secrets
import time
from urllib.error import URLError
from urllib.request import Request, urlopen

from .broker_protocol import (
    BROKER_CLIENT_HEADER,
    BROKER_NONCE_HEADER,
    BROKER_SIGNATURE_HEADER,
    BROKER_TIMESTAMP_HEADER,
    build_broker_signature,
    validate_loopback_url,
    validate_shared_key,
)
from .legacy_contract import validate_renew_payload, validate_username
from .child_contract import (
    validate_child_credentials_request,
    validate_child_ensure_request,
)
from .marzban import MarzbanClient
from .shadowsocks_retirement import validate_retirement_request


class BrokerTransport:
    def __init__(
        self, base_url: str, shared_key: str, *, client_id: str = "mgboost-main",
        timeout: float = 10.0,
    ):
        self.base_url = validate_loopback_url(base_url)
        self.shared_key = validate_shared_key(shared_key)
        if not client_id or len(client_id) > 64:
            raise ValueError("invalid broker client id")
        self.client_id = client_id
        self.timeout = max(0.1, min(float(timeout), 30.0))

    def call(self, operation: str, payload: dict):
        path = f"/v1/operations/{operation}"
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        timestamp = str(int(time.time()))
        nonce = secrets.token_urlsafe(24)
        signature = build_broker_signature(
            self.shared_key, "POST", path, timestamp, nonce, self.client_id, body
        )
        request = Request(
            f"{self.base_url}{path}",
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                BROKER_CLIENT_HEADER: self.client_id,
                BROKER_TIMESTAMP_HEADER: timestamp,
                BROKER_NONCE_HEADER: nonce,
                BROKER_SIGNATURE_HEADER: signature,
            },
        )
        response = urlopen(request, timeout=self.timeout)
        raw = response.read()
        try:
            data = json.loads(raw or b"{}")
        except json.JSONDecodeError as exc:
            raise URLError("Invalid broker response") from exc
        if not isinstance(data, dict) or "result" not in data:
            raise URLError("Invalid broker response")
        return data["result"]


class ServiceMarzbanClient:
    """Compatibility facade for existing callers.

    In broker mode the `admin_token` positional parameter is a non-secret
    sentinel retained only to avoid a broad Phase 1 caller rewrite.  The real
    SUDO credential exists exclusively in the broker process.  Direct mode is
    an explicit rollback/staging mode and must not be used for the secure
    production cutover.
    """

    BROKER_SENTINEL = "typed-local-broker"

    def __init__(
        self, *, mode: str | None = None, broker_url: str | None = None,
        broker_key: str | None = None, broker_client_id: str | None = None,
        broker_timeout: float | None = None, direct_client=None,
    ):
        self.mode = (mode or os.getenv("MARZBAN_SERVICE_MODE", "broker")).strip().lower()
        if self.mode not in {"broker", "direct"}:
            raise ValueError("MARZBAN_SERVICE_MODE must be broker or direct")
        self.direct = direct_client or MarzbanClient()
        self._transport = None
        self._broker_settings = {
            "base_url": broker_url or os.getenv("MARZBAN_BROKER_URL", "http://127.0.0.1:8002"),
            "shared_key": broker_key if broker_key is not None else os.getenv("MARZBAN_BROKER_AUTH_KEY", ""),
            "client_id": broker_client_id or os.getenv("MARZBAN_BROKER_CLIENT_ID", "mgboost-main"),
            "timeout": broker_timeout if broker_timeout is not None else float(os.getenv("MARZBAN_BROKER_TIMEOUT_SECONDS", "10")),
        }

    def _broker(self) -> BrokerTransport:
        if self._transport is None:
            self._transport = BrokerTransport(**self._broker_settings)
        return self._transport

    def assert_credential_boundary(self):
        if self.mode == "broker" and (
            os.environ.get("MARZBAN_ADMIN_USER") or os.environ.get("MARZBAN_ADMIN_PASS")
        ):
            raise RuntimeError(
                "Marzban SUDO credentials must not be present in the MGBoost environment in broker mode"
            )

    def get_admin_token_from_env(self):
        if self.mode == "direct":
            return self.direct.get_admin_token_from_env()
        self._broker()  # validate loopback URL/key before claiming availability
        return self.BROKER_SENTINEL

    # Public subscription operations intentionally bypass the broker.
    def get_sub(self, token, extra_headers=None):
        return self.direct.get_sub(token, extra_headers)

    def get_username_for_token(self, token):
        return self.direct.get_username_for_token(token)

    def get_user(self, username, admin_token=None):
        if self.mode == "direct":
            return self.direct.get_user(username, admin_token)
        return self._broker().call("legacy.user.get", {"username": username})

    def get_user_usage(self, username, admin_token=None, start="", end=""):
        if self.mode == "direct":
            return self.direct.get_user_usage(username, admin_token, start=start, end=end)
        return self._broker().call(
            "legacy.user.usage", {"username": username, "start": start, "end": end}
        )

    def get_users(self, admin_token=None, limit=100, offset=0):
        if self.mode == "direct":
            return self.direct.get_users(admin_token, limit=limit, offset=offset)
        return self._broker().call("legacy.users.list", {"limit": limit, "offset": offset})

    def get_nodes(self, admin_token=None):
        if self.mode == "direct":
            return self.direct.get_nodes(admin_token)
        return self._broker().call("legacy.nodes.list", {})

    def get_nodes_usage(self, admin_token=None, start="", end=""):
        if self.mode == "direct":
            return self.direct.get_nodes_usage(admin_token, start=start, end=end)
        return self._broker().call("legacy.nodes.usage", {"start": start, "end": end})

    def get_inbounds(self, admin_token=None):
        if self.mode == "direct":
            return self.direct.get_inbounds(admin_token)
        return self._broker().call("legacy.inbounds.list", {})

    def create_user(self, payload, admin_token=None):
        if self.mode == "direct":
            return self.direct.create_user(payload, admin_token)
        return self._broker().call("legacy.user.create", {"user": payload})

    def renew_user(self, username, renewal, admin_token=None):
        if self.mode == "direct":
            renewal = validate_renew_payload(renewal)
            username = validate_username(username)
            update = {}
            if "add_days" in renewal:
                user = self.direct.get_user(username, admin_token)
                current_expire = int(user.get("expire") or 0)
                update["expire"] = max(current_expire, int(time.time())) + renewal["add_days"] * 86400
            if "expire" in renewal:
                update["expire"] = renewal["expire"]
            if "data_limit" in renewal:
                update["data_limit"] = renewal["data_limit"] or None
            if "status" in renewal:
                update["status"] = renewal["status"]
            return self.direct.modify_user(username, update, admin_token)
        return self._broker().call(
            "legacy.user.renew", {"username": username, "renewal": renewal}
        )

    def modify_user(self, username, payload, admin_token=None):
        """Existing Stars compatibility: service writes may only set expire."""
        if self.mode == "direct":
            return self.direct.modify_user(username, payload, admin_token)
        if not isinstance(payload, dict) or set(payload) != {"expire"}:
            raise ValueError("broker service modify_user only permits expire")
        return self._broker().call(
            "legacy.user.set_expire", {"username": username, "expire": payload["expire"]}
        )

    def delete_user(self, username, admin_token=None):
        if self.mode == "direct":
            return self.direct.delete_user(username, admin_token)
        return self._broker().call("legacy.user.delete", {"username": username})

    def ensure_child_user(self, request):
        """Execute only the typed idempotent child ensure contract.

        Direct mode exists solely for isolated comparison/rollback testing;
        secure production broker mode keeps SUDO out of this process.
        """
        normalized = validate_child_ensure_request(request)
        if self.mode == "direct":
            from .broker_operations import BrokerOperations
            return BrokerOperations(self.direct).dispatch("child.user.ensure", normalized)
        return self._broker().call("child.user.ensure", normalized)

    def get_child_credentials(self, request):
        """Typed ephemeral credential reread; callers must never persist/log result."""
        normalized = validate_child_credentials_request(request)
        if self.mode == "direct":
            from .broker_operations import BrokerOperations
            return BrokerOperations(self.direct).dispatch(
                "child.user.credentials.get", normalized
            )
        return self._broker().call("child.user.credentials.get", normalized)

    def retire_shadowsocks_metadata(self, request):
        """Remove only retired SS metadata through the typed broker boundary."""
        normalized = validate_retirement_request(request)
        if self.mode == "direct":
            from .broker_operations import BrokerOperations
            return BrokerOperations(self.direct).dispatch(
                "maintenance.user.retire_shadowsocks", normalized
            )
        return self._broker().call(
            "maintenance.user.retire_shadowsocks", normalized
        )
