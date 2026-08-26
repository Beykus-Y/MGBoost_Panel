"""PH6-02 -- ADMIN_RESET: close the current WL period, create a successor.

Never rewrites `mgboost_wl_periods` identity/quota fields (PH5-02's own
immutability trigger already forbids that); only ever moves `status`
forward on the old row and inserts a brand-new row for the successor, plus
one immutable audit row linking the two. Requires the same sealed
`PrimaryAdminAuthority` capability every other consequential PH3-06+
action requires.
"""

from __future__ import annotations

import sqlite3
import time

from .subscription_renewal import align_to_utc_hour


class WLPeriodResetError(ValueError):
    pass


class PeriodNotFound(WLPeriodResetError):
    pass


class PeriodNotResettable(WLPeriodResetError):
    pass


class PrimaryAdminRequired(WLPeriodResetError):
    pass


class WLPeriodAdminResetStore:
    def __init__(self, connection: sqlite3.Connection, lock, primary_admin_authority):
        self._conn = connection
        self._lock = lock
        self._authority = primary_admin_authority

    def _require_primary(self, capability) -> str:
        from .admin_authority import PrimaryAdminAuthorizationError
        try:
            return self._authority.require(capability)
        except PrimaryAdminAuthorizationError:
            raise PrimaryAdminRequired("primary MGBoost admin capability required")

    def reset_period(
        self,
        *,
        capability,
        period_id: int,
        reason: str,
        actor_ref: str | None = None,
        now: int | None = None,
    ) -> dict:
        authorized_actor_id = self._require_primary(capability)
        actor_ref = actor_ref or authorized_actor_id
        if not reason or not isinstance(reason, str):
            raise WLPeriodResetError("reason is required")
        timestamp = int(time.time()) if now is None else int(now)
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")

                period = self._conn.execute(
                    "SELECT * FROM mgboost_wl_periods WHERE id=?", (int(period_id),)
                ).fetchone()
                if period is None:
                    raise PeriodNotFound(f"WL period {period_id} not found")
                if period["status"] not in ("PLANNED", "ACTIVE"):
                    raise PeriodNotResettable(
                        f"WL period {period_id} has status {period['status']!r}; "
                        "only PLANNED/ACTIVE periods can be reset"
                    )
                already = self._conn.execute(
                    "SELECT 1 FROM mgboost_wl_period_resets WHERE closed_period_id=?",
                    (int(period_id),),
                ).fetchone()
                if already is not None:
                    raise PeriodNotResettable(f"WL period {period_id} was already reset")

                new_start = align_to_utc_hour(timestamp)
                new_end = period["ends_at"]
                if new_start >= new_end:
                    raise PeriodNotResettable(
                        "reset time is at or past the current period's own end; "
                        "let the period close naturally instead"
                    )

                self._conn.execute(
                    "UPDATE mgboost_wl_periods SET status='CLOSED' WHERE id=?",
                    (int(period_id),),
                )

                next_seq = self._conn.execute(
                    "SELECT COALESCE(MAX(sequence_no),0) FROM mgboost_wl_periods "
                    "WHERE subscription_id=?", (period["subscription_id"],),
                ).fetchone()[0] + 1

                successor_cursor = self._conn.execute(
                    "INSERT INTO mgboost_wl_periods "
                    "(account_id,subscription_id,subscription_term_id,sequence_no,"
                    "starts_at,ends_at,quota_mode,base_quota_bytes,status,created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        period["account_id"], period["subscription_id"], period["subscription_term_id"],
                        next_seq, new_start, new_end, period["quota_mode"], period["base_quota_bytes"],
                        "ACTIVE" if period["status"] == "ACTIVE" else "PLANNED",
                        timestamp,
                    ),
                )
                successor_id = successor_cursor.lastrowid

                self._conn.execute(
                    "INSERT INTO mgboost_wl_period_resets "
                    "(account_id,subscription_id,closed_period_id,successor_period_id,"
                    "reason,actor_type,actor_ref,created_at) VALUES (?,?,?,?,?,?,?,?)",
                    (
                        period["account_id"], period["subscription_id"], int(period_id), successor_id,
                        reason, "ADMIN", actor_ref, timestamp,
                    ),
                )

                self._conn.commit()
                return {
                    "closed_period_id": int(period_id),
                    "successor_period_id": successor_id,
                    "successor_sequence_no": next_seq,
                    "successor_starts_at": new_start,
                    "successor_ends_at": new_end,
                }
            except Exception:
                self._conn.rollback()
                raise
