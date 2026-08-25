import re
from http.server import BaseHTTPRequestHandler, HTTPServer

from .http_utils import DEFAULT_SECURITY_HEADERS, error_response
from .routes.admin import (
    handle_admin_remove_device,
    handle_admin_set_device_limit,
    handle_admin_user_device_counts,
    handle_admin_user_devices,
    handle_bot_restart,
    handle_bot_settings_get,
    handle_bot_settings_save,
    handle_configs_add,
    handle_configs_delete,
    handle_configs_list,
    handle_configs_reorder,
    handle_node_filters_list,
    handle_node_filters_save,
    handle_node_settings_get,
    handle_node_settings_save,
    handle_per_user_list,
    handle_per_user_save,
    handle_settings_get,
    handle_settings_save,
    handle_stars_payment_confirm_applied,
    handle_stars_payment_recheck,
    handle_stars_payment_reconcile_refund,
    handle_stars_payment_refund,
    handle_stars_payment_requeue,
    handle_stars_payments_list,
    handle_stars_orphan_payment_reconcile_refund,
    handle_stars_orphan_payment_refund,
    handle_stars_orphan_payments_list,
    handle_stars_settings_get,
    handle_stars_settings_save,
    handle_stars_tariffs_delete,
    handle_stars_tariffs_list,
    handle_stars_tariffs_reorder,
    handle_stars_tariffs_save,
    handle_stars_tariffs_toggle,
    handle_stats_get,
    handle_stats_update,
    handle_ticket_close,
    handle_ticket_detail,
    handle_ticket_reply,
    handle_tickets_list,
)
from .routes.internal import (
    handle_internal_configs_add,
    handle_internal_configs_delete,
    handle_internal_configs_list,
    handle_internal_configs_reorder,
    handle_internal_inbounds,
    handle_internal_node_filters_list,
    handle_internal_node_filters_save,
    handle_internal_node_settings_get,
    handle_internal_node_settings_save,
    handle_internal_nodes_usage,
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
from .routes.admin_proxy import handle_admin_marzban_proxy
from .routes.admin_session import (
    handle_admin_session_login,
    handle_admin_session_logout,
    handle_admin_session_rotate,
    handle_admin_session_status,
)
from .routes.lk import (
    handle_lk_device_delete,
    handle_lk_device_rename,
    handle_lk_devices,
    handle_lk_info,
    handle_lk_mgmt_exchange,
    handle_lk_page,
    handle_lk_usage,
)
from .routes.opaque_sub import handle_opaque_sub
from .routes.panel import handle_panel, handle_static_asset
from .routes.sub import handle_sub
from .security import require_admin_auth, require_internal_auth
from .sensitive import redact_request_target

# (method, regex_pattern) -> handler(request_handler, **groups)
_ROUTES = [
    ("GET",    re.compile(r"^/(?:docs|redoc|openapi\.json|debug|version)/?$"), lambda h: error_response(h, 404, "Not found")),
    ("GET",    re.compile(r"^/lk/$"),                            lambda h: handle_lk_page(h)),
    ("GET",    re.compile(r"^/lk/api/info$"),                   lambda h: handle_lk_info(h)),
    ("GET",    re.compile(r"^/lk/api/usage$"),                  lambda h: handle_lk_usage(h)),
    ("GET",    re.compile(r"^/lk/api/devices$"),                lambda h: handle_lk_devices(h)),
    ("POST",   re.compile(r"^/lk/api/mgmt/exchange$"),           lambda h: handle_lk_mgmt_exchange(h)),
    ("DELETE", re.compile(r"^/lk/api/devices/(?P<device_id>\d+)$"), lambda h, device_id: handle_lk_device_delete(h, device_id)),
    ("PATCH",  re.compile(r"^/lk/api/devices/(?P<device_id>\d+)$"), lambda h, device_id: handle_lk_device_rename(h, device_id)),
    ("GET",    re.compile(r"^/(?:.*?/)?assets/(?P<path>.+)$"),  lambda h, path: handle_static_asset(h, path)),
    ("GET",    re.compile(r"^/sub/(?P<token>[^/]+)$"),         lambda h, token: handle_sub(h, token)),
    ("POST",   re.compile(r"^/admin/session/login$"),          lambda h: handle_admin_session_login(h)),
    ("GET",    re.compile(r"^/admin/session$"),                lambda h: handle_admin_session_status(h)),
    ("POST",   re.compile(r"^/admin/session/logout$"),         lambda h: handle_admin_session_logout(h) if require_admin_auth(h) else None),
    ("POST",   re.compile(r"^/admin/session/rotate$"),         lambda h: handle_admin_session_rotate(h) if require_admin_auth(h) else None),
    ("GET",    re.compile(r"^/admin/marzban/(?P<proxy_path>.+)$"), lambda h, proxy_path: handle_admin_marzban_proxy(h, proxy_path) if require_admin_auth(h) else None),
    ("POST",   re.compile(r"^/admin/marzban/(?P<proxy_path>.+)$"), lambda h, proxy_path: handle_admin_marzban_proxy(h, proxy_path) if require_admin_auth(h) else None),
    ("PUT",    re.compile(r"^/admin/marzban/(?P<proxy_path>.+)$"), lambda h, proxy_path: handle_admin_marzban_proxy(h, proxy_path) if require_admin_auth(h) else None),
    ("DELETE", re.compile(r"^/admin/marzban/(?P<proxy_path>.+)$"), lambda h, proxy_path: handle_admin_marzban_proxy(h, proxy_path) if require_admin_auth(h) else None),
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
    ("GET",    re.compile(r"^/admin/node-settings$"),           lambda h: handle_node_settings_get(h) if require_admin_auth(h) else None),
    ("POST",   re.compile(r"^/admin/node-settings$"),           lambda h: handle_node_settings_save(h) if require_admin_auth(h) else None),
    ("GET",    re.compile(r"^/admin/bot-settings$"),            lambda h: handle_bot_settings_get(h) if require_admin_auth(h) else None),
    ("POST",   re.compile(r"^/admin/bot-settings$"),            lambda h: handle_bot_settings_save(h) if require_admin_auth(h) else None),
    ("POST",   re.compile(r"^/admin/bot-restart$"),             lambda h: handle_bot_restart(h) if require_admin_auth(h) else None),
    ("GET",    re.compile(r"^/admin/tickets$"),                 lambda h: handle_tickets_list(h, status=h.path.split("status=")[1] if "status=" in h.path else None) if require_admin_auth(h) else None),
    ("GET",    re.compile(r"^/admin/tickets/(?P<tid>\d+)$"),    lambda h, tid: handle_ticket_detail(h, tid) if require_admin_auth(h) else None),
    ("POST",   re.compile(r"^/admin/tickets/(?P<tid>\d+)/reply$"), lambda h, tid: handle_ticket_reply(h, tid) if require_admin_auth(h) else None),
    ("POST",   re.compile(r"^/admin/tickets/(?P<tid>\d+)/close$"), lambda h, tid: handle_ticket_close(h, tid) if require_admin_auth(h) else None),
    ("GET",    re.compile(r"^/admin/settings$"),                lambda h: handle_settings_get(h) if require_admin_auth(h) else None),
    ("POST",   re.compile(r"^/admin/settings$"),                lambda h: handle_settings_save(h) if require_admin_auth(h) else None),
    ("GET",    re.compile(r"^/admin/user-devices/(?P<username>[^/]+)$"), lambda h, username: handle_admin_user_devices(h, username) if require_admin_auth(h) else None),
    ("POST",   re.compile(r"^/admin/user-devices-counts$"),      lambda h: handle_admin_user_device_counts(h) if require_admin_auth(h) else None),
    ("POST",   re.compile(r"^/admin/user-devices/(?P<username>[^/]+)/limit$"), lambda h, username: handle_admin_set_device_limit(h, username) if require_admin_auth(h) else None),
    ("DELETE", re.compile(r"^/admin/user-devices/device/(?P<device_id>\d+)$"), lambda h, device_id: handle_admin_remove_device(h, device_id) if require_admin_auth(h) else None),
    ("GET",    re.compile(r"^/admin/stars-tariffs$"),            lambda h: handle_stars_tariffs_list(h) if require_admin_auth(h) else None),
    ("POST",   re.compile(r"^/admin/stars-tariffs$"),            lambda h: handle_stars_tariffs_save(h) if require_admin_auth(h) else None),
    ("DELETE", re.compile(r"^/admin/stars-tariffs/(?P<tariff_id>\d+)$"), lambda h, tariff_id: handle_stars_tariffs_delete(h, tariff_id) if require_admin_auth(h) else None),
    ("POST",   re.compile(r"^/admin/stars-tariffs/(?P<tariff_id>\d+)/toggle$"), lambda h, tariff_id: handle_stars_tariffs_toggle(h, tariff_id) if require_admin_auth(h) else None),
    ("POST",   re.compile(r"^/admin/stars-tariffs/reorder$"),    lambda h: handle_stars_tariffs_reorder(h) if require_admin_auth(h) else None),
    ("GET",    re.compile(r"^/admin/stars-settings$"),           lambda h: handle_stars_settings_get(h) if require_admin_auth(h) else None),
    ("POST",   re.compile(r"^/admin/stars-settings$"),           lambda h: handle_stars_settings_save(h) if require_admin_auth(h) else None),
    ("GET",    re.compile(r"^/admin/stars-payments$"),           lambda h: handle_stars_payments_list(h) if require_admin_auth(h) else None),
    ("POST",   re.compile(r"^/admin/stars-payments/(?P<payment_id>\d+)/recheck$"), lambda h, payment_id: handle_stars_payment_recheck(h, payment_id) if require_admin_auth(h) else None),
    ("POST",   re.compile(r"^/admin/stars-payments/(?P<payment_id>\d+)/confirm-applied$"), lambda h, payment_id: handle_stars_payment_confirm_applied(h, payment_id) if require_admin_auth(h) else None),
    ("POST",   re.compile(r"^/admin/stars-payments/(?P<payment_id>\d+)/requeue$"), lambda h, payment_id: handle_stars_payment_requeue(h, payment_id) if require_admin_auth(h) else None),
    ("POST",   re.compile(r"^/admin/stars-payments/(?P<payment_id>\d+)/refund$"), lambda h, payment_id: handle_stars_payment_refund(h, payment_id) if require_admin_auth(h) else None),
    ("POST",   re.compile(r"^/admin/stars-payments/(?P<payment_id>\d+)/reconcile-refund$"), lambda h, payment_id: handle_stars_payment_reconcile_refund(h, payment_id) if require_admin_auth(h) else None),
    ("GET",    re.compile(r"^/admin/stars-orphan-payments$"),    lambda h: handle_stars_orphan_payments_list(h) if require_admin_auth(h) else None),
    ("POST",   re.compile(r"^/admin/stars-orphan-payments/(?P<payment_id>\d+)/refund$"), lambda h, payment_id: handle_stars_orphan_payment_refund(h, payment_id) if require_admin_auth(h) else None),
    ("POST",   re.compile(r"^/admin/stars-orphan-payments/(?P<payment_id>\d+)/reconcile-refund$"), lambda h, payment_id: handle_stars_orphan_payment_reconcile_refund(h, payment_id) if require_admin_auth(h) else None),
    ("GET",    re.compile(r"^/internal/v1/status$"),            lambda h: handle_internal_status(h) if require_internal_auth(h) else None),
    ("GET",    re.compile(r"^/internal/v1/inbounds$"),          lambda h: handle_internal_inbounds(h) if require_internal_auth(h) else None),
    ("GET",    re.compile(r"^/internal/v1/nodes/usage$"),       lambda h: handle_internal_nodes_usage(h) if require_internal_auth(h) else None),
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
    ("GET",    re.compile(r"^/internal/v1/node-settings$"),     lambda h: handle_internal_node_settings_get(h) if require_internal_auth(h) else None),
    ("POST",   re.compile(r"^/internal/v1/node-settings$"),     lambda h: handle_internal_node_settings_save(h) if require_internal_auth(h) else None),
    ("GET",    re.compile(r"^/internal/v1/settings$"),          lambda h: handle_internal_settings_get(h) if require_internal_auth(h) else None),
    ("POST",   re.compile(r"^/internal/v1/settings$"),          lambda h: handle_internal_settings_save(h) if require_internal_auth(h) else None),
    # PH2-01: exact opaque-token root route, matched only after every
    # reserved application path above and before the SPA catch-all below.
    ("GET",    re.compile(r"^/(?P<token>[A-Za-z0-9_-]{43})$"),  lambda h, token: handle_opaque_sub(h, token)),
    # SPA catch-all: serve frontend for any path not matched above
    ("GET",    re.compile(r"^/.*$"),                            lambda h: handle_panel(h)),
]


class _Handler(BaseHTTPRequestHandler):
    # Do not expose the stdlib/Python patch version in every response.
    server_version = "MGBoost"
    sys_version = ""

    def version_string(self):
        return self.server_version

    def send_response(self, code, message=None):
        self._sent_header_names = set()
        super().send_response(code, message)

    def send_header(self, keyword, value):
        sent = getattr(self, "_sent_header_names", None)
        if sent is None:
            sent = self._sent_header_names = set()
        sent.add(keyword.lower())
        super().send_header(keyword, value)

    def end_headers(self):
        sent = getattr(self, "_sent_header_names", set())
        for name, value in DEFAULT_SECURITY_HEADERS.items():
            if name.lower() not in sent:
                self.send_header(name, value)
        super().end_headers()

    def log_message(self, format, *args):
        status = str(args[1]) if len(args) > 1 else "-"
        print(
            f"[Server] {self.address_string()} - {getattr(self, 'command', '-')} "
            f"{redact_request_target(getattr(self, 'path', '/'))} -> {status}"
        )

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
                    # urllib exception strings can include a raw request URL.
                    print(
                        f"[Server] Unhandled {type(exc).__name__} on {method} "
                        f"{redact_request_target(self.path)}"
                    )
                    error_response(self, 500, "Internal server error")
                return
        error_response(self, 404, "Not found")

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def do_PATCH(self):
        self._dispatch("PATCH")

    def do_PUT(self):
        self._dispatch("PUT")


class _ServerWithDB(HTTPServer):
    def __init__(self, address, handler_cls, db, bot_runner=None, bot_runner_factory=None):
        super().__init__(address, handler_cls)
        self.db = db
        self.bot_runner = bot_runner
        self.bot_runner_factory = bot_runner_factory or (lambda: None)


class Server:
    def __init__(self, db):
        self._db = db

    def run(self, host, port, bot_runner=None, bot_runner_factory=None):
        server = _ServerWithDB((host, port), _Handler, self._db,
                               bot_runner=bot_runner,
                               bot_runner_factory=bot_runner_factory)
        print(f"[Server] Listening on {host}:{port}")
        server.serve_forever()
