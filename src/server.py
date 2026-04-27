import re
from http.server import BaseHTTPRequestHandler, HTTPServer

from .http_utils import error_response
from .routes.admin import (
    handle_admin_remove_device,
    handle_admin_set_device_limit,
    handle_admin_user_device_counts,
    handle_admin_user_devices,
    handle_configs_add,
    handle_configs_delete,
    handle_configs_list,
    handle_configs_reorder,
    handle_node_filters_list,
    handle_node_filters_save,
    handle_per_user_list,
    handle_per_user_save,
    handle_settings_get,
    handle_settings_save,
    handle_stats_get,
    handle_stats_update,
)
from .routes.internal import (
    handle_internal_configs_add,
    handle_internal_configs_delete,
    handle_internal_configs_list,
    handle_internal_configs_reorder,
    handle_internal_inbounds,
    handle_internal_node_filters_list,
    handle_internal_node_filters_save,
    handle_internal_per_user_list,
    handle_internal_per_user_save,
    handle_internal_settings_get,
    handle_internal_settings_save,
    handle_internal_status,
    handle_internal_user_create,
    handle_internal_user_delete,
    handle_internal_user_detail,
    handle_internal_user_renew,
    handle_internal_users_list,
)
from .routes.lk import (
    handle_lk_device_delete,
    handle_lk_device_rename,
    handle_lk_devices,
    handle_lk_info,
    handle_lk_page,
    handle_lk_usage,
)
from .routes.panel import handle_panel, handle_static_asset
from .routes.sub import handle_sub
from .security import require_admin_auth, require_internal_auth

