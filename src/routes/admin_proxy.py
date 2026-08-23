import json
import re
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, unquote, urlsplit

from ..http_utils import json_response, read_body
from ..marzban import MarzbanClient
from ..security import admin_session_cookie, get_admin_session_id, revoke_admin_session


_client = MarzbanClient()
_DATE_RE = re.compile(r"^[0-9T:Z+.-]{1,40}$")


def _error(handler, status: int, message: str, *, clear_session: bool = False):
    body = json.dumps({"error": message}, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    if clear_session:
        handler.send_header("Set-Cookie", admin_session_cookie("", clear=True))
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _single_query(query: dict[str, list[str]], name: str, default=""):
    values = query.get(name, [])
    if len(values) > 1:
        raise ValueError(f"Duplicate {name}")
    return values[0] if values else default


def _date_query(query, name):
    value = _single_query(query, name, "")
    if value and not _DATE_RE.fullmatch(value):
        raise ValueError(f"Invalid {name}")
    return value


def _json_body(handler):
    try:
        data = json.loads(read_body(handler) or b"{}")
    except json.JSONDecodeError as exc:
        raise ValueError("Invalid JSON") from exc
    if not isinstance(data, dict):
        raise ValueError("Expected JSON object")
    return data


def handle_admin_marzban_proxy(handler, proxy_path: str):
    """Allowlisted server-side broker for the operations used by admin.js.

    It intentionally is not an arbitrary URL proxy: method/path/query shapes
    are matched explicitly and the Marzban JWT stays in the server session.
    PH1-05 will further reduce operation/field privileges.
    """

    session = getattr(handler, "_admin_session", None)
    if session is None:
        _error(handler, 401, "Unauthorized")
        return

    method = handler.command.upper()
    path = unquote(proxy_path).strip("/")
    query = parse_qs(urlsplit(handler.path).query, keep_blank_values=True)
    token = session.marzban_token

    try:
        if method == "GET" and path == "system":
            result = _client.get_system(token)
        elif method == "GET" and path == "nodes":
            result = _client.get_nodes(token)
        elif method == "GET" and path == "nodes/usage":
            result = _client.get_nodes_usage(
                token, start=_date_query(query, "start"), end=_date_query(query, "end")
            )
        elif method == "GET" and path == "users":
            limit = int(_single_query(query, "limit", "100"))
            offset = int(_single_query(query, "offset", "0"))
            if not 1 <= limit <= 500 or offset < 0:
                raise ValueError("Invalid pagination")
            result = _client.get_users(token, limit=limit, offset=offset)
        elif method == "GET" and path == "users/usage":
            result = _client.get_users_usage(
                token, start=_date_query(query, "start"), end=_date_query(query, "end")
            )
        elif method == "GET" and path == "inbounds":
            result = _client.get_inbounds(token)
        elif method == "POST" and path == "user":
            result = _client.create_user(_json_body(handler), token)
        else:
            user_match = re.fullmatch(r"user/([^/]+)(?:/(usage|reset))?", path)
            node_match = re.fullmatch(r"node/(\d+)/reconnect", path)
            if user_match:
                username, operation = user_match.groups()
                if not username or len(username) > 128:
                    raise ValueError("Invalid username")
                if method == "GET" and operation is None:
                    result = _client.get_user(username, token)
                elif method == "GET" and operation == "usage":
                    result = _client.get_user_usage(
                        username,
                        token,
                        start=_date_query(query, "start"),
                        end=_date_query(query, "end"),
                    )
                elif method == "PUT" and operation is None:
                    result = _client.modify_user(username, _json_body(handler), token)
                elif method == "DELETE" and operation is None:
                    result = _client.delete_user(username, token)
                elif method == "POST" and operation == "reset":
                    result = _client.reset_user_traffic(username, token)
                else:
                    _error(handler, 405, "Operation not allowed")
                    return
            elif node_match and method == "POST":
                result = _client.reconnect_node(int(node_match.group(1)), token)
            else:
                _error(handler, 404, "Operation not found")
                return
    except (TypeError, ValueError):
        _error(handler, 400, "Invalid request")
        return
    except HTTPError as exc:
        if exc.code in (401, 403):
            revoke_admin_session(get_admin_session_id(handler))
            _error(handler, 401, "Admin session is no longer valid", clear_session=True)
        else:
            _error(handler, 502, "Marzban request failed")
        return
    except (URLError, TimeoutError):
        _error(handler, 502, "Marzban unavailable")
        return
    except Exception:
        _error(handler, 502, "Marzban request failed")
        return

    json_response(handler, 200, result)
