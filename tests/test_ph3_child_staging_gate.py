import json

import pytest

from scripts.build_ph3_03_staging_xray import (
    DECOY_VLESS_TAGS,
    EFFECTIVE_VLESS_TAGS,
    build_config,
)
from scripts.verify_ph3_03_marzban_staging import require_isolated_url


def test_staging_topology_proves_effective_vless_is_not_global():
    config = build_config()
    by_protocol = {}
    for inbound in config["inbounds"]:
        by_protocol.setdefault(inbound["protocol"], set()).add(inbound["tag"])

    assert len(EFFECTIVE_VLESS_TAGS) == 25
    assert by_protocol["vless"] == set(EFFECTIVE_VLESS_TAGS + DECOY_VLESS_TAGS)
    assert set(EFFECTIVE_VLESS_TAGS) < by_protocol["vless"]
    assert "shadowsocks" not in by_protocol


def test_staging_url_requires_literal_loopback_non_default_and_ack(monkeypatch):
    monkeypatch.setenv("PH3_ISOLATED_STAGING_ACK", "isolated-marzban-0.8.4")
    assert require_isolated_url("http://127.0.0.1:18043/") == "http://127.0.0.1:18043"
    for value in (
        "https://127.0.0.1:18043",
        "http://localhost:18043",
        "http://178.250.186.127:18043",
        "http://127.0.0.1:8000",
    ):
        with pytest.raises(RuntimeError):
            require_isolated_url(value)


def test_staging_gate_source_contains_no_caller_credentials():
    source = open("scripts/verify_ph3_03_marzban_staging.py", encoding="utf-8").read()
    forbidden_payload_keys = (
        '"id": raw_uuid',
        '"password": raw_ss_password',
        '"uuid": raw_uuid',
    )
    assert all(value not in source for value in forbidden_payload_keys)
    assert json.dumps(list(EFFECTIVE_VLESS_TAGS))[1:-1]
