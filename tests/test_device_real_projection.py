"""PH8-05 slot <-> real-device telemetry projection: pure matching engine."""

from src.device_real_projection import (
    MATCH_CONFIRMED,
    MATCH_GENESIS_PLACEHOLDER,
    MATCH_NOT_CLAIMED,
    MATCH_UNKNOWN,
    project_real_device,
)


def _slot(**overrides):
    base = {
        "account_id": 1,
        "generation_status": "ACTIVE",
        "is_genesis": False,
        "hwid_verifier": "hmac-sha256:" + "a" * 64,
    }
    base.update(overrides)
    return base


def _obs(**overrides):
    base = {
        "account_id": 1,
        "hwid_verifier": "hmac-sha256:" + "a" * 64,
        "model": "iPad", "platform": "iPadOS", "client_name": "INCY",
        "client_version": "2.5.1", "last_seen_at": 1000, "observed_id": 1,
    }
    base.update(overrides)
    return base


def test_exact_device_evidence_confirms_the_slot():
    result = project_real_device(_slot(), [_obs()])
    assert result["matched"] is True
    assert result["match_state"] == MATCH_CONFIRMED
    assert result["model"] == "iPad"
    assert result["platform"] == "iPadOS"
    assert result["client_name"] == "INCY"
    assert result["client_version"] == "2.5.1"
    assert result["last_seen_at"] == 1000
    assert result["model_source"] == "CLIENT_REPORTED"


def test_no_evidence_is_unknown_not_a_guess():
    result = project_real_device(_slot(), [])
    assert result["matched"] is False
    assert result["match_state"] == MATCH_UNKNOWN
    assert result["model"] is None


def test_telemetry_from_another_account_never_matches():
    result = project_real_device(_slot(account_id=1), [_obs(account_id=2)])
    assert result["match_state"] == MATCH_UNKNOWN


def test_genesis_placeholder_is_never_reported_as_a_real_device():
    result = project_real_device(_slot(is_genesis=True), [_obs()])
    assert result["matched"] is False
    assert result["match_state"] == MATCH_GENESIS_PLACEHOLDER
    assert result["model"] is None


def test_slot_with_no_active_generation_is_not_claimed():
    result = project_real_device(_slot(generation_status=None, hwid_verifier=None), [_obs()])
    assert result["match_state"] == MATCH_NOT_CLAIMED


def test_old_generation_evidence_is_never_passed_in_so_never_inherited():
    # Caller contract: only the CURRENT active generation's hwid_verifier is
    # ever passed as `slot`. A telemetry row proving the OLD (released)
    # generation's identity simply never matches the new one.
    old_generation_obs = _obs(hwid_verifier="hmac-sha256:" + "b" * 64)
    result = project_real_device(_slot(hwid_verifier="hmac-sha256:" + "c" * 64), [old_generation_obs])
    assert result["match_state"] == MATCH_UNKNOWN


def test_rebind_drops_the_old_device_once_the_new_generation_has_no_evidence_yet():
    # Same scenario expressed as an explicit rebind: generation moved from
    # HWID "old" to HWID "new"; only telemetry for "old" exists so far.
    old_obs = _obs(hwid_verifier="hmac-sha256:" + "d" * 64, model="Old Phone")
    new_slot = _slot(hwid_verifier="hmac-sha256:" + "e" * 64)
    result = project_real_device(new_slot, [old_obs])
    assert result["match_state"] == MATCH_UNKNOWN
    assert result["model"] is None


def test_multiple_telemetry_rows_pick_newest_deterministically():
    older = _obs(last_seen_at=500, observed_id=1, client_version="2.4.0")
    newer = _obs(last_seen_at=1500, observed_id=2, client_version="2.5.1")
    result = project_real_device(_slot(), [older, newer])
    assert result["match_state"] == MATCH_CONFIRMED
    assert result["client_version"] == "2.5.1"
    assert result["last_seen_at"] == 1500


def test_same_device_newer_client_version_updates_projection():
    v1 = _obs(last_seen_at=100, observed_id=1, client_version="2.4.0")
    result_v1 = project_real_device(_slot(), [v1])
    assert result_v1["client_version"] == "2.4.0"

    v2 = _obs(last_seen_at=200, observed_id=2, client_version="2.5.1")
    result_v2 = project_real_device(_slot(), [v1, v2])
    assert result_v2["client_version"] == "2.5.1"


def test_tie_on_last_seen_breaks_deterministically_by_observed_id():
    a = _obs(last_seen_at=1000, observed_id=5, client_version="A")
    b = _obs(last_seen_at=1000, observed_id=9, client_version="B")
    result = project_real_device(_slot(), [a, b])
    assert result["client_version"] == "B"


def test_raw_verifier_and_masked_hwid_never_appear_in_the_returned_projection():
    result = project_real_device(_slot(), [_obs()])
    dumped = repr(result)
    assert "hmac-sha256" not in dumped
    assert "hwid_verifier" not in result
    assert "hwid_masked" not in result


def test_malformed_telemetry_fails_closed_to_unknown():
    malformed = [
        {"account_id": 1, "hwid_verifier": None},
        {"account_id": 1},
        {"hwid_verifier": "hmac-sha256:" + "a" * 64},
        "not-a-dict",
        None,
        {"account_id": 1, "hwid_verifier": 12345},
    ]
    result = project_real_device(_slot(), malformed)
    assert result["match_state"] == MATCH_UNKNOWN


def test_malformed_slot_fails_closed_to_unknown():
    result = project_real_device(_slot(account_id=None), [_obs(account_id=None)])
    assert result["match_state"] == MATCH_UNKNOWN
    result2 = project_real_device(_slot(hwid_verifier=123), [_obs()])
    assert result2["match_state"] == MATCH_UNKNOWN
