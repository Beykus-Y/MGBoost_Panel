import json
import ipaddress


def read_body(handler) -> bytes:
    cached = getattr(handler, "_cached_body", None)
    if cached is not None:
        return cached

    length = int(handler.headers.get("Content-Length", 0) or 0)
    body = handler.rfile.read(length) if length > 0 else b""
    handler._cached_body = body
    return body


def json_response(handler, status: int, data):
    body = json.dumps(data, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def error_response(handler, status: int, message: str, *, details: dict | None = None):
    payload = {"error": message}
    if details:
        payload["details"] = details
    json_response(handler, status, payload)


def client_ip(handler) -> str:
    """Return a validated client IP, trusting X-Real-IP only from loopback nginx."""
    try:
        peer = str(handler.client_address[0])
        peer_address = ipaddress.ip_address(peer)
    except (AttributeError, IndexError, TypeError, ValueError):
        return "0.0.0.0"
    if peer_address.is_loopback:
        forwarded = (handler.headers.get("X-Real-IP") or "").strip()
        try:
            return str(ipaddress.ip_address(forwarded)) if forwarded else peer
        except ValueError:
            return peer
    return peer
