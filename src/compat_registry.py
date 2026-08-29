"""PH3-04 versioned client/version/platform compatibility registry.

This is a static, git-tracked, human-reviewable allowlist -- not a database
table and not fuzzy matching on client/platform. Every entry requires an
exact `(client, version, platform)` match against the same bounded,
lowercased dimensions PH3-07 telemetry already normalizes
(`compat_telemetry._dimension`), so a registry lookup and a telemetry
observation always agree on the same normalized identity space.

Only `SUPPORTED` entries ever let the future PH3-04 HWID gate proceed past
the client-compatibility check. `client` and `platform` are still exact-match
only -- an unlisted client family, or a listed client on a platform it has no
vetted evidence for, is always `UNKNOWN`.

2026-08-29 (owner decision, following the account_id=21 support case):
`version` is no longer exact-match-only for `SUPPORTED` entries. For each
`(client, platform)` pair with at least one numeric-versioned `SUPPORTED`
entry, the *lowest* vetted version is treated as a minimum-supported
baseline (`_MIN_SUPPORTED_VERSION`, built in `_build_min_supported_versions`
purely from existing registry evidence -- no new tuple is invented); any
request whose numeric version parses and is `>=` that baseline also
classifies as `SUPPORTED`, so a newer patch/minor release of an
already-vetted client no longer needs its own registry entry. This is a
deliberate, bounded loosening of the original "never guess a newer version
is fine" stance: it still never guesses across client or platform
boundaries, and a version that fails to parse as dot-separated integers (or
is below the baseline) falls back to the old exact-match-or-`UNKNOWN`
behavior. `UNSUPPORTED_MISSING_HWID`/`UNSUPPORTED_MALFORMED_HWID` records are
unaffected -- they stay exact-match, since they document specific negative
observations rather than a supported baseline.

No raw HWID, UUID, subscription token, username, IP or Telegram ID belongs in
this file, ever.
"""

from __future__ import annotations

from dataclasses import dataclass

from .compat_telemetry import _dimension

SUPPORTED = "SUPPORTED"
UNSUPPORTED_MISSING_HWID = "UNSUPPORTED_MISSING_HWID"
UNSUPPORTED_MALFORMED_HWID = "UNSUPPORTED_MALFORMED_HWID"
UNKNOWN = "UNKNOWN"

_CLASSIFICATIONS = frozenset({SUPPORTED, UNSUPPORTED_MISSING_HWID, UNSUPPORTED_MALFORMED_HWID})
_EVIDENCE_TYPES = frozenset({"ORGANIC_LIVE", "CONTROLLED", "HISTORICAL"})


@dataclass(frozen=True)
class CompatibilityRecord:
    client: str
    version: str
    platform: str
    classification: str
    evidence_type: str
    evidence_date: str
    caveat: str


# REGISTRY_VERSION must be bumped whenever an entry is added, removed,
# reclassified, or whenever the classify() lookup semantics themselves
# change (as in the 2026-08-29 min-supported-version change below).
REGISTRY_VERSION = 3

# --- SUPPORTED: exact (client, version, platform) tuples with positive,
# reviewed evidence that the real client actually sends a well-formed HWID
# candidate. Sourced from the fresh production PH3-07 snapshot taken
# 2026-08-25 (window covering all telemetry since PH3-07 activation on
# 2026-08-24; `mgboost-owner-verification`/`mgboost-ph3-postcanary`/
# `python-urllib` gate/tool traffic excluded -- those are not real clients).
_REGISTRY: tuple[CompatibilityRecord, ...] = (
    CompatibilityRecord(
        "happ", "3.26.3", "android", SUPPORTED, "ORGANIC_LIVE", "2026-08-25",
        "20 live requests / 5 correlated subjects in the 2026-08-25 snapshot.",
    ),
    CompatibilityRecord(
        "v2raytun", "2.4.7", "ios", SUPPORTED, "ORGANIC_LIVE", "2026-08-25",
        "19 live requests / 8 correlated subjects in the 2026-08-25 snapshot.",
    ),
    CompatibilityRecord(
        "incy", "2.5.2", "ios", SUPPORTED, "ORGANIC_LIVE", "2026-08-25",
        "17 live requests / 7 correlated subjects, including the approved "
        "PH3-03 dormant canary device.",
    ),
    CompatibilityRecord(
        "incy", "3.5.4", "android", SUPPORTED, "ORGANIC_LIVE", "2026-08-25",
        "12 live requests / 4 correlated subjects in the 2026-08-25 snapshot.",
    ),
    CompatibilityRecord(
        "happ", "2.7.0", "windows", SUPPORTED, "ORGANIC_LIVE", "2026-08-25",
        "11 live requests / 3 correlated subjects in the 2026-08-25 snapshot.",
    ),
    CompatibilityRecord(
        "v2raytun", "3.8.11", "windows", SUPPORTED, "HISTORICAL", "2026-08-30",
        "Seven production requests carried a well-formed HWID and fingerprint; "
        "the evidence is historical, so this is the reviewed Windows baseline.",
    ),
    CompatibilityRecord(
        "v2raytun", "5.25.81", "android", SUPPORTED, "ORGANIC_LIVE", "2026-08-25",
        "9 live requests / 6 correlated subjects in the 2026-08-25 snapshot.",
    ),
    CompatibilityRecord(
        "v2raytun", "2.4.4", "ios", SUPPORTED, "ORGANIC_LIVE", "2026-08-25",
        "5 live requests / 2 correlated subjects in the 2026-08-25 snapshot.",
    ),
    CompatibilityRecord(
        "incy", "3.3.0", "android", SUPPORTED, "ORGANIC_LIVE", "2026-08-25",
        "3 live requests / 1 correlated subject -- low sample, single device.",
    ),
    CompatibilityRecord(
        "happ", "3.24.1", "android", SUPPORTED, "ORGANIC_LIVE", "2026-08-25",
        "2 live requests / 1 correlated subject -- low sample, single device.",
    ),
    CompatibilityRecord(
        "v2raytun", "2.4.6", "ios", SUPPORTED, "ORGANIC_LIVE", "2026-08-25",
        "1 live request / 1 correlated subject -- single observation only.",
    ),
    # --- Known non-HWID client families (never SUPPORTED). Recorded so the
    # gate's deny reason is explicit rather than a bare UNKNOWN, and so this
    # file documents why they cannot become SUPPORTED without new evidence.
    CompatibilityRecord(
        "streisand", "48", "darwin", UNSUPPORTED_MISSING_HWID, "ORGANIC_LIVE", "2026-08-25",
        "Client family sends no HWID candidate in every observed live request.",
    ),
    CompatibilityRecord(
        "streisand", "41", "darwin", UNSUPPORTED_MISSING_HWID, "ORGANIC_LIVE", "2026-08-25",
        "Client family sends no HWID candidate in every observed live request.",
    ),
    CompatibilityRecord(
        "hiddifynext", "2.5.7", "windows", UNSUPPORTED_MISSING_HWID, "ORGANIC_LIVE", "2026-08-25",
        "Client family sends no HWID candidate in every observed live request.",
    ),
)

