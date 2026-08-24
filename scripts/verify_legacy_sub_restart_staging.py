#!/usr/bin/env python3
"""PH1-05 staging smoke for MGBoost restart and broker-independent /sub.

Only a synthetic ``mgboost_stage_`` Marzban user is created and removed.  No
token or UUID is printed.  The script deliberately restarts MGBoost while its
localhost broker is unavailable and verifies equivalent legacy VPN output.
"""

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from src.broker_operations import BrokerOperations
from src.broker_server import BrokerApplication, build_broker_server
from src.marzban import MarzbanClient


AUTH_KEY = "staging-restart-key-" + secrets.token_urlsafe(32)
CLIENT_ID = "mgboost-main"
INTERNAL_KEY = "staging-filin-hmac-key-" + secrets.token_urlsafe(32)
_DYNAMIC_SID_RE = re.compile(r"([?&]sid=)[^&#]*")


def canonical_subscription(body):
    decoded = base64.b64decode(body, validate=True).decode("utf-8")
    return sorted(
        _DYNAMIC_SID_RE.sub(r"\1<DYNAMIC-SID>", line.strip())
        for line in decoded.splitlines()
        if line.strip()
    )


def unused_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def require_staging_url(value):
    parsed = urlsplit(value)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "::1"}:
        raise RuntimeError("STAGING_MARZBAN_URL must be loopback HTTP")
    if parsed.port is None or parsed.port in {8000, 443}:
        raise RuntimeError("Refusing default/production-like Marzban port")
    return value.rstrip("/")


def identity_snapshot(user):
    return {
        "proxies": user.get("proxies"),
        "subscription_url": user.get("subscription_url"),
        "inbounds": user.get("inbounds"),
    }


