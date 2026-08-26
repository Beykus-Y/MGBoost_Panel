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

from .admin_authority import PrimaryAdminAuthorizationError


DEFAULT_LEGACY_PAID_DEVICE_LIMIT = 3
_PLAN_KIND = "COMMERCIAL"


class LegacyPaidCompatError(RuntimeError):
    pass


class PrimaryAdminRequired(LegacyPaidCompatError):
    pass


class PrerequisiteMissing(LegacyPaidCompatError):
    pass


class DeviceOverageConflict(LegacyPaidCompatError):
    pass


class SubscriptionConflict(LegacyPaidCompatError):
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
