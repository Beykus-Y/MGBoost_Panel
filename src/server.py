import re
from http.server import BaseHTTPRequestHandler, HTTPServer

from .routes.admin import (
    handle_configs_add,
    handle_configs_delete,
    handle_configs_list,
    handle_configs_reorder,
    handle_node_filters_list,
    handle_node_filters_save,
    handle_per_user_list,
    handle_per_user_save,
    handle_stats_get,
    handle_stats_update,
)
from .routes.lk import handle_lk_devices, handle_lk_info, handle_lk_page, handle_lk_usage
from .routes.panel import handle_panel
from .routes.sub import handle_sub

# (method, regex_pattern) -> handler(request_handler, **groups)
_ROUTES = [
    ("GET",    re.compile(r"^/lk/$"),                            lambda h: handle_lk_page(h)),
    ("GET",    re.compile(r"^/lk/api/info$"),                   lambda h: handle_lk_info(h)),
    ("GET",    re.compile(r"^/lk/api/usage$"),                  lambda h: handle_lk_usage(h)),
    ("GET",    re.compile(r"^/lk/api/devices$"),                lambda h: handle_lk_devices(h)),
    ("GET",    re.compile(r"^/sub/(?P<token>[^/]+)$"),         lambda h, token: handle_sub(h, token)),
    ("GET",    re.compile(r"^/admin/configs$"),                 lambda h: handle_configs_list(h)),
    ("POST",   re.compile(r"^/admin/configs$"),                 lambda h: handle_configs_add(h)),
    ("DELETE", re.compile(r"^/admin/configs/(?P<cid>\d+)$"),   lambda h, cid: handle_configs_delete(h, cid)),
    ("POST",   re.compile(r"^/admin/configs/reorder$"),         lambda h: handle_configs_reorder(h)),
    ("GET",    re.compile(r"^/admin/stats$"),                   lambda h: handle_stats_get(h)),
    ("POST",   re.compile(r"^/admin/stats$"),                   lambda h: handle_stats_update(h)),
    ("GET",    re.compile(r"^/admin/per-user-configs$"),        lambda h: handle_per_user_list(h)),
    ("POST",   re.compile(r"^/admin/per-user-configs$"),        lambda h: handle_per_user_save(h)),
    ("GET",    re.compile(r"^/admin/node-filters$"),            lambda h: handle_node_filters_list(h)),
    ("POST",   re.compile(r"^/admin/node-filters$"),            lambda h: handle_node_filters_save(h)),
    # SPA catch-all: serve frontend for any path not matched above
    ("GET",    re.compile(r"^/.*$"),                            lambda h: handle_panel(h)),
]


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        print(f"[Server] {self.address_string()} - {format % args}")

    def _dispatch(self, method):
        path = self.path.split("?", 1)[0]
        for route_method, pattern, handler in _ROUTES:
            if route_method != method:
                continue
            m = pattern.match(path)
            if m:
                handler(self, **m.groupdict())
                return
        self.send_response(404)
        self.end_headers()

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_DELETE(self):
        self._dispatch("DELETE")


class _ServerWithDB(HTTPServer):
    def __init__(self, address, handler_cls, db):
        super().__init__(address, handler_cls)
        self.db = db


class Server:
    def __init__(self, db):
        self._db = db

    def run(self, host, port):
        server = _ServerWithDB((host, port), _Handler, self._db)
        print(f"[Server] Listening on {host}:{port}")
        server.serve_forever()
