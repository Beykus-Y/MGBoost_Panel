#!/usr/bin/env python3
"""Destructive-only-to-prefixed-users PH1-05 Marzban staging contract check.

This script must never be pointed at production.  It creates two synthetic
users whose names start with ``mgboost_stage_``, compares the legacy direct
client with the typed localhost broker, and deletes only those exact users in
``finally``.  It intentionally prints no UUID, subscription token, password,
or admin JWT.
"""

import json
import os
import secrets
import threading
import time
from contextlib import contextmanager
from urllib.error import HTTPError
from urllib.parse import urlsplit

from src.broker_operations import BrokerOperations
from src.broker_server import BrokerApplication, build_broker_server
from src.marzban import MarzbanClient
from src.service_marzban import ServiceMarzbanClient


AUTH_KEY = "staging-only-broker-key-" + secrets.token_urlsafe(32)
CLIENT_ID = "mgboost-staging-verifier"
PRESERVED_FIELDS = ("proxies", "inbounds", "data_limit", "status")


def require_staging_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "::1"}:
        raise RuntimeError("STAGING_MARZBAN_URL must be a literal loopback HTTP URL")
    if parsed.port is None or parsed.port in {8000, 443}:
        raise RuntimeError("Refusing the default/production-like Marzban port")
    return value.rstrip("/")


@contextmanager
def running_broker(marzban, *, port=0):
    app = BrokerApplication(
        BrokerOperations(marzban), shared_key=AUTH_KEY, client_id=CLIENT_ID
    )
    server = build_broker_server("127.0.0.1", port, app, max_workers=4)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def comparable_user(user):
    # Values generated per user are intentionally excluded; their *stability*
    # is asserted independently for each user across every mutation.
    return {
        "expire": user.get("expire"),
        "data_limit": user.get("data_limit"),
        "data_limit_reset_strategy": user.get("data_limit_reset_strategy"),
        "status": user.get("status"),
        "note": user.get("note"),
        "proxy_types": sorted((user.get("proxies") or {}).keys()),
        "inbounds": user.get("inbounds"),
    }


def identity_snapshot(user):
    return {
        "proxies": user.get("proxies"),
        "subscription_url": user.get("subscription_url"),
        "inbounds": user.get("inbounds"),
    }


