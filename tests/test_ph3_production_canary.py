import os
import sqlite3

import pytest
from dotenv import dotenv_values

from scripts.configure_ph3_03_canary import ACTOR_ID, configure
from scripts.run_ph3_03_production_canary import (
    ALIASES,
    ALIAS_DEVICE_COUNTS,
    EXPECTED_ACCOUNT_PUBLIC_ID,
    EXPECTED_CHILD_USERNAME,
    EXPECTED_OPERATION_ID,
    SOURCE_USERNAME,
    _alias_evidence,
    _safe_legacy_snapshot,
)
from src.child_contract import derive_child_username, derive_operation_id
from src.internal_entitlements import derive_reviewed_account_public_id


def _write(path, content, mode=0o640):
    path.write_text(content, encoding="utf-8")
    path.chmod(mode)


def test_canary_config_is_atomic_idempotent_and_uses_protected_login(tmp_path):
    main = tmp_path / "main.env"
    broker = tmp_path / "broker.env"
    backup = tmp_path / "backup.env"
    telemetry = "telemetry-independent-key-at-least-thirty-two-bytes"
    _write(main, f"COMPAT_TELEMETRY_HMAC_KEY={telemetry}\nOTHER=value\n")
    _write(
        broker,
        "MARZBAN_ADMIN_USER=authenticated-owner-login\n"
        "MARZBAN_ADMIN_PASS=protected-test-password\n",
    )

    first = configure(main, broker, backup)
    first_values = dotenv_values(main)
    slot_key = first_values["DEVICE_SLOT_HMAC_KEY"]
    assert first["slot_key_generated_now"] is True
    assert len(slot_key.encode()) >= 32
    assert slot_key != telemetry
    assert first_values["PRIMARY_MGBOOST_ADMIN_ACTOR_ID"] == ACTOR_ID
    assert first_values["PRIMARY_MGBOOST_ADMIN_LOGIN"] == "authenticated-owner-login"
    assert first_values["OTHER"] == "value"
    assert oct(backup.stat().st_mode & 0o777) == "0o600"

    second = configure(main, broker, backup)
    assert second["slot_key_generated_now"] is False
    assert dotenv_values(main)["DEVICE_SLOT_HMAC_KEY"] == slot_key


def test_canary_config_rejects_conflicting_actor_or_reused_key(tmp_path):
    main = tmp_path / "main.env"
    broker = tmp_path / "broker.env"
    backup = tmp_path / "backup.env"
    key = "same-key-material-that-is-at-least-thirty-two-bytes"
    _write(
        main,
        f"COMPAT_TELEMETRY_HMAC_KEY={key}\nDEVICE_SLOT_HMAC_KEY={key}\n"
        "PRIMARY_MGBOOST_ADMIN_ACTOR_ID=other-actor\n",
    )
    _write(broker, "MARZBAN_ADMIN_USER=admin\nMARZBAN_ADMIN_PASS=password\n")
    with pytest.raises(RuntimeError):
        configure(main, broker, backup)


def test_approved_canary_identities_are_server_derived_and_fixed():
    account = derive_reviewed_account_public_id("INTERNAL_OWNER_PRIMARY")
    child = derive_child_username(account, 1, 1)
    assert account == EXPECTED_ACCOUNT_PUBLIC_ID
    assert child == EXPECTED_CHILD_USERNAME
    assert derive_operation_id(child) == EXPECTED_OPERATION_ID


def test_alias_evidence_uses_counts_without_copying_legacy_request_keys():
    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE user_devices(username TEXT,request_key TEXT)")
    for alias in ALIASES:
        for index in range(ALIAS_DEVICE_COUNTS[alias]):
            connection.execute(
                "INSERT INTO user_devices VALUES (?,?)", (alias, f"raw-{alias}-{index}")
            )
    evidence = _alias_evidence(connection)
    assert {row["legacy_username"]: row["observed_device_count"] for row in evidence} == (
        ALIAS_DEVICE_COUNTS
    )
    assert [row["legacy_username"] for row in evidence if row["alias_role"] == "PRIMARY"] == [
        SOURCE_USERNAME
    ]
    serialized = repr(evidence)
    assert "raw-" not in serialized


def test_production_canary_has_no_generic_create_delete_or_runtime_switch():
    source = open(
        "scripts/run_ph3_03_production_canary.py", encoding="utf-8"
    ).read()
    assert "legacy.user.create" not in source
    assert ".delete_user(" not in source
    assert "PH3_04" not in source
    assert 'EXPECTED_CHILD_USERNAME = "mgc_' in source
    assert "device_slots.claim(" in source
    assert "child_provisioning.prepare_child_ensure(" in source


def test_legacy_snapshot_serializes_sqlite_rows_without_raw_output():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(
        "CREATE TABLE user_devices(username,token,request_key,is_active,first_seen);"
        "CREATE TABLE hwid_lock(request_key,username,locked_at);"
        "CREATE TABLE stars_tariffs("
        "id,name,duration_days,stars_price,active,sort_order,created_at,updated_at);"
    )
    connection.execute(
        "INSERT INTO user_devices VALUES (?,?,?,?,?)",
        ("source", "legacy-token", "legacy-hwid", 1, 100),
    )
    connection.execute(
        "INSERT INTO hwid_lock VALUES (?,?,?)", ("legacy-hwid", "source", 100)
    )
    connection.execute(
        "INSERT INTO stars_tariffs VALUES (?,?,?,?,?,?,?,?)",
        (1, "Base", 30, 99, 1, 1, 100, 100),
    )

    class Client:
        def get_user(self, username, _sentinel):
            return {
                "username": username,
                "subscription_url": "https://example.invalid/sub/bearer",
                "expire": 0,
                "status": "active",
                "data_limit": None,
                "data_limit_reset_strategy": "no_reset",
                "proxies": {"vless": {"flow": "xtls-rprx-vision"}},
                "inbounds": {"vless": ["one"]},
            }

        def get_sub(self, _token, _headers):
            return b"vless://example", {}

    snapshot = _safe_legacy_snapshot(Client(), object(), connection, ["source"])
    assert snapshot["device_count"] == 1
    assert snapshot["hwid_lock_count"] == 1
    assert snapshot["stars_tariff_count"] == 1
    assert "legacy-token" not in repr(snapshot)
    assert "legacy-hwid" not in repr(snapshot)
