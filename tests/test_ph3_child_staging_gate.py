import json

import pytest

from scripts.build_ph3_03_staging_xray import (
    EFFECTIVE_VLESS_TAGS,
    RETIRED_SHADOWSOCKS_BOOTSTRAP_TAG,
    build_config,
)
from scripts.verify_ph3_03_marzban_staging import _normalize_vless, require_isolated_url


def test_staging_topology_is_exact_vless_only_contract():
    config = build_config()
    by_protocol = {}
    for inbound in config["inbounds"]:
        by_protocol.setdefault(inbound["protocol"], set()).add(inbound["tag"])

    assert len(EFFECTIVE_VLESS_TAGS) == 25
    assert by_protocol["vless"] == set(EFFECTIVE_VLESS_TAGS)
    assert "shadowsocks" not in by_protocol


def test_retired_shadowsocks_fixture_is_opt_in_only():
    config = build_config(include_retired_shadowsocks=True)
    shadowsocks = [
        inbound for inbound in config["inbounds"]
        if inbound["protocol"] == "shadowsocks"
    ]
    assert [item["tag"] for item in shadowsocks] == [
        RETIRED_SHADOWSOCKS_BOOTSTRAP_TAG
    ]


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


def test_subscription_comparison_normalizes_only_credential_and_username():
    source = (
        "vless://11111111-1111-1111-1111-111111111111@127.0.0.1:31000"
        "?security=none&type=tcp#%F0%9F%9A%80%20Marz%20%28beykusios%29%20"
        "%5BVLESS%20-%20tcp%5D"
    )
    child = (
        "vless://22222222-2222-2222-2222-222222222222@127.0.0.1:31000"
        "?security=none&type=tcp#%F0%9F%9A%80%20Marz%20"
        "%28mgc_sgg6v7t6he43yytsqmkdczzfpa%29%20%5BVLESS%20-%20tcp%5D"
    )
    assert _normalize_vless(source, "beykusios") == _normalize_vless(
        child, "mgc_sgg6v7t6he43yytsqmkdczzfpa"
    )

    changed_endpoint = child.replace("127.0.0.1:31000", "127.0.0.1:31001")
    assert _normalize_vless(source, "beykusios") != _normalize_vless(
        changed_endpoint, "mgc_sgg6v7t6he43yytsqmkdczzfpa"
    )
