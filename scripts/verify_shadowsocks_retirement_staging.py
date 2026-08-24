#!/usr/bin/env python3
"""Real Marzban 0.8.4 gate for typed retirement of stale SS metadata."""

from __future__ import annotations

import argparse
import io
import json
import logging
import os
import threading
from contextlib import contextmanager
from urllib.error import HTTPError

from scripts.build_ph3_03_staging_xray import (
    EFFECTIVE_VLESS_TAGS,
    RETIRED_SHADOWSOCKS_BOOTSTRAP_TAG,
)
from scripts.retire_shadowsocks_metadata import _canonical_config, _token
from scripts.verify_ph3_03_marzban_staging import require_isolated_url
from src.broker_operations import BrokerOperations
from src.broker_server import BrokerApplication, build_broker_server
from src.marzban import MarzbanClient
from src.service_marzban import ServiceMarzbanClient
from src.shadowsocks_retirement import retirement_snapshot


EXPECTED_VERSION = "0.8.4"
USERNAME = "beykusios"
AUTH_KEY = "ss-retirement-stage-" + os.urandom(32).hex()
CLIENT_ID = "mgboost-ss-retirement-stage"


class InstrumentedMarzban:
    def __init__(self, client):
        self.client = client
        self.modify_payloads = []

    def __getattr__(self, name):
        return getattr(self.client, name)

    def modify_user(self, username, payload, token):
        self.modify_payloads.append(json.loads(json.dumps(payload)))
        return self.client.modify_user(username, payload, token)


