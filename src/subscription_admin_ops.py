"""PH7-01 admin expiry operations -- the durable writer behind the panel's
+7/+30/+60, -N, exact-date and end-now support actions.

No arbitrary SQL-edit path exists: the ONLY way to move an admin-chosen
expiry is this store, which

  * reuses the documented DL-044 anchor formula
    (`subscription_renewal.compute_new_expiry`: an ACTIVE subscription
    extends from its current expiry, an expired/absent one resumes from
    now) for +N days;
  * subtracts N days / sets an exact UTC-epoch second / ends the term now,
    always as one optimistic-CAS update of ONLY
    `mgboost_subscriptions.current_expiry` (status is never touched here --
    the canonical PH3-08 desired-state policy already derives EXPIRED from
    the wall clock);
  * appends the immutable evidence row to the EXISTING PH3-09/PH7-08 ledger
    (`mgboost_entitlement_mutations`, mutation_source='ADMIN') with actor /
    reason / before-after in the same transaction;
  * leaves WL periods, subscription terms and packages completely untouched
    (roadmap "no WL reset": the PH5-02/PH6 period chain stays exactly as it
    was; a later real purchase continues from its own documented anchor);

and nothing else. Child convergence after commit is the caller's job via the
existing `parent_sync.run_account_sync_cycle` -- any expiry change bumps the
desired-state revision through that cycle's own
`ParentSyncStore.refresh_desired_state` (the desired_expire comparison),
stamping fresh revision-stamped ops for every current child.

Retry/idempotency matches every other engine on this ledger: the same
idempotency key replays its original result (`already_applied`), a different
key is a genuinely new audited adjustment. Admin-granted UNLIMITED
subscriptions are refused (documented: not extendable, structurally
expiry-less), as are accounts without a live subscription or with an
UNKNOWN_LEGACY subscription without a plan version.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time

from .subscription_renewal import compute_new_expiry

_NAMESPACE = "ph7-01-admin-expiry-v1\0"

ADJUSTMENT_KINDS = ("EXTEND_DAYS", "REDUCE_DAYS", "SET_EXACT", "END_NOW")
_MAX_HORIZON_DAYS = 3650


class AdminExpiryError(ValueError):
    pass


class AdminExpiryConflict(AdminExpiryError):
    pass


def _idempotency_hash(idempotency_key: str) -> str:
    key = idempotency_key if isinstance(idempotency_key, str) else ""
    if not 16 <= len(key) <= 512:
        raise AdminExpiryError("idempotency_key must be a string of 16..512 characters")
    return hashlib.sha256((_NAMESPACE + key).encode("utf-8")).hexdigest()


def _clean_reason(reason) -> str:
    text = (reason or "").strip()
    if not 3 <= len(text) <= 300:
        raise AdminExpiryError("a bounded human-readable reason (3..300) is required")
    return text


def _validate_adjustment(kind: str, value) -> tuple[str, int | None]:
    if kind not in ADJUSTMENT_KINDS:
        raise AdminExpiryError(f"unknown adjustment kind {kind!r}")
    if kind == "END_NOW":
        return kind, None
    if isinstance(value, bool) or not isinstance(value, int):
        raise AdminExpiryError("an integer value is required for this adjustment kind")
    if kind == "EXTEND_DAYS":
        if not 1 <= value <= _MAX_HORIZON_DAYS:
            raise AdminExpiryError("days must be within 1..3650")
    elif kind == "REDUCE_DAYS":
        if not 1 <= value <= _MAX_HORIZON_DAYS:
            raise AdminExpiryError("days must be within 1..3650")
    else:  # SET_EXACT
        if value <= 0:
            raise AdminExpiryError("exact expiry must be a positive UTC epoch second")
    return kind, value


def _latest_subscription(conn, account_id: int):
    return conn.execute(
        "SELECT s.*,p.plan_code AS current_plan_code FROM mgboost_subscriptions s "
        "LEFT JOIN mgboost_plan_versions p ON p.id=s.current_plan_version_id "
        "WHERE s.account_id=? ORDER BY s.id DESC LIMIT 1",
        (int(account_id),),
    ).fetchone()


def _target_expiry(subscription, kind: str, value: int | None, *, now: int) -> int:
    current = subscription["current_expiry"]
    if kind == "EXTEND_DAYS":
        # The documented DL-044 anchor: resume-from-now when already expired.
        _, new_expiry = compute_new_expiry(current, int(value), now=now)
        return new_expiry
    if current is None:
        raise AdminExpiryError(
            "this subscription has no finite expiry to adjust"
        )
    if kind == "REDUCE_DAYS":
        return int(current) - int(value) * 86400
    if kind == "SET_EXACT":
        if int(value) > now + _MAX_HORIZON_DAYS * 86400:
            raise AdminExpiryError(
                f"exact expiry must stay within {_MAX_HORIZON_DAYS} days of now"
            )
        return int(value)
    return int(now)  # END_NOW


class SubscriptionAdminOpsStore:
    def __init__(self, connection: sqlite3.Connection, lock, primary_admin_authority):
        self._conn = connection
        self._lock = lock
        self._authority = primary_admin_authority

    # --- read-only preview -----------------------------------------------------

    def preview(self, account_id: int, *, adjustment_kind: str, value=None,
                now: int | None = None) -> dict:
        timestamp = int(time.time()) if now is None else int(now)
        adjustment_kind, value = _validate_adjustment(adjustment_kind, value)
        account_id = int(account_id)
        response: dict = {
            "account_id": account_id, "adjustment_kind": adjustment_kind,
            "value": value, "wl_periods_touched": False,
        }
        with self._lock:
            subscription = _latest_subscription(self._conn, account_id)
            if subscription is None:
                raise AdminExpiryError("account has no subscription")
            if subscription["status"] == "UNLIMITED":
                raise AdminExpiryError(
                    "ADMIN_GRANTED_UNLIMITED_NOT_ADJUSTABLE: an admin-granted "
                    "unlimited subscription has no finite expiry"
                )
            if subscription["current_plan_code"] is None:
                raise AdminExpiryError(
                    "UNKNOWN_LEGACY subscription without a plan version cannot be adjusted"
                )
            current = subscription["current_expiry"]
            target = _target_expiry(subscription, adjustment_kind, value, now=timestamp)
            response.update({
                "current_status": subscription["status"],
                "current_expiry": current,
                "new_expiry": target,
                "currently_expired": bool(current is not None and current <= timestamp),
                "becomes_expired_now": target <= timestamp,
            })
        return response

    # --- mutation ----------------------------------------------------------------

    def apply_adjustment(
        self, capability, *, account_id: int, adjustment_kind: str, value=None,
        reason: str, idempotency_key: str, now: int | None = None,
    ) -> dict:
        timestamp = int(time.time()) if now is None else int(now)
        reason = _clean_reason(reason)
        actor_ref = self._authority.require(capability)
        adjustment_kind, value = _validate_adjustment(adjustment_kind, value)
        idem_hash = _idempotency_hash(idempotency_key)
        account_id = int(account_id)

        replay = self._replay(idem_hash)
        if replay is not None:
            return replay

        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                subscription = _latest_subscription(self._conn, account_id)
                if subscription is None:
                    raise AdminExpiryError("account has no subscription")
                if subscription["status"] == "UNLIMITED":
                    raise AdminExpiryError(
                        "ADMIN_GRANTED_UNLIMITED_NOT_ADJUSTABLE: an admin-granted "
                        "unlimited subscription has no finite expiry"
                    )
                if subscription["current_plan_code"] is None:
                    raise AdminExpiryError(
                        "UNKNOWN_LEGACY subscription without a plan version cannot be adjusted"
                    )
                current_expiry = subscription["current_expiry"]
                new_expiry = _target_expiry(subscription, adjustment_kind, value, now=timestamp)

                updated = self._conn.execute(
                    "UPDATE mgboost_subscriptions SET current_expiry=?,updated_at=?,"
                    "row_version=row_version+1 WHERE id=? AND account_id=? AND row_version=?",
                    (new_expiry, timestamp, subscription["id"], account_id,
                     subscription["row_version"]),
                )
                if updated.rowcount != 1:
                    raise AdminExpiryConflict(
                        "concurrent subscription modification detected; retry to "
                        "recompute against the live state"
                    )

                before_payload = {
                    "current_expiry": current_expiry,
                    "subscription_status": subscription["status"],
                }
                after_payload = {
                    "account_id": account_id,
                    "subscription_id": subscription["id"],
                    "adjustment_kind": adjustment_kind,
                    "value": value,
                    "previous_expiry": current_expiry,
                    "new_expiry": new_expiry,
                    "wl_periods_touched": False,
                }
                cursor = self._conn.execute(
                    "INSERT INTO mgboost_entitlement_mutations "
                    "(account_id,subscription_id,operation,payment_channel,"
                    "mutation_source,actor_type,actor_ref,reason,idempotency_key_hash,"
                    "before_json,after_json,created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        account_id, subscription["id"], "ADMIN_EXPIRY_ADJUSTMENT",
                        "NOT_APPLICABLE", "ADMIN", "PRIMARY_ADMIN", actor_ref, reason,
                        idem_hash,
                        json.dumps(before_payload, sort_keys=True, separators=(",", ":")),
                        json.dumps(after_payload, sort_keys=True, separators=(",", ":")),
                        timestamp,
                    ),
                )
                mutation_id = cursor.lastrowid
                self._conn.commit()
                return {
                    **after_payload, "mutation_id": mutation_id,
                    "already_applied": False, "converged": False,
                }
            except sqlite3.IntegrityError as exc:
                self._conn.rollback()
                raise AdminExpiryConflict(
                    "an identical expiry adjustment is already being applied"
                ) from exc
            except Exception:
                self._conn.rollback()
                raise

    def _replay(self, idem_hash: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT before_json,after_json FROM mgboost_entitlement_mutations "
                "WHERE idempotency_key_hash=?",
                (idem_hash,),
            ).fetchone()
        if not row:
            return None
        try:
            after = json.loads(row["after_json"])
        except (TypeError, ValueError):
            return None
        if not isinstance(after, dict):
            return None
        result = {**after, "mutation_id": None, "already_applied": True, "converged": True}
        try:
            result["previous_expiry"] = json.loads(row["before_json"]).get("current_expiry")
        except (TypeError, ValueError):
            pass
        return result
