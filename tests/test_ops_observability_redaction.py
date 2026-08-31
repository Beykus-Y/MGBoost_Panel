"""PH8-04 -- redaction + fail-open regression baseline.

Step 1 scaffolding, established BEFORE any new `mgboost_ops_*`
counters/tables exist. Two properties are locked in now so later steps
(new counters, the acquisition milestone, the ops-monitor) can be
exercised against the same harness without re-deriving it:

1. Redaction: the existing operator read models (`backlog_snapshot`,
   `usage_freshness`) never surface a raw token/HWID/UUID/username/
   password -- proven structurally (their source tables have no such
   columns) and behaviorally (their JSON output never contains one).
2. Fail-open: the existing hot-path rate limiters
   (`AdminLoginRateLimiter`, `SubscriptionRateLimiter`) never touch
   sqlite at all, so a broken/locked telemetry store cannot possibly
   change their decision or add DB-timeout latency -- this is the
   baseline PH8-04's new counters must preserve once they exist.
"""

from __future__ import annotations

import importlib
import json
import os
import sqlite3
import tempfile

import pytest

from src import security
from src.subscription_rate_limit import SubscriptionRateLimiter
from src.wl_freshness import usage_freshness
from src.wl_reconciliation import backlog_snapshot

from tests.test_child_provisioning import PRIMARY, PRIMARY_LOGIN


FORBIDDEN_COLUMN_SUBSTRINGS = ("hwid", "token", "uuid", "password", "username", "bearer")

# Tables read (directly or transitively) by backlog_snapshot()/usage_freshness().
_BACKLOG_SNAPSHOT_SOURCE_TABLES = (
    "mgboost_wl_reconciliation_cycles",
    "mgboost_wl_enforcement_states",
    "mgboost_wl_enforcement_ops",
    "mgboost_wl_reconciliation_drift",
    "mgboost_wl_usage_collector_lease",
)


@pytest.fixture
def db(monkeypatch):
    tmp = tempfile.mkdtemp()
    monkeypatch.setenv("DATA_DIR", tmp)
    monkeypatch.setenv("PRIMARY_MGBOOST_ADMIN_ACTOR_ID", PRIMARY)
    monkeypatch.setenv("PRIMARY_MGBOOST_ADMIN_LOGIN", PRIMARY_LOGIN)
    import src.config as config
    import src.database as database
    importlib.reload(config)
    importlib.reload(database)
    database.DB_PATH = os.path.join(tmp, "db.sqlite3")
    instance = database.Database()
    yield instance
    instance._conn.close()


# ---------------------------------------------------------------------------
# 1. Redaction
# ---------------------------------------------------------------------------

def test_backlog_snapshot_source_tables_have_no_raw_identifier_columns(db):
    """Structural guarantee: the tables backlog_snapshot() aggregates over
    have no column that could hold a raw token/HWID/UUID/username/password
    in the first place -- there is nothing to accidentally leak."""
    for table in _BACKLOG_SNAPSHOT_SOURCE_TABLES:
        columns = {row[1].lower() for row in db._conn.execute(f"PRAGMA table_info({table})")}
        for forbidden in FORBIDDEN_COLUMN_SUBSTRINGS:
            offenders = [c for c in columns if forbidden in c]
            assert not offenders, (
                f"{table} has column(s) {offenders} matching forbidden "
                f"substring {forbidden!r} -- backlog_snapshot() must stay "
                f"identifier-free"
            )


def test_backlog_snapshot_output_is_identifier_free(db):
    result = backlog_snapshot(db, now=1_700_000_000)
    serialized = json.dumps(result, sort_keys=True, default=str).lower()
    for forbidden in FORBIDDEN_COLUMN_SUBSTRINGS:
        assert forbidden not in serialized, (
            f"backlog_snapshot() output unexpectedly contains {forbidden!r}: "
            f"{serialized}"
        )


