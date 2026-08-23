import base64

from src.subscription import process_subscription


class FakeDB:
    def __init__(self, settings=None):
        self.settings = settings or {}

    def get_setting(self, key):
        return self.settings.get(key)

    def get_node_filter(self, username):
        return None

    def get_extra_configs(self):
        return []

    def get_per_user_configs(self, username):
        return []

    def get_hysteria_traffic(self, token):
        return 0, 0


def _decode_profile_header(value):
    assert value.startswith("base64:")
    return base64.b64decode(value.removeprefix("base64:")).decode("utf-8")


def _get_header(headers, name):
    return next(value for key, value in headers.items() if key.lower() == name.lower())


def test_custom_description_is_sent_as_metadata_and_kept_as_info_node():
    body = base64.b64encode(b"vless://user@example.com:443#Server")
    headers = {
        "Subscription-Userinfo": "upload=0; download=1024; total=2048; expire=0",
    }
    db = FakeDB({"sub_custom_desc": "Осталось: {remaining}"})

    new_body, out_headers = process_subscription(body, headers, "token", "user", db)

    expected = "Осталось: 1.00 KB"
    assert _decode_profile_header(_get_header(out_headers, "profile-description")) == expected
    assert _decode_profile_header(_get_header(out_headers, "announce")) == expected
    decoded_body = base64.b64decode(new_body).decode("utf-8").splitlines()
    assert decoded_body[0].startswith(
        "vless://00000000-0000-0000-0000-000000000000@127.0.0.1:1?type=tcp#"
    )
    assert decoded_body[1] == "vless://user@example.com:443#Server"


def test_announce_is_limited_but_profile_description_keeps_full_text():
    description = "я" * 250
    body = base64.b64encode(b"vless://user@example.com:443#Server")

    _, out_headers = process_subscription(
        body, {}, "token", "user", FakeDB({"sub_custom_desc": description})
    )

    assert _decode_profile_header(_get_header(out_headers, "profile-description")) == description
    assert _decode_profile_header(_get_header(out_headers, "announce")) == description[:200]


def test_upstream_description_and_announce_are_forwarded_without_override():
    body = base64.b64encode(b"vless://user@example.com:443#Server")
    headers = {
        "Profile-Description": "upstream description",
        "Announce": "upstream announce",
    }

    _, out_headers = process_subscription(body, headers, "token", "user", FakeDB())

    assert out_headers["Profile-Description"] == "upstream description"
    assert out_headers["Announce"] == "upstream announce"
