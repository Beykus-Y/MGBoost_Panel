"""PH2-06 focused tests for the subscription-fetch abuse-control rate
limiter, the trusted-XFF client identity boundary it relies on, and its
wiring into `handle_sub`/`handle_opaque_sub`."""

import pytest

from src.http_utils import client_ip
from src.subscription_rate_limit import SubscriptionRateLimiter


class _Headers(dict):
    def get(self, key, default=None):
        return super().get(key, default)


class FakeHandler:
    def __init__(self, peer="127.0.0.1", headers=None):
        self.client_address = (peer, 12345)
        self.headers = _Headers(headers or {})
        self._sent = []
        self._status = None

    def send_response(self, status, message=None):
        self._status = status

    def send_header(self, key, value):
        self._sent.append((key, value))

    def end_headers(self):
        pass

    class _WFile:
        def __init__(self, outer):
            self.outer = outer
            self.written = b""

        def write(self, data):
            self.written += data

    def __getattr__(self, name):
        if name == "wfile":
            self.wfile = self._WFile(self)
            return self.wfile
        raise AttributeError(name)


# --- limiter unit behavior ---------------------------------------------------

def test_normal_valid_client_refresh_is_not_rate_limited():
    limiter = SubscriptionRateLimiter(window_seconds=60, max_requests=30)
    for i in range(10):
        assert limiter.check("203.0.113.1", now=1000 + i) == 0


def test_burst_from_one_ip_hits_a_controlled_limit():
    limiter = SubscriptionRateLimiter(window_seconds=60, max_requests=5)
    for _ in range(5):
        assert limiter.check("203.0.113.2", now=1000) == 0
    retry_after = limiter.check("203.0.113.2", now=1000)
    assert retry_after > 0


def test_a_different_ip_is_unaffected_by_another_ips_burst():
    limiter = SubscriptionRateLimiter(window_seconds=60, max_requests=5)
    for _ in range(5):
        assert limiter.check("203.0.113.3", now=1000) == 0
    assert limiter.check("203.0.113.3", now=1000) > 0
    # a different IP still has its full budget
    assert limiter.check("203.0.113.4", now=1000) == 0


def test_bucket_recovers_after_the_window_elapses():
    limiter = SubscriptionRateLimiter(window_seconds=10, max_requests=2)
    assert limiter.check("203.0.113.5", now=1000) == 0
    assert limiter.check("203.0.113.5", now=1000) == 0
    assert limiter.check("203.0.113.5", now=1000) > 0
    assert limiter.check("203.0.113.5", now=1011) == 0


def test_rejected_requests_do_not_grow_the_bucket_or_extend_the_block():
    limiter = SubscriptionRateLimiter(window_seconds=10, max_requests=2)
    limiter.check("203.0.113.6", now=1000)
    limiter.check("203.0.113.6", now=1000)
    for i in range(50):
        limiter.check("203.0.113.6", now=1000 + i * 0.01)
    # still recovers at the original window boundary, not pushed further out
    assert limiter.check("203.0.113.6", now=1010) == 0


def test_limiter_state_is_bounded_by_max_tracked_ips(monkeypatch):
    import src.subscription_rate_limit as mod
    monkeypatch.setattr(mod, "_MAX_TRACKED_IPS", 5)
    limiter = SubscriptionRateLimiter(window_seconds=60, max_requests=100)
    for i in range(20):
        limiter.check(f"203.0.113.{i}", now=1000)
    assert limiter.tracked_ip_count() <= 5


def test_limiter_never_stores_a_token_or_token_hash():
    limiter = SubscriptionRateLimiter()
    limiter.check("203.0.113.7", now=1000)
    for bucket_key in limiter._buckets:
        assert bucket_key == "203.0.113.7"
    # only numeric timestamps are ever stored as values
    for values in limiter._buckets.values():
        assert all(isinstance(v, (int, float)) for v in values)


# --- trusted XFF / client identity -------------------------------------------

def test_spoofed_xff_from_untrusted_peer_is_ignored():
    handler = FakeHandler(peer="198.51.100.9", headers={"X-Real-IP": "1.2.3.4"})
    assert client_ip(handler) == "198.51.100.9"


def test_xff_from_trusted_loopback_boundary_is_honored():
    handler = FakeHandler(peer="127.0.0.1", headers={"X-Real-IP": "203.0.113.55"})
    assert client_ip(handler) == "203.0.113.55"


def test_missing_xff_from_loopback_falls_back_to_peer():
    handler = FakeHandler(peer="127.0.0.1", headers={})
    assert client_ip(handler) == "127.0.0.1"


def test_malformed_xff_from_loopback_falls_back_to_peer():
    handler = FakeHandler(peer="127.0.0.1", headers={"X-Real-IP": "not-an-ip;drop table"})
    assert client_ip(handler) == "127.0.0.1"


# --- route wiring -------------------------------------------------------------

