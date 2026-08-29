"""PH3-04 versioned compatibility registry: exact-match only, schema-valid,
no fuzzy matching, no raw identifiers."""

import re

import pytest

from src import compat_registry as cr


def test_exact_supported_tuple():
    assert cr.classify("happ", "3.26.3", "android") == cr.SUPPORTED
    assert cr.classify("incy", "2.5.2", "ios") == cr.SUPPORTED


def test_case_and_whitespace_insensitive_but_still_exact():
    assert cr.classify("Happ", " 3.26.3 ", "ANDROID") == cr.SUPPORTED


def test_newer_version_above_baseline_is_supported():
    # happ/android's lowest vetted SUPPORTED version is 3.24.1; a newer
    # numeric version no longer needs its own registry entry.
    assert cr.classify("happ", "99.99.99", "android") == cr.SUPPORTED


def test_windows_desktop_baselines_cover_newer_happ_and_v2raytun_versions():
    assert cr.classify("happ", "99.99.99", "windows") == cr.SUPPORTED
    assert cr.classify("v2raytun", "3.8.11", "windows") == cr.SUPPORTED
    assert cr.classify("v2raytun", "3.8.12", "windows") == cr.SUPPORTED
    assert cr.classify("v2raytun", "3.8.10", "windows") == cr.UNKNOWN


def test_older_version_below_baseline_is_unknown():
    assert cr.classify("happ", "3.20.0", "android") == cr.UNKNOWN


def test_baseline_does_not_cross_platform():
    # incy/android's baseline (3.3.0) must not leak into incy/windows, which
    # has no vetted evidence at all.
    assert cr.classify("incy", "9.9.9", "windows") == cr.UNKNOWN


def test_non_numeric_version_falls_back_to_exact_match():
    assert cr.classify("streisand", "48-beta", "darwin") == cr.UNKNOWN
    assert cr.classify("streisand", "48", "darwin") == cr.UNSUPPORTED_MISSING_HWID


def test_min_supported_version_is_the_lowest_vetted_entry():
    assert cr._MIN_SUPPORTED_VERSION[("incy", "android")] == (3, 3, 0)
    assert cr._MIN_SUPPORTED_VERSION[("happ", "android")] == (3, 24, 1)
    assert cr._MIN_SUPPORTED_VERSION[("v2raytun", "windows")] == (3, 8, 11)


def test_supported_client_wrong_platform_is_unknown():
    assert cr.classify("happ", "3.26.3", "ios") == cr.UNKNOWN


def test_unknown_client_is_unknown():
    assert cr.classify("totally-unheard-of-client", "1.0", "android") == cr.UNKNOWN


def test_no_substring_fuzzy_match():
    # "happ" is supported at 3.26.3/android; a client whose name merely
    # contains "happ" as a substring must not match.
    assert cr.classify("nothapp", "3.26.3", "android") == cr.UNKNOWN
    assert cr.classify("happpro", "3.26.3", "android") == cr.UNKNOWN


def test_missing_hwid_family_is_recorded_not_supported():
    assert cr.classify("streisand", "48", "darwin") == cr.UNSUPPORTED_MISSING_HWID
    assert cr.classify("hiddifynext", "2.5.7", "windows") == cr.UNSUPPORTED_MISSING_HWID


def test_no_raw_identifier_shapes_in_registry():
    uuid_re = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
    for record in cr.registry_snapshot():
        blob = " ".join(str(v) for v in record.values())
        assert not uuid_re.search(blob)
        assert "sha256:" not in blob
        assert "hmac-" not in blob


def test_registry_snapshot_has_no_duplicate_keys():
    keys = [(r["client"], r["version"], r["platform"]) for r in cr.registry_snapshot()]
    assert len(keys) == len(set(keys))


def test_classification_values_are_restricted():
    allowed = {cr.SUPPORTED, cr.UNSUPPORTED_MISSING_HWID, cr.UNSUPPORTED_MALFORMED_HWID}
    for record in cr.registry_snapshot():
        assert record["classification"] in allowed


def test_evidence_type_is_labeled_and_not_fabricated_as_live():
    for record in cr.registry_snapshot():
        assert record["evidence_type"] in {"ORGANIC_LIVE", "CONTROLLED", "HISTORICAL"}
        assert record["caveat"]


def test_build_index_rejects_duplicate_key():
    duplicate = (
        cr.CompatibilityRecord("happ", "3.26.3", "android", cr.SUPPORTED, "ORGANIC_LIVE",
                                "2026-08-25", "first"),
        cr.CompatibilityRecord("happ", "3.26.3", "android", cr.SUPPORTED, "ORGANIC_LIVE",
                                "2026-08-25", "second"),
    )
    with pytest.raises(ValueError, match="duplicate"):
        cr._build_index(duplicate)


def test_build_index_rejects_non_normalized_entry():
    bad = (
        cr.CompatibilityRecord("Happ", "3.26.3", "android", cr.SUPPORTED, "ORGANIC_LIVE",
                                "2026-08-25", "not lowercased"),
    )
    with pytest.raises(ValueError, match="normalized form"):
        cr._build_index(bad)


def test_build_index_rejects_bad_classification():
    bad = (
        cr.CompatibilityRecord("happ", "3.26.3", "android", "MAYBE", "ORGANIC_LIVE",
                                "2026-08-25", "invalid classification"),
    )
    with pytest.raises(ValueError, match="classification"):
        cr._build_index(bad)
