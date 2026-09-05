"""PH4-03 migration-only legacy paid compatibility entitlement.

NOT a commercial catalog entry: no reconstructed historical tariff name or
price, no new customer-facing purchase option. Exists solely so a reviewed
DIRECT account (already owner-attested as a real, historically-paid legacy
customer -- `DirectEnrollmentStore`) has the `mgboost_subscriptions`/
`mgboost_plan_versions` row PH4-02's migration machinery needs
(`resolve_account_device` -> `parent_sync.refresh_desired_state` ->
`DeviceSlotStore.claim`), without inventing amount/date/plan details the
owner never approved.

Owner decision (2026-08-26):
- Historical default device limit for legacy paid subscriptions is 3.
  Some legacy users had an individually owner-approved extra device
  allowance: `device_limit = 3 + approved_extra_device_slots`. This is
  never inferred from current device/HWID/registration counts -- current
  usage is not proof of a historically-granted limit. Callers must pass an
  explicit `approved_extra_device_slots` plus evidence for anything above
  the default.
- WL is unlimited: legacy paid users never had a WL traffic quota, so this
  entitlement preserves `wl_mode='UNLIMITED'` with no quota bytes -- never
  a new 100/150 GB cap applied retroactively.
- Expiry is the exact already-reviewed legacy expiry (from the account's
  PRIMARY `mgboost_legacy_account_aliases` row) -- never extended,
  shortened, rounded, or replaced with a new period.
- A terminal legacy state (`DISABLED`/`EXPIRED`) is preserved as-is, never
  promoted to a fresh `ACTIVE` paid period.

`DeviceSlotStore._entitlement_capacity` hard-requires `plan_kind='COMMERCIAL'`
and `device_limit_mode='LIMITED'` with `device_limit` in the existing
`PAID_BASELINE_LIMITS` allowlist for any `DIRECT` account -- this module
reuses that exact commercial-capacity contract unchanged, it does not
introduce a parallel entitlement path.
"""

from __future__ import annotations

import sqlite3
import time
import json
import hashlib
import re

from .admin_authority import PrimaryAdminAuthorizationError
from .device_slots import PAID_BASELINE_LIMITS


DEFAULT_LEGACY_PAID_DEVICE_LIMIT = 3
_PLAN_KIND = "COMMERCIAL"


class LegacyPaidCompatError(RuntimeError):
    pass


class AmbiguousLegacyExpiry(LegacyPaidCompatError):
    pass


class PrimaryAdminRequired(LegacyPaidCompatError):
    pass


class PrerequisiteMissing(LegacyPaidCompatError):
    pass


class DeviceOverageConflict(LegacyPaidCompatError):
    pass


class SubscriptionConflict(LegacyPaidCompatError):
    pass


class NotLegacyCompatPlan(LegacyPaidCompatError):
    pass


class DeviceLimitDecreaseRefused(LegacyPaidCompatError):
    pass


def _require_primary(db, capability) -> str:
    try:
        return db.primary_admin_authority.require(capability)
    except PrimaryAdminAuthorizationError:
        raise PrimaryAdminRequired("primary MGBoost admin capability required")


def plan_code_for(device_limit: int) -> str:
    return f"LEGACY_PAID_COMPAT_V1_D{int(device_limit)}"


# Owner decision (2026-08-26): a distinct, individually-reviewed exemption
# for a real legacy account whose device count genuinely has no meaningful
# ceiling (e.g. a family/household account) -- never inferred, never a
# catalog tariff, never self-service. Reuses the exact same generic
# `device_limit_mode='UNLIMITED'` concept `mgboost_plan_versions` already
# defines for INTERNAL accounts; `DeviceSlotStore._entitlement_capacity`
# is extended (see device_slots.py) to accept it for a DIRECT account too,
# but ONLY ever via this capability-gated, audited, per-account assignment
# path -- never a self-service or plan-default toggle.
_UNLIMITED_PLAN_CODE = "LEGACY_PAID_COMPAT_V1_UNLIMITED"


