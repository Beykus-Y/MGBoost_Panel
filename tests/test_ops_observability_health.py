"""PH8-04 Step 2 -- `GET /admin/ops/health` composed operator health
snapshot: happy path, fail-open per-signal degradation, redaction, admin
auth, and no arbitrary-source access. `tests/test_ops_observability_redaction.py`
covers the Step 1 baseline (existing `backlog_snapshot`/`usage_freshness`
redaction + hot-path fail-open); this file covers the Step 2 composition
(`health_snapshot`, `legacy_transition_review_snapshot`, `_safe_source`) and
the `/admin/ops/health` route itself.
"""

from __future__ import annotations

import json

import pytest

from src.ops_observability import health_snapshot, legacy_transition_review_snapshot
from src.routes.admin_ops import handle_admin_ops_health

from tests._ops_helpers import make_handler
from tests.test_legacy_commercial_transition import _legacy, _payment
from tests.test_legacy_paid_compat import db  # noqa: F401  (pytest fixture)


FORBIDDEN_SUBSTRINGS = ("hwid", "token", "uuid", "password", "username", "bearer")


def _manual_review_transition(db, *, tag, now):
    """Drives a real transition through the P0 engine into MANUAL_REVIEW --
    same sequence as
    test_legacy_commercial_transition.test_manual_review_requires_explicit_audited_retry_and_never_regrants_grace,
    reused here rather than faking rows with a raw INSERT (foreign keys are
    enforced)."""
    account_id, cap = _legacy(db, expiry=3600, username=f"lct-{tag}", tg=994900 + hash(tag) % 900)
    payment = _payment(db, cap, account_id, tag=tag)
    transition = db.legacy_commercial_transitions.create(
        cap, payment_record_id=payment["id"], reason="real paid transition", now=now,
    )
    transition = db.legacy_commercial_transitions.confirm_payment(cap, transition["id"], now=now)
    db.legacy_commercial_transitions.claim_due(worker_id="review-worker", now=transition["activation_at"])
    db.legacy_commercial_transitions.manual_review(
        transition["id"], reason="RemoteStateMismatch", now=transition["activation_at"],
    )
    return transition["id"], cap


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_health_snapshot_happy_path_status_ok(db):
    result = health_snapshot(db, now=1_700_000_000)
    assert result["status"] == "OK"
    assert result["generated_at"] == 1_700_000_000
    assert set(result["sources"].values()) == {"OK"}
    assert result["sources"].keys() == {
        "wl_reconciliation_backlog", "monotonicity", "error_reconcile",
        "legacy_transition_review",
    }
    # a fresh DB has no reconciliation cycles/bindings/transitions yet
    assert result["error_reconcile"]["count_in_state"] == 0
    assert result["legacy_transition_review"]["count_in_state"] == 0
    assert result["monotonicity"]["reset_events"] == 0


def test_legacy_transition_review_snapshot_counts_manual_review_backlog(db):
    transition_id, cap = _manual_review_transition(db, tag="snap", now=1000)
    stuck_at = db._conn.execute(
        "SELECT updated_at FROM mgboost_legacy_commercial_transitions WHERE id=?",
        (transition_id,),
    ).fetchone()["updated_at"]

    result = legacy_transition_review_snapshot(db, now=stuck_at + 200)
    assert result["count_in_state"] == 1
    assert result["oldest_age_seconds"] == 200
    assert result["manual_review_retries_total"] == 0

    db.legacy_commercial_transitions.retry_manual_review(
        cap, transition_id, reason="remote state was authoritatively verified", now=stuck_at + 201,
    )
    result = legacy_transition_review_snapshot(db, now=stuck_at + 300)
    assert result["count_in_state"] == 0
    assert result["manual_review_retries_total"] == 1


# ---------------------------------------------------------------------------
# Fail-open per-signal degradation
# ---------------------------------------------------------------------------

def test_health_snapshot_degrades_only_the_broken_signal(db, monkeypatch):
    """A broken WL reconciliation source must not prevent the other three
    signals (monotonicity, error_reconcile, legacy_transition_review) from
    resolving, and must not raise past health_snapshot()."""
    import sqlite3

    import src.ops_observability as ops_observability

    def _broken_backlog_snapshot(*_args, **_kwargs):
        raise sqlite3.OperationalError("no such table: mgboost_wl_reconciliation_cycles")

    monkeypatch.setattr(ops_observability, "backlog_snapshot", _broken_backlog_snapshot)

    result = health_snapshot(db, now=1_700_000_000)
    assert result["status"] == "DEGRADED"
    assert result["sources"]["wl_reconciliation_backlog"] == "UNKNOWN"
    assert result["collector_freshness"] == {
        "status": "UNKNOWN", "error_class": "OperationalError",
    }
    assert result["outbox"] == {"status": "UNKNOWN", "error_class": "OperationalError"}
    # the other three signals were never touched by the broken source
    assert result["sources"]["monotonicity"] == "OK"
    assert result["sources"]["error_reconcile"] == "OK"
    assert result["sources"]["legacy_transition_review"] == "OK"