def _build_index(records) -> dict[tuple[str, str, str], CompatibilityRecord]:
    index: dict[tuple[str, str, str], CompatibilityRecord] = {}
    for record in records:
        if record.classification not in _CLASSIFICATIONS:
            raise ValueError(f"invalid compatibility classification: {record.classification!r}")
        if record.evidence_type not in _EVIDENCE_TYPES:
            raise ValueError(f"invalid compatibility evidence_type: {record.evidence_type!r}")
        if not record.caveat or len(record.caveat) > 300:
            raise ValueError("compatibility record caveat must be a short non-empty string")
        normalized = (
            _dimension(record.client, maximum=64, fallback="unknown"),
            _dimension(record.version, maximum=64, fallback="unknown"),
            _dimension(record.platform, maximum=32, fallback="unknown"),
        )
        if normalized != (record.client, record.version, record.platform):
            raise ValueError(
                f"compatibility record {record.client!r}/{record.version!r}/"
                f"{record.platform!r} is not already stored in its normalized form"
            )
        key = (record.client, record.version, record.platform)
        if key in index:
            raise ValueError(f"duplicate compatibility registry key: {key!r}")
        index[key] = record
    return index


_INDEX: dict[tuple[str, str, str], CompatibilityRecord] = _build_index(_REGISTRY)


def _parse_numeric_version(version: str) -> tuple[int, ...] | None:
    """Dot-separated non-negative integers only (e.g. "3.26.3", "48"). Any
    other shape (missing segment, non-digit segment, empty string) returns
    None so the caller falls back to exact-match behavior instead of
    guessing an ordering."""
    parts = version.split(".")
    if not parts or any(p == "" or not p.isdigit() for p in parts):
        return None
    return tuple(int(p) for p in parts)


def _build_min_supported_versions(
    records,
) -> dict[tuple[str, str], tuple[int, ...]]:
    """Lowest numeric-parseable SUPPORTED version per (client, platform),
    derived only from evidence already in `records` -- never a new value."""
    baselines: dict[tuple[str, str], tuple[int, ...]] = {}
    for record in records:
        if record.classification != SUPPORTED:
            continue
        parsed = _parse_numeric_version(record.version)
        if parsed is None:
            continue
        key = (record.client, record.platform)
        if key not in baselines or parsed < baselines[key]:
            baselines[key] = parsed
    return baselines


_MIN_SUPPORTED_VERSION: dict[tuple[str, str], tuple[int, ...]] = (
    _build_min_supported_versions(_REGISTRY)
)


def classify(client_name, client_version, platform) -> str:
    """Client and platform are always exact-match. Version is exact-match
    first; if that misses, a numeric version at or above the vetted
    minimum-supported baseline for this (client, platform) also counts as
    SUPPORTED -- see the module docstring. Never a substring/fuzzy match on
    client or platform."""
    client = _dimension(client_name, maximum=64, fallback="unknown")
    version = _dimension(client_version, maximum=64, fallback="unknown")
    plat = _dimension(platform, maximum=32, fallback="unknown")

    record = _INDEX.get((client, version, plat))
    if record is not None:
        return record.classification

    baseline = _MIN_SUPPORTED_VERSION.get((client, plat))
    if baseline is not None:
        parsed = _parse_numeric_version(version)
        if parsed is not None and parsed >= baseline:
            return SUPPORTED

    return UNKNOWN


def registry_snapshot() -> tuple[dict, ...]:
    """Read-only, privacy-safe export for documentation/report tooling."""
    return tuple(
        {
            "client": r.client, "version": r.version, "platform": r.platform,
            "classification": r.classification, "evidence_type": r.evidence_type,
            "evidence_date": r.evidence_date, "caveat": r.caveat,
        }
        for r in _REGISTRY
    )
