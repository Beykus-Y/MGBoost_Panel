"""Real-format client header fixtures for the privacy-safe parser."""

from src.device_headers import extract_device_metadata


def test_throne_subscription_headers_feed_the_hwid_pipeline():
    """Fixture from Throne upstream ``HTTPRequestHelper.cpp``.

    Values are synthetic test values; no raw production HWID is recorded.
    """
    metadata = extract_device_metadata({
        "User-Agent": "Throne/1.2.1",
        "x-hwid": "throne-test-device-001",
        "x-device-os": "Windows",
        "x-ver-os": "10.0",
        "x-device-model": "test-pc",
    })
    assert metadata["client_name"] == "Throne"
    assert metadata["client_version"] == "1.2.1"
    assert metadata["platform"] == "Windows"
    assert metadata["device_id"] == "throne-test-device-001"
    assert metadata["hwid_candidate_present"] is True
    assert metadata["hwid_candidate_supported"] is True
    assert metadata["metadata"]["sources"]["platform"] == "derived:header:x-device-os"


def test_unknown_os_never_invents_a_platform():
    metadata = extract_device_metadata({
        "User-Agent": "Throne/1.2.1",
        "x-hwid": "throne-test-device-002",
        "x-device-os": "unverified-os-family",
    })
    assert metadata["platform"] is None
