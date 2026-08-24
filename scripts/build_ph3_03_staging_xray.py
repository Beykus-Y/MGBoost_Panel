#!/usr/bin/env python3
"""Write an isolated Xray topology for the PH3-03 real-Marzban gate."""

from __future__ import annotations

import argparse
import json
import secrets
from pathlib import Path


EFFECTIVE_VLESS_TAGS = (
    "de-grpc-smart", "de-tcp-smart", "grpc-direct", "grpc-smart",
    "nl-grpc-smart", "nl-tcp-smart", "tcp-direct", "tcp-smart",
    "vless-grpc-cdn", "vless-ws-cdn", "vless-xhttp-cdn",
    "wl-selec-grpc-direct", "wl-selec-grpc-direct-5post",
    "wl-selec-grpc-direct-yandex-maps", "wl-selec-grpc-smart",
    "wl-selec-grpc-smart-5post", "wl-selec-grpc-smart-yandex-maps",
    "wl-tcp-direct", "wl-tcp-direct-5post", "wl-tcp-direct-yandex-maps",
    "wl-tcp-smart", "wl-tcp-smart-5post", "wl-tcp-smart-yandex-maps",
    "xhttp-direct", "xhttp-smart",
)
DECOY_VLESS_TAGS = ("mgboost-stage-decoy-vless-a", "mgboost-stage-decoy-vless-b")


def build_config():
    inbounds = []
    for index, tag in enumerate(EFFECTIVE_VLESS_TAGS + DECOY_VLESS_TAGS):
        inbounds.append({
            "tag": tag,
            "listen": "127.0.0.1",
            "port": 31000 + index,
            "protocol": "vless",
            "settings": {"clients": [], "decryption": "none"},
            "streamSettings": {"network": "tcp", "security": "none"},
        })
    return {
        "log": {"loglevel": "warning"},
        "inbounds": inbounds,
        "outbounds": [{"tag": "DIRECT", "protocol": "freedom"}],
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument("--marzban-env-output")
    parser.add_argument("--verifier-env-output")
    parser.add_argument("--mgboost-data-dir")
    parser.add_argument("--port", type=int, default=18043)
    args = parser.parse_args()
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(build_config(), indent=2) + "\n", encoding="utf-8")
    output.chmod(0o600)
    if bool(args.marzban_env_output) != bool(args.verifier_env_output):
        parser.error("both staging environment outputs must be specified together")
    if args.marzban_env_output:
        if not args.mgboost_data_dir:
            parser.error("--mgboost-data-dir is required with environment outputs")
        admin_user = "mgboost_ph3_stage_admin"
        admin_password = secrets.token_urlsafe(48)
        marzban_env = Path(args.marzban_env_output).resolve()
        verifier_env = Path(args.verifier_env_output).resolve()
        data_dir = Path(args.mgboost_data_dir).resolve()
        data_dir.mkdir(parents=True, exist_ok=True)
        marzban_env.write_text(
            "\n".join((
                f"SUDO_USERNAME={admin_user}",
                f"SUDO_PASSWORD={admin_password}",
                "SQLALCHEMY_DATABASE_URL=sqlite:////var/lib/marzban/db.sqlite3",
                "UVICORN_HOST=0.0.0.0",
                f"UVICORN_PORT={args.port}",
                "XRAY_JSON=/var/lib/marzban/xray_config.json",
                "DOCS=false",
                "DEBUG=false",
                "NOTIFY=false",
                "TELEGRAM_API_TOKEN=",
                "TELEGRAM_ADMIN_ID=",
            )) + "\n",
            encoding="utf-8",
        )
        verifier_env.write_text(
            "\n".join((
                f"MARZBAN_ADMIN_USER={admin_user}",
                f"MARZBAN_ADMIN_PASS={admin_password}",
                f"DATA_DIR={data_dir}",
                "PRIMARY_MGBOOST_ADMIN_ACTOR_ID=owner:mgboost-primary:v1",
                "PRIMARY_MGBOOST_ADMIN_LOGIN=mgboost_ph3_stage_primary",
                f"DEVICE_SLOT_HMAC_KEY={secrets.token_urlsafe(48)}",
                "PH3_ISOLATED_STAGING_ACK=isolated-marzban-0.8.4",
                f"PH3_STAGING_URL=http://127.0.0.1:{args.port}",
            )) + "\n",
            encoding="utf-8",
        )
        for path in (marzban_env, verifier_env):
            path.chmod(0o600)
    print(json.dumps({
        "output": str(output),
        "effective_vless_count": len(EFFECTIVE_VLESS_TAGS),
        "global_vless_count": len(EFFECTIVE_VLESS_TAGS) + len(DECOY_VLESS_TAGS),
        "global_shadowsocks_count": 0,
        "environment_files_created": bool(args.marzban_env_output),
    }, sort_keys=True))


if __name__ == "__main__":
    main()
