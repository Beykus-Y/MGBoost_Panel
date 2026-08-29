"""PH5-02 same-plan renewal formula + WL-period scheduling.

This is the "PH6 period interface" `PH5-02`'s own roadmap entry depends on:
not a wait for Phase 6 code to exist first (`PH6-02 Immutable WL periods`
itself depends on PH5-02, so that would be a cycle), but the contract PH6-02
will later consume -- sequential, UTC-epoch-second-aligned, immutable-once-
created WL period rows in the already-existing PH3-01 `mgboost_wl_periods`
table. This module is that contract's producer.

Scope, per `ROADMAP.md` PH5-02:
  - DL-044's exact renewal formula: `max(current_expiry, now) + purchased_duration`.
    An active subscription (current_expiry in the future) extends from its
    current expiry; an expired/absent one extends from `now`. Both are the
    same formula (`max(current_expiry, now)` degenerates to `now` when
    `current_expiry` is None or already in the past) -- no separate
    active/expired branch is needed or used.
  - 60-day purchase = two sequential 30-day WL periods, never a merged
    60-day quota (roadmap "Approved product catalog" note / DL-044). A
    Non-WL plan (`wl_mode='NONE'`) creates zero periods -- Non-WL is
    unlimited, there is nothing to schedule.
  - Same-plan only: a different plan than the account's current live plan
    is refused (`PlanMismatch`) -- that is upgrade/downgrade policy
    (PH5-06), not built here and not a "stacking" input.
  - Idempotent: the exact same `idempotency_key` never applies twice (reuses
    the existing `mgboost_entitlement_mutations.idempotency_key_hash`
    uniqueness), matching every other PH3/PH4 capability-gated store's own
    pattern.
  - All timestamps are UTC epoch seconds throughout (`int(time.time())`);
    duration arithmetic is exact `duration_days * 86400` seconds -- there is
    no calendar-month or local-timezone semantics anywhere in this module.

Not wired to any live purchase flow yet: PH5-05 (Stars) and PH5-09 (manual
external payment) are the future callers, each responsible for verifying
its own payment/actor before calling this engine -- this module does not
gate by admin capability itself, matching PH5-04's own "deterministic
engine, no username hardcode" framing (a pure inputs -> outputs primitive,
not an authorization boundary).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time


_NAMESPACE = "ph5-02-renewal-v1\0"


class RenewalError(ValueError):
    pass


class UnknownPlan(RenewalError):
    pass


class PlanMismatch(RenewalError):
    pass


class UnlimitedSubscriptionConflict(RenewalError):
    pass


def _idempotency_hash(idempotency_key: str) -> str:
    if not isinstance(idempotency_key, str) or not 16 <= len(idempotency_key) <= 512:
        raise RenewalError("idempotency_key must be a string between 16 and 512 characters")
    return hashlib.sha256((_NAMESPACE + idempotency_key).encode("utf-8")).hexdigest()


def compute_new_expiry(current_expiry: int | None, duration_days: int, *, now: int) -> tuple[int, int]:
    """DL-044: `max(current_expiry, now) + purchased_duration`. Returns (anchor, new_expiry)."""
    anchor = current_expiry if current_expiry is not None and current_expiry > now else now
    return anchor, anchor + int(duration_days) * 86400


def align_to_utc_hour(timestamp: int) -> int:
    """DL-020: WL quota periods are UTC-hour-aligned. Floors down to the
    start of the current UTC hour -- floor (not ceil) so that, because every
    plan duration is a whole number of days (a multiple of the 3600-second
    hour), flooring the anchor of each successive purchase always lands
    exactly on the previous purchase's own (also-floored) period boundary:
    no gap, no overlap, ever, across repeated purchases. Subscription
    expiry itself is never aligned this way (DL-020: "subscription expiry
    хранится отдельно") -- only the WL period anchor is.
    """
    return int(timestamp) - (int(timestamp) % 3600)


def schedule_wl_period_windows(
    *, anchor: int, duration_days: int, wl_period_days: int
) -> list[tuple[int, int]]:
    """Sequential, contiguous, non-overlapping `wl_period_days`-long windows
    covering exactly `duration_days` starting at `anchor`. A 60-day purchase
    on a 30-day-period plan returns exactly two windows, back-to-back.
    """
    if wl_period_days <= 0:
        raise RenewalError("wl_period_days must be positive")
    if duration_days % wl_period_days != 0:
        raise RenewalError(
            f"duration_days={duration_days} is not an exact multiple of "
            f"wl_period_days={wl_period_days}"
        )
    period_seconds = wl_period_days * 86400
    count = duration_days // wl_period_days
    return [
        (anchor + i * period_seconds, anchor + (i + 1) * period_seconds)
        for i in range(count)
    ]


class SubscriptionRenewalStore:
    def __init__(self, connection: sqlite3.Connection, lock, accounts, plan_catalog):
        self._conn = connection
        self._lock = lock
        self._accounts = accounts
        self._plan_catalog = plan_catalog

    def _replay(self, mutation_row) -> dict:
        term = self._conn.execute(
            "SELECT * FROM mgboost_subscription_terms WHERE mutation_id=?",
            (mutation_row["id"],),
        ).fetchone()
        periods = []
        if term is not None:
            periods = [
                {"sequence_no": row["sequence_no"], "starts_at": row["starts_at"], "ends_at": row["ends_at"]}
                for row in self._conn.execute(
                    "SELECT sequence_no,starts_at,ends_at FROM mgboost_wl_periods "
                    "WHERE subscription_term_id=? ORDER BY sequence_no",
                    (term["id"],),
                ).fetchall()
            ]
        after = json.loads(mutation_row["after_json"])
        return {
            **after,
            "term_id": term["id"] if term else None,
            "mutation_id": mutation_row["id"],
            "wl_periods": periods,
            "already_applied": True,
        }

    def apply_same_plan_purchase(
        self,
        *,
        account_id: int,
        plan_code: str,
        duration_days: int,
        payment_channel: str,
        mutation_source: str,
        actor_type: str,
        actor_ref: str | None = None,
        reason: str | None = None,
        external_reference: str | None = None,
        idempotency_key: str,
        now: int | None = None,
    ) -> dict:
        timestamp = int(time.time()) if now is None else int(now)
        idem_hash = _idempotency_hash(idempotency_key)
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")

                existing_mutation = self._conn.execute(
                    "SELECT * FROM mgboost_entitlement_mutations WHERE idempotency_key_hash=?",
                    (idem_hash,),
                ).fetchone()
                if existing_mutation is not None:
                    result = self._replay(existing_mutation)
                    self._conn.commit()
                    return result

                plan = self._plan_catalog.get_plan_version(plan_code)
                if plan is None:
                    raise UnknownPlan(f"unknown plan_code {plan_code!r}")
                if plan["plan_kind"] != "COMMERCIAL" or not plan["billing_required"]:
                    raise RenewalError("same-plan purchase requires a billed commercial plan")
                duration = self._plan_catalog.get_plan_duration(plan["id"], duration_days)
                if duration is None:
                    raise RenewalError(
                        f"plan {plan_code!r} has no {duration_days}-day duration in its catalog"
                    )

                account = self._accounts.get_account(int(account_id))
                if account is None or account["status"] == "CLOSED":
                    raise RenewalError("account not found or closed")

                subscription = self._conn.execute(
                    "SELECT s.*, pv.plan_code AS current_plan_code, "
                    "pv.billing_required AS current_plan_billing_required "
                    "FROM mgboost_subscriptions s "
                    "LEFT JOIN mgboost_plan_versions pv ON pv.id=s.current_plan_version_id "
                    "WHERE s.account_id=? ORDER BY s.id DESC LIMIT 1",
                    (int(account_id),),
                ).fetchone()

                if subscription is not None:
                    if subscription["status"] == "UNLIMITED":
                        raise UnlimitedSubscriptionConflict(
                            "account has an admin-granted unlimited subscription; a "
                            "commercial purchase must not overwrite it"
                        )
                    if subscription["current_plan_code"] not in (None, plan_code):
                        # PH5-13 promo trial exception: an EXPIRED WL_TRIAL is
                        # not a live commitment -- it must never block a real
                        # purchase of a DIFFERENT plan. Scoped strictly to the
                        # registered trial plan: any other billing_required=0
                        # plan (and any still-live trial) hits PlanMismatch
                        # unchanged -- never general upgrade/downgrade
                        # (PH5-06 unchanged).
                        expired_free_trial = (
                            subscription["current_plan_code"] == "WL_TRIAL"
                            and subscription["current_plan_billing_required"] == 0
                            and subscription["current_expiry"] is not None
                            and subscription["current_expiry"] <= timestamp
                        )
                        if not expired_free_trial:
                            raise PlanMismatch(
                                f"account's current plan is "
                                f"{subscription['current_plan_code']!r}, not {plan_code!r}; "
                                "a different-plan purchase is upgrade/downgrade policy (PH5-06)"
                            )
                    current_expiry = subscription["current_expiry"]
                    subscription_id = subscription["id"]
                    expected_row_version = subscription["row_version"]
                else:
                    current_expiry = None
                    subscription_id = None
                    expected_row_version = None

                anchor, new_expiry = compute_new_expiry(current_expiry, duration_days, now=timestamp)

                if subscription_id is None:
                    cursor = self._conn.execute(
                        "INSERT INTO mgboost_subscriptions "
                        "(account_id,current_plan_version_id,status,started_at,current_expiry,"
                        "created_at,updated_at,row_version) VALUES (?,?,?,?,?,?,?,1)",
                        (int(account_id), plan["id"], "ACTIVE", anchor, new_expiry,
                         timestamp, timestamp),
                    )
                    subscription_id = cursor.lastrowid
                else:
                    new_row_version = expected_row_version + 1
                    updated = self._conn.execute(
                        "UPDATE mgboost_subscriptions SET current_plan_version_id=?,"
                        "status='ACTIVE',current_expiry=?,updated_at=?,row_version=? "
                        "WHERE id=? AND account_id=? AND row_version=?",
                        (plan["id"], new_expiry, timestamp, new_row_version,
                         subscription_id, int(account_id), expected_row_version),
                    )
                    if updated.rowcount != 1:
                        raise RenewalError("concurrent subscription modification detected")

                before_payload = {
                    "current_expiry": current_expiry,
                    "plan_version_id": subscription["current_plan_version_id"] if subscription else None,
                }
                after_payload = {
                    "account_id": int(account_id),
                    "subscription_id": subscription_id,
                    "plan_code": plan_code,
                    "plan_version_id": plan["id"],
                    "duration_days": int(duration_days),
                    "anchor": anchor,
                    "new_expiry": new_expiry,
                }
                mutation_cursor = self._conn.execute(
                    "INSERT INTO mgboost_entitlement_mutations "
                    "(account_id,subscription_id,operation,payment_channel,mutation_source,"
                    "actor_type,actor_ref,reason,external_reference,idempotency_key_hash,"
                    "before_json,after_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (int(account_id), subscription_id,
                     "CREATE" if subscription is None else "RENEW",
                     payment_channel, mutation_source, actor_type, actor_ref, reason,
                     external_reference, idem_hash,
                     json.dumps(before_payload, sort_keys=True, separators=(",", ":")),
                     json.dumps(after_payload, sort_keys=True, separators=(",", ":")),
                     timestamp),
                )
                mutation_id = mutation_cursor.lastrowid

                existing_terms_seq = self._conn.execute(
                    "SELECT COALESCE(MAX(sequence_no),0) FROM mgboost_subscription_terms "
                    "WHERE subscription_id=?", (subscription_id,),
                ).fetchone()[0]
                term_snapshot = {
                    "plan_code": plan_code, "plan_version": plan["version"],
                    "duration_days": int(duration_days),
                    "device_limit_mode": plan["device_limit_mode"],
                    "device_limit": plan["device_limit"],
                    "wl_mode": plan["wl_mode"], "wl_quota_bytes": plan["wl_quota_bytes"],
                    "wl_period_days": plan["wl_period_days"],
                }
                term_cursor = self._conn.execute(
                    "INSERT INTO mgboost_subscription_terms ("
                    "account_id,subscription_id,sequence_no,plan_version_id,duration_id,"
                    "duration_days,starts_at,ends_at,billing_required_snapshot,"
                    "device_limit_mode_snapshot,device_limit_snapshot,wl_mode_snapshot,"
                    "wl_quota_bytes_snapshot,wl_period_days_snapshot,plan_snapshot_json,"
                    "mutation_id,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (int(account_id), subscription_id, existing_terms_seq + 1, plan["id"],
                     duration["id"], int(duration_days), anchor, new_expiry, 1,
                     plan["device_limit_mode"], plan["device_limit"], plan["wl_mode"],
                     plan["wl_quota_bytes"], plan["wl_period_days"],
                     json.dumps(term_snapshot, sort_keys=True, separators=(",", ":")),
                     mutation_id, timestamp),
                )
                term_id = term_cursor.lastrowid

                periods = []
                if plan["wl_mode"] == "LIMITED":
                    windows = schedule_wl_period_windows(
                        anchor=align_to_utc_hour(anchor), duration_days=int(duration_days),
                        wl_period_days=plan["wl_period_days"],
                    )
                    existing_wl_seq = self._conn.execute(
                        "SELECT COALESCE(MAX(sequence_no),0) FROM mgboost_wl_periods "
                        "WHERE subscription_id=?", (subscription_id,),
                    ).fetchone()[0]
                    for offset, (start, end) in enumerate(windows, start=1):
                        self._conn.execute(
                            "INSERT INTO mgboost_wl_periods "
                            "(account_id,subscription_id,subscription_term_id,sequence_no,"
                            "starts_at,ends_at,quota_mode,base_quota_bytes,status,created_at) "
                            "VALUES (?,?,?,?,?,?,?,?,?,?)",
                            (int(account_id), subscription_id, term_id, existing_wl_seq + offset,
                             start, end, "LIMITED", plan["wl_quota_bytes"], "PLANNED", timestamp),
                        )
                        periods.append({
                            "sequence_no": existing_wl_seq + offset,
                            "starts_at": start, "ends_at": end,
                        })

                self._conn.commit()
                return {
                    **after_payload,
                    "term_id": term_id,
                    "mutation_id": mutation_id,
                    "wl_periods": periods,
                    "already_applied": False,
                }
            except Exception:
                self._conn.rollback()
                raise

    def append_promo_wl_period(
        self,
        *,
        account_id: int,
        days: int,
        quota_bytes: int,
        operation: str,
        plan_version_id: int | None = None,
        mutation_source: str,
        payment_channel: str,
        actor_type: str,
        actor_ref: str | None = None,
        reason: str | None = None,
        idempotency_key: str,
        now: int | None = None,
    ) -> dict:
        """PH5-13 promo primitive: append exactly one immutable WL period of
        `days` days / `quota_bytes` quota, bypassing the plan_catalog entirely
        (no duration_days%catalog check, no billed-plan requirement). Used by
        `PromoStore` for EXTEND_SUBSCRIPTION (on an already-LIMITED plan,
        `plan_version_id=None` -- current plan identity untouched) and
        TRIAL_GRANT (`plan_version_id` = the real registered WL_TRIAL plan
        version -- this IS what legitimizes the trial's entitlement; never
        left unset for a subscription that doesn't already have one).

        The WL-period anchor is
        `max(MAX(existing wl_periods.ends_at), subscription.current_expiry,
        now)`: the promo period must never start before the subscription's
        own canonical end, otherwise an `ADMIN_EXPIRY_ADJUSTMENT` (PH7-01,
        which moves `current_expiry` alone) can leave the promo period
        stranded inside an already-paid-for term instead of extending the
        subscription past it. `current_expiry` itself is extended by the
        same DL-044 `max(current_expiry, now) + days` formula, kept in step
        via one CAS UPDATE (or INSERT for a fresh subscription). Idempotent by `idempotency_key`, same
        `mgboost_entitlement_mutations.idempotency_key_hash` boundary as
        every other PH5 writer."""
        timestamp = int(time.time()) if now is None else int(now)
        if not isinstance(days, int) or isinstance(days, bool) or days <= 0:
            raise RenewalError("days must be a positive integer")
        if not isinstance(quota_bytes, int) or isinstance(quota_bytes, bool) or quota_bytes <= 0:
            raise RenewalError("quota_bytes must be a positive integer")
        idem_hash = _idempotency_hash(idempotency_key)
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")

                existing_mutation = self._conn.execute(
                    "SELECT * FROM mgboost_entitlement_mutations WHERE idempotency_key_hash=?",
                    (idem_hash,),
                ).fetchone()
                if existing_mutation is not None:
                    result = self._replay(existing_mutation)
                    self._conn.commit()
                    return result

                account = self._accounts.get_account(int(account_id))
                if account is None or account["status"] == "CLOSED":
                    raise RenewalError("account not found or closed")

                subscription = self._conn.execute(
                    "SELECT * FROM mgboost_subscriptions WHERE account_id=? "
                    "ORDER BY id DESC LIMIT 1",
                    (int(account_id),),
                ).fetchone()

                if subscription is not None:
                    if subscription["status"] == "UNLIMITED":
                        raise UnlimitedSubscriptionConflict(
                            "account has an admin-granted unlimited subscription; "
                            "a promo period must not overwrite it"
                        )
                    current_expiry = subscription["current_expiry"]
                    subscription_id = subscription["id"]
                    expected_row_version = subscription["row_version"]
                    effective_plan_version_id = (
                        plan_version_id if plan_version_id is not None
                        else subscription["current_plan_version_id"]
                    )
                else:
                    if plan_version_id is None:
                        raise RenewalError(
                            "plan_version_id is required to create a subscription "
                            "for an account with no prior subscription"
                        )
                    current_expiry = None
                    subscription_id = None
                    expected_row_version = None
                    effective_plan_version_id = plan_version_id

                anchor, new_expiry = compute_new_expiry(current_expiry, days, now=timestamp)

                existing_max_ends_at = self._conn.execute(
                    "SELECT MAX(ends_at) FROM mgboost_wl_periods WHERE account_id=?",
                    (int(account_id),),
                ).fetchone()[0]
                period_anchor = max(
                    existing_max_ends_at or 0, current_expiry or 0, timestamp
                )
                period_start = align_to_utc_hour(period_anchor)
                period_end = period_start + int(days) * 86400

                if subscription_id is None:
                    cursor = self._conn.execute(
                        "INSERT INTO mgboost_subscriptions "
                        "(account_id,current_plan_version_id,status,started_at,current_expiry,"
                        "created_at,updated_at) VALUES (?,?,'ACTIVE',?,?,?,?)",
                        (int(account_id), effective_plan_version_id, anchor, new_expiry,
                         timestamp, timestamp),
                    )
                    subscription_id = cursor.lastrowid
                else:
                    new_row_version = expected_row_version + 1
                    updated = self._conn.execute(
                        "UPDATE mgboost_subscriptions SET current_plan_version_id=?,"
                        "status='ACTIVE',current_expiry=?,updated_at=?,row_version=? "
                        "WHERE id=? AND account_id=? AND row_version=?",
                        (effective_plan_version_id, new_expiry, timestamp, new_row_version,
                         subscription_id, int(account_id), expected_row_version),
                    )
                    if updated.rowcount != 1:
                        raise RenewalError("concurrent subscription modification detected")

                before_payload = {"current_expiry": current_expiry}
                after_payload = {
                    "account_id": int(account_id),
                    "subscription_id": subscription_id,
                    "days": int(days),
                    "quota_bytes": int(quota_bytes),
                    "anchor": anchor,
                    "new_expiry": new_expiry,
                    "period_starts_at": period_start,
                    "period_ends_at": period_end,
                }
                mutation_cursor = self._conn.execute(
                    "INSERT INTO mgboost_entitlement_mutations "
                    "(account_id,subscription_id,operation,payment_channel,mutation_source,"
                    "actor_type,actor_ref,reason,external_reference,idempotency_key_hash,"
                    "before_json,after_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (int(account_id), subscription_id, operation, payment_channel,
                     mutation_source, actor_type, actor_ref, reason, None, idem_hash,
                     json.dumps(before_payload, sort_keys=True, separators=(",", ":")),
                     json.dumps(after_payload, sort_keys=True, separators=(",", ":")),
                     timestamp),
                )
                mutation_id = mutation_cursor.lastrowid

                existing_terms_seq = self._conn.execute(
                    "SELECT COALESCE(MAX(sequence_no),0) FROM mgboost_subscription_terms "
                    "WHERE subscription_id=?", (subscription_id,),
                ).fetchone()[0]
                term_snapshot = {
                    "kind": operation, "days": int(days), "quota_bytes": int(quota_bytes),
                    "plan_version_id": effective_plan_version_id,
                }
                term_cursor = self._conn.execute(
                    "INSERT INTO mgboost_subscription_terms ("
                    "account_id,subscription_id,sequence_no,plan_version_id,duration_id,"
                    "duration_days,starts_at,ends_at,billing_required_snapshot,"
                    "device_limit_mode_snapshot,device_limit_snapshot,wl_mode_snapshot,"
                    "wl_quota_bytes_snapshot,wl_period_days_snapshot,plan_snapshot_json,"
                    "mutation_id,created_at) VALUES (?,?,?,?,NULL,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (int(account_id), subscription_id, existing_terms_seq + 1,
                     plan_version_id, int(days), period_start, period_end, 0,
                     None, None, "LIMITED", int(quota_bytes), int(days),
                     json.dumps(term_snapshot, sort_keys=True, separators=(",", ":")),
                     mutation_id, timestamp),
                )
                term_id = term_cursor.lastrowid

                existing_wl_seq = self._conn.execute(
                    "SELECT COALESCE(MAX(sequence_no),0) FROM mgboost_wl_periods "
                    "WHERE subscription_id=?", (subscription_id,),
                ).fetchone()[0]
                self._conn.execute(
                    "INSERT INTO mgboost_wl_periods "
                    "(account_id,subscription_id,subscription_term_id,sequence_no,"
                    "starts_at,ends_at,quota_mode,base_quota_bytes,status,created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (int(account_id), subscription_id, term_id, existing_wl_seq + 1,
                     period_start, period_end, "LIMITED", int(quota_bytes), "PLANNED", timestamp),
                )
                periods = [{
                    "sequence_no": existing_wl_seq + 1,
                    "starts_at": period_start, "ends_at": period_end,
                }]

                self._conn.commit()
                return {
                    **after_payload,
                    "term_id": term_id,
                    "mutation_id": mutation_id,
                    "wl_periods": periods,
                    "already_applied": False,
                }
            except Exception:
                self._conn.rollback()
                raise
