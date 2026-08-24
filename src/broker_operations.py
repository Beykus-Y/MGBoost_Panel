"""Typed allowlisted Marzban SUDO operations executed inside the broker."""

import json
import threading
import time
from urllib.error import HTTPError

from .broker_protocol import BROKER_OPERATIONS
from .child_contract import (
    build_child_payload,
    source_contract_hash,
    validate_child_ensure_request,
    validate_created_child,
    validate_child_credentials_request,
    reread_child_credentials,
)
from .legacy_contract import validate_renew_payload, validate_user_payload, validate_username


def _exact_object(data, *, required=(), optional=()):
    if not isinstance(data, dict):
        raise ValueError("Expected JSON object")
    allowed = set(required) | set(optional)
    if set(data) - allowed:
        raise ValueError("Unexpected operation fields")
    missing = [key for key in required if key not in data]
    if missing:
        raise ValueError("Missing operation fields")
    return data


def _bounded_text(value, name: str, *, max_length: int = 128) -> str:
    if not isinstance(value, str) or len(value) > max_length:
        raise ValueError(f"{name} must be a string up to {max_length} characters")
    return value


class BrokerOperations:
    def __init__(self, marzban, *, clock=time.time):
        self.marzban = marzban
        self.clock = clock
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def _lock_for(self, username: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(username, threading.Lock())

    def _admin_token(self):
        return self.marzban.get_admin_token_from_env()

    def dispatch(self, operation: str, data: dict):
        if operation not in BROKER_OPERATIONS:
            raise ValueError("Unknown broker operation")

        if operation == "legacy.user.get":
            data = _exact_object(data, required=("username",))
            username = validate_username(data["username"])
            return self.marzban.get_user(username, self._admin_token())

        if operation == "legacy.user.usage":
            data = _exact_object(data, required=("username",), optional=("start", "end"))
            username = validate_username(data["username"])
            start = _bounded_text(data.get("start", ""), "start")
            end = _bounded_text(data.get("end", ""), "end")
            return self.marzban.get_user_usage(
                username, self._admin_token(), start=start, end=end
            )

        if operation == "legacy.users.list":
            data = _exact_object(data, optional=("limit", "offset"))
            try:
                limit = int(data.get("limit", 100))
                offset = int(data.get("offset", 0))
            except (TypeError, ValueError):
                raise ValueError("Invalid pagination")
            if not 1 <= limit <= 1000 or offset < 0:
                raise ValueError("Invalid pagination")
            return self.marzban.get_users(self._admin_token(), limit=limit, offset=offset)

        if operation == "legacy.nodes.list":
            _exact_object(data)
            return self.marzban.get_nodes(self._admin_token())

        if operation == "legacy.nodes.usage":
            data = _exact_object(data, optional=("start", "end"))
            start = _bounded_text(data.get("start", ""), "start")
            end = _bounded_text(data.get("end", ""), "end")
            return self.marzban.get_nodes_usage(self._admin_token(), start=start, end=end)

        if operation == "legacy.inbounds.list":
            _exact_object(data)
            return self.marzban.get_inbounds(self._admin_token())

        if operation == "legacy.user.create":
            data = _exact_object(data, required=("user",))
            payload = validate_user_payload(data["user"], creating=True, strict=True)
            return self.marzban.create_user(payload, self._admin_token())

        if operation == "legacy.user.renew":
            data = _exact_object(data, required=("username", "renewal"))
            username = validate_username(data["username"])
            renewal = validate_renew_payload(data["renewal"], strict=True)
            with self._lock_for(username):
                update = {}
                token = self._admin_token()
                if "add_days" in renewal:
                    user = self.marzban.get_user(username, token)
                    current_expire = int(user.get("expire") or 0)
                    update["expire"] = max(current_expire, int(self.clock())) + renewal["add_days"] * 86400
                if "expire" in renewal:
                    update["expire"] = renewal["expire"]
                if "data_limit" in renewal:
                    update["data_limit"] = renewal["data_limit"] or None
                if "status" in renewal:
                    update["status"] = renewal["status"]
                return self.marzban.modify_user(username, update, token)

        if operation == "legacy.user.set_expire":
            data = _exact_object(data, required=("username", "expire"))
            username = validate_username(data["username"])
            try:
                expire = int(data["expire"])
            except (TypeError, ValueError):
                raise ValueError("expire must be an integer")
            if expire < 0:
                raise ValueError("expire must be >= 0")
            with self._lock_for(username):
                return self.marzban.modify_user(
                    username, {"expire": expire}, self._admin_token()
                )

        if operation == "legacy.user.delete":
            data = _exact_object(data, required=("username",))
            username = validate_username(data["username"])
            with self._lock_for(username):
                return self.marzban.delete_user(username, self._admin_token())

        if operation == "child.user.ensure":
            request = validate_child_ensure_request(data)
            child_username = request["child_username"]
            with self._lock_for(child_username):
                token = self._admin_token()
                source = self.marzban.get_user(request["source_username"], token)
                if source_contract_hash(source) != request["source_contract_hash"]:
                    raise ValueError("source user contract changed")
                existing = None
                try:
                    existing = self.marzban.get_user(child_username, token)
                except HTTPError as exc:
                    if exc.code != 404:
                        raise
                if existing is not None:
                    verified = validate_created_child(existing, request, source)
                    return {"outcome": "EXISTING", **verified}
                self.marzban.create_user(build_child_payload(request, source), token)
                # The create response is not trusted as an acknowledgement:
                # always reread the authoritative remote state.
                created = self.marzban.get_user(child_username, token)
                verified = validate_created_child(created, request, source)
                return {"outcome": "CREATED", **verified}

        if operation == "child.user.credentials.get":
            request = validate_child_credentials_request(data)
            with self._lock_for(request["child_username"]):
                child = self.marzban.get_user(
                    request["child_username"], self._admin_token()
                )
                return reread_child_credentials(child, request)

        raise AssertionError(f"unhandled operation: {operation}")


def safe_upstream_error(exc: HTTPError) -> tuple[int, str]:
    message = "Marzban request failed"
    try:
        raw = exc.read().decode("utf-8")
        payload = json.loads(raw)
        candidate = payload.get("detail") or payload.get("error")
        if isinstance(candidate, str) and candidate:
            message = candidate[:300]
    except Exception:
        pass
    return exc.code, message
