"""Observe-only, privacy-safe PH4-05 grace-period activity counters.

Mirrors `compat_telemetry.py`'s own isolated short-timeout connection
pattern deliberately -- a busy/slow write here must never contend with, block
or affect the main request-serving connection/lock, and any failure here
must never change what a legacy/opaque subscription response contains.
Callers (`routes/sub.py`, `routes/opaque_sub.py`) always wrap this
fail-open, exactly like the existing PH3-07 hook."""

from __future__ import annotations

import sqlite3

from .legacy_grace_activity_schema import CHANNELS

SECONDS_PER_DAY = 86400
RETENTION_DAYS = 60  # 14-day grace + support/post-mortem buffer, matches PH3-07's rollup precedent
DEFAULT_SQLITE_TIMEOUT_SECONDS = 0.05


class InvalidChannel(ValueError):
    pass


def record_activity(
    db_path: str, account_id: int, channel: str, *, now: int,
    timeout_seconds: float = DEFAULT_SQLITE_TIMEOUT_SECONDS,
) -> None:
    if channel not in CHANNELS:
        raise InvalidChannel(f"unknown grace activity channel: {channel!r}")
    timestamp = int(now)
    day_start = timestamp - (timestamp % SECONDS_PER_DAY)
    cutoff = day_start - RETENTION_DAYS * SECONDS_PER_DAY
    connection = sqlite3.connect(
        db_path, timeout=max(0.0, float(timeout_seconds)), isolation_level=None
    )
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(f"PRAGMA busy_timeout={max(0, int(float(timeout_seconds) * 1000))}")
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "DELETE FROM mgboost_legacy_grace_activity_daily WHERE day_start<?", (cutoff,),
        )
        connection.execute(
            "INSERT INTO mgboost_legacy_grace_activity_daily "
            "(day_start,account_id,channel,request_count,first_seen,last_seen) "
            "VALUES (?,?,?,1,?,?) "
            "ON CONFLICT(day_start,account_id,channel) "
            "DO UPDATE SET request_count=request_count+1,last_seen=excluded.last_seen",
            (day_start, int(account_id), channel, timestamp, timestamp),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def count_since(
    connection: sqlite3.Connection, account_id: int, channel: str, *, since: int, now: int,
) -> int:
    """Sum of `request_count` for day buckets touching `[since, now]`. Day
    granularity means this can slightly over-count just outside the exact
    boundary -- acceptable for an operational "~24h/~72h" support metric,
    never used for grace boundary enforcement itself."""
    timestamp = int(now)
    since_day = int(since) - (int(since) % SECONDS_PER_DAY)
    row = connection.execute(
        "SELECT COALESCE(SUM(request_count),0) FROM mgboost_legacy_grace_activity_daily "
        "WHERE account_id=? AND channel=? AND day_start>=? AND day_start<=?",
        (int(account_id), channel, since_day, timestamp),
    ).fetchone()
    return int(row[0]) if row else 0


def last_seen(connection: sqlite3.Connection, account_id: int, channel: str) -> int | None:
    row = connection.execute(
        "SELECT MAX(last_seen) FROM mgboost_legacy_grace_activity_daily "
        "WHERE account_id=? AND channel=?",
        (int(account_id), channel),
    ).fetchone()
    return int(row[0]) if row and row[0] is not None else None


def cleanup_expired(
    db_path: str, *, now: int, timeout_seconds: float = 10.0,
) -> dict[str, int]:
    timestamp = int(now)
    day_start = timestamp - (timestamp % SECONDS_PER_DAY)
    cutoff = day_start - RETENTION_DAYS * SECONDS_PER_DAY
    connection = sqlite3.connect(
        db_path, timeout=max(0.0, float(timeout_seconds)), isolation_level=None
    )
    try:
        connection.execute(f"PRAGMA busy_timeout={max(0, int(float(timeout_seconds) * 1000))}")
        connection.execute("BEGIN IMMEDIATE")
        tables = {
            row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name='mgboost_legacy_grace_activity_daily'"
            )
        }
        if not tables:
            connection.commit()
            return {"rows_deleted": 0}
        deleted = connection.execute(
            "DELETE FROM mgboost_legacy_grace_activity_daily WHERE day_start<?", (cutoff,),
        ).rowcount
        connection.commit()
        return {"rows_deleted": deleted}
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
