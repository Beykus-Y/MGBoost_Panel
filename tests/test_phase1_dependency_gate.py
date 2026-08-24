import base64
import importlib.util
import json
import sqlite3
from pathlib import Path


ROOT = Path(__file__).parents[1]


def load_script(name):
    path = ROOT / "scripts" / name
    spec = importlib.util.spec_from_file_location(name.removesuffix(".py"), path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def test_masked_snapshot_canonicalizes_only_vpn_links():
    module = load_script("capture_phase1_masked_state.py")
    payload = "\n".join(
        [
            "INFO NODE",
            (
                "vless://00000000-0000-0000-0000-000000000000@"
                "127.0.0.1:1?type=tcp#dynamic-description"
            ),
            "vless://synthetic-id@example.test:443?security=reality&sid=first#A",
            "https://not-a-vpn-link.example/",
        ]
    ).encode()
    encoded = base64.b64encode(payload)
    links = module.canonical_config(encoded)
    assert links == [
        (
            "vless://synthetic-id@example.test:443?security=reality&"
            "sid=<DYNAMIC-SID>#A"
        )
    ]

    second = base64.b64encode(
        b"vless://synthetic-id@example.test:443?security=reality&sid=second#A"
    )
    assert module.canonical_config(second) == links


def test_local_masked_snapshot_emits_no_device_or_hwid_values(tmp_path):
    module = load_script("capture_phase1_masked_state.py")
    db_path = tmp_path / "db.sqlite3"
    connection = sqlite3.connect(db_path)
    connection.executescript(
        """
        CREATE TABLE user_devices (
            username TEXT, token TEXT, request_key TEXT,
            is_active INTEGER, first_seen INTEGER
        );
        CREATE TABLE hwid_lock (
            request_key TEXT, username TEXT, locked_at INTEGER
        );
        """
    )
    values = ("secret-user", "sha256:secret-token", "secret-hwid")
    connection.execute(
        "INSERT INTO user_devices VALUES (?, ?, ?, 1, 123)", values
    )
    connection.execute(
        "INSERT INTO hwid_lock VALUES (?, ?, 123)", (values[2], values[0])
    )
    connection.commit()
    connection.close()

    snapshot = module.capture_local(db_path)
    rendered = json.dumps(snapshot)
    assert snapshot["device_count"] == 1
    assert snapshot["hwid_lock_count"] == 1
    assert all(value not in rendered for value in values)


def test_runtime_integration_settings_reader_is_read_only(tmp_path):
    module = load_script("verify_runtime_integrations.py")
    db_path = tmp_path / "db.sqlite3"
    connection = sqlite3.connect(db_path)
    connection.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)")
    connection.execute("INSERT INTO settings VALUES ('bot:token', 'synthetic-secret')")
    connection.commit()
    connection.close()

    before = db_path.read_bytes()
    assert module.read_settings(db_path)["bot:token"] == "synthetic-secret"
    assert db_path.read_bytes() == before
