import json
import os
import time
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .config import MARZBAN_URL

_USERNAME_CACHE = {}
_USERNAME_TTL = 300
_ADMIN_TOKEN_CACHE = [None, 0.0]
_ADMIN_TOKEN_TTL = 60.0


class MarzbanClient:
    def __init__(self, base_url=None):
        self.base_url = (base_url or MARZBAN_URL).rstrip("/")

    def _api_json(self, path, admin_token, timeout=10):
        req = Request(f"{self.base_url}{path}", headers={"Authorization": f"Bearer {admin_token}"})
        resp = urlopen(req, timeout=timeout)
        return json.loads(resp.read())

    def get_sub(self, token, extra_headers=None):
        """Fetch raw subscription from Marzban. Returns (body_bytes, headers_dict) or raises URLError."""
        url = f"{self.base_url}/sub/{token}"
        headers = dict(extra_headers or {})
        headers.pop("host", None)
        headers.pop("Host", None)
        headers.pop("connection", None)
        headers.pop("Connection", None)
        req = Request(url, headers=headers)
        resp = urlopen(req, timeout=10)
        body = resp.read()
        return body, dict(resp.headers)

    def get_username_for_token(self, token):
        now = time.time()
        cached = _USERNAME_CACHE.get(token)
        if cached and cached[1] > now:
            return cached[0]
        try:
            req = Request(f"{self.base_url}/sub/{token}/info")
            resp = urlopen(req, timeout=5)
            data = json.loads(resp.read())
            username = data.get("username")
            if username:
                _USERNAME_CACHE[token] = (username, now + _USERNAME_TTL)
            return username
        except Exception as e:
            print(f"[Marzban] Could not resolve username for token: {e}")
            return None

    def get_token(self, username, password):
        url = f"{self.base_url}/api/admin/token"
        body = f"username={username}&password={password}".encode()
        req = Request(url, data=body, headers={"Content-Type": "application/x-www-form-urlencoded"})
        resp = urlopen(req, timeout=10)
        data = json.loads(resp.read())
        return data.get("access_token")

    def get_admin_token_from_env(self):
        now = time.time()
        if _ADMIN_TOKEN_CACHE[0] and _ADMIN_TOKEN_CACHE[1] > now:
            return _ADMIN_TOKEN_CACHE[0]

        username = os.environ.get("MARZBAN_ADMIN_USER", "admin")
        password = os.environ.get("MARZBAN_ADMIN_PASS", "")
        if not username or not password:
            raise RuntimeError("MARZBAN_ADMIN_USER and MARZBAN_ADMIN_PASS are required")

        token = self.get_token(username, password)
        if not token:
            raise RuntimeError("Could not obtain Marzban admin token")

        _ADMIN_TOKEN_CACHE[0] = token
        _ADMIN_TOKEN_CACHE[1] = now + _ADMIN_TOKEN_TTL
        return token

    def get_nodes(self, admin_token):
        return self._api_json("/api/nodes", admin_token)

    def get_nodes_usage(self, admin_token):
        return self._api_json("/api/nodes/usage", admin_token)

    def get_system(self, admin_token):
        return self._api_json("/api/system", admin_token)

    def get_users(self, admin_token, limit=100, offset=0):
        query = urlencode({"limit": limit, "offset": offset})
        return self._api_json(f"/api/users?{query}", admin_token)

    def get_inbounds(self, admin_token):
        return self._api_json("/api/inbounds", admin_token)

    def get_user(self, username, admin_token):
        return self._api_json(f"/api/user/{username}", admin_token)

    def get_user_usage(self, username, admin_token):
        return self._api_json(f"/api/user/{username}/usage", admin_token)
