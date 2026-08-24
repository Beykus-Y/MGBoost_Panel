#!/usr/bin/env python3
"""Inventory and narrowly retire non-functional legacy Shadowsocks metadata."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re

from src.service_marzban import ServiceMarzbanClient
from src.shadowsocks_retirement import retirement_snapshot


_DYNAMIC_SID_RE = re.compile(r"([?&]sid=)[^&#]*")
_INFO_PREFIX = "vless://00000000-0000-0000-0000-000000000000@127.0.0.1:1?"
_CONFIRM = "retired-shadowsocks-v1"


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")).hexdigest()


def _token(user: dict) -> str:
    raw_url = str(user.get("subscription_url") or "")
    token = raw_url.rstrip("/").rsplit("/", 1)[-1]
    if not token or token == raw_url:
        raise RuntimeError("subscription bearer is unavailable")
    return token


def _canonical_config(body: bytes) -> list[str]:
    candidates = [body]
    try:
        candidates.insert(0, base64.b64decode(body, validate=True))
    except Exception:
        pass
    for candidate in candidates:
        try:
            lines = candidate.decode("utf-8").splitlines()
        except UnicodeDecodeError:
            continue
        links = sorted(
            _DYNAMIC_SID_RE.sub(r"\1<DYNAMIC-SID>", line.strip())
            for line in lines
            if "://" in line and not line.strip().startswith(_INFO_PREFIX)
        )
        if links:
            return links
    raise RuntimeError("subscription config contains no VPN entries")


def _config_snapshot(client: ServiceMarzbanClient, user: dict) -> dict:
    body, _headers = client.get_sub(
        _token(user), {"User-Agent": "MGBoost-Shadowsocks-Retirement/1"}
    )
    lines = _canonical_config(body)
    protocols = {}
    for line in lines:
        protocol = line.split("://", 1)[0].lower()
        protocols[protocol] = protocols.get(protocol, 0) + 1
    if any(protocol != "vless" for protocol in protocols):
        raise RuntimeError("subscription contains a non-VLESS entry")
    return {
        "line_count": len(lines),
        "protocol_counts": dict(sorted(protocols.items())),
        "digest": _digest(lines),
    }


def _users(client: ServiceMarzbanClient, sentinel) -> list[dict]:
    result = []
    offset = 0
    while True:
        page = client.get_users(sentinel, limit=100, offset=offset)
        rows = page.get("users", []) if isinstance(page, dict) else page
        result.extend(rows)
        if len(rows) < 100:
            return result
        offset += len(rows)


def _safe_inventory(client, sentinel, *, verify_subscriptions: bool) -> dict:
    topology = client.get_inbounds(sentinel)
    if (topology or {}).get("shadowsocks"):
        raise RuntimeError("production topology still has Shadowsocks inbound")
    users = _users(client, sentinel)
    rows = []
    aggregate_protocols = {}
    for user in users:
        snapshot = retirement_snapshot(user)
        row = {
            "username": snapshot["username"],
            "shadowsocks_metadata": snapshot["shadowsocks_metadata"],
            "vless_uuid_verifier": snapshot["vless_uuid_verifier"],
            "vless_uuid_mask": snapshot["vless_uuid_mask"],
            "expire": snapshot["expire"],
            "status": snapshot["status"],
            "data_limit": snapshot["data_limit"],
            "vless_flow": snapshot["vless_flow"],
            "vless_inbounds": snapshot["vless_inbounds"],
            "proxy_types": snapshot["proxy_types"],
            "api_link_protocol_counts": snapshot["subscription_protocol_counts"],
            "state_digest": snapshot["state_digest"],
        }
        if verify_subscriptions:
            row["subscription"] = _config_snapshot(client, user)
            for protocol, count in row["subscription"]["protocol_counts"].items():
                aggregate_protocols[protocol] = aggregate_protocols.get(protocol, 0) + count
        rows.append(row)
    targets = [row["username"] for row in rows if row["shadowsocks_metadata"]]
    return {
        "user_count": len(rows),
        "shadowsocks_metadata_count": len(targets),
        "shadowsocks_metadata_users": targets,
        "global_inbound_counts": {
            protocol: len(values) for protocol, values in sorted((topology or {}).items())
        },
        "verified_subscription_protocol_counts": dict(sorted(aggregate_protocols.items())),
        "users": rows,
    }


def _apply_one(client, sentinel, username: str) -> dict:
    before = client.get_user(username, sentinel)
    before_snapshot = retirement_snapshot(before)
    before_config = _config_snapshot(client, before)
    result = client.retire_shadowsocks_metadata({
        "username": username,
        "expected_state_digest": before_snapshot["state_digest"],
    })
    after = client.get_user(username, sentinel)
    after_snapshot = retirement_snapshot(after)
    after_config = _config_snapshot(client, after)
    if before_config != after_config:
        raise RuntimeError("subscription config changed during metadata retirement")
    if result["outcome"] == "REMOVED" and after_snapshot["shadowsocks_metadata"]:
        raise RuntimeError("broker reported removal but metadata remains")
    return {
        "username": username,
        "outcome": result["outcome"],
        "vless_uuid_unchanged": (
            before_snapshot["vless_uuid_verifier"]
            == after_snapshot["vless_uuid_verifier"]
        ),
        "subscription_token_unchanged": (
            before_snapshot["subscription_token_verifier"]
            == after_snapshot["subscription_token_verifier"]
        ),
        "functional_contract_unchanged": all(
            before_snapshot[key] == after_snapshot[key]
            for key in (
                "vless_uuid_verifier", "vless_flow", "vless_inbounds", "expire",
                "status", "data_limit", "data_limit_reset_strategy",
                "subscription_token_verifier", "subscription_links_digest",
                "subscription_protocol_counts",
            )
        ),
        "subscription_config_unchanged": True,
        "subscription": after_config,
        "shadowsocks_metadata_remaining": after_snapshot["shadowsocks_metadata"],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--inventory", action="store_true")
    mode.add_argument("--apply-user")
    mode.add_argument("--apply-all", action="store_true")
    parser.add_argument("--verify-subscriptions", action="store_true")
    parser.add_argument("--expected-user-count", type=int)
    parser.add_argument("--expected-target-count", type=int)
    parser.add_argument("--confirm")
    args = parser.parse_args()

    client = ServiceMarzbanClient()
    client.assert_credential_boundary()
    sentinel = client.get_admin_token_from_env()
    if args.inventory:
        print(json.dumps(
            _safe_inventory(
                client, sentinel, verify_subscriptions=args.verify_subscriptions
            ), ensure_ascii=False, indent=2, sort_keys=True,
        ))
        return
    if args.confirm != _CONFIRM:
        raise RuntimeError("explicit retirement confirmation is required")
    if args.apply_user:
        print(json.dumps(
            _apply_one(client, sentinel, args.apply_user),
            ensure_ascii=False, indent=2, sort_keys=True,
        ))
        return
    inventory = _safe_inventory(client, sentinel, verify_subscriptions=False)
    if args.expected_user_count != inventory["user_count"]:
        raise RuntimeError("production user-count gate changed")
    if args.expected_target_count != inventory["shadowsocks_metadata_count"]:
        raise RuntimeError("retirement target-count gate changed")
    results = []
    for username in inventory["shadowsocks_metadata_users"]:
        results.append(_apply_one(client, sentinel, username))
    print(json.dumps({
        "outcome": "PASS",
        "processed": len(results),
        "results": results,
    }, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