def test_usage_freshness_output_is_identifier_free(db):
    result = usage_freshness(db, now=1_700_000_000)
    serialized = json.dumps(result, sort_keys=True, default=str).lower()
    for forbidden in FORBIDDEN_COLUMN_SUBSTRINGS:
        assert forbidden not in serialized, (
            f"usage_freshness() output unexpectedly contains {forbidden!r}: "
            f"{serialized}"
        )


# ---------------------------------------------------------------------------
# 2. Fail-open hot-path baseline (locked-DB harness)
# ---------------------------------------------------------------------------

def test_admin_login_limiter_never_touches_sqlite(monkeypatch):
    """The admin-login hot path must be able to decide/record a failure with
    sqlite3.connect entirely unavailable -- proves today's baseline before
    PH8-04 adds any auth-failure counter alongside this limiter."""

    def _forbidden_connect(*args, **kwargs):
        raise AssertionError(
            "AdminLoginRateLimiter must never call sqlite3.connect on the "
            "admin-login hot path"
        )

    monkeypatch.setattr(sqlite3, "connect", _forbidden_connect)

    limiter = security.AdminLoginRateLimiter(
        window_seconds=10, identity_failures=2, ip_failures=10
    )
    limiter.record_failure("198.51.100.1", "admin", now=100)
    limiter.record_failure("198.51.100.1", "admin", now=101)
    assert limiter.retry_after("198.51.100.1", "admin", now=102) == 8
    limiter.record_success("198.51.100.1", "admin")
    assert limiter.retry_after("198.51.100.1", "admin", now=103) == 0


def test_subscription_rate_limiter_never_touches_sqlite(monkeypatch):
    """Same baseline for the public /sub/{token} hot path."""

    def _forbidden_connect(*args, **kwargs):
        raise AssertionError(
            "SubscriptionRateLimiter must never call sqlite3.connect on the "
            "subscription-fetch hot path"
        )

    monkeypatch.setattr(sqlite3, "connect", _forbidden_connect)

    limiter = SubscriptionRateLimiter(window_seconds=10, max_requests=2)
    assert limiter.check("203.0.113.5", now=100) == 0
    assert limiter.check("203.0.113.5", now=101) == 0
    retry_after = limiter.check("203.0.113.5", now=102)
    assert retry_after > 0


# ---------------------------------------------------------------------------
# 3. Lint precedent: raw exception interpolation (the src/stars.py:624 leak
#    class) must never appear in logger.*/print() calls anywhere in src/.
# ---------------------------------------------------------------------------

def test_logger_calls_never_interpolate_raw_exception_objects():
    """Regression fixture for the exact leak this PH8-04 revision fixed at
    src/stars.py:624 (`f"...{e}"` -> `type(e).__name__`). A raw exception
    object can echo request/response bodies from an underlying HTTP client,
    which could include a token -- every logger.*/print() call must log the
    exception's type name, never the exception object itself."""
    import re

    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src_dir = os.path.join(repo_root, "src")
    offenders = []
    # Matches an f-string/format call whose interpolated expression is a
    # bare exception-like name (commonly `e`/`exc`/`err`), not an attribute
    # access on it (e.g. `{type(e).__name__}` or `{e.args}` are both fine).
    pattern = re.compile(r"\{(e|exc|err|error)\}")
    for dirpath, _dirnames, filenames in os.walk(src_dir):
        for filename in filenames:
            if not filename.endswith(".py"):
                continue
            path = os.path.join(dirpath, filename)
            text = open(path, encoding="utf-8").read()
            for lineno, line in enumerate(text.splitlines(), start=1):
                if ("logger." not in line and "print(" not in line):
                    continue
                if pattern.search(line):
                    offenders.append(f"{path}:{lineno}: {line.strip()}")
    assert offenders == [], (
        "logger.*/print() calls must never interpolate a raw exception "
        "object (use type(e).__name__ instead) -- offenders:\n"
        + "\n".join(offenders)
    )
