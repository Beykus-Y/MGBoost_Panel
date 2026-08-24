"""Observe-only, privacy-safe HWID/client compatibility telemetry."""

from __future__ import annotations

import hashlib
import hmac
import re
import sqlite3
from dataclasses import dataclass

from .sensitive import subscription_token_ref


SUPPORTED_HWID_PRESENT = "SUPPORTED_HWID_PRESENT"
HWID_MISSING = "HWID_MISSING"
HWID_UNSUPPORTED_OR_MALFORMED = "HWID_UNSUPPORTED_OR_MALFORMED"

DETAIL_RETENTION_DAYS = 30
ROLLUP_RETENTION_DAYS = 60
SECONDS_PER_DAY = 86400
CLIENT_REF_VERSION = 1
DEFAULT_SQLITE_TIMEOUT_SECONDS = 0.05
MAX_SUBJECT_ROWS_PER_DAY = 10000
MAX_ROLLUP_ROWS_PER_DAY = 2000

_DIMENSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+() -]*$")
_SUPPORTED_HWID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+\-=/]{5,179}$")


@dataclass(frozen=True)
class CompatibilityObservation:
    client_name: str
    client_version: str
    platform: str
    category: str
    client_ref: str


class TelemetryCapacityReached(RuntimeError):
    pass


def _key_bytes(value: str | bytes) -> bytes:
    key = value.encode("utf-8") if isinstance(value, str) else value
    if not isinstance(key, bytes) or len(key) < 32:
        raise ValueError("compatibility telemetry HMAC key must contain at least 32 bytes")
    return key


def telemetry_key_is_valid(value: str | bytes | None) -> bool:
    try:
        _key_bytes(value or b"")
        return True
    except (TypeError, ValueError):
        return False


def _dimension(value, *, maximum: int, fallback: str) -> str:
    text = str(value or "").strip()
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if not text or len(text) > maximum or not _DIMENSION_RE.fullmatch(text):
        return fallback
    return text.lower()


def _candidate(metadata: dict) -> tuple[bool, bool, str]:
    raw = metadata.get("device_id")
    if "hwid_candidate_present" in metadata:
        present = bool(metadata.get("hwid_candidate_present"))
        supported = bool(metadata.get("hwid_candidate_supported"))
        return present, supported, str(raw or "").strip()
    if raw is None:
        return False, False, ""
    value = str(raw).strip()
    return True, bool(_SUPPORTED_HWID_RE.fullmatch(value)), value


def build_observation(
    token: str, metadata: dict, hmac_key: str | bytes
) -> CompatibilityObservation:
    """Build one pseudonymous observation; raw inputs are never returned."""
    key = _key_bytes(hmac_key)
    present, supported, candidate = _candidate(metadata)
    if not present:
        category = HWID_MISSING
    elif supported:
        category = SUPPORTED_HWID_PRESENT
    else:
        category = HWID_UNSUPPORTED_OR_MALFORMED

    client_name = _dimension(metadata.get("client_name"), maximum=64, fallback="unknown")
    client_version = _dimension(
        metadata.get("client_version"), maximum=64, fallback="unknown"
    )
    platform = _dimension(metadata.get("platform"), maximum=32, fallback="unknown")

    token_ref = subscription_token_ref(token)
    material = "\n".join(
        [
            "mgboost-hwid-compat-v1",
            token_ref,
            category,
            candidate,
            client_name,
            client_version,
            platform,
        ]
    ).encode("utf-8")
    client_ref = "hmac-sha256:" + hmac.new(key, material, hashlib.sha256).hexdigest()
    return CompatibilityObservation(
        client_name=client_name,
        client_version=client_version,
        platform=platform,
        category=category,
        client_ref=client_ref,
    )


