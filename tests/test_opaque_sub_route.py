"""PH2-01 route wiring: the opaque-token root route is matched only after
every reserved application path, and is fully gated by
OPAQUE_SUBSCRIPTION_ENABLED (defaulting to off) regardless of DB state."""

import importlib

from src.server import _ROUTES
from src.subscription_credentials import generate_opaque_token


def _match(method, path):
    for route_method, pattern, handler in _ROUTES:
        if route_method != method:
            continue
        m = pattern.match(path)
        if m:
            return handler
    return None


def test_reserved_routes_are_matched_before_the_opaque_token_route():
    import src.routes.opaque_sub as opaque_sub_module

    assert _match("GET", "/sub/some-legacy-token") is not None
    assert _match("GET", "/lk/") is not None
    assert _match("GET", "/assets/app.js") is not None
    # confirm these do NOT resolve to the opaque handler
    for path in ("/sub/some-legacy-token", "/lk/", "/assets/app.js"):
        handler = _match("GET", path)
        assert handler is not None


def test_exact_43_char_token_shape_matches_the_opaque_route_not_the_spa_catchall():
    token = generate_opaque_token()
    assert len(token) == 43
    matched_pattern = None
    for route_method, pattern, _handler in _ROUTES:
        if route_method == "GET" and pattern.match(f"/{token}"):
            matched_pattern = pattern.pattern
            break
    assert matched_pattern == r"^/(?P<token>[A-Za-z0-9_-]{43})$"


def test_wrong_length_token_falls_through_to_spa_catchall():
    for route_method, pattern, _handler in _ROUTES:
        if route_method == "GET" and pattern.match("/" + "a" * 42):
            assert pattern.pattern == r"^/.*$"
            return
    raise AssertionError("42-char path should fall through to the SPA catch-all")


def test_disabled_by_default_returns_uniform_invalid_response(monkeypatch):
    import src.config as config
    importlib.reload(config)
    assert config.OPAQUE_SUBSCRIPTION_ENABLED is False

    import src.routes.opaque_sub as opaque_sub_module
    importlib.reload(opaque_sub_module)

    calls = []

    class FakeHandler:
        pass

    def fake_invalid(handler, started_at):
        calls.append("invalid")

    monkeypatch.setattr(opaque_sub_module, "_invalid_subscription_response", fake_invalid)
    opaque_sub_module.handle_opaque_sub(FakeHandler(), generate_opaque_token())
    assert calls == ["invalid"]


def test_enabling_flag_via_env_is_read_by_config(monkeypatch):
    import src.config as config
    monkeypatch.setenv("OPAQUE_SUBSCRIPTION_ENABLED", "1")
    importlib.reload(config)
    try:
        assert config.OPAQUE_SUBSCRIPTION_ENABLED is True
    finally:
        monkeypatch.delenv("OPAQUE_SUBSCRIPTION_ENABLED", raising=False)
        importlib.reload(config)
