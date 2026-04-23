import json
import time
from urllib.error import URLError
from urllib.request import Request, urlopen

from .config import MARZBAN_URL

_USERNAME_CACHE = {}
_USERNAME_TTL = 300


class MarzbanClient:
    def __init__(self, base_url=None):
        self.base_url = (base_url or MARZBAN_URL).rstrip("/")

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

    def get_nodes(self, admin_token):
        req = Request(f"{self.base_url}/api/nodes", headers={"Authorization": f"Bearer {admin_token}"})
        resp = urlopen(req, timeout=10)
        return json.loads(resp.read())

    def get_user(self, username, admin_token):
        req = Request(f"{self.base_url}/api/user/{username}", headers={"Authorization": f"Bearer {admin_token}"})
        resp = urlopen(req, timeout=10)
        return json.loads(resp.read())

    def get_user_usage(self, username, admin_token):
        req = Request(f"{self.base_url}/api/user/{username}/usage", headers={"Authorization": f"Bearer {admin_token}"})
        resp = urlopen(req, timeout=10)
        return json.loads(resp.read())
