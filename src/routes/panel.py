import os
from mimetypes import guess_type
from urllib.parse import unquote

FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "frontend")
ASSETS_DIR = os.path.join(FRONTEND_DIR, "assets")


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


def handle_static_asset(handler, path):
    asset_name = unquote(path).replace("\\", "/")
    asset_path = os.path.abspath(os.path.join(ASSETS_DIR, asset_name))
    assets_root = os.path.abspath(ASSETS_DIR)

    if not asset_path.startswith(assets_root + os.sep):
        handler.send_response(403)
        handler.end_headers()
        return

    try:
        with open(asset_path, "rb") as f:
            body = f.read()
    except FileNotFoundError:
        handler.send_response(404)
        handler.end_headers()
        return

    content_type = guess_type(asset_path)[0] or "application/octet-stream"
    if asset_path.endswith(".js"):
        content_type = "text/javascript"
    elif asset_path.endswith(".css"):
        content_type = "text/css"

    handler.send_response(200)
    handler.send_header("Content-Type", f"{content_type}; charset=utf-8")
    handler.send_header("Cache-Control", "public, max-age=3600")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)
