import json
import os
import sys
import tempfile
from http.server import BaseHTTPRequestHandler
from unittest.mock import MagicMock, patch

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


def _make_handler(db, method="GET", path="/", body=b"", bot_runner=None):
    handler = MagicMock(spec=BaseHTTPRequestHandler)
    server = MagicMock()
    server.db = db
    server.bot_runner = bot_runner
    handler.server = server
    handler.path = path
    handler.command = method
    headers = MagicMock()
    headers.get = MagicMock(return_value=str(len(body)))
    handler.headers = headers
    handler.rfile = MagicMock()
    handler.rfile.read = MagicMock(return_value=body)
    handler.wfile = MagicMock()
    responses = []
    handler.send_response = MagicMock(side_effect=lambda code: responses.append({"code": code}))
    handler.send_header = MagicMock()
    handler.end_headers = MagicMock()
    sent_data = []
    handler.wfile.write = MagicMock(side_effect=lambda d: sent_data.append(d))
    handler._responses = responses
    handler._sent_data = sent_data
    return handler


def _get_json(handler):
    data = b"".join(handler._sent_data)
    return json.loads(data)


# --- GET /admin/tickets ---

def test_list_tickets_empty(db):
    from src.routes.admin import handle_tickets_list
    h = _make_handler(db, path="/admin/tickets")
    handle_tickets_list(h)
    assert h._responses[0]["code"] == 200
    result = _get_json(h)
    assert result == []


def test_list_tickets_returns_created(db):
    from src.routes.admin import handle_tickets_list
    db.create_ticket(111, marzban_username="alice", status="open")
    db.create_ticket(222, status="waiting_human")
    h = _make_handler(db, path="/admin/tickets")
    handle_tickets_list(h)
    result = _get_json(h)
    assert len(result) == 2


def test_list_tickets_filter_by_status(db):
    from src.routes.admin import handle_tickets_list
    db.create_ticket(111, status="open")
    db.create_ticket(222, status="closed")
    h = _make_handler(db, path="/admin/tickets?status=open")
    h.path = "/admin/tickets?status=open"
    handle_tickets_list(h, status="open")
    result = _get_json(h)
    assert len(result) == 1
    assert result[0]["status"] == "open"


# --- GET /admin/tickets/{id} ---

def test_get_ticket_detail(db):
    from src.routes.admin import handle_ticket_detail
    tid = db.create_ticket(111, marzban_username="bob", status="open")
    db.add_ticket_message(tid, "user", "Вопрос")
    db.add_ticket_message(tid, "ai", "Ответ")
    h = _make_handler(db, path=f"/admin/tickets/{tid}")
    handle_ticket_detail(h, tid)
    result = _get_json(h)
    assert result["ticket"]["id"] == tid
    assert len(result["messages"]) == 2


def test_get_ticket_not_found(db):
    from src.routes.admin import handle_ticket_detail
    h = _make_handler(db, path="/admin/tickets/9999")
    handle_ticket_detail(h, 9999)
    assert h._responses[0]["code"] == 404


# --- POST /admin/tickets/{id}/close ---

def test_close_ticket(db):
    from src.routes.admin import handle_ticket_close
    tid = db.create_ticket(111, status="open")
    h = _make_handler(db, method="POST", path=f"/admin/tickets/{tid}/close")
    with patch("src.routes.admin._send_ticket_notification") as mock_notify:
        handle_ticket_close(h, tid)
    assert h._responses[0]["code"] == 200
    assert db.get_ticket(tid)["status"] == "closed"


def test_close_nonexistent_ticket(db):
    from src.routes.admin import handle_ticket_close
    h = _make_handler(db, method="POST", path="/admin/tickets/9999/close")
    with patch("src.routes.admin._send_ticket_notification"):
        handle_ticket_close(h, 9999)
    assert h._responses[0]["code"] == 404


# --- POST /admin/tickets/{id}/reply ---

def test_reply_to_ticket(db):
    from src.routes.admin import handle_ticket_reply
    tid = db.create_ticket(111, status="waiting_human")
    body = json.dumps({"text": "Привет от оператора"}).encode()
    h = _make_handler(db, method="POST", body=body)
    with patch("src.routes.admin._send_ticket_notification") as mock_notify:
        handle_ticket_reply(h, tid)
    assert h._responses[0]["code"] == 200
    msgs = db.get_ticket_messages(tid)
    assert len(msgs) == 1
    assert msgs[0]["role"] == "human"
    assert msgs[0]["text"] == "Привет от оператора"


def test_reply_missing_text(db):
    from src.routes.admin import handle_ticket_reply
    tid = db.create_ticket(111)
    body = json.dumps({}).encode()
    h = _make_handler(db, method="POST", body=body)
    with patch("src.routes.admin._send_ticket_notification"):
        handle_ticket_reply(h, tid)
    assert h._responses[0]["code"] == 400