def test_burst_invalid_tokens_are_rate_limited_before_any_resolver_work(monkeypatch):
    import io
    from urllib.error import HTTPError

    import src.routes.sub as sub_module
    sub_module.SUBSCRIPTION_FETCH_LIMITER.clear()
    monkeypatch.setattr(sub_module.SUBSCRIPTION_FETCH_LIMITER, "max_requests", 3)

    def _raise_not_found(token, extra_headers):
        raise HTTPError("http://marzban/sub", 404, "not found", {}, io.BytesIO(b""))

    monkeypatch.setattr(sub_module._client, "get_sub", _raise_not_found)

    calls = []
    monkeypatch.setattr(
        sub_module, "_invalid_subscription_response",
        lambda handler, started_at: calls.append("invalid"),
    )

    handler = FakeHandler(peer="127.0.0.1", headers={"X-Real-IP": "203.0.113.88"})
    for _ in range(3):
        sub_module.handle_sub(handler, "a" * 20)
    assert calls == ["invalid", "invalid", "invalid"]

    # the 4th request in the same window must be rejected by the limiter,
    # never reaching token-length validation / resolver work at all
    calls.clear()
    handler2 = FakeHandler(peer="127.0.0.1", headers={"X-Real-IP": "203.0.113.88"})
    sub_module.handle_sub(handler2, "a" * 20)
    assert calls == []
    assert handler2._status == 429
    assert any(k == "Retry-After" for k, _ in handler2._sent)


def test_a_different_ip_still_works_during_anothers_burst(monkeypatch):
    import io
    from urllib.error import HTTPError

    import src.routes.sub as sub_module
    sub_module.SUBSCRIPTION_FETCH_LIMITER.clear()
    monkeypatch.setattr(sub_module.SUBSCRIPTION_FETCH_LIMITER, "max_requests", 1)

    def _raise_not_found(token, extra_headers):
        raise HTTPError("http://marzban/sub", 404, "not found", {}, io.BytesIO(b""))

    monkeypatch.setattr(sub_module._client, "get_sub", _raise_not_found)

    calls = []
    monkeypatch.setattr(
        sub_module, "_invalid_subscription_response",
        lambda handler, started_at: calls.append("invalid"),
    )
    flooder = FakeHandler(peer="127.0.0.1", headers={"X-Real-IP": "203.0.113.90"})
    sub_module.handle_sub(flooder, "a" * 20)
    blocked = FakeHandler(peer="127.0.0.1", headers={"X-Real-IP": "203.0.113.90"})
    sub_module.handle_sub(blocked, "a" * 20)
    assert blocked._status == 429

    other = FakeHandler(peer="127.0.0.1", headers={"X-Real-IP": "203.0.113.91"})
    sub_module.handle_sub(other, "a" * 20)
    assert calls == ["invalid", "invalid"]


def test_opaque_route_rate_limit_precedes_disabled_flag_check(monkeypatch):
    import src.routes.opaque_sub as opaque_module
    import src.routes.sub as sub_module
    sub_module.SUBSCRIPTION_FETCH_LIMITER.clear()
    monkeypatch.setattr(sub_module.SUBSCRIPTION_FETCH_LIMITER, "max_requests", 1)

    from src.subscription_credentials import generate_opaque_token

    calls = []
    monkeypatch.setattr(
        opaque_module, "_invalid_subscription_response",
        lambda handler, started_at: calls.append("invalid"),
    )
    handler1 = FakeHandler(peer="127.0.0.1", headers={"X-Real-IP": "203.0.113.92"})
    opaque_module.handle_opaque_sub(handler1, generate_opaque_token())
    handler2 = FakeHandler(peer="127.0.0.1", headers={"X-Real-IP": "203.0.113.92"})
    opaque_module.handle_opaque_sub(handler2, generate_opaque_token())
    assert handler2._status == 429
    assert calls == ["invalid"]


def test_rate_limit_response_never_references_the_token(monkeypatch):
    import io
    from urllib.error import HTTPError

    import src.routes.sub as sub_module
    sub_module.SUBSCRIPTION_FETCH_LIMITER.clear()
    sub_module.SUBSCRIPTION_FETCH_LIMITER.check("203.0.113.93")

    def _raise_not_found(token, extra_headers):
        raise HTTPError("http://marzban/sub", 404, "not found", {}, io.BytesIO(b""))

    monkeypatch.setattr(sub_module._client, "get_sub", _raise_not_found)
    handler = FakeHandler(peer="127.0.0.1", headers={"X-Real-IP": "203.0.113.93"})
    secret_token = "super-secret-token-value-should-never-appear"
    for _ in range(SubscriptionRateLimiter().max_requests + 5):
        sub_module.handle_sub(handler, secret_token)
        if handler._status == 429:
            break
    assert handler._status == 429
    written = handler.wfile.written
    assert secret_token.encode() not in written
    for _key, value in handler._sent:
        assert secret_token not in str(value)