def record_observation(
    db_path: str,
    token: str,
    metadata: dict,
    hmac_key: str | bytes,
    *,
    now: int,
    timeout_seconds: float = DEFAULT_SQLITE_TIMEOUT_SECONDS,
) -> CompatibilityObservation:
    """Atomically update detail and identifier-free daily aggregates."""
    observation = build_observation(token, metadata, hmac_key)
    timestamp = int(now)
    day_start = timestamp - (timestamp % SECONDS_PER_DAY)
    detail_cutoff = day_start - DETAIL_RETENTION_DAYS * SECONDS_PER_DAY
    rollup_cutoff = day_start - ROLLUP_RETENTION_DAYS * SECONDS_PER_DAY
    connection = sqlite3.connect(
        db_path, timeout=max(0.0, float(timeout_seconds)), isolation_level=None
    )
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            f"PRAGMA busy_timeout={max(0, int(float(timeout_seconds) * 1000))}"
        )
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "DELETE FROM mgboost_hwid_compat_subjects WHERE day_start<?",
            (detail_cutoff,),
        )
        connection.execute(
            "DELETE FROM mgboost_hwid_compat_daily WHERE day_start<?",
            (rollup_cutoff,),
        )
        dimensions = (
            day_start,
            CLIENT_REF_VERSION,
            observation.client_ref,
            observation.client_name,
            observation.client_version,
            observation.platform,
            observation.category,
        )
        existing = connection.execute(
            "SELECT 1 FROM mgboost_hwid_compat_subjects "
            "WHERE day_start=? AND client_ref_version=? AND client_ref=? "
            "AND client_name=? AND client_version=? AND platform=? "
            "AND compatibility_category=?",
            dimensions,
        ).fetchone()
        if existing:
            connection.execute(
                "UPDATE mgboost_hwid_compat_subjects "
                "SET request_count=request_count+1,last_seen=? "
                "WHERE day_start=? AND client_ref_version=? AND client_ref=? "
                "AND client_name=? AND client_version=? AND platform=? "
                "AND compatibility_category=?",
                (timestamp,) + dimensions,
            )
            connection.execute(
                "UPDATE mgboost_hwid_compat_daily "
                "SET request_count=request_count+1,"
                "repeat_request_count=repeat_request_count+1,last_seen=? "
                "WHERE day_start=? AND client_name=? AND client_version=? "
                "AND platform=? AND compatibility_category=?",
                (
                    timestamp,
                    day_start,
                    observation.client_name,
                    observation.client_version,
                    observation.platform,
                    observation.category,
                ),
            )
        else:
            subject_rows = connection.execute(
                "SELECT COUNT(*) FROM mgboost_hwid_compat_subjects WHERE day_start=?",
                (day_start,),
            ).fetchone()[0]
            if subject_rows >= MAX_SUBJECT_ROWS_PER_DAY:
                raise TelemetryCapacityReached("daily compatibility subject cap reached")
            rollup_exists = connection.execute(
                "SELECT 1 FROM mgboost_hwid_compat_daily "
                "WHERE day_start=? AND client_name=? AND client_version=? "
                "AND platform=? AND compatibility_category=?",
                (
                    day_start,
                    observation.client_name,
                    observation.client_version,
                    observation.platform,
                    observation.category,
                ),
            ).fetchone()
            if not rollup_exists:
                rollup_rows = connection.execute(
                    "SELECT COUNT(*) FROM mgboost_hwid_compat_daily WHERE day_start=?",
                    (day_start,),
                ).fetchone()[0]
                if rollup_rows >= MAX_ROLLUP_ROWS_PER_DAY:
                    raise TelemetryCapacityReached("daily compatibility rollup cap reached")
            connection.execute(
                "INSERT INTO mgboost_hwid_compat_subjects "
                "(day_start,client_ref_version,client_ref,client_name,client_version,"
                "platform,compatibility_category,request_count,first_seen,last_seen) "
                "VALUES (?,?,?,?,?,?,?,1,?,?)",
                dimensions + (timestamp, timestamp),
            )
            connection.execute(
                "INSERT INTO mgboost_hwid_compat_daily "
                "(day_start,client_name,client_version,platform,compatibility_category,"
                "request_count,correlated_subject_count,repeat_request_count,first_seen,last_seen) "
                "VALUES (?,?,?,?,?,1,1,0,?,?) "
                "ON CONFLICT(day_start,client_name,client_version,platform,compatibility_category) "
                "DO UPDATE SET request_count=request_count+1,"
                "correlated_subject_count=correlated_subject_count+1,"
                "last_seen=excluded.last_seen",
                (
                    day_start,
                    observation.client_name,
                    observation.client_version,
                    observation.platform,
                    observation.category,
                    timestamp,
                    timestamp,
                ),
            )
        connection.commit()
        return observation
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def cleanup_expired(
    db_path: str,
    *,
    now: int,
    timeout_seconds: float = 10.0,
) -> dict[str, int]:
    """Enforce the fixed 30/60-day policy independently of request traffic."""
    timestamp = int(now)
    day_start = timestamp - (timestamp % SECONDS_PER_DAY)
    detail_cutoff = day_start - DETAIL_RETENTION_DAYS * SECONDS_PER_DAY
    rollup_cutoff = day_start - ROLLUP_RETENTION_DAYS * SECONDS_PER_DAY
    connection = sqlite3.connect(
        db_path, timeout=max(0.0, float(timeout_seconds)), isolation_level=None
    )
    try:
        connection.execute(
            f"PRAGMA busy_timeout={max(0, int(float(timeout_seconds) * 1000))}"
        )
        connection.execute("BEGIN IMMEDIATE")
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name IN ('mgboost_hwid_compat_subjects','mgboost_hwid_compat_daily')"
            )
        }
        if tables != {"mgboost_hwid_compat_subjects", "mgboost_hwid_compat_daily"}:
            # Safe during first-start migration races and application rollback.
            connection.commit()
            return {"detail_rows_deleted": 0, "rollup_rows_deleted": 0}
        details = connection.execute(
            "DELETE FROM mgboost_hwid_compat_subjects WHERE day_start<?",
            (detail_cutoff,),
        ).rowcount
        rollups = connection.execute(
            "DELETE FROM mgboost_hwid_compat_daily WHERE day_start<?",
            (rollup_cutoff,),
        ).rowcount
        connection.commit()
        return {"detail_rows_deleted": details, "rollup_rows_deleted": rollups}
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
