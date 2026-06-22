import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


@pytest.fixture
def db():
    tmp = tempfile.mkdtemp()
    os.environ["DATA_DIR"] = tmp
    from importlib import reload
    import src.config as cfg
    reload(cfg)
    cfg.DATA_DIR = tmp
    import src.database as db_mod
    reload(db_mod)
    db_mod.DB_PATH = os.path.join(tmp, "db.sqlite3")
    instance = db_mod.Database()
    yield instance
    instance._conn.close()


# --- parse_sub_link ---

def test_parse_sub_link_http():
    from src.bot_support import parse_sub_link
    assert parse_sub_link("http://sub.beykus.fun/sub/abc123") == "abc123"


def test_parse_sub_link_https():
    from src.bot_support import parse_sub_link
    assert parse_sub_link("https://sub.beykus.fun/sub/TOKEN_XYZ") == "TOKEN_XYZ"


def test_parse_sub_link_old_domain():
    from src.bot_support import parse_sub_link
    assert parse_sub_link("https://mgboostmsk.ddns.net/sub/mytoken") == "mytoken"


def test_parse_sub_link_invalid():
    from src.bot_support import parse_sub_link
    assert parse_sub_link("not a link") is None


def test_parse_sub_link_empty():
    from src.bot_support import parse_sub_link
    assert parse_sub_link("") is None


def test_parse_sub_link_no_sub_path():
    from src.bot_support import parse_sub_link
    assert parse_sub_link("https://example.com/other/path") is None


# --- format_subscription ---

def test_format_subscription_active():
    from src.bot_support import format_subscription
    user = {
        "username": "testuser",
        "status": "active",
        "data_limit": 10 * 1024**3,
        "used_traffic": 3 * 1024**3,
        "expire": None,
    }
    text = format_subscription(user)
    assert "testuser" in text
    assert "active" in text.lower() or "активн" in text.lower()


def test_format_subscription_no_limit():
    from src.bot_support import format_subscription
    user = {
        "username": "unlimited",
        "status": "active",
        "data_limit": 0,
        "used_traffic": 1024,
        "expire": None,
    }
    text = format_subscription(user)
    assert "unlimited" in text or "безлим" in text.lower() or "∞" in text


def test_format_subscription_expired():
    from src.bot_support import format_subscription
    user = {
        "username": "user2",
        "status": "expired",
        "data_limit": 5 * 1024**3,
        "used_traffic": 5 * 1024**3,
        "expire": 1000000,
    }
    text = format_subscription(user)
    assert "user2" in text


# --- build_ai_messages ---

def test_build_ai_messages_empty_history():
    from src.bot_support import build_ai_messages
    msgs = build_ai_messages("Помоги", [], system="Ты помощник VPN")
    assert msgs[0]["role"] == "system"
    assert msgs[-1]["role"] == "user"
    assert msgs[-1]["content"] == "Помоги"


def test_build_ai_messages_with_history():
    from src.bot_support import build_ai_messages
    history = [
        {"role": "user", "text": "Привет"},
        {"role": "ai", "text": "Здравствуйте!"},
    ]
    msgs = build_ai_messages("Вопрос", history, system="sys")
    roles = [m["role"] for m in msgs]
    assert roles[0] == "system"
    assert "user" in roles
    assert "assistant" in roles


def test_build_ai_messages_ai_role_becomes_assistant():
    from src.bot_support import build_ai_messages
    history = [{"role": "ai", "text": "Привет"}]
    msgs = build_ai_messages("next", history, system="s")
    assistant_msgs = [m for m in msgs if m["role"] == "assistant"]
    assert len(assistant_msgs) == 1


def test_build_ai_messages_human_role_skipped():
    from src.bot_support import build_ai_messages
    history = [
        {"role": "human", "text": "оператор написал"},
        {"role": "user", "text": "Вопрос"},
    ]
    msgs = build_ai_messages("next", history, system="s")
    contents = [m["content"] for m in msgs]
    assert "оператор написал" not in contents


def test_build_ai_messages_limits_history():
    from src.bot_support import build_ai_messages
    history = [{"role": "user", "text": f"msg {i}"} for i in range(30)]
    msgs = build_ai_messages("latest", history, system="s")
    user_msgs = [m for m in msgs if m["role"] == "user" and m["content"] != "latest"]
    assert len(user_msgs) <= 10
