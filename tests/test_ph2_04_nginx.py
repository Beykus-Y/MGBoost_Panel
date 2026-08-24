from scripts.apply_ph2_04_nginx import HSTS_DIRECTIVE, harden_nginx_conf, harden_site


def test_nginx_global_hardening_is_idempotent():
    original = "events {}\nhttp {\n    include mime.types;\n}\n"
    hardened = harden_nginx_conf(original)
    assert "server_tokens off;" in hardened
    assert HSTS_DIRECTIVE in hardened
    assert harden_nginx_conf(hardened) == hardened


def test_sensitive_locations_repeat_hsts_after_local_add_header():
    original = """
server {
    location /sub/ {
        add_header Referrer-Policy no-referrer always;
        add_header Cache-Control no-store always;
    }
    location /lk/ {
        add_header Cache-Control no-store always;
    }
}
"""
    hardened, inserted = harden_site(original)
    assert inserted == 2
    assert hardened.count(HSTS_DIRECTIVE) == 2
    repeated, inserted_again = harden_site(hardened)
    assert inserted_again == 0
    assert repeated == hardened


def test_unexpected_nginx_shape_fails_closed():
    try:
        harden_nginx_conf("events {}\n")
    except ValueError as exc:
        assert "http block" in str(exc)
    else:
        raise AssertionError("missing http block was accepted")