# (method, regex_pattern) -> handler(request_handler, **groups)
_ROUTES = [
    ("GET",    re.compile(r"^/lk/$"),                            lambda h: handle_lk_page(h)),
    ("GET",    re.compile(r"^/lk/api/info$"),                   lambda h: handle_lk_info(h)),
    ("GET",    re.compile(r"^/lk/api/usage$"),                  lambda h: handle_lk_usage(h)),
    ("GET",    re.compile(r"^/lk/api/devices$"),                lambda h: handle_lk_devices(h)),
    ("DELETE", re.compile(r"^/lk/api/devices/(?P<device_id>\d+)$"), lambda h, device_id: handle_lk_device_delete(h, device_id)),
    ("PATCH",  re.compile(r"^/lk/api/devices/(?P<device_id>\d+)$"), lambda h, device_id: handle_lk_device_rename(h, device_id)),
    ("GET",    re.compile(r"^/(?:.*?/)?assets/(?P<path>.+)$"),  lambda h, path: handle_static_asset(h, path)),
    ("GET",    re.compile(r"^/sub/(?P<token>[^/]+)$"),         lambda h, token: handle_sub(h, token)),
    ("GET",    re.compile(r"^/admin/configs$"),                 lambda h: handle_configs_list(h) if require_admin_auth(h) else None),
    ("POST",   re.compile(r"^/admin/configs$"),                 lambda h: handle_configs_add(h) if require_admin_auth(h) else None),
    ("DELETE", re.compile(r"^/admin/configs/(?P<cid>\d+)$"),   lambda h, cid: handle_configs_delete(h, cid) if require_admin_auth(h) else None),
    ("POST",   re.compile(r"^/admin/configs/reorder$"),         lambda h: handle_configs_reorder(h) if require_admin_auth(h) else None),
    ("GET",    re.compile(r"^/admin/stats$"),                   lambda h: handle_stats_get(h) if require_admin_auth(h) else None),
    ("POST",   re.compile(r"^/admin/stats$"),                   lambda h: handle_stats_update(h) if require_admin_auth(h) else None),
    ("GET",    re.compile(r"^/admin/per-user-configs$"),        lambda h: handle_per_user_list(h) if require_admin_auth(h) else None),
    ("POST",   re.compile(r"^/admin/per-user-configs$"),        lambda h: handle_per_user_save(h) if require_admin_auth(h) else None),
    ("GET",    re.compile(r"^/admin/node-filters$"),            lambda h: handle_node_filters_list(h) if require_admin_auth(h) else None),
    ("POST",   re.compile(r"^/admin/node-filters$"),            lambda h: handle_node_filters_save(h) if require_admin_auth(h) else None),
    ("GET",    re.compile(r"^/admin/settings$"),                lambda h: handle_settings_get(h) if require_admin_auth(h) else None),
    ("POST",   re.compile(r"^/admin/settings$"),                lambda h: handle_settings_save(h) if require_admin_auth(h) else None),
    ("GET",    re.compile(r"^/admin/user-devices/(?P<username>[^/]+)$"), lambda h, username: handle_admin_user_devices(h, username) if require_admin_auth(h) else None),
    ("POST",   re.compile(r"^/admin/user-devices-counts$"),      lambda h: handle_admin_user_device_counts(h) if require_admin_auth(h) else None),
    ("POST",   re.compile(r"^/admin/user-devices/(?P<username>[^/]+)/limit$"), lambda h, username: handle_admin_set_device_limit(h, username) if require_admin_auth(h) else None),
    ("DELETE", re.compile(r"^/admin/user-devices/device/(?P<device_id>\d+)$"), lambda h, device_id: handle_admin_remove_device(h, device_id) if require_admin_auth(h) else None),
    ("GET",    re.compile(r"^/internal/v1/status$"),            lambda h: handle_internal_status(h) if require_internal_auth(h) else None),
    ("GET",    re.compile(r"^/internal/v1/inbounds$"),          lambda h: handle_internal_inbounds(h) if require_internal_auth(h) else None),
    ("GET",    re.compile(r"^/internal/v1/users$"),             lambda h: handle_internal_users_list(h) if require_internal_auth(h) else None),
    ("POST",   re.compile(r"^/internal/v1/users$"),             lambda h: handle_internal_user_create(h) if require_internal_auth(h) else None),
    ("GET",    re.compile(r"^/internal/v1/users/(?P<username>[^/]+)$"), lambda h, username: handle_internal_user_detail(h, username) if require_internal_auth(h) else None),
    ("POST",   re.compile(r"^/internal/v1/users/(?P<username>[^/]+)/renew$"), lambda h, username: handle_internal_user_renew(h, username) if require_internal_auth(h) else None),
    ("DELETE", re.compile(r"^/internal/v1/users/(?P<username>[^/]+)$"), lambda h, username: handle_internal_user_delete(h, username) if require_internal_auth(h) else None),
    ("GET",    re.compile(r"^/internal/v1/configs$"),           lambda h: handle_internal_configs_list(h) if require_internal_auth(h) else None),
    ("POST",   re.compile(r"^/internal/v1/configs$"),           lambda h: handle_internal_configs_add(h) if require_internal_auth(h) else None),
    ("DELETE", re.compile(r"^/internal/v1/configs/(?P<config_id>\d+)$"), lambda h, config_id: handle_internal_configs_delete(h, config_id) if require_internal_auth(h) else None),
    ("POST",   re.compile(r"^/internal/v1/configs/reorder$"),   lambda h: handle_internal_configs_reorder(h) if require_internal_auth(h) else None),
    ("GET",    re.compile(r"^/internal/v1/per-user-configs$"),  lambda h: handle_internal_per_user_list(h) if require_internal_auth(h) else None),
    ("POST",   re.compile(r"^/internal/v1/per-user-configs$"),  lambda h: handle_internal_per_user_save(h) if require_internal_auth(h) else None),
    ("GET",    re.compile(r"^/internal/v1/node-filters$"),      lambda h: handle_internal_node_filters_list(h) if require_internal_auth(h) else None),
    ("POST",   re.compile(r"^/internal/v1/node-filters$"),      lambda h: handle_internal_node_filters_save(h) if require_internal_auth(h) else None),
    ("GET",    re.compile(r"^/internal/v1/settings$"),          lambda h: handle_internal_settings_get(h) if require_internal_auth(h) else None),
    ("POST",   re.compile(r"^/internal/v1/settings$"),          lambda h: handle_internal_settings_save(h) if require_internal_auth(h) else None),
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
                try:
                    handler(self, **m.groupdict())
                except Exception as exc:
                    print(f"[Server] Unhandled error on {method} {self.path}: {exc}")
                    error_response(self, 500, "Internal server error")
                return
        self.send_response(404)
        self.end_headers()

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def do_PATCH(self):
        self._dispatch("PATCH")


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
