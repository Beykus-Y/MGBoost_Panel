"""Typed allowlisted Marzban SUDO operations executed inside the broker."""

import base64
import hmac
import json
import threading
import time
import uuid
from urllib.error import HTTPError

from .broker_protocol import BROKER_OPERATIONS
from .child_contract import (
    build_child_payload,
    build_revoke_payload,
    build_state_sync_payload,
    credential_verifier,
    source_contract_hash,
    validate_child_ensure_request,
    validate_child_observe_request,
    validate_created_child,
    validate_child_credentials_request,
    validate_child_revoke_request,
    validate_child_state_sync_request,
    verify_revoked_child,
    verify_synced_child,
    reread_child_credentials,
)
from .legacy_contract import validate_renew_payload, validate_user_payload, validate_username
from .wl_enforcement_contract import (
    build_wl_target,
    normalize_observed_vless,
    validate_wl_set_request,
    verify_wl_converged,
)
from .shadowsocks_retirement import (
    build_functional_repair_payload,
    build_retirement_payload,
    functional_contract,
    retirement_snapshot,
    validate_retirement_request,
    verify_retirement,
)


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

    def _authoritative_child(self, child_username: str, uuid_verifier: str, token) -> tuple[dict, str]:
        """Authoritative reread + identity/UUID-verifier check shared by
        every PH3-08 state-touching and state-observing operation. Raises
        HTTPError(404) if the remote child is absent, ValueError on identity
        or UUID drift -- callers must never blindly patch a possibly-wrong
        remote user."""
        current = self.marzban.get_user(child_username, token)
        if current.get("username") != child_username:
            raise ValueError("remote child identity mismatch")
        try:
            current_uuid = str(uuid.UUID(current["proxies"]["vless"]["id"])).lower()
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise ValueError("remote child UUID is invalid") from exc
        if not hmac.compare_digest(credential_verifier(current_uuid), uuid_verifier):
            raise ValueError("remote child UUID verifier mismatch")
        return current, current_uuid

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

        if operation == "child.user.observe":
            request = validate_child_observe_request(data)
            child_username = request["child_username"]
            with self._lock_for(child_username):
                token = self._admin_token()
                source = self.marzban.get_user(request["source_username"], token)
                if source_contract_hash(source) != request["source_contract_hash"]:
                    return {
                        "presence": "MISMATCH",
                        "mismatch_code": "SOURCE_CONTRACT_MISMATCH",
                    }
                try:
                    child = self.marzban.get_user(child_username, token)
                except HTTPError as exc:
                    if exc.code == 404:
                        return {"presence": "ABSENT"}
                    raise
                try:
                    verified = validate_created_child(child, request, source)
                except (TypeError, ValueError, KeyError):
                    return {
                        "presence": "MISMATCH",
                        "mismatch_code": "REMOTE_CONTRACT_MISMATCH",
                    }
                return {"presence": "MATCH", **verified}

        if operation == "child.user.credentials.get":
            request = validate_child_credentials_request(data)
            with self._lock_for(request["child_username"]):
                child = self.marzban.get_user(
                    request["child_username"], self._admin_token()
                )
                return reread_child_credentials(child, request)

        if operation == "child.user.revoke":
            request = validate_child_revoke_request(data)
            child_username = request["child_username"]
            with self._lock_for(child_username):
                token = self._admin_token()
                try:
                    current = self.marzban.get_user(child_username, token)
                except HTTPError as exc:
                    if exc.code == 404:
                        return {"outcome": "ALREADY_ABSENT"}
                    raise
                if current.get("username") != child_username:
                    raise ValueError("remote child identity mismatch")
                try:
                    current_uuid = str(uuid.UUID(current["proxies"]["vless"]["id"])).lower()
                except (KeyError, TypeError, ValueError, AttributeError) as exc:
                    raise ValueError("remote child UUID is invalid") from exc
                verifier_matches = hmac.compare_digest(
                    credential_verifier(current_uuid), request["uuid_verifier"]
                )
                if current.get("status") == "disabled" and not verifier_matches:
                    # Idempotent: a prior REVOKE already rotated this child's
                    # credential away from what the caller has on file (e.g.
                    # lost local ACK); never re-mutate/re-rotate. A child that
                    # is merely `disabled` via PH3-08's reversible suspend
                    # still has its original UUID, so it falls through to a
                    # real revoke below instead of short-circuiting here --
                    # PH3-05 REVOKE must always be able to actually kill a
                    # credential, even one PH3-08 has only suspended.
                    return {"outcome": "ALREADY_REVOKED"}
                if not verifier_matches:
                    raise ValueError("remote child UUID verifier mismatch")
                new_uuid = str(uuid.uuid4())
                flow = (current.get("proxies", {}).get("vless", {}) or {}).get("flow") or ""
                self.marzban.modify_user(
                    child_username, build_revoke_payload(new_uuid, flow), token
                )
                after = self.marzban.get_user(child_username, token)
                verify_revoked_child(after, child_username)
                return {"outcome": "REVOKED"}

        if operation == "child.user.subscription.get":
            # PH2-01: fetch the child's own rendered Marzban subscription
            # body so the caller can run it through the existing
            # process_subscription() filter pipeline -- exactly the same
            # public-endpoint mechanism the legacy resolver already uses,
            # just pointed at a per-device child instead of the shared
            # legacy user. The subscription path segment (bearer-equivalent
            # for this one child, analogous to the existing legacy token) is
            # resolved and used here, inside the broker, and never returned
            # to the caller.
            request = validate_child_credentials_request(data)
            child_username = request["child_username"]
            with self._lock_for(child_username):
                token = self._admin_token()
                user = self.marzban.get_user(child_username, token)
                reread_child_credentials(user, request)  # identity/contract/verifier check
                sub_url = user.get("subscription_url") or ""
                sub_token = sub_url.rstrip("/").rsplit("/", 1)[-1]
                if not sub_token:
                    raise ValueError("remote child has no subscription path")
                body, headers = self.marzban.get_sub(sub_token)
                return {
                    "body_b64": base64.b64encode(body).decode("ascii"),
                    "headers": {str(k): str(v) for k, v in headers.items()},
                }

        if operation == "child.user.state.observe":
            # Read-only counterpart to child.user.state.sync: authoritative
            # reread + identity/UUID-verifier check, but never calls
            # modify_user. This is what the PH3-08 drift audit uses to
            # detect a post-ACK rollback (or any other remote drift) before
            # deciding whether a repair mutation is actually needed --
            # detection is separated from mutation so a periodic audit tick
            # never blindly writes to Marzban.
            request = validate_child_state_sync_request(data)
            child_username = request["child_username"]
            with self._lock_for(child_username):
                token = self._admin_token()
                try:
                    current, _ = self._authoritative_child(
                        child_username, request["uuid_verifier"], token,
                    )
                except HTTPError as exc:
                    if exc.code == 404:
                        return {"outcome": "REMOTE_MISSING"}
                    raise
                return {
                    "outcome": "OBSERVED",
                    "observed_status": current.get("status"),
                    "observed_expire": current.get("expire"),
                }

        if operation == "child.user.state.sync":
            request = validate_child_state_sync_request(data)
            child_username = request["child_username"]
            desired_status = request["desired_status"]
            desired_expire = request["desired_expire"]
            with self._lock_for(child_username):
                token = self._admin_token()
                try:
                    current, current_uuid = self._authoritative_child(
                        child_username, request["uuid_verifier"], token,
                    )
                except HTTPError as exc:
                    if exc.code == 404:
                        # PH3-08 never (re)creates a remote child -- that is
                        # PH3-03's job. A missing remote user here is handed
                        # back for reconciliation, not silently ignored.
                        return {"outcome": "REMOTE_MISSING"}
                    raise
                already_active_expiry = (
                    desired_status == "active"
                    and (desired_expire is None or int(current.get("expire") or 0) == int(desired_expire))
                )
                if current.get("status") == desired_status and (
                    desired_status == "disabled" or already_active_expiry
                ):
                    # `current` IS the authoritative already-converged
                    # snapshot -- surface it so the caller's audit verifier
                    # reflects real remote state, not just this outcome label.
                    return {
                        "outcome": "ALREADY_IN_SYNC",
                        "observed_status": current.get("status"),
                        "observed_expire": current.get("expire"),
                    }
                self.marzban.modify_user(
                    child_username,
                    build_state_sync_payload(desired_status, desired_expire),
                    token,
                )
                after = self.marzban.get_user(child_username, token)
                verify_synced_child(after, child_username, desired_status, desired_expire)
                after_uuid = str(uuid.UUID(after["proxies"]["vless"]["id"])).lower()
                if after_uuid != current_uuid:
                    # STOP condition per the PH3-08 contract: a plain
                    # status/expire mutation must never rotate the credential.
                    raise RuntimeError(
                        "unexpected credential rotation on a reversible state sync"
                    )
                return {
                    "outcome": "SYNCED",
                    "observed_status": after.get("status"),
                    "observed_expire": after.get("expire"),
                }

        if operation == "child.user.wl.set":
            # PH6-06: the only quota-enforcement mutation. Reread -> compute
            # the exact inbound-only target from authoritative live state and
            # the static PH0-05 allowlist -> minimal partial update of
            # `inbounds.vless` ONLY -> reread/verify (identity, UUID, status,
            # expire byte-stable; membership equal to the target). Proxies/
            # expire/data_limit/status are never part of the payload.
            request = validate_wl_set_request(data)
            child_username = request["child_username"]
            with self._lock_for(child_username):
                token = self._admin_token()
                try:
                    current = self.marzban.get_user(child_username, token)
                except HTTPError as exc:
                    if exc.code == 404:
                        # Never (re)create -- a missing remote child is a
                        # reconciliation matter, not an enforcement no-op.
                        return {"outcome": "REMOTE_MISSING"}
                    raise
                if current.get("username") != child_username:
                    raise ValueError("remote child identity mismatch")
                try:
                    current_uuid = str(uuid.UUID(current["proxies"]["vless"]["id"])).lower()
                except (KeyError, TypeError, ValueError, AttributeError) as exc:
                    raise ValueError("remote child UUID is invalid") from exc
                if not hmac.compare_digest(
                    credential_verifier(current_uuid), request["uuid_verifier"]
                ):
                    raise ValueError("remote child UUID verifier mismatch")
                observed = normalize_observed_vless(current)
                target = build_wl_target(
                    observed, request["direction"], request["baseline_wl_tags"],
                )
                if observed == target:
                    return {"outcome": "ALREADY_IN_SYNC"}
                self.marzban.modify_user(
                    child_username,
                    {"inbounds": {"vless": list(target)}},
                    token,
                )
                after = self.marzban.get_user(child_username, token)
                verify_wl_converged(
                    after,
                    child_username=child_username,
                    target_vless=target,
                    before_uuid=current_uuid,
                    before_status=current.get("status"),
                    before_expire=current.get("expire"),
                )
                return {"outcome": "SYNCED", "target_inbounds_count": len(target)}

        if operation == "maintenance.user.retire_shadowsocks":
            request = validate_retirement_request(data)
            username = request["username"]
            with self._lock_for(username):
                token = self._admin_token()
                topology = self.marzban.get_inbounds(token)
                if (topology or {}).get("shadowsocks"):
                    raise ValueError("Shadowsocks topology must be empty before retirement")
                before = self.marzban.get_user(username, token)
                snapshot = retirement_snapshot(before)
                if snapshot["state_digest"] != request["expected_state_digest"]:
                    raise ValueError("Marzban user changed after retirement inventory")
                if not snapshot["shadowsocks_metadata"]:
                    return {"outcome": "UNCHANGED", **snapshot}
                self.marzban.modify_user(
                    username, build_retirement_payload(before), token
                )
                after = self.marzban.get_user(username, token)
                try:
                    return verify_retirement(before, after)
                except ValueError:
                    repair = build_functional_repair_payload(before, after)
                    if repair:
                        self.marzban.modify_user(username, repair, token)
                        repaired = self.marzban.get_user(username, token)
                        if functional_contract(repaired) == functional_contract(before):
                            raise ValueError(
                                "unexpected retirement drift was repaired; rollout stopped"
                            )
                    raise

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
