import os

FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "frontend")


def handle_panel(handler):
    index_path = os.path.abspath(os.path.join(FRONTEND_DIR, "index.html"))
    try:
        with open(index_path, "rb") as f:
            body = f.read()
    except FileNotFoundError:
        handler.send_response(404)
        handler.end_headers()
        return
    handler.send_response(200)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)
