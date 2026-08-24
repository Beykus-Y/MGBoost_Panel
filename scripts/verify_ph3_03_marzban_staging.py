#!/usr/bin/env python3
"""Real Marzban 0.8.4 PH3-03 child create/reread staging gate.

The target must be an explicitly isolated loopback instance on a non-default
port. The script prints no raw UUID, subscription token, Shadowsocks password,
admin credential or full subscription line.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import logging
import os
import re
import socket
import threading
import time
from contextlib import contextmanager
from urllib.error import HTTPError
from urllib.parse import unquote, urlsplit

from src.broker_operations import BrokerOperations
from src.broker_server import BrokerApplication, build_broker_server
from src.child_contract import credential_verifier, source_contract, source_contract_hash
from src.database import Database
from src.marzban import MarzbanClient
from src.security import AdminSessionStore
from src.service_marzban import ServiceMarzbanClient
from scripts.build_ph3_03_staging_xray import (
    DECOY_VLESS_TAGS,
    EFFECTIVE_VLESS_TAGS,
)


EXPECTED_VERSION = "0.8.4"
EXPECTED_SOURCE_HASH = "b4798b928c481570bf1388cb06b73907a1afd8295e047d39cfee715e27ca0f98"
EXPECTED_ACCOUNT_PUBLIC_ID = "acct_435p4hjeoxeq3bzg4ifkdut4veower4r"
EXPECTED_CHILD_USERNAME = "mgc_sgg6v7t6he43yytsqmkdczzfpa"
EXPECTED_OPERATION_ID = "op_lw33pjhqhnvorrgh4p754bnc34"
AUTH_KEY = "ph3-staging-broker-key-" + os.urandom(32).hex()
CLIENT_ID = "mgboost-ph3-staging"


def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def require_isolated_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "::1"}:
        raise RuntimeError("staging target must be literal loopback HTTP")
    if parsed.port is None or parsed.port in {8000, 8001, 8002, 80, 443}:
        raise RuntimeError("refusing default or production-like port")
    if os.environ.get("PH3_ISOLATED_STAGING_ACK") != "isolated-marzban-0.8.4":
        raise RuntimeError("explicit isolated staging acknowledgement is required")
    return value.rstrip("/")


@contextmanager
def running_broker(operations):
    app = BrokerApplication(
        operations, shared_key=AUTH_KEY, client_id=CLIENT_ID,
    )
    server = build_broker_server("127.0.0.1", 0, app, max_workers=4)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield ServiceMarzbanClient(
            mode="broker",
            broker_url=f"http://127.0.0.1:{server.server_address[1]}",
            broker_key=AUTH_KEY,
            broker_client_id=CLIENT_ID,
            broker_timeout=3,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


class InstrumentedMarzban:
    def __init__(self, client):
        self.client = client
        self.child_create_calls = 0

    def __getattr__(self, name):
        return getattr(self.client, name)

    def create_user(self, payload, token):
        if payload.get("username", "").startswith("mgc_"):
            self.child_create_calls += 1
        return self.client.create_user(payload, token)


class RecordingOperations(BrokerOperations):
    def __init__(self, marzban):
        super().__init__(marzban)
        self.dispatched = []

    def dispatch(self, operation, data):
        self.dispatched.append(operation)
        return super().dispatch(operation, data)


def _decode_lines(body: bytes) -> list[str]:
    try:
        text = base64.b64decode(body, validate=True).decode("utf-8")
    except Exception:
        text = body.decode("utf-8")
    return sorted(line.strip() for line in text.splitlines() if line.strip())


def _line_tag(line: str) -> str:
    return unquote(line.rsplit("#", 1)[1]) if "#" in line else ""


def _credential_from_vless(line: str) -> str:
    if not line.startswith("vless://") or "@" not in line:
        raise AssertionError("unexpected non-VLESS effective config")
    return line[len("vless://"):].split("@", 1)[0]


def _normalize_vless(line: str) -> str:
    credential = _credential_from_vless(line)
    return line.replace(f"vless://{credential}@", "vless://<credential>@", 1)


def _subscription_token(user: dict) -> str:
    value = user.get("subscription_url") or ""
    token = value.rstrip("/").rsplit("/", 1)[-1]
    if not token or token == value:
        raise AssertionError("staging user has no subscription bearer")
    return token


def _unused_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _make_parent_and_slot(db: Database):
    _raw, session = AdminSessionStore().create(
        os.environ["PRIMARY_MGBOOST_ADMIN_LOGIN"], "staging-server-jwt"
    )
    capability = db.primary_admin_authority.authorize_session(session)
    plan = db.internal_entitlements.create_internal_plan(
        capability=capability,
        plan_code="INTERNAL_OWNER_CANARY",
        version=1,
        display_name="Internal owner canary",
        device_limit_mode="LIMITED",
        device_limit=10,
        wl_mode="UNLIMITED",
        terms={"schema": 1, "staging": True},
        now=100,
    )
    aliases = [
        {
            "legacy_username": name,
            "alias_role": "PRIMARY" if name == "beykusios" else "SECONDARY",
            "ownership_provenance": "OWNER_APPROVED",
            "legacy_status": "UNLIMITED",
            "legacy_expiry": None,
            "observed_device_count": count,
            "observed_hwid_count": count,
            "evidence": {"privacy": "masked", "staging": True},
        }
        for name, count in (("beykus", 3), ("beykusios", 4), ("BeykusLaptop", 2))
    ]
    account = db.internal_entitlements.create_reviewed_account(
        capability=capability,
        plan_version_id=plan["id"],
        legacy_username="beykusios",
        mapping_key="INTERNAL_OWNER_PRIMARY",
        decision_ref="DL-045-and-owner-canary-approval",
        legacy_aliases=aliases,
        ownership_evidence="PROVEN",
        telegram_id=905302972,
        legacy_status="UNLIMITED",
        legacy_expiry=None,
        device_evidence_count=9,
        hwid_evidence_count=9,
        internal_reason="Owner-approved isolated first PH3 child canary",
        migration_confidence="HIGH",
        evidence={"legacy_device_row": 56, "privacy_ref": "corr_701f5982b4"},
        idempotency_key="ph3-staging-approved-parent-v1",
        now=100,
    )
    if account["public_id"] != EXPECTED_ACCOUNT_PUBLIC_ID:
        raise AssertionError("server-derived parent public id drift")
    alias = db._conn.execute(
        "SELECT id FROM mgboost_legacy_account_aliases "
        "WHERE account_id=? AND legacy_username='beykusios'",
        (account["account_id"],),
    ).fetchone()
    slot = db.device_slots.claim(
        account["account_id"],
        "legacy-device-row:56:corr_701f5982b4",
        os.environ["DEVICE_SLOT_HMAC_KEY"],
        now=101,
    )
    if (slot["slot_number"], slot["generation"]) != (1, 1):
        raise AssertionError("unexpected staging slot generation")
    return account, alias["id"], slot


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    args = parser.parse_args()
    base_url = require_isolated_url(args.url)
    required_env = (
        "MARZBAN_ADMIN_USER", "MARZBAN_ADMIN_PASS", "DATA_DIR",
        "PRIMARY_MGBOOST_ADMIN_ACTOR_ID", "PRIMARY_MGBOOST_ADMIN_LOGIN",
        "DEVICE_SLOT_HMAC_KEY",
    )
    if any(not os.environ.get(name) for name in required_env):
        raise RuntimeError("staging environment is incomplete")

    marzban = MarzbanClient(base_url=base_url)
    admin_token = marzban.get_admin_token_from_env()
    system = marzban.get_system(admin_token)
    version = system.get("version") if isinstance(system, dict) else None
    if version != EXPECTED_VERSION:
        raise RuntimeError(f"expected Marzban {EXPECTED_VERSION}, got {version!r}")

    source_name = "beykusios"
    child_name = EXPECTED_CHILD_USERNAME
    created = []
    log_capture = io.StringIO()
    log_handler = logging.StreamHandler(log_capture)
    broker_logger = logging.getLogger("src.broker_server")
    broker_logger.addHandler(log_handler)
    broker_logger.setLevel(logging.INFO)
    results = {}
    raw_credentials = []
    db = Database()
    try:
        source_payload = {
            "username": source_name,
            "proxies": {
                "vless": {"flow": "xtls-rprx-vision"},
                "shadowsocks": {"method": "aes-128-gcm"},
            },
            "inbounds": {
                "vless": list(EFFECTIVE_VLESS_TAGS),
                "shadowsocks": [],
            },
            "expire": 0,
            "data_limit": None,
            "data_limit_reset_strategy": "no_reset",
            "status": "active",
            "note": "PH3-03 isolated legacy source template",
        }
        try:
            source = marzban.create_user(source_payload, admin_token)
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            if exc.code != 400 or "Shadowsocks is disabled" not in detail:
                raise
            print(json.dumps({
                "staging_contract": "FAIL",
                "marzban_version": version,
                "failure_stage": "production_equivalent_source_create",
                "reason": "disabled_shadowsocks_proxy_rejected_by_marzban_api",
                "effective_vless_count": len(EFFECTIVE_VLESS_TAGS),
                "effective_shadowsocks_count": 0,
                "child_created": False,
                "production_mutation": False,
            }, ensure_ascii=False, indent=2, sort_keys=True))
            raise SystemExit(2)
        created.append(source_name)
        source = marzban.get_user(source_name, admin_token)
        source_shape = source_contract(source)
        actual_source_hash = source_contract_hash(source)
        if actual_source_hash != EXPECTED_SOURCE_HASH:
            raise AssertionError(
                "staging source differs from approved production shape: "
                f"hash={actual_source_hash} shape={_canonical(source_shape)}"
            )
        if source_shape["inbounds"]["vless"] != sorted(EFFECTIVE_VLESS_TAGS):
            raise AssertionError("source did not retain exact effective VLESS set")
        global_inbounds = marzban.get_inbounds(admin_token)
        global_vless = {
            item["tag"] for item in global_inbounds.get("vless", [])
        }
        global_ss = {
            item["tag"] for item in global_inbounds.get("shadowsocks", [])
        }
        if not set(DECOY_VLESS_TAGS).issubset(global_vless):
            raise AssertionError("staging lacks VLESS decoy topology")
        if global_ss:
            raise AssertionError("staging Shadowsocks topology differs from production")
        if set(source_shape["inbounds"]["vless"]) == global_vless:
            raise AssertionError("source inbound set accidentally selected all globals")
        if source_shape["inbounds"]["shadowsocks"]:
            raise AssertionError("source unexpectedly selected a Shadowsocks inbound")
        results["source_exact_effective_set_not_global"] = True

        account, alias_id, slot = _make_parent_and_slot(db)
        prepared = db.child_provisioning.prepare_child_ensure(
            account_id=account["account_id"],
            slot_generation_id=slot["generation_id"],
            source_alias_id=alias_id,
            source_contract_hash=EXPECTED_SOURCE_HASH,
            expire=0,
            idempotency_key="ph3-staging-child-ensure-v1",
            now=102,
        )
        if (
            prepared["child_username"] != EXPECTED_CHILD_USERNAME
            or prepared["operation_id"] != EXPECTED_OPERATION_ID
        ):
            raise AssertionError("server-derived canary identity drift")
        payload = json.loads(prepared["payload_json"])
        if prepared["request_hash"] != _hash(_canonical(payload)):
            raise AssertionError("outbox payload digest mismatch")
        if db._conn.execute(
            "SELECT observed_state FROM mgboost_child_user_intents WHERE id=?",
            (prepared["child_intent_id"],),
        ).fetchone()[0] != "NOT_CREATED":
            raise AssertionError("intent was not durable before remote mutation")
        try:
            marzban.get_user(child_name, admin_token)
        except HTTPError as exc:
            if exc.code != 404:
                raise
        else:
            raise AssertionError("child existed before outbox dispatch")
        results["intent_before_remote"] = True
        results["stable_operation_and_digest"] = True

        instrumented = InstrumentedMarzban(marzban)
        operations = RecordingOperations(instrumented)
        with running_broker(operations) as service:
            claimed = db.child_provisioning.claim(
                prepared["operation_id"], worker_id="stage-worker-1",
                now=103, lease_seconds=5,
            )
            first = service.ensure_child_user(claimed["payload"])
            if first["outcome"] != "CREATED":
                raise AssertionError("first child ensure did not create")
            created.append(child_name)

            # No ACK: local state must still say not created while the remote
            # child exists. A same-payload retry must not create another child.
            immediate_retry = service.ensure_child_user(claimed["payload"])
            if immediate_retry["outcome"] != "EXISTING":
                raise AssertionError("same operation was not idempotent")
            if instrumented.child_create_calls != 1:
                raise AssertionError("idempotent retry created another child")
            if db._conn.execute(
                "SELECT uuid_verifier FROM mgboost_child_user_intents WHERE id=?",
                (prepared["child_intent_id"],),
            ).fetchone()[0] is not None:
                raise AssertionError("lost ACK incorrectly persisted credential")

            reclaimed = db.child_provisioning.claim(
                prepared["operation_id"], worker_id="stage-worker-2",
                now=109, lease_seconds=5,
            )
            reconciled = service.ensure_child_user(reclaimed["payload"])
            if reconciled["outcome"] != "EXISTING":
                raise AssertionError("lost-ACK retry did not reread existing child")
            raw_uuid = reconciled.pop("uuid")
            raw_ss_password = reconciled.pop("shadowsocks_password")
            raw_credentials.extend((raw_uuid, raw_ss_password))
            child = db.child_provisioning.acknowledge(
                prepared["operation_id"], worker_id="stage-worker-2",
                outcome="EXISTING", child_uuid=raw_uuid,
                child_shadowsocks_password=raw_ss_password,
                remote_result=reconciled, now=110,
            )
            if instrumented.child_create_calls != 1:
                raise AssertionError("lost-ACK reconciliation created duplicate")
            results["single_remote_child"] = True
            results["idempotent_existing"] = True
            results["remote_created_local_ack_failed"] = True

            credential_request = {
                "operation_id": prepared["operation_id"],
                "child_username": prepared["child_username"],
                "source_contract_hash": prepared["request_hash"] and EXPECTED_SOURCE_HASH,
                "expire": 0,
                "uuid_verifier": child["uuid_verifier"],
                "shadowsocks_verifier": child["shadowsocks_verifier"],
            }
            reread = service.get_child_credentials(credential_request)
            if reread["credentials"]["vless_uuid"] != raw_uuid:
                raise AssertionError("typed reread UUID mismatch")
            if reread["credentials"]["shadowsocks_password"] != raw_ss_password:
                raise AssertionError("typed reread Shadowsocks mismatch")
            if credential_verifier(raw_uuid) != child["uuid_verifier"]:
                raise AssertionError("persisted UUID verifier mismatch")
            if credential_verifier(raw_ss_password) != child["shadowsocks_verifier"]:
                raise AssertionError("persisted Shadowsocks verifier mismatch")
            results["typed_ephemeral_credential_reread"] = True

            source_current = marzban.get_user(source_name, admin_token)
            child_current = marzban.get_user(child_name, admin_token)
            if source_contract(source_current) != source_contract(child_current):
                raise AssertionError("child non-credential contract differs from source")
            if child_current.get("status") != source_current.get("status"):
                raise AssertionError("child status semantics changed")
            if int(child_current.get("expire") or 0) != int(source_current.get("expire") or 0):
                raise AssertionError("child expiry semantics changed")
            if child_current.get("data_limit") is not None or source_current.get("data_limit") is not None:
                raise AssertionError("child data-limit semantics changed")
            if raw_uuid == source_current["proxies"]["vless"]["id"]:
                raise AssertionError("child reused legacy VLESS UUID")
            if raw_ss_password == source_current["proxies"]["shadowsocks"]["password"]:
                raise AssertionError("child reused legacy Shadowsocks password")
            results["remote_reread_contract_equal"] = True
            results["credential_differences_only"] = True

            source_token = _subscription_token(source_current)
            child_token = _subscription_token(child_current)
            source_body, source_headers = marzban.get_sub(
                source_token, {"User-Agent": "MGBoost-PH3-Staging/1"}
            )
            child_body, child_headers = marzban.get_sub(
                child_token, {"User-Agent": "MGBoost-PH3-Staging/1"}
            )
            source_lines = _decode_lines(source_body)
            child_lines = _decode_lines(child_body)
            if len(source_lines) != 25 or len(child_lines) != 25:
                raise AssertionError("unexpected effective subscription line count")
            if {_line_tag(line) for line in source_lines} != set(EFFECTIVE_VLESS_TAGS):
                raise AssertionError("source subscription tags differ from exact effective set")
            if {_line_tag(line) for line in child_lines} != set(EFFECTIVE_VLESS_TAGS):
                raise AssertionError("child subscription tags differ from exact effective set")
            if sorted(map(_normalize_vless, source_lines)) != sorted(map(_normalize_vless, child_lines)):
                raise AssertionError("subscription config has non-credential differences")
            if {_credential_from_vless(line) for line in source_lines} != {
                source_current["proxies"]["vless"]["id"]
            }:
                raise AssertionError("source config credential mismatch")
            if {_credential_from_vless(line) for line in child_lines} != {raw_uuid}:
                raise AssertionError("child config credential mismatch")
            for header in ("profile-title", "profile-update-interval", "support-url"):
                if source_headers.get(header) != child_headers.get(header):
                    raise AssertionError(f"subscription header drift: {header}")
            results["subscription_functionally_equivalent"] = True

            # Drift must be rejected by typed reread and persisted as ERROR,
            # never interpreted as successful refresh/reconciliation.
            marzban.modify_user(child_name, {"expire": 123}, admin_token)
            try:
                service.get_child_credentials(credential_request)
            except HTTPError as exc:
                if exc.code != 400:
                    raise
            else:
                raise AssertionError("unexpected remote drift was accepted")
            db.child_provisioning.record_reconciliation_error(
                prepared["operation_id"], error_class="REMOTE_CONTRACT_DRIFT", now=111
            )
            state = db._conn.execute(
                "SELECT c.observed_state,o.state,o.last_error_class "
                "FROM mgboost_child_user_intents c JOIN mgboost_outbox o "
                "ON o.child_intent_id=c.id WHERE o.operation_id=?",
                (prepared["operation_id"],),
            ).fetchone()
            if tuple(state) != ("ERROR", "ERROR", "REMOTE_CONTRACT_DRIFT"):
                raise AssertionError("remote drift did not enter reconciliation error")
            results["unexpected_remote_state_error"] = True

        # A real authenticated broker backed by an unreachable Marzban endpoint
        # must return 503 for credential refresh; it cannot use persisted raw data.
        dead = MarzbanClient(base_url=f"http://127.0.0.1:{_unused_port()}")
        with running_broker(BrokerOperations(dead)) as outage_service:
            try:
                outage_service.get_child_credentials(credential_request)
            except HTTPError as exc:
                if exc.code != 503:
                    raise
            else:
                raise AssertionError("Marzban outage produced false credential success")
        results["credential_refresh_outage_503"] = True

        db_dump = "\n".join(db._conn.iterdump())
        mgboost_logs = log_capture.getvalue()
        for raw in raw_credentials:
            if raw in db_dump or raw in mgboost_logs:
                raise AssertionError("raw child credential persisted or logged")
        if "legacy.user.create" in operations.dispatched:
            raise AssertionError("child provisioning used generic legacy create")
        if operations.dispatched.count("child.user.ensure") != 3:
            raise AssertionError("unexpected child ensure dispatch count")
        results["no_raw_credentials_in_mgboost_db_or_logs"] = True
        results["generic_legacy_create_not_used"] = True

        output = {
            "staging_contract": "PASS",
            "marzban_version": version,
            "source_contract_hash": EXPECTED_SOURCE_HASH,
            "effective_vless_count": len(EFFECTIVE_VLESS_TAGS),
            "effective_vless_tags": list(EFFECTIVE_VLESS_TAGS),
            "effective_shadowsocks_count": 0,
            "global_vless_count": len(global_vless),
            "global_shadowsocks_count": len(global_ss),
            "server_derived_child_username": EXPECTED_CHILD_USERNAME,
            "operation_id": EXPECTED_OPERATION_ID,
            "child_create_calls": instrumented.child_create_calls,
            "uuid_masked": child["uuid_masked"],
            "shadowsocks_masked": child["shadowsocks_masked"],
            "results": results,
        }
        print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))
    finally:
        broker_logger.removeHandler(log_handler)
        for username in (child_name, source_name):
            if username not in created:
                continue
            try:
                marzban.delete_user(username, admin_token)
            except HTTPError as exc:
                if exc.code not in {404, 500}:
                    raise
                if exc.code == 500:
                    try:
                        marzban.get_user(username, admin_token)
                    except HTTPError as reread_exc:
                        if reread_exc.code != 404:
                            raise
                    else:
                        raise
        db._conn.close()


if __name__ == "__main__":
    main()