@contextmanager
def running_broker(marzban, port):
    app = BrokerApplication(
        BrokerOperations(marzban), shared_key=AUTH_KEY, client_id=CLIENT_ID
    )
    server = build_broker_server("127.0.0.1", port, app, max_workers=4)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def start_mgboost(*, marzban_url, broker_port, listen_port, data_dir):
    env = os.environ.copy()
    env.pop("MARZBAN_ADMIN_USER", None)
    env.pop("MARZBAN_ADMIN_PASS", None)
    env.update({
        "MARZBAN_URL": marzban_url,
        "MARZBAN_SERVICE_MODE": "broker",
        "MARZBAN_BROKER_URL": f"http://127.0.0.1:{broker_port}",
        "MARZBAN_BROKER_AUTH_KEY": AUTH_KEY,
        "MARZBAN_BROKER_CLIENT_ID": CLIENT_ID,
        "PYTHON_DOTENV_DISABLED": "1",
        "LISTEN_HOST": "127.0.0.1",
        "LISTEN_PORT": str(listen_port),
        "DATA_DIR": data_dir,
        "SECRET_KEY": "synthetic-staging-secret-key-only",
        "PUBLIC_HOST": "http://127.0.0.1",
        "INTERNAL_API_KEY": INTERNAL_KEY,
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    assert "MARZBAN_ADMIN_USER" not in env and "MARZBAN_ADMIN_PASS" not in env
    return subprocess.Popen(
        [sys.executable, "main.py"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def stop_process(process):
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=3)


def fetch_when_ready(url, process, timeout=12):
    deadline = time.monotonic() + timeout
    last_error = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            output = (process.stdout.read() if process.stdout else "")[-2000:]
            raise RuntimeError(f"MGBoost exited during staging smoke: {output}")
        try:
            response = urlopen(Request(url, headers={"User-Agent": "Happ/PH1-05"}), timeout=2)
            return response.status, response.read(), dict(response.headers)
        except (URLError, HTTPError) as exc:
            last_error = exc
            time.sleep(0.2)
    raise RuntimeError(f"MGBoost did not become ready: {type(last_error).__name__}")


def filin_request(base_url, method, path, payload=None):
    body = b"" if payload is None else json.dumps(
        payload, separators=(",", ":")
    ).encode("utf-8")
    timestamp = str(int(time.time()))
    nonce = secrets.token_urlsafe(24)
    body_hash = hashlib.sha256(body).hexdigest()
    signed = "\n".join([method, path, timestamp, nonce, body_hash])
    signature = hmac.new(
        INTERNAL_KEY.encode("utf-8"), signed.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    request = Request(
        base_url + path,
        data=body if method != "GET" else None,
        method=method,
        headers={
            "Content-Type": "application/json",
            "X-Filin-Timestamp": timestamp,
            "X-Filin-Nonce": nonce,
            "X-Filin-Signature": signature,
        },
    )
    response = urlopen(request, timeout=5)
    return response.status, json.loads(response.read() or b"{}")


def main():
    marzban_url = require_staging_url(
        os.environ.get("STAGING_MARZBAN_URL", "http://127.0.0.1:18000")
    )
    if not os.environ.get("MARZBAN_ADMIN_USER") or not os.environ.get("MARZBAN_ADMIN_PASS"):
        raise RuntimeError("Synthetic staging admin credentials are required")
    marzban = MarzbanClient(marzban_url)
    admin_token = marzban.get_admin_token_from_env()
    username = "mgboost_stage_restart_" + secrets.token_hex(5)
    broker_port = unused_port()
    listen_port = unused_port()
    process = None
    created = False
    filin_name = "mgboost_stage_filin_" + secrets.token_hex(5)
    filin_created = False

    try:
        user = marzban.create_user({
            "username": username,
            "proxies": {"vless": {}},
            "inbounds": {"vless": ["VLESS STAGING"]},
            "expire": int(time.time()) + 30 * 86400,
            "data_limit": 0,
            "data_limit_reset_strategy": "no_reset",
            "status": "active",
        }, admin_token)
        created = True
        identity = (user.get("proxies") or {}).get("vless", {}).get("id")
        raw_url = user.get("subscription_url") or ""
        token = raw_url.rstrip("/").rsplit("/", 1)[-1]
        if not identity or not token or token == raw_url:
            raise AssertionError("Synthetic user lacks expected UUID/token")

        with tempfile.TemporaryDirectory(prefix="mgboost-ph1-05-stage-") as data_dir:
            with running_broker(marzban, broker_port):
                process = start_mgboost(
                    marzban_url=marzban_url,
                    broker_port=broker_port,
                    listen_port=listen_port,
                    data_dir=data_dir,
                )
                status, before, before_headers = fetch_when_ready(
                    f"http://127.0.0.1:{listen_port}/sub/{token}", process
                )
                assert status == 200
                decoded = base64.b64decode(before).decode("utf-8")
                assert identity in decoded

                # Exercise the actual nginx-facing Filin contract layer,
                # including its existing HMAC, not just broker methods.
                filin_payload = {
                    "username": filin_name,
                    "proxies": {"vless": {}},
                    "inbounds": {"vless": ["VLESS STAGING"]},
                    "expire": int(time.time()) + 14 * 86400,
                    "data_limit": 777,
                    "data_limit_reset_strategy": "no_reset",
                    "status": "active",
                }
                filin_status, filin_user = filin_request(
                    f"http://127.0.0.1:{listen_port}", "POST",
                    "/internal/v1/users", filin_payload,
                )
                assert filin_status == 201
                filin_created = True
                filin_identity = identity_snapshot(filin_user)
                filin_old_url = filin_user.get("subscription_url") or ""
                filin_old_token = filin_old_url.rstrip("/").rsplit("/", 1)[-1]
                filin_uuid = (filin_user.get("proxies") or {}).get("vless", {}).get("id")
                if not filin_old_token or filin_old_token == filin_old_url or not filin_uuid:
                    raise AssertionError("Synthetic Filin user lacks legacy identity")

                # Marzban's current subscription_url contains a timestamped
                # legacy token and may advance when expire is updated.  Force
                # that boundary and verify the user-facing contract instead:
                # the already-issued alias must keep resolving to the same
                # UUID/config, so no client reconfiguration is required.
                time.sleep(1.05)
                renew_status, renewed = filin_request(
                    f"http://127.0.0.1:{listen_port}", "POST",
                    f"/internal/v1/users/{filin_name}/renew",
                    {"add_days": 30, "data_limit": 0},
                )
                # The established legacy implementation maps caller 0 to a
                # JSON null.  Marzban 0.8.4 treats that partial-update value
                # as "leave unchanged", so the existing 777 remains.  PH1-05
                # preserves this observable behavior; changing it belongs to
                # a separately approved API/product migration.
                if renew_status != 200 or renewed.get("data_limit") != 777:
                    raise AssertionError(
                        "Filin renew mismatch: "
                        f"status={renew_status}, data_limit={renewed.get('data_limit')!r}, "
                        f"response_keys={sorted(renewed)}"
                    )
                filin_identity_after = identity_snapshot(
                    marzban.get_user(filin_name, admin_token)
                )
                protected_fields = ("proxies", "inbounds")
                if any(
                    filin_identity_after.get(key) != filin_identity.get(key)
                    for key in protected_fields
                ):
                    changed_fields = sorted(
                        key for key in protected_fields
                        if filin_identity.get(key) != filin_identity_after.get(key)
                    )
                    raise AssertionError(
                        "Filin renew changed protected identity fields: "
                        f"{changed_fields}"
                    )
                old_alias_body, _ = marzban.get_sub(
                    filin_old_token, {"User-Agent": "Happ/PH1-07"}
                )
                old_alias_decoded = base64.b64decode(old_alias_body).decode("utf-8")
                if filin_uuid not in old_alias_decoded:
                    raise AssertionError(
                        "Legacy Filin alias stopped resolving the original UUID after renew"
                    )
                delete_status, deleted = filin_request(
                    f"http://127.0.0.1:{listen_port}", "DELETE",
                    f"/internal/v1/users/{filin_name}", None,
                )
                assert delete_status == 200 and deleted == {"ok": True}
                filin_created = False

            # Broker is down here.  The existing MGBoost process must keep
            # serving the same public config, then start cleanly in that state.
            status, during, during_headers = fetch_when_ready(
                f"http://127.0.0.1:{listen_port}/sub/{token}", process
            )
            assert status == 200
            assert canonical_subscription(during) == canonical_subscription(before)
            assert during_headers.get("profile-web-page-url") == before_headers.get("profile-web-page-url")

            stop_process(process)
            process = start_mgboost(
                marzban_url=marzban_url,
                broker_port=broker_port,
                listen_port=listen_port,
                data_dir=data_dir,
            )
            status, restarted, restarted_headers = fetch_when_ready(
                f"http://127.0.0.1:{listen_port}/sub/{token}", process
            )
            assert status == 200
            assert canonical_subscription(restarted) == canonical_subscription(before)
            assert restarted_headers.get("subscription-userinfo") == before_headers.get("subscription-userinfo")
            assert marzban.get_user(username, admin_token)["proxies"]["vless"]["id"] == identity

        print("legacy_sub_with_broker_up=PASS")
        print("legacy_sub_with_broker_down=PASS")
        print("mgboost_restart_with_broker_down=PASS")
        print("legacy_config_body_differences=0")
        print("legacy_uuid_changes=0")
        print("main_process_marzban_sudo_env_keys_present=0")
        print("filin_hmac_create_renew_delete=PASS")
    finally:
        if process is not None and process.poll() is None:
            stop_process(process)
        if created:
            try:
                marzban.delete_user(username, admin_token)
            except HTTPError as exc:
                # Marzban 0.8.4 may report after committing deletion; verify
                # the exact synthetic user is gone before accepting that case.
                if exc.code != 500:
                    raise
                try:
                    marzban.get_user(username, admin_token)
                except HTTPError as reread:
                    if reread.code != 404:
                        raise
                else:
                    raise
        if filin_created:
            try:
                marzban.delete_user(filin_name, admin_token)
            except HTTPError as exc:
                if exc.code != 500:
                    raise
                try:
                    marzban.get_user(filin_name, admin_token)
                except HTTPError as reread:
                    if reread.code != 404:
                        raise
                else:
                    raise


if __name__ == "__main__":
    main()