@contextmanager
def running_broker(marzban, log_capture):
    logger = logging.getLogger("src.broker_server")
    handler = logging.StreamHandler(log_capture)
    logger.addHandler(handler)
    app = BrokerApplication(
        BrokerOperations(marzban), shared_key=AUTH_KEY, client_id=CLIENT_ID,
    )
    server = build_broker_server("127.0.0.1", 0, app, max_workers=2)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield ServiceMarzbanClient(
            mode="broker",
            broker_url=f"http://127.0.0.1:{server.server_address[1]}",
            broker_key=AUTH_KEY,
            broker_client_id=CLIENT_ID,
            broker_timeout=5,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        logger.removeHandler(handler)


def _client(url):
    client = MarzbanClient(base_url=url)
    token = client.get_admin_token_from_env()
    system = client.get_system(token)
    if system.get("version") != EXPECTED_VERSION:
        raise RuntimeError("isolated target is not Marzban 0.8.4")
    return client, token


def _delete_fixture(client, token):
    try:
        client.delete_user(USERNAME, token)
    except HTTPError as exc:
        if exc.code not in {404, 500}:
            raise
        if exc.code == 500:
            try:
                client.get_user(USERNAME, token)
            except HTTPError as reread:
                if reread.code != 404:
                    raise
            else:
                raise


def seed(url):
    client, token = _client(url)
    topology = client.get_inbounds(token)
    ss_tags = {item["tag"] for item in topology.get("shadowsocks", [])}
    if ss_tags != {RETIRED_SHADOWSOCKS_BOOTSTRAP_TAG}:
        raise RuntimeError("bootstrap Shadowsocks topology is not exact")
    try:
        client.get_user(USERNAME, token)
    except HTTPError as exc:
        if exc.code != 404:
            raise
    else:
        raise RuntimeError("staging fixture already exists")
    user = client.create_user({
        "username": USERNAME,
        "proxies": {
            "vless": {"flow": "xtls-rprx-vision"},
            "shadowsocks": {"method": "aes-128-gcm"},
        },
        "inbounds": {
            "vless": list(EFFECTIVE_VLESS_TAGS),
            "shadowsocks": [RETIRED_SHADOWSOCKS_BOOTSTRAP_TAG],
        },
        "expire": 0,
        "data_limit": None,
        "data_limit_reset_strategy": "no_reset",
        "status": "active",
        "note": "isolated retired Shadowsocks fixture",
    }, token)
    print(json.dumps({
        "stage": "SEED_PASS",
        "marzban_version": EXPECTED_VERSION,
        "username": USERNAME,
        "proxy_types": sorted((user.get("proxies") or {}).keys()),
        "vless_inbound_count": len((user.get("inbounds") or {}).get("vless") or []),
        "shadowsocks_inbound_count": len(
            (user.get("inbounds") or {}).get("shadowsocks") or []
        ),
    }, sort_keys=True))


def verify(url):
    client, token = _client(url)
    topology = client.get_inbounds(token)
    if topology.get("shadowsocks"):
        raise RuntimeError("production-equivalent topology still has Shadowsocks")
    before = client.get_user(USERNAME, token)
    before_snapshot = retirement_snapshot(before)
    if not before_snapshot["shadowsocks_metadata"]:
        raise RuntimeError("stale Shadowsocks fixture was not retained")
    before_body, _ = client.get_sub(
        _token(before), {"User-Agent": "MGBoost-SS-Retirement-Staging/1"}
    )
    before_config = _canonical_config(before_body)
    if any(not line.startswith("vless://") for line in before_config):
        raise RuntimeError("pre-cleanup subscription is not VLESS-only")

    instrumented = InstrumentedMarzban(client)
    logs = io.StringIO()
    raw_uuid = before["proxies"]["vless"]["id"]
    with running_broker(instrumented, logs) as service:
        result = service.retire_shadowsocks_metadata({
            "username": USERNAME,
            "expected_state_digest": before_snapshot["state_digest"],
        })
        after = client.get_user(USERNAME, token)
        after_snapshot = retirement_snapshot(after)
        retry = service.retire_shadowsocks_metadata({
            "username": USERNAME,
            "expected_state_digest": after_snapshot["state_digest"],
        })
    after_body, _ = client.get_sub(
        _token(after), {"User-Agent": "MGBoost-SS-Retirement-Staging/1"}
    )
    after_config = _canonical_config(after_body)
    try:
        if result["outcome"] != "REMOVED" or retry["outcome"] != "UNCHANGED":
            raise AssertionError("typed retirement is not idempotent")
        if before_config != after_config:
            raise AssertionError("subscription config changed")
        if before_snapshot["subscription_token_verifier"] != after_snapshot[
            "subscription_token_verifier"
        ]:
            raise AssertionError("subscription token changed")
        if len(instrumented.modify_payloads) != 1:
            raise AssertionError("retirement was not exactly one mutation")
        payload = instrumented.modify_payloads[0]
        if set(payload) != {"proxies"} or set(payload["proxies"]) != {"vless"}:
            raise AssertionError("retirement payload is not narrow")
        if payload["proxies"]["vless"].get("id") != raw_uuid:
            raise AssertionError("retirement payload did not preserve UUID")
        if raw_uuid in json.dumps(result) or raw_uuid in logs.getvalue():
            raise AssertionError("raw UUID leaked through broker result/log")
        print(json.dumps({
            "stage": "RETIREMENT_PASS",
            "marzban_version": EXPECTED_VERSION,
            "username": USERNAME,
            "first_outcome": result["outcome"],
            "retry_outcome": retry["outcome"],
            "modify_calls": len(instrumented.modify_payloads),
            "vless_uuid_mask": after_snapshot["vless_uuid_mask"],
            "vless_uuid_unchanged": (
                before_snapshot["vless_uuid_verifier"]
                == after_snapshot["vless_uuid_verifier"]
            ),
            "vless_inbound_count": len(after_snapshot["vless_inbounds"]),
            "subscription_line_count": len(after_config),
            "subscription_unchanged": True,
            "shadowsocks_metadata_remaining": False,
            "raw_uuid_leak_count": 0,
        }, sort_keys=True))
    finally:
        _delete_fixture(client, token)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--seed", action="store_true")
    mode.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    url = require_isolated_url(args.url)
    if args.seed:
        seed(url)
    else:
        verify(url)


if __name__ == "__main__":
    main()
