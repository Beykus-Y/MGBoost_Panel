import json
from urllib.parse import parse_qs

from src.marzban import MarzbanClient


class _Response:
    def read(self):
        return json.dumps({"access_token": "token"}).encode()


def test_marzban_login_form_encodes_high_entropy_special_characters(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout):
        captured["body"] = request.data
        captured["content_type"] = request.headers["Content-type"]
        captured["timeout"] = timeout
        return _Response()

    monkeypatch.setattr("src.marzban.urlopen", fake_urlopen)
    username = "svc+rotation@example.test"
    password = "CSPRNG+/=_-value with spaces&delimiters"

    token = MarzbanClient("http://127.0.0.1:8000").get_token(username, password)

    assert token == "token"
    assert parse_qs(captured["body"].decode(), strict_parsing=True) == {
        "username": [username],
        "password": [password],
    }
    assert captured["content_type"] == "application/x-www-form-urlencoded"
    assert captured["timeout"] == 10