def main():
    base_url = require_staging_url(
        os.environ.get("STAGING_MARZBAN_URL", "http://127.0.0.1:18000")
    )
    if not os.environ.get("MARZBAN_ADMIN_USER") or not os.environ.get("MARZBAN_ADMIN_PASS"):
        raise RuntimeError("Synthetic staging admin credentials are required")

    marzban = MarzbanClient(base_url=base_url)
    admin_token = marzban.get_admin_token_from_env()
    system = marzban.get_system(admin_token)
    version = system.get("version") if isinstance(system, dict) else None
    if version != "0.8.4":
        raise RuntimeError(f"Expected Marzban 0.8.4 staging, got {version!r}")

    suffix = secrets.token_hex(5)
    direct_name = f"mgboost_stage_direct_{suffix}"
    broker_name = f"mgboost_stage_broker_{suffix}"
    created_names = []
    results = {}
    base_expire = int(time.time()) + 10 * 86400
    payload_common = {
        "proxies": {"vless": {}},
        "inbounds": {"vless": ["VLESS STAGING"]},
        "expire": base_expire,
        "data_limit": 123456789,
        "data_limit_reset_strategy": "no_reset",
        "note": "PH1-05 staging contract",
        "status": "active",
    }

    try:
        direct_created = marzban.create_user(
            {"username": direct_name, **payload_common}, admin_token
        )
        created_names.append(direct_name)

        with running_broker(marzban) as server:
            port = server.server_address[1]
            service = ServiceMarzbanClient(
                mode="broker",
                broker_url=f"http://127.0.0.1:{port}",
                broker_key=AUTH_KEY,
                broker_client_id=CLIENT_ID,
                direct_client=marzban,
            )
            sentinel = service.get_admin_token_from_env()

            broker_created = service.create_user(
                {"username": broker_name, **payload_common}, sentinel
            )
            created_names.append(broker_name)
            direct_created = marzban.get_user(direct_name, admin_token)
            broker_created = marzban.get_user(broker_name, admin_token)
            assert comparable_user(direct_created) == comparable_user(broker_created)
            results["legacy.user.create"] = True

            broker_identity = identity_snapshot(broker_created)
            direct_identity = identity_snapshot(direct_created)

            direct_get = marzban.get_user(broker_name, admin_token)
            broker_get = service.get_user(broker_name, sentinel)
            assert canonical(direct_get) == canonical(broker_get)
            results["legacy.user.get"] = True

            direct_usage = marzban.get_user_usage(broker_name, admin_token)
            broker_usage = service.get_user_usage(broker_name, sentinel)
            assert canonical(direct_usage) == canonical(broker_usage)
            results["legacy.user.usage"] = True

            direct_users = marzban.get_users(admin_token, limit=100, offset=0)
            broker_users = service.get_users(sentinel, limit=100, offset=0)
            assert canonical(direct_users) == canonical(broker_users)
            results["legacy.users.list"] = True

            direct_nodes = marzban.get_nodes(admin_token)
            broker_nodes = service.get_nodes(sentinel)
            assert canonical(direct_nodes) == canonical(broker_nodes)
            results["legacy.nodes.list"] = True

            direct_node_usage = marzban.get_nodes_usage(admin_token)
            broker_node_usage = service.get_nodes_usage(sentinel)
            assert canonical(direct_node_usage) == canonical(broker_node_usage)
            results["legacy.nodes.usage"] = True

            direct_inbounds = marzban.get_inbounds(admin_token)
            broker_inbounds = service.get_inbounds(sentinel)
            assert canonical(direct_inbounds) == canonical(broker_inbounds)
            results["legacy.inbounds.list"] = True

            direct_renewed = ServiceMarzbanClient(
                mode="direct", direct_client=marzban
            ).renew_user(direct_name, {"add_days": 7, "data_limit": 0}, admin_token)
            broker_renewed = service.renew_user(
                broker_name, {"add_days": 7, "data_limit": 0}, sentinel
            )
            assert comparable_user(direct_renewed) == comparable_user(broker_renewed)
            assert identity_snapshot(marzban.get_user(direct_name, admin_token)) == direct_identity
            assert identity_snapshot(marzban.get_user(broker_name, admin_token)) == broker_identity
            results["legacy.user.renew"] = True

            exact_expire = base_expire + 21 * 86400
            direct_set = marzban.modify_user(
                direct_name, {"expire": exact_expire}, admin_token
            )
            broker_set = service.modify_user(
                broker_name, {"expire": exact_expire}, sentinel
            )
            assert comparable_user(direct_set) == comparable_user(broker_set)
            direct_current = marzban.get_user(direct_name, admin_token)
            broker_current = marzban.get_user(broker_name, admin_token)
            assert identity_snapshot(direct_current) == direct_identity
            assert identity_snapshot(broker_current) == broker_identity
            results["legacy.user.set_expire"] = True

            # A real legacy subscription remains a direct non-SUDO path, and
            # an expire-only broker mutation does not alter its body/identity.
            raw_url = broker_current.get("subscription_url") or ""
            legacy_token = raw_url.rstrip("/").rsplit("/", 1)[-1]
            if not legacy_token or legacy_token == raw_url:
                raise AssertionError("Staging user did not receive a subscription URL")
            before_body, _ = service.get_sub(legacy_token, {"User-Agent": "Happ/PH1-05"})
            after_body, _ = marzban.get_sub(legacy_token, {"User-Agent": "Happ/PH1-05"})
            assert before_body == after_body

            service.delete_user(broker_name, sentinel)
            created_names.remove(broker_name)
            try:
                marzban.get_user(broker_name, admin_token)
            except HTTPError as exc:
                assert exc.code == 404
            else:
                raise AssertionError("Broker delete did not remove the staging user")
            results["legacy.user.delete"] = True

        # Broker restart on the same address must require no credential or
        # ServiceMarzbanClient recreation.
        restart_port = port
        with running_broker(marzban, port=restart_port):
            assert service.get_user(direct_name, sentinel)["username"] == direct_name
        results["broker.restart"] = True

        marzban.delete_user(direct_name, admin_token)
        created_names.remove(direct_name)

        expected = {
            "legacy.user.get", "legacy.user.usage", "legacy.users.list",
            "legacy.nodes.list", "legacy.nodes.usage", "legacy.inbounds.list",
            "legacy.user.create", "legacy.user.renew",
            "legacy.user.set_expire", "legacy.user.delete",
        }
        assert expected.issubset(results)
        print(f"marzban_version={version}")
        for operation in sorted(expected):
            print(f"{operation}=PASS")
        print("broker.restart=PASS")
        print("identity_fields_changed_by_broker_mutations=0")
        print("subscription_body_differences=0")
        print("staging_contract=PASS")
    finally:
        for username in list(created_names):
            if not username.startswith("mgboost_stage_"):
                raise AssertionError("Refusing unsafe staging cleanup target")
            try:
                marzban.delete_user(username, admin_token)
            except HTTPError as exc:
                if exc.code != 404:
                    raise


if __name__ == "__main__":
    main()
