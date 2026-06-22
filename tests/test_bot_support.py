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


# --- tool definitions ---

def test_get_tools_returns_list():
    from src.bot_support import get_tools
    tools = get_tools()
    assert isinstance(tools, list)
    assert len(tools) >= 4


def test_tools_have_required_fields():
    from src.bot_support import get_tools
    for tool in get_tools():
        assert tool["type"] == "function"
        assert "function" in tool
        fn = tool["function"]
        assert "name" in fn
        assert "description" in fn
        assert "parameters" in fn


def test_tool_names():
    from src.bot_support import get_tools
    names = {t["function"]["name"] for t in get_tools()}
    assert "get_subscription_info" in names
    assert "get_nodes_status" in names
    assert "get_ticket_history" in names
    assert "escalate_to_human" in names


# --- execute_tool ---

def test_execute_get_nodes_status(db):
    from src.bot_support import execute_tool
    import asyncio

    states = {1: {"up": True, "last_check": None}, 2: {"up": False, "last_check": None}}
    names = {1: "Estonia", 2: "Selectel"}

    result = asyncio.get_event_loop().run_until_complete(
        execute_tool("get_nodes_status", {}, db=db, marzban=None,
                     telegram_id=111, node_states=states, node_names=names)
    )
    assert "Estonia" in result
    assert "Selectel" in result
    assert "🟢" in result
    assert "🔴" in result


def test_execute_get_ticket_history_empty(db):
    from src.bot_support import execute_tool
    import asyncio

    result = asyncio.get_event_loop().run_until_complete(
        execute_tool("get_ticket_history", {}, db=db, marzban=None,
                     telegram_id=999, node_states={}, node_names={})
    )
    assert "нет" in result.lower() or "пуст" in result.lower() or "история" in result.lower()


def test_execute_get_ticket_history_with_data(db):
    from src.bot_support import execute_tool
    import asyncio

    t1 = db.create_ticket(111, status="closed")
    db.add_ticket_message(t1, "user", "Вопрос 1")
    db.add_ticket_message(t1, "ai", "Ответ 1")
    db.update_ticket_status(t1, "closed")

    result = asyncio.get_event_loop().run_until_complete(
        execute_tool("get_ticket_history", {}, db=db, marzban=None,
                     telegram_id=111, node_states={}, node_names={})
    )
    assert "Вопрос 1" in result or "тикет" in result.lower()


def test_execute_escalate_to_human(db):
    from src.bot_support import execute_tool
    import asyncio

    result = asyncio.get_event_loop().run_until_complete(
        execute_tool("escalate_to_human", {"reason": "Не могу решить проблему"},
                     db=db, marzban=None, telegram_id=111, node_states={}, node_names={})
    )
    assert isinstance(result, str)
    ticket = db.get_open_ticket(111)
    assert ticket is not None
    assert ticket["status"] == "waiting_human"


# --- ask_openrouter_with_tools ---

def test_ask_openrouter_with_tools_no_key(db):
    from src.bot_support import ask_openrouter_with_tools
    import asyncio

    result = asyncio.get_event_loop().run_until_complete(
        ask_openrouter_with_tools(
            api_key="", model="openai/gpt-4o-mini",
            messages=[{"role": "user", "content": "test"}],
            tools=[], db=db, marzban=None,
            telegram_id=111, node_states={}, node_names={}
        )
    )
    assert isinstance(result, str)