def _ensure_plan_version(db, *, device_limit: int | None, unlimited: bool, now: int) -> dict:
    plan_code = _UNLIMITED_PLAN_CODE if unlimited else plan_code_for(device_limit)
    existing = db._conn.execute(
        "SELECT * FROM mgboost_plan_versions WHERE plan_code=? AND version=1", (plan_code,)
    ).fetchone()
    expected = {
        "display_name": (
            "Legacy paid migration compatibility (device-limit exempt)" if unlimited
            else f"Legacy paid migration compatibility ({device_limit} devices)"
        ),
        "plan_kind": _PLAN_KIND,
        "billing_required": 0,
        "device_limit_mode": "UNLIMITED" if unlimited else "LIMITED",
        "device_limit": None if unlimited else int(device_limit),
        "wl_mode": "UNLIMITED",
        "wl_quota_bytes": None,
        "wl_period_days": None,
    }
    if existing:
        actual = {
            "display_name": existing["display_name"], "plan_kind": existing["plan_kind"],
            "billing_required": existing["billing_required"],
            "device_limit_mode": existing["device_limit_mode"],
            "device_limit": existing["device_limit"], "wl_mode": existing["wl_mode"],
            "wl_quota_bytes": existing["wl_quota_bytes"], "wl_period_days": existing["wl_period_days"],
        }
        if actual != expected:
            raise LegacyPaidCompatError(
                f"plan version {plan_code} already exists with different terms"
            )
        return dict(existing)
    return db.accounts.create_plan_version(
        {
            "plan_code": plan_code, "version": 1, "display_name": expected["display_name"],
            "plan_kind": _PLAN_KIND, "billing_required": False,
            "device_limit_mode": expected["device_limit_mode"], "device_limit": expected["device_limit"],
            "wl_mode": "UNLIMITED", "wl_quota_bytes": None, "wl_period_days": None,
            "terms": {
                "schema": 1, "kind": "LEGACY_PAID_COMPAT",
                "purpose": "PH4-03 migration compatibility, not a commercial catalog entry",
                "device_limit": expected["device_limit"],
                "device_limit_exempt": unlimited,
            },
        },
        now=now,
    )