def test_health_snapshot_never_raises_when_a_source_table_is_missing(db):
    """Simulates a not-yet-migrated/dropped table for one signal: the
    endpoint must degrade, never throw."""
    db._conn.execute("DROP TABLE mgboost_migration_bindings")
    db._conn.commit()

    result = health_snapshot(db, now=1_700_000_000)
    assert result["status"] == "DEGRADED"
    assert result["sources"]["error_reconcile"] == "UNKNOWN"
    assert result["error_reconcile"]["status"] == "UNKNOWN"
    assert "error_class" in result["error_reconcile"]
    # unaffected signals still resolve
    assert result["sources"]["wl_reconciliation_backlog"] == "OK"
    assert result["sources"]["legacy_transition_review"] == "OK"


def test_health_snapshot_never_raises_on_malformed_source_row(db):
    """A row in a state the read model doesn't group specially (here:
    SCHEDULED, not MANUAL_REVIEW) must not crash the aggregate -- COUNT/GROUP
    BY style read models degrade gracefully by construction; this proves the
    endpoint still returns 200-shaped output when such rows exist."""
    account_id, cap = _legacy(db, expiry=3600, username="lct-malformed", tg=994800)
    payment = _payment(db, cap, account_id, tag="malformed")
    db.legacy_commercial_transitions.create(
        cap, payment_record_id=payment["id"], reason="real paid transition", now=1000,
    )
    result = health_snapshot(db, now=1_700_000_000)
    assert result["status"] == "OK"
    assert result["legacy_transition_review"]["count_in_state"] == 0


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------

def test_health_snapshot_output_is_identifier_free(db):
    result = health_snapshot(db, now=1_700_000_000)
    serialized = json.dumps(result, sort_keys=True, default=str).lower()
    for forbidden in FORBIDDEN_SUBSTRINGS:
        assert forbidden not in serialized, (
            f"health_snapshot() output unexpectedly contains {forbidden!r}: {serialized}"
        )


def test_legacy_transition_review_source_tables_have_no_raw_identifier_columns(db):
    for table in (
        "mgboost_legacy_commercial_transitions",
        "mgboost_legacy_commercial_transition_events",
    ):
        columns = {row[1].lower() for row in db._conn.execute(f"PRAGMA table_info({table})")}
        for forbidden in FORBIDDEN_SUBSTRINGS:
            offenders = [c for c in columns if forbidden in c]
            assert not offenders, (
                f"{table} has column(s) {offenders} matching forbidden substring "
                f"{forbidden!r} -- legacy_transition_review_snapshot() must stay "
                f"identifier-free"
            )


def test_health_snapshot_never_leaks_a_raw_exception_message(db, monkeypatch):
    """The fail-open stub must carry only type(exc).__name__, never str(exc)
    -- a raw DB error message can echo a query/bound value."""
    import src.ops_observability as ops_observability

    def _broken(*_args, **_kwargs):
        raise RuntimeError("leaked secret-looking-value hwid=abc123 token=xyz")

    monkeypatch.setattr(ops_observability, "monotonicity_snapshot", _broken)
    result = health_snapshot(db, now=1_700_000_000)
    assert result["monotonicity"] == {"status": "UNKNOWN", "error_class": "RuntimeError"}
    serialized = json.dumps(result).lower()
    assert "leaked" not in serialized
    assert "secret-looking-value" not in serialized


# ---------------------------------------------------------------------------
# Route: admin auth + no arbitrary source access
# ---------------------------------------------------------------------------

def test_route_requires_admin_auth(db):
    h = make_handler(db, command="GET", authenticated=False)
    handle_admin_ops_health(h)
    assert h.status == 401


def test_route_returns_health_snapshot_for_authenticated_admin(db):
    h = make_handler(db, command="GET")
    handle_admin_ops_health(h)
    assert h.status == 200
    body = h.json()
    assert body["status"] == "OK"
    assert "legacy_transition_review" in body


def test_route_ignores_query_string_source_selection(db):
    """The route must not accept a client-supplied source/query selector --
    only the fixed, predefined signal set is ever composed, regardless of
    what a query string asks for."""
    h = make_handler(db, command="GET", path="/admin/ops/health?source=/etc/passwd")
    handle_admin_ops_health(h)
    assert h.status == 200
    body = h.json()
    assert set(body["sources"].keys()) == {
        "wl_reconciliation_backlog", "monotonicity", "error_reconcile",
        "legacy_transition_review",
    }


# ---------------------------------------------------------------------------
# Deterministic ordering / bootstrap
# ---------------------------------------------------------------------------

def test_health_snapshot_shape_is_deterministic_across_calls(db):
    first = health_snapshot(db, now=1_700_000_000)
    second = health_snapshot(db, now=1_700_000_000)
    assert first == second


def test_database_module_does_not_import_ops_observability():
    """PH8-04 Step 2 introduced no new schema/table and no startup call --
    `ops_observability` is imported only by the admin route, never by
    `src/database.py`. A broken observability module must not be able to
    crash server bootstrap because bootstrap never touches it."""
    import os
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    source = open(os.path.join(repo_root, "src", "database.py"), encoding="utf-8").read()
    assert "ops_observability" not in source
