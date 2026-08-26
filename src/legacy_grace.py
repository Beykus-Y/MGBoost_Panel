"""PH4-05 durable legacy grace-period store.

Fixed 14-day UTC policy (OPD-09/DL-023): `GRACE_PERIOD_SECONDS` below is the
single source of truth and is asserted equal to the schema's own CHECK
constraint by `tests/test_legacy_grace_schema.py`, so the two can never
silently drift.

Boundary semantics (exact, tested): a request at `now < current_end_at` is
still within grace; `now == current_end_at` and any `now > current_end_at`
are both outside grace. This module never itself denies/revokes a legacy
request -- it only exposes `grace_active()`/`seconds_remaining()` as pure
functions of durable state for a caller (PH4-06, not built yet) to consult.

Starting a grace period requires the same sealed `PrimaryAdminAuthority`
capability every other destructive/consequential PH3-06/PH4-01..04 action
already requires. Extending one requires it too, and is the only way
`current_end_at` can move -- there is no code path that silently restarts,
resets or shortens an account's clock; the DB trigger in
`legacy_grace_schema.py` backstops this even against a bug in this module.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time

from .admin_authority import PrimaryAdminAuthorizationError
from .legacy_grace_schema import GRACE_PERIOD_SECONDS


class LegacyGraceError(RuntimeError):
    pass


class GraceConflict(LegacyGraceError):
    """Same account, different start payload -- never silently merged."""


class GraceAlreadyStarted(LegacyGraceError):
    """A genuinely new start attempt for an account that already has one.

    An account's grace lineage can only ever be started once; asking to
    start it again (a different idempotency key) is a caller bug, not a
    reset -- extend the existing one instead."""


class GraceStaleRevision(LegacyGraceError):
    pass


class GraceTransitionError(LegacyGraceError):
    pass


class PrimaryAdminRequired(LegacyGraceError):
    pass


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical(value: dict) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def grace_active(current_end_at: int, *, now: int) -> bool:
    """Exact boundary: `now == current_end_at` is already outside grace."""
    return int(now) < int(current_end_at)


def seconds_remaining(current_end_at: int, *, now: int) -> int:
    return max(0, int(current_end_at) - int(now))


def day_index(started_at: int, *, now: int) -> int:
    """1-based day-of-grace for display ("day 3 of 14"); never negative,
    uncapped above 14 so an extended/overdue account still reports truthfully."""
    elapsed = max(0, int(now) - int(started_at))
    return elapsed // 86400 + 1


class LegacyGraceStore:
    def __init__(self, connection: sqlite3.Connection, lock, primary_admin_authority=None):
        self._conn = connection
        self._lock = lock
        self._authority = primary_admin_authority

    def _require_primary(self, capability) -> str:
        if self._authority is None:
            raise PrimaryAdminRequired("primary admin authority not configured")
        try:
            return self._authority.require(capability)
        except PrimaryAdminAuthorizationError:
            raise PrimaryAdminRequired("primary MGBoost admin capability required")

    def _event(self, grace_period_id, account_id, event_type, *, from_end_at, to_end_at,
               actor_ref, reason, evidence_ref, now):
        self._conn.execute(
            "INSERT INTO mgboost_legacy_grace_events "
            "(grace_period_id,account_id,event_type,from_end_at,to_end_at,actor_ref,reason,"
            "evidence_ref,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (grace_period_id, account_id, event_type, from_end_at, to_end_at, actor_ref,
             reason, evidence_ref, now),
        )

    # --- read-only lookups ---------------------------------------------------

    def find_by_account(self, account_id: int) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM mgboost_legacy_grace_periods WHERE account_id=?",
            (int(account_id),),
        ).fetchone()
        return dict(row) if row else None

    def list_active(self, *, now: int) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM mgboost_legacy_grace_periods WHERE current_end_at > ? "
            "ORDER BY started_at ASC",
            (int(now),),
        ).fetchall()
        return [dict(row) for row in rows]

    def list_expired(self, *, now: int) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM mgboost_legacy_grace_periods WHERE current_end_at <= ? "
            "ORDER BY current_end_at ASC",
            (int(now),),
        ).fetchall()
        return [dict(row) for row in rows]

    def list_by_cohort(self, cohort_ref: str | None = None) -> list[dict]:
        """All grace rows, optionally filtered to one cohort_ref. No time
        filtering -- callers wanting active/expired-only use the two
        methods above."""
        if cohort_ref is None:
            rows = self._conn.execute(
                "SELECT * FROM mgboost_legacy_grace_periods ORDER BY account_id ASC",
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM mgboost_legacy_grace_periods WHERE cohort_ref=? ORDER BY account_id ASC",
                (cohort_ref,),
            ).fetchall()
        return [dict(row) for row in rows]

    # --- start (idempotent, primary-admin gated) ------------------------------

    def start(
        self, *, account_id: int, cohort_ref: str, capability, reason: str,
        idempotency_key: str, now: int | None = None,
    ) -> dict:
        actor = self._require_primary(capability)
        reason = (reason or "").strip()
        if not 3 <= len(reason) <= 300:
            raise LegacyGraceError("a bounded human-readable reason is required")
        cohort_ref = (cohort_ref or "").strip()
        if not 1 <= len(cohort_ref) <= 128:
            raise LegacyGraceError("a bounded cohort reference is required")
        if not isinstance(idempotency_key, str) or not 16 <= len(idempotency_key) <= 512:
            raise LegacyGraceError("invalid idempotency key")
        timestamp = int(time.time()) if now is None else int(now)
        idem_hash = _sha("legacy-grace-v1\0" + idempotency_key)
        payload = {"account_id": int(account_id), "cohort_ref": cohort_ref}
        request_hash = _sha(_canonical(payload))
        original_end_at = timestamp + GRACE_PERIOD_SECONDS
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                by_account = self._conn.execute(
                    "SELECT * FROM mgboost_legacy_grace_periods WHERE account_id=?",
                    (int(account_id),),
                ).fetchone()
                if by_account:
                    if by_account["idempotency_key_hash"] == idem_hash:
                        if by_account["request_hash"] != request_hash:
                            raise GraceConflict(
                                "idempotency key reused with a different grace-start request"
                            )
                        self._conn.commit()
                        return dict(by_account)
                    raise GraceAlreadyStarted(
                        "this account's legacy grace period was already started -- "
                        "use extend(), never a second start"
                    )
                prior = self._conn.execute(
                    "SELECT * FROM mgboost_legacy_grace_periods WHERE idempotency_key_hash=?",
                    (idem_hash,),
                ).fetchone()
                if prior:
                    if prior["request_hash"] != request_hash:
                        raise GraceConflict(
                            "idempotency key reused with a different grace-start request"
                        )
                    self._conn.commit()
                    return dict(prior)
                cursor = self._conn.execute(
                    "INSERT INTO mgboost_legacy_grace_periods "
                    "(account_id,cohort_ref,started_at,original_end_at,current_end_at,revision,"
                    "idempotency_key_hash,request_hash,actor_ref,reason,created_at,updated_at) "
                    "VALUES (?,?,?,?,?,1,?,?,?,?,?,?)",
                    (int(account_id), cohort_ref, timestamp, original_end_at, original_end_at,
                     idem_hash, request_hash, actor, reason, timestamp, timestamp),
                )
                self._event(
                    cursor.lastrowid, int(account_id), "STARTED",
                    from_end_at=None, to_end_at=original_end_at, actor_ref=actor,
                    reason=reason, evidence_ref=None, now=timestamp,
                )
                row = self._conn.execute(
                    "SELECT * FROM mgboost_legacy_grace_periods WHERE id=?", (cursor.lastrowid,),
                ).fetchone()
                self._conn.commit()
                return dict(row)
            except Exception:
                self._conn.rollback()
                raise

    # --- extend (explicit, audited, monotonic-forward only) -------------------

    def extend(
        self, *, account_id: int, expected_revision: int, new_end_at: int, capability,
        reason: str, evidence_ref: str | None = None, now: int | None = None,
    ) -> dict:
        actor = self._require_primary(capability)
        reason = (reason or "").strip()
        if not 3 <= len(reason) <= 300:
            raise LegacyGraceError("a bounded human-readable reason is required")
        if evidence_ref is not None:
            evidence_ref = evidence_ref.strip() or None
            if evidence_ref is not None and not 1 <= len(evidence_ref) <= 256:
                raise LegacyGraceError("invalid evidence reference")
        timestamp = int(time.time()) if now is None else int(now)
        new_end_at = int(new_end_at)
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                row = self._conn.execute(
                    "SELECT * FROM mgboost_legacy_grace_periods WHERE account_id=?",
                    (int(account_id),),
                ).fetchone()
                if row is None:
                    raise LegacyGraceError("no grace period exists for this account")
                if row["revision"] != int(expected_revision):
                    raise GraceStaleRevision("stale revision -- a newer decision already applied")
                if new_end_at <= row["current_end_at"]:
                    raise GraceTransitionError(
                        "extension must move current_end_at strictly forward -- "
                        "silent no-op/shortening is not allowed"
                    )
                self._conn.execute(
                    "UPDATE mgboost_legacy_grace_periods SET current_end_at=?,revision=revision+1,"
                    "updated_at=?,row_version=row_version+1 WHERE id=? AND revision=?",
                    (new_end_at, timestamp, row["id"], expected_revision),
                )
                self._event(
                    row["id"], row["account_id"], "EXTENDED",
                    from_end_at=row["current_end_at"], to_end_at=new_end_at, actor_ref=actor,
                    reason=reason, evidence_ref=evidence_ref, now=timestamp,
                )
                result = self._conn.execute(
                    "SELECT * FROM mgboost_legacy_grace_periods WHERE id=?", (row["id"],),
                ).fetchone()
                self._conn.commit()
                return dict(result)
            except Exception:
                self._conn.rollback()
                raise

    # --- events ----------------------------------------------------------------

    def list_events(self, account_id: int) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM mgboost_legacy_grace_events WHERE account_id=? ORDER BY id ASC",
            (int(account_id),),
        ).fetchall()
        return [dict(row) for row in rows]
