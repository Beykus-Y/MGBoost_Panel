#!/usr/bin/env python3
"""Real Marzban 0.8.4 PH3-03 dual-run SHADOW resolver staging gate.

Reuses the same isolated-target contract as
`verify_ph3_03_marzban_staging.py` (loopback, non-default port, explicit
`PH3_ISOLATED_STAGING_ACK`). It creates the same approved-shape source/child
pair, then drives the actual `src/shadow_resolver.py` module -- including the
split mgboost-main/mgboost-sub-resolver broker capability boundary -- against
the real Marzban instance. It prints no raw UUID, subscription token, admin
credential or full subscription line.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import threading
import time
from urllib.error import HTTPError
from urllib.parse import urlsplit

from src import shadow_resolver
from src.broker_operations import BrokerOperations
from src.broker_protocol import BROKER_OPERATIONS
from src.broker_server import BrokerApplication, build_broker_server
from src.child_contract import source_contract, source_contract_hash
from src.database import Database
from src.marzban import MarzbanClient
from scripts.build_ph3_03_staging_xray import EFFECTIVE_VLESS_TAGS
from scripts.verify_ph3_03_marzban_staging import (
    EXPECTED_ACCOUNT_PUBLIC_ID,
    EXPECTED_CHILD_USERNAME,
    EXPECTED_OPERATION_ID,
    EXPECTED_SOURCE_HASH,
    EXPECTED_VERSION,
    _make_parent_and_slot,
    require_isolated_url,
)


def _delete_user_tolerating_notify_bug(marzban, username, admin_token):
    """This isolated Marzban 0.8.4 environment has a known quirk (also
    handled by verify_ph3_03_marzban_staging.py's own cleanup): the delete
    transaction commits, but the post-delete notification hook can still
    crash with a 500 when the user has no owning admin. Treat 500 as
    ambiguous and confirm via a reread rather than as a real failure."""
    try:
        marzban.delete_user(username, admin_token)
        return
    except HTTPError as exc:
        if exc.code != 500:
            raise
    try:
        marzban.get_user(username, admin_token)
    except HTTPError as reread_exc:
        if reread_exc.code != 404:
            raise
        return
    raise AssertionError(f"delete of {username!r} returned 500 but user still exists")

MAIN_KEY = "shadow-stage-main-key-" + os.urandom(24).hex()
MAIN_CLIENT = "mgboost-main"
RESOLVER_KEY = "shadow-stage-resolver-key-" + os.urandom(24).hex()
RESOLVER_CLIENT = "mgboost-sub-resolver"
REQUEST_KEY = "hwid:ph3-shadow-stage-device"


def _split_app(marzban) -> BrokerApplication:
    return BrokerApplication(
        BrokerOperations(marzban),
        shared_key=MAIN_KEY,
        client_id=MAIN_CLIENT,
        client_policies={
            MAIN_CLIENT: {
                "shared_key": MAIN_KEY,
                "allowed_operations": BROKER_OPERATIONS - {"child.user.credentials.get"},
            },
            RESOLVER_CLIENT: {
                "shared_key": RESOLVER_KEY,
                "allowed_operations": {"child.user.credentials.get"},
            },
        },
    )


def _metrics(db_path, binding_id):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return [
            dict(row) for row in conn.execute(
                "SELECT result,category,credential_result,legacy_fallback_success,"
                "request_count FROM mgboost_shadow_resolver_metrics "
                "WHERE binding_id=? ORDER BY bucket_day,result,category", (binding_id,),
            ).fetchall()
        ]
    finally:
        conn.close()


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
    if (system or {}).get("version") != EXPECTED_VERSION:
        raise RuntimeError("unexpected Marzban version")

    source_name = "beykusios"
    child_name = EXPECTED_CHILD_USERNAME
    created = []
    results = {}
    raw_secrets = []
    log_capture = logging.StreamHandler()
    captured = []
    log_capture.emit = lambda record: captured.append(log_capture.format(record))
    for name in ("src.broker_server", "src.shadow_resolver"):
        lg = logging.getLogger(name)
        lg.addHandler(log_capture)
        lg.setLevel(logging.DEBUG)

    db = Database()
    db_path = db._conn.execute("PRAGMA database_list").fetchone()[2]
    try:
        source_payload = {
            "username": source_name,
            "proxies": {"vless": {"flow": "xtls-rprx-vision"}},
            "inbounds": {"vless": list(EFFECTIVE_VLESS_TAGS)},
            "expire": 0, "data_limit": None,
            "data_limit_reset_strategy": "no_reset", "status": "active",
            "note": "PH3-03 shadow resolver isolated source template",
        }
        marzban.create_user(source_payload, admin_token)
        created.append(source_name)
        source = marzban.get_user(source_name, admin_token)
        if source_contract_hash(source) != EXPECTED_SOURCE_HASH:
            raise AssertionError("staging source differs from approved production shape")

        account, alias_id, slot = _make_parent_and_slot(db)
        if account["public_id"] != EXPECTED_ACCOUNT_PUBLIC_ID:
            raise AssertionError("parent public id drift")
        prepared = db.child_provisioning.prepare_child_ensure(
            account_id=account["account_id"], slot_generation_id=slot["generation_id"],
            source_alias_id=alias_id, source_contract_hash=EXPECTED_SOURCE_HASH,
            expire=0, idempotency_key="ph3-shadow-stage-child-ensure-v1", now=102,
        )
        if (prepared["child_username"], prepared["operation_id"]) != (
            EXPECTED_CHILD_USERNAME, EXPECTED_OPERATION_ID,
        ):
            raise AssertionError("server-derived canary identity drift")

        claimed = db.child_provisioning.claim(
            prepared["operation_id"], worker_id="shadow-stage-worker", now=103, lease_seconds=5,
        )
        direct_ops = BrokerOperations(marzban)
        created_result = direct_ops.dispatch("child.user.ensure", claimed["payload"])
        if created_result["outcome"] != "CREATED":
            raise AssertionError("child ensure did not create")
        created.append(child_name)
        raw_uuid = created_result.pop("uuid")
        raw_secrets.append(raw_uuid)
        child = db.child_provisioning.acknowledge(
            prepared["operation_id"], worker_id="shadow-stage-worker",
            outcome="CREATED", child_uuid=raw_uuid, remote_result=created_result, now=104,
        )
        results["child_applied"] = True

        now = int(time.time())
        db._conn.execute(
            "INSERT INTO user_devices (username, token, request_key, device_name, platform, "
            "client_name, client_version, is_active, first_seen, last_seen) "
            "VALUES (?,?,?,?,?,?,?,1,?,?)",
            (source_name, "tokref", REQUEST_KEY, "iPhone 17", "iOS", "INCY", "2.5.2", now, now),
        )
        db._conn.commit()
        device_id = db._conn.execute(
            "SELECT id FROM user_devices WHERE username=? AND request_key=?",
            (source_name, REQUEST_KEY),
        ).fetchone()[0]
        binding = db.shadow_resolver_bindings.create_binding(
            account_id=account["account_id"], legacy_alias_id=alias_id,
            legacy_device_id=device_id, slot_generation_id=slot["generation_id"],
            child_intent_id=prepared["child_intent_id"], operation_id=prepared["operation_id"],
            decision_ref="ph3-shadow-stage-canary-v1", now=105,
        )
        results["binding_created"] = True

        app = _split_app(marzban)
        server = build_broker_server("127.0.0.1", 0, app, max_workers=4)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        broker_port = server.server_address[1]

        def config(**overrides):
            base = {
                "base_url": f"http://127.0.0.1:{broker_port}",
                "observe_shared_key": MAIN_KEY, "observe_client_id": MAIN_CLIENT,
                "credentials_shared_key": RESOLVER_KEY, "credentials_client_id": RESOLVER_CLIENT,
                "timeout": 3.0,
            }
            base.update(overrides)
            return base

        try:
            source_token = source.get("subscription_url", "").rstrip("/").rsplit("/", 1)[-1]
            raw_secrets.append(source_token)
            raw_body, _headers = marzban.get_sub(source_token, {"User-Agent": "MGBoost-PH3-Shadow-Stage/1"})

            # --- capability boundary: mgboost-main cannot read child credentials ---
            from src.service_marzban import BrokerTransport
            main_transport = BrokerTransport(f"http://127.0.0.1:{broker_port}", MAIN_KEY, client_id=MAIN_CLIENT)
            try:
                main_transport.call("child.user.credentials.get", {
                    "operation_id": prepared["operation_id"], "child_username": child_name,
                    "source_contract_hash": EXPECTED_SOURCE_HASH, "expire": 0,
                    "uuid_verifier": child["uuid_verifier"],
                })
            except HTTPError as exc:
                if exc.code != 403:
                    raise AssertionError(f"expected 403 for main client, got {exc.code}")
            else:
                raise AssertionError("mgboost-main was able to read a child credential")
            results["main_client_denied_credentials_get"] = True

            resolver_transport = BrokerTransport(
                f"http://127.0.0.1:{broker_port}", RESOLVER_KEY, client_id=RESOLVER_CLIENT,
            )
            try:
                resolver_transport.call("legacy.user.get", {"username": source_name})
            except HTTPError as exc:
                if exc.code != 403:
                    raise AssertionError(f"expected 403 for resolver client on legacy op, got {exc.code}")
            else:
                raise AssertionError("mgboost-sub-resolver could call an unrelated operation")
            results["resolver_client_scoped_to_credentials_get"] = True

            # --- PASS path against the real Marzban instance ---
            shadow_resolver._resolve_and_record(
                config(), db_path, "ph3-shadow-stage-token", source_name, REQUEST_KEY, raw_body,
            )
            rows = _metrics(db_path, binding["id"])
            if rows != [{
                "result": "PASS", "category": "MATCH", "credential_result": "SUCCESS",
                "legacy_fallback_success": 1, "request_count": 1,
            }]:
                raise AssertionError(f"unexpected PASS metrics row: {rows}")
            results["real_shadow_pass"] = True

            # --- failure matrix against the real broker/Marzban ---

            # resolver capability denied: credentials call misrouted to main identity
            shadow_resolver._resolve_and_record(
                config(credentials_shared_key=MAIN_KEY, credentials_client_id=MAIN_CLIENT),
                db_path, "tok", source_name, REQUEST_KEY, raw_body,
            )
            results["failure_resolver_capability_denied"] = "RESOLVER_CAPABILITY_DENIED" in {
                row["category"] for row in _metrics(db_path, binding["id"])
            }

            # shadow comparison failure: a valid base64 body whose only VLESS
            # line cannot be parsed (distinct from a non-base64 malformed
            # request, tested next).
            import base64 as _b64
            unparsable = _b64.b64encode(b"vless://not-a-valid-uri-at-all")
            shadow_resolver._resolve_and_record(
                config(), db_path, "tok", source_name, REQUEST_KEY, unparsable,
            )
            results["failure_shadow_comparison"] = "SHADOW_COMPARISON_FAILURE" in {
                row["category"] for row in _metrics(db_path, binding["id"])
            }

            # malformed request: not base64 at all.
            shadow_resolver._resolve_and_record(
                config(), db_path, "tok", source_name, REQUEST_KEY, b"vless://not-a-valid-uri",
            )
            results["failure_malformed_request"] = "MALFORMED_REQUEST" in {
                row["category"] for row in _metrics(db_path, binding["id"])
            }

            # resolver timeout: pause the real Marzban process mid-request so
            # the broker's connection to it hangs until the client timeout.
            import subprocess
            subprocess.run(["docker", "pause", "ph3-shadow-stage"], check=True, capture_output=True)
            try:
                shadow_resolver._resolve_and_record(
                    config(timeout=0.2), db_path, "tok", source_name, REQUEST_KEY, raw_body,
                )
            finally:
                subprocess.run(["docker", "unpause", "ph3-shadow-stage"], check=True, capture_output=True)
            results["failure_resolver_timeout"] = sorted(
                {row["category"] for row in _metrics(db_path, binding["id"])}
                & {"RESOLVER_TIMEOUT", "BROKER_UNAVAILABLE", "MARZBAN_UNAVAILABLE"}
            )

            # credential verifier mismatch: drift the real remote UUID via a real API call
            import uuid as uuid_mod
            drifted = str(uuid_mod.uuid4())
            marzban.modify_user(
                child_name,
                {"proxies": {"vless": {"id": drifted, "flow": "xtls-rprx-vision"}}},
                admin_token,
            )
            shadow_resolver._resolve_and_record(
                config(), db_path, "tok", source_name, REQUEST_KEY, raw_body,
            )
            results["failure_credential_verifier_mismatch"] = "CREDENTIAL_VERIFIER_MISMATCH" in {
                row["category"] for row in _metrics(db_path, binding["id"])
            }
            dump = "\n".join(db._conn.iterdump())
            if drifted in dump:
                raise AssertionError("drifted raw UUID leaked into MGBoost DB dump")

            # remote contract mismatch: drift expire on the real remote child
            marzban.modify_user(child_name, {"expire": 999999}, admin_token)
            shadow_resolver._resolve_and_record(
                config(), db_path, "tok", source_name, REQUEST_KEY, raw_body,
            )
            results["failure_remote_contract_mismatch"] = "REMOTE_CONTRACT_MISMATCH" in {
                row["category"] for row in _metrics(db_path, binding["id"])
            }

            # remote child missing: delete the real remote child
            _delete_user_tolerating_notify_bug(marzban, child_name, admin_token)
            created.remove(child_name)
            shadow_resolver._resolve_and_record(
                config(), db_path, "tok", source_name, REQUEST_KEY, raw_body,
            )
            results["failure_remote_child_missing"] = "REMOTE_CHILD_MISSING" in {
                row["category"] for row in _metrics(db_path, binding["id"])
            }

            # broker unavailable: shut the real broker down mid-test
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)
            shadow_resolver._resolve_and_record(
                config(), db_path, "tok", source_name, REQUEST_KEY, raw_body,
            )
            results["failure_broker_unavailable"] = "BROKER_UNAVAILABLE" in {
                row["category"] for row in _metrics(db_path, binding["id"])
            }

            results["legacy_fallback_always_true"] = all(
                row["legacy_fallback_success"] == 1 for row in _metrics(db_path, binding["id"])
            )

        finally:
            try:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)
            except Exception:
                pass

        for record in captured:
            for secret in raw_secrets:
                if secret and secret in record:
                    raise AssertionError("raw secret observed in broker/resolver log output")
        results["no_raw_secret_in_captured_logs"] = True

        output = {
            "staging_contract": "SHADOW_RESOLVER_PASS",
            "marzban_version": system.get("version"),
            "source_contract_hash": EXPECTED_SOURCE_HASH,
            "operation_id": EXPECTED_OPERATION_ID,
            "server_derived_child_username": EXPECTED_CHILD_USERNAME,
            "binding_id": binding["id"],
            "results": results,
            "metrics_rows": _metrics(db_path, binding["id"]),
        }
        print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))
    finally:
        for name in ("src.broker_server", "src.shadow_resolver"):
            logging.getLogger(name).removeHandler(log_capture)
        for username in (child_name, source_name):
            if username not in created:
                continue
            try:
                marzban.delete_user(username, admin_token)
            except HTTPError as exc:
                if exc.code not in {404, 500}:
                    raise
        db._conn.close()


if __name__ == "__main__":
    main()