def ensure_legacy_paid_compat_entitlement(
    db, *, capability, account_id: int, approved_extra_device_slots: int = 0,
    device_limit_exempt: bool = False, acknowledge_observed_overage: bool = False,
    decision_ref: str, evidence: dict | None = None, now: int | None = None,
) -> dict:
    """`acknowledge_observed_overage=True` is a distinct, explicit owner
    decision from `approved_extra_device_slots`/`device_limit_exempt`
    themselves: it means the owner has personally reviewed the raw
    `observed_device_count` (frozen, immutable evidence from enrollment
    time) and knowingly confirmed the chosen limit is still correct even
    though it is below that raw count -- typically because some of the raw
    rows are understood to be the same physical device registered under
    more than one client/app (never merged/deleted -- see
    `docs/PHASE4_GRACE_PERIOD_RUNBOOK.md`). It never changes what limit is
    assigned, only whether the safety check that exists for the *unreviewed*
    case is allowed to be bypassed for this one, evidenced, human decision."""
    actor = _require_primary(db, capability)
    account_id = int(account_id)
    decision_ref = (decision_ref or "").strip()
    if not 3 <= len(decision_ref) <= 128:
        raise LegacyPaidCompatError("a bounded decision reference is required")
    if (
        isinstance(approved_extra_device_slots, bool)
        or not isinstance(approved_extra_device_slots, int)
        or approved_extra_device_slots < 0
    ):
        raise LegacyPaidCompatError("approved extra device slots must be a nonnegative integer")
    if device_limit_exempt and approved_extra_device_slots > 0:
        raise LegacyPaidCompatError(
            "device_limit_exempt and approved_extra_device_slots are mutually exclusive"
        )
    if (
        approved_extra_device_slots > 0 or device_limit_exempt or acknowledge_observed_overage
    ) and not evidence:
        raise LegacyPaidCompatError(
            "an increased/exempt device limit, or acknowledging an observed overage, requires "
            "recorded evidence of the owner's approval"
        )
    evidence = evidence or {}
    if not isinstance(evidence, dict):
        raise LegacyPaidCompatError("evidence must be an object")

    timestamp = int(time.time()) if now is None else int(now)

    row = db._conn.execute(
        "SELECT a.id AS account_id, a.account_source, "
        "al.legacy_status, al.legacy_expiry, al.observed_device_count "
        "FROM mgboost_accounts a "
        "JOIN mgboost_direct_account_reviews r ON r.account_id=a.id "
        "JOIN mgboost_legacy_account_aliases al ON al.account_id=a.id AND al.alias_role='PRIMARY' "
        "WHERE a.id=?", (account_id,),
    ).fetchone()
    if row is None or row["account_source"] != "DIRECT":
        raise PrerequisiteMissing("account is not a reviewed DIRECT enrollment")
    account_status = db._conn.execute(
        "SELECT status FROM mgboost_accounts WHERE id=?", (account_id,),
    ).fetchone()
    if account_status is None or account_status["status"] != "ACTIVE":
        raise PrerequisiteMissing("account must be ACTIVE")
    attested = db._conn.execute(
        "SELECT id FROM mgboost_owner_attested_legacy_payments WHERE account_id=?", (account_id,)
    ).fetchone()
    if attested is None:
        raise PrerequisiteMissing("owner-attested legacy external payment evidence is required")

    device_limit = None if device_limit_exempt else DEFAULT_LEGACY_PAID_DEVICE_LIMIT + approved_extra_device_slots
    observed = row["observed_device_count"]
    if (
        not device_limit_exempt and not acknowledge_observed_overage
        and observed is not None and observed > device_limit
    ):
        raise DeviceOverageConflict(
            f"observed device count {observed} exceeds the derived device limit "
            f"{device_limit} -- requires owner review before entitlement assignment"
        )

    legacy_status = row["legacy_status"]
    legacy_expiry = row["legacy_expiry"]
    if legacy_status == "UNLIMITED":
        legacy_expiry = None
    subscription_status = legacy_status
    if subscription_status == "ACTIVE" and legacy_expiry is not None and legacy_expiry <= timestamp:
        subscription_status = "EXPIRED"

    plan = _ensure_plan_version(
        db, device_limit=device_limit, unlimited=device_limit_exempt, now=timestamp,
    )

    with db._lock:
        try:
            db._conn.execute("BEGIN IMMEDIATE")
            correction = _expiry_correction(db, account_id)
            if correction is not None:
                resolved = _resolved_subscription(db, account_id, correction)
                if resolved["current_plan_version_id"] != plan["id"]:
                    raise SubscriptionConflict("resolved subscription has a different plan")
                db._conn.commit()
                return {**resolved, "_plan": plan, "_is_new": False}
            if subscription_status == "ACTIVE" and legacy_expiry is None and plan["plan_kind"] == "COMMERCIAL":
                raise AmbiguousLegacyExpiry(
                    "ACTIVE legacy commercial entitlement requires an explicit expiry "
                    "or an explicitly reviewed non-expiring status"
                )
            existing_sub = db._conn.execute(
                "SELECT * FROM mgboost_subscriptions WHERE account_id=? "
                "AND status IN ('PENDING','ACTIVE','DISABLED','UNLIMITED','UNKNOWN_LEGACY')",
                (account_id,),
            ).fetchone()
            if existing_sub is not None:
                if (
                    existing_sub["current_plan_version_id"] != plan["id"]
                    or existing_sub["current_expiry"] != legacy_expiry
                    or existing_sub["status"] != subscription_status
                ):
                    raise SubscriptionConflict(
                        "a different live subscription already exists for this account"
                    )
                db._conn.commit()
                result = dict(existing_sub)
                is_new = False
            else:
                cursor = db._conn.execute(
                    "INSERT INTO mgboost_subscriptions "
                    "(account_id,current_plan_version_id,status,started_at,current_expiry,"
                    "created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                    (account_id, plan["id"], subscription_status, timestamp, legacy_expiry,
                     timestamp, timestamp),
                )
                row2 = db._conn.execute(
                    "SELECT * FROM mgboost_subscriptions WHERE id=?", (cursor.lastrowid,)
                ).fetchone()
                db._conn.commit()
                result = dict(row2)
                is_new = True
        except sqlite3.IntegrityError as exc:
            db._conn.rollback()
            raise SubscriptionConflict(
                "a live subscription already exists for this account"
            ) from exc
        except Exception:
            db._conn.rollback()
            raise

    db.provenance.record_mutation(
        account_id,
        subscription_id=result["id"],
        operation="LEGACY_PAID_COMPAT_ENTITLEMENT_ASSIGNED",
        payment_channel="NOT_APPLICABLE",
        mutation_source="ADMIN",
        actor_type="PRIMARY_ADMIN",
        actor_ref=actor,
        reason=decision_ref,
        external_reference=None,
        before=None,
        after={
            "plan_code": plan["plan_code"], "device_limit": device_limit,
            "legacy_expiry": legacy_expiry, "status": subscription_status,
            "approved_extra_device_slots": approved_extra_device_slots, "evidence": evidence,
        },
        idempotency_key=f"legacy-paid-compat-v1:{account_id}",
        now=timestamp,
    )
    result["_plan"] = plan
    result["_is_new"] = is_new
    return result


