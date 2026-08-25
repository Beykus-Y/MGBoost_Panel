"""PH3-04 versioned client/version/platform compatibility registry.

This is a static, git-tracked, human-reviewable allowlist -- not a database
table and not fuzzy matching. Every entry requires an exact
`(client, version, platform)` match against the same bounded, lowercased
dimensions PH3-07 telemetry already normalizes (`compat_telemetry._dimension`),
so a registry lookup and a telemetry observation always agree on the same
normalized identity space.

Only `SUPPORTED` entries ever let the future PH3-04 HWID gate proceed past
the client-compatibility check. Any tuple not present here -- including a
newer/older version of an otherwise-supported client -- classifies as
`UNKNOWN`, which the gate treats identically to "not compatible". This is a
deliberate conservative allowlist: it never guesses that "one version of a
client is fine, so every version must be fine."

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


# REGISTRY_VERSION must be bumped whenever an entry is added, removed or
# reclassified, so a diff always carries an explicit version bump.
REGISTRY_VERSION = 1

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


def classify(client_name, client_version, platform) -> str:
    """Exact, deterministic lookup only -- never a substring/fuzzy match."""
    key = (
        _dimension(client_name, maximum=64, fallback="unknown"),
        _dimension(client_version, maximum=64, fallback="unknown"),
        _dimension(platform, maximum=32, fallback="unknown"),
    )
    record = _INDEX.get(key)
    return record.classification if record else UNKNOWN


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