_EXPIRY_OPERATION = "LEGACY_PAID_COMPAT_EXPIRY_RESOLVED"


def _expiry_correction(db, account_id):
    return db._conn.execute(
        "SELECT * FROM mgboost_entitlement_mutations WHERE account_id=? AND operation=?",
        (account_id, _EXPIRY_OPERATION),
    ).fetchone()


def _resolved_subscription(db, account_id, correction):
    """Immutable correction supersedes alias evidence only for its pinned subscription.

    Never recreate an expired subscription or overwrite subsequent admin changes.
    """
    after = json.loads(correction["after_json"])["after"]
    sub = db._conn.execute(
        "SELECT * FROM mgboost_subscriptions WHERE account_id=? ORDER BY id DESC LIMIT 1",
        (account_id,),
    ).fetchone()
    if sub is None or any(sub[key] != after[key] for key in (
        "id", "status", "current_expiry", "row_version", "current_plan_version_id",
    )):
        raise SubscriptionConflict("reviewed expiry resolution is stale")
    return dict(sub)


def resolve_legacy_expiry_ambiguity(
    db, *, capability, account_id: int, resolution: str, decision_ref: str,
    evidence: dict, expiry: int | None = None, now: int | None = None,
) -> dict:
    """Owner-reviewed correction, with immutable provenance in the same transaction.

    Evidence is deliberately restricted to an external review reference and explicit
    owner confirmation. Put sensitive supporting material in the referenced review,
    never in this ledger. An identical retry returns the pinned result; a changed
    decision or subsequent subscription mutation conflicts.
    """
    actor = _require_primary(db, capability)
    safe_ref = r"[A-Za-z0-9_.:/-]{3,128}"
    if not isinstance(decision_ref, str) or not re.fullmatch(safe_ref, decision_ref):
        raise LegacyPaidCompatError("a bounded decision reference is required")
    if (not isinstance(evidence, dict)
        or set(evidence) != {"review_ref", "owner_confirmed"}
        or evidence["owner_confirmed"] is not True
        or not isinstance(evidence["review_ref"], str)
        or not re.fullmatch(safe_ref, evidence["review_ref"])):
        raise LegacyPaidCompatError("evidence requires review_ref and owner_confirmed=true")
    # Reject UUIDs even when accidentally supplied as a review reference.
    if any(re.search(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", ref)
           for ref in (decision_ref, evidence["review_ref"])):
        raise LegacyPaidCompatError("review references must not contain raw UUIDs")
    if resolution not in {"FINITE_EXPIRY", "NON_EXPIRING"}:
        raise LegacyPaidCompatError("explicit expiry resolution required")
    if resolution == "FINITE_EXPIRY":
        if isinstance(expiry, bool) or not isinstance(expiry, int) or not 0 <= expiry <= 2**63 - 1:
            raise LegacyPaidCompatError("finite resolution requires an exact timestamp")
    elif expiry is not None:
        raise LegacyPaidCompatError("non-expiring resolution requires NULL expiry")
    account_id = int(account_id)
    timestamp = int(time.time()) if now is None else int(now)
    request = dict(resolution=resolution, expiry=expiry, evidence=evidence)
    with db._lock:
        try:
            db._conn.execute("BEGIN IMMEDIATE")
            source = db._conn.execute(
                "SELECT al.id FROM mgboost_accounts a "
                "JOIN mgboost_direct_account_reviews r ON r.account_id=a.id "
                "JOIN mgboost_legacy_account_aliases al ON al.account_id=a.id AND al.alias_role='PRIMARY' "
                "WHERE a.id=? AND a.account_source='DIRECT' AND a.status='ACTIVE' "
                "AND al.legacy_status='ACTIVE' AND al.legacy_expiry IS NULL "
                "AND EXISTS (SELECT 1 FROM mgboost_owner_attested_legacy_payments p WHERE p.account_id=a.id)",
                (account_id,),
            ).fetchone()
            if source is None:
                raise PrerequisiteMissing("requires reviewed DIRECT paid ACTIVE/NULL PRIMARY evidence")
            prior = _expiry_correction(db, account_id)
            if prior is not None:
                if (prior["actor_ref"] != actor or prior["reason"] != decision_ref
                    or json.loads(prior["after_json"])["request"] != request):
                    raise SubscriptionConflict("expiry decision already recorded")
                result = _resolved_subscription(db, account_id, prior)
                db._conn.commit()
                return {**result, "already_applied": True}
            sub = db._conn.execute(
                "SELECT s.*, p.plan_code, p.plan_kind, p.billing_required "
                "FROM mgboost_subscriptions s JOIN mgboost_plan_versions p "
                "ON p.id=s.current_plan_version_id WHERE s.account_id=? ORDER BY s.id DESC LIMIT 1",
                (account_id,),
            ).fetchone()
            if (sub is None or sub["status"] != "ACTIVE" or sub["current_expiry"] is not None
                or sub["plan_kind"] != "COMMERCIAL" or sub["billing_required"]
                or not sub["plan_code"].startswith("LEGACY_PAID_COMPAT_V1_")):
                raise SubscriptionConflict("current subscription is not an ambiguous legacy commercial entitlement")
            status = "UNLIMITED" if resolution == "NON_EXPIRING" else (
                "ACTIVE" if expiry > timestamp else "EXPIRED"
            )
            before = {key: sub[key] for key in (
                "id", "status", "current_expiry", "row_version", "current_plan_version_id",
            )}
            after = {**before, "status": status, "current_expiry": expiry,
                     "row_version": sub["row_version"] + 1}
            updated = db._conn.execute(
                "UPDATE mgboost_subscriptions SET status=?,current_expiry=?,updated_at=?,"
                "row_version=row_version+1 WHERE id=? AND row_version=? AND status='ACTIVE' AND current_expiry IS NULL",
                (status, expiry, timestamp, sub["id"], sub["row_version"]),
            )
            if updated.rowcount != 1:
                raise SubscriptionConflict("concurrent subscription modification")
            db._conn.execute(
                "INSERT INTO mgboost_entitlement_mutations "
                "(account_id,subscription_id,operation,payment_channel,mutation_source,actor_type,"
                "actor_ref,reason,idempotency_key_hash,before_json,after_json,created_at) "
                "VALUES (?,?,?,'NOT_APPLICABLE','ADMIN','PRIMARY_ADMIN',?,?,?,?,?,?)",
                (account_id, sub["id"], _EXPIRY_OPERATION, actor, decision_ref,
                 hashlib.sha256(f"legacy-expiry-resolution-v1:{account_id}".encode()).hexdigest(),
                 json.dumps(before, sort_keys=True), json.dumps({
                     "after": after, "request": request, "primary_alias_id": source["id"],
                 }, sort_keys=True), timestamp),
            )
            result = _resolved_subscription(db, account_id, _expiry_correction(db, account_id))
            db._conn.commit()
            return {**result, "already_applied": False}
        except Exception:
            db._conn.rollback()
            raise


def detect_legacy_expiry_ambiguities(connection) -> list[dict]:
    """Read-only global verification; accepts an existing SQLite connection."""
    rows = connection.execute(
        "SELECT s.account_id,s.id,p.plan_code FROM mgboost_subscriptions s "
        "JOIN mgboost_plan_versions p ON p.id=s.current_plan_version_id "
        "WHERE s.status='ACTIVE' AND s.current_expiry IS NULL AND p.plan_kind='COMMERCIAL' "
        "AND p.plan_code GLOB 'LEGACY_PAID_COMPAT_V1_*' "
        "AND s.id=(SELECT max(s2.id) FROM mgboost_subscriptions s2 WHERE s2.account_id=s.account_id) "
        "ORDER BY s.account_id"
    ).fetchall()
    return [dict(account_id=row[0], subscription_id=row[1], plan_code=row[2],
                 violation_class="CROSS_MODULE_INVARIANT_GAP") for row in rows]


def increase_device_limit(
    db, *, capability, account_id: int, approved_extra_device_slots: int,
    decision_ref: str, evidence: dict, now: int | None = None,
) -> dict:
    """DL-057's narrow, explicitly-scoped companion to
    `ensure_legacy_paid_compat_entitlement`: that function only ever
    bootstraps a brand-new entitlement and hard-conflicts on any existing
    live subscription with a different plan -- it has no path to change an
    already-provisioned account's device limit. This is the one canonical
    way to raise the device limit of an ALREADY-live
    `LEGACY_PAID_COMPAT_V1_D{n}` subscription in place: it changes ONLY
    `current_plan_version_id` (device limit); `current_expiry`, `status`
    and WL semantics (already `UNLIMITED` on every legacy-compat plan) are
    never touched, and no second subscription row is ever created (a
    single-field CAS `UPDATE` of the existing live row, mirroring
    `subscription_admin_ops.py`'s surgical-adjustment discipline).

    Refuses any COMMERCIAL/billed or non-legacy-compat plan outright: PH5-06
    (the general upgrade/downgrade engine) is not implemented, and this
    function must never be mistaken for it. Only ever increases -- a
    decrease is a distinct, not-yet-implemented decision."""
    actor = _require_primary(db, capability)
    account_id = int(account_id)
    decision_ref = (decision_ref or "").strip()
    if not 3 <= len(decision_ref) <= 128:
        raise LegacyPaidCompatError("a bounded decision reference is required")
    if (
        isinstance(approved_extra_device_slots, bool)
        or not isinstance(approved_extra_device_slots, int)
        or approved_extra_device_slots <= 0
    ):
        raise LegacyPaidCompatError("approved extra device slots must be a positive integer")
    if not evidence or not isinstance(evidence, dict):
        raise LegacyPaidCompatError(
            "increasing a device limit requires recorded evidence of the owner's approval"
        )

    timestamp = int(time.time()) if now is None else int(now)
    new_limit = DEFAULT_LEGACY_PAID_DEVICE_LIMIT + approved_extra_device_slots
    if new_limit not in PAID_BASELINE_LIMITS:
        raise LegacyPaidCompatError(f"device limit {new_limit} is not an approved baseline value")

    with db._lock:
        try:
            db._conn.execute("BEGIN IMMEDIATE")
            account = db._conn.execute(
                "SELECT status FROM mgboost_accounts WHERE id=?", (account_id,),
            ).fetchone()
            if account is None or account["status"] != "ACTIVE":
                raise PrerequisiteMissing("account must be ACTIVE")
            sub = db._conn.execute(
                "SELECT s.*, pv.plan_code AS current_plan_code, "
                "pv.device_limit AS current_device_limit, pv.plan_kind, pv.billing_required "
                "FROM mgboost_subscriptions s "
                "JOIN mgboost_plan_versions pv ON pv.id=s.current_plan_version_id "
                "WHERE s.account_id=? AND s.status IN ('ACTIVE','DISABLED','UNLIMITED')",
                (account_id,),
            ).fetchone()
            if sub is None:
                raise PrerequisiteMissing(
                    "account has no live legacy-compat subscription to increase"
                )
            if (
                sub["plan_kind"] != _PLAN_KIND or sub["billing_required"]
                or not str(sub["current_plan_code"]).startswith("LEGACY_PAID_COMPAT_V1_D")
            ):
                raise NotLegacyCompatPlan(
                    "this function only ever changes a pinned LEGACY_PAID_COMPAT_V1_D{n} "
                    "plan's device limit -- a commercial/billed plan is PH5-06 upgrade/"
                    "downgrade territory, which is not implemented"
                )
            if sub["current_device_limit"] is None:
                raise NotLegacyCompatPlan("current plan has no fixed device limit to change")
            if new_limit == sub["current_device_limit"]:
                db._conn.commit()
                return {**dict(sub), "already_applied": True}
            if new_limit < sub["current_device_limit"]:
                raise DeviceLimitDecreaseRefused(
                    "this function only ever increases a device limit; a decrease is a "
                    "separate, not-yet-implemented decision"
                )

            plan = _ensure_plan_version(db, device_limit=new_limit, unlimited=False, now=timestamp)

            updated = db._conn.execute(
                "UPDATE mgboost_subscriptions SET current_plan_version_id=?,updated_at=?,"
                "row_version=row_version+1 WHERE id=? AND account_id=? AND row_version=?",
                (plan["id"], timestamp, sub["id"], account_id, sub["row_version"]),
            )
            if updated.rowcount != 1:
                raise SubscriptionConflict("concurrent subscription modification detected")
            db._conn.commit()
        except sqlite3.IntegrityError as exc:
            db._conn.rollback()
            raise SubscriptionConflict("concurrent subscription modification detected") from exc
        except Exception:
            db._conn.rollback()
            raise

    db.provenance.record_mutation(
        account_id, subscription_id=sub["id"],
        operation="LEGACY_PAID_COMPAT_DEVICE_LIMIT_INCREASED",
        payment_channel="NOT_APPLICABLE", mutation_source="ADMIN",
        actor_type="PRIMARY_ADMIN", actor_ref=actor, reason=decision_ref,
        external_reference=None,
        before={"plan_code": sub["current_plan_code"], "device_limit": sub["current_device_limit"]},
        after={"plan_code": plan["plan_code"], "device_limit": new_limit, "evidence": evidence},
        idempotency_key=f"legacy-paid-compat-device-limit-v1:{account_id}:{new_limit}",
        now=timestamp,
    )
    result = db._conn.execute(
        "SELECT * FROM mgboost_subscriptions WHERE id=?", (sub["id"],),
    ).fetchone()
    return {**dict(result), "_plan": plan, "already_applied": False}
