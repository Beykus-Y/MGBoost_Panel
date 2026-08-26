"""PH5-04 deterministic, side-effect-free entitlement calculation.

This module is deliberately a *composition* layer.  It does not own a new
entitlement, accounting, package, period, or override table.  Instead one
read-only calculation binds together the immutable/current canonical models
that already exist:

* ``mgboost_subscriptions`` + immutable ``mgboost_plan_versions`` for the
  actual pinned plan and expiry;
* ``mgboost_wl_periods`` plus PH6-04's ``compute_parent_wl_pool`` for the
  canonical current-period usage;
* PH5-03's ``WLPackageStore.package_state`` for base-first FIFO package
  consumption, rollover and freeze/resume; and
* ``mgboost_entitlement_overrides`` for durable, expiring admin overrides.

It is intentionally not a purchase, period-lifecycle, collector, adjustment,
or enforcement path.  In particular it never calls the PH6 status synchronizer
because calculation must not mutate its input snapshot.
"""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Any

from .internal_entitlements import TECHNICAL_DEVICE_CAP
from .wl_parent_pool import compute_parent_wl_pool


CALCULATION_VERSION = "ph5-04-entitlement-v1"


class EntitlementNotFoundError(LookupError):
    """The requested account has no canonical account/subscription state."""


def _json(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    parsed = json.loads(value)
    return parsed if isinstance(parsed, dict) else {}


def _effective_subscription_status(subscription: sqlite3.Row, plan: sqlite3.Row, now: int) -> str:
    raw = subscription["status"]
    if raw == "UNLIMITED":
        return "UNLIMITED"
    if raw != "ACTIVE":
        return raw
    expiry = subscription["current_expiry"]
    if expiry is not None and int(expiry) <= now:
        return "EXPIRED"
    # PH3-06 has already represented reviewed, non-billed INTERNAL accounts
    # with an ACTIVE/no-expiry subscription.  Preserve that explicit model;
    # a commercial ACTIVE/no-expiry row is malformed and never becomes a
    # fabricated paid entitlement.
    if expiry is None and plan["plan_kind"] != "INTERNAL":
        return "EXPIRED"
    return "ACTIVE"


def _subscription_active(effective_status: str) -> bool:
    return effective_status in {"ACTIVE", "UNLIMITED"}


def _override_payload(row: sqlite3.Row) -> dict[str, Any]:
    value: int | bool | None
    if row["value_type"] == "BOOLEAN":
        value = bool(row["boolean_value"])
    elif row["value_type"] == "INTEGER":
        value = int(row["integer_value"])
    else:
        value = None
    return {
        "id": int(row["id"]),
        "entitlement_key": row["entitlement_key"],
        "value_type": row["value_type"],
        "value": value,
        "starts_at": int(row["starts_at"]),
        "expires_at": int(row["expires_at"]),
        "reason": row["reason"],
        "mutation_id": int(row["mutation_id"]),
    }


class EntitlementEngine:
    """The sole PH5-04 public calculation path.

    ``calculate`` is safe to call repeatedly for the same ``(account_id,
    now)`` and same SQLite snapshot.  It opens a read transaction under the
    database's existing re-entrant lock, performs no INSERT/UPDATE/DELETE and
    performs no network/Marzban I/O.
    """

    def __init__(self, db):
        self._db = db

    def calculate(self, *, account_id: int, now: int | None = None) -> dict[str, Any]:
        timestamp = int(time.time()) if now is None else int(now)
        connection = self._db._conn

        with self._db._lock:
            # A read transaction makes every component below see the same
            # SQLite snapshot.  It is explicitly committed, never written.
            connection.execute("BEGIN")
            try:
                result = self._calculate_snapshot(
                    connection, account_id=int(account_id), now=timestamp
                )
                connection.commit()
                return result
            except Exception:
                connection.rollback()
                raise

    def _calculate_snapshot(
        self, connection: sqlite3.Connection, *, account_id: int, now: int
    ) -> dict[str, Any]:
        account = connection.execute(
            "SELECT id,public_id,status,account_source FROM mgboost_accounts WHERE id=?",
            (account_id,),
        ).fetchone()
        if account is None:
            raise EntitlementNotFoundError(f"account {account_id} not found")

        subscription = connection.execute(
            "SELECT * FROM mgboost_subscriptions WHERE account_id=? "
            "ORDER BY id DESC LIMIT 1",
            (account_id,),
        ).fetchone()
        if subscription is None:
            return self._without_subscription(account, now)

        plan = connection.execute(
            "SELECT * FROM mgboost_plan_versions WHERE id=?",
            (subscription["current_plan_version_id"],),
        ).fetchone()
        if plan is None:
            raise EntitlementNotFoundError(
                f"subscription {subscription['id']} has no plan version"
            )

        effective_status = _effective_subscription_status(subscription, plan, now)
        active = account["status"] != "CLOSED" and _subscription_active(effective_status)
        overrides = connection.execute(
            "SELECT * FROM mgboost_entitlement_overrides WHERE account_id=? "
            "AND revoked_at IS NULL AND starts_at<=? AND expires_at>? "
            "AND (subscription_id IS NULL OR subscription_id=?) "
            "ORDER BY starts_at ASC,id ASC",
            (account_id, now, now, subscription["id"]),
        ).fetchall()

        device_mode = plan["device_limit_mode"]
        device_limit = plan["device_limit"]
        effective_wl_mode = plan["wl_mode"] if active else "NONE"
        configured_quota = plan["wl_quota_bytes"]
        access_override_mode = "AUTO"
        applied_override_ids: list[int] = []

        # This mirrors the existing PH3-06 evaluator's deterministic ordering
        # exactly.  The override list itself is also exposed as durable proof.
        for override in overrides:
            applied_override_ids.append(int(override["id"]))
            key, kind = override["entitlement_key"], override["value_type"]
            if key == "DEVICE_LIMIT":
                device_mode = "UNLIMITED" if kind == "UNLIMITED" else "LIMITED"
                device_limit = None if kind == "UNLIMITED" else int(override["integer_value"])
            elif key == "WL_ACCESS":
                if kind == "UNLIMITED":
                    effective_wl_mode = "UNLIMITED" if active else "NONE"
                    access_override_mode = "FORCE_ENABLED"
                elif bool(override["boolean_value"]):
                    # Existing PH3-06 semantics: an enabled WL access override
                    # is unmetered access, never a fabricated commercial quota.
                    effective_wl_mode = "UNLIMITED" if active else "NONE"
                    access_override_mode = "FORCE_ENABLED"
                else:
                    effective_wl_mode = "NONE"
                    access_override_mode = "FORCE_DISABLED"
            elif key == "WL_QUOTA_BYTES":
                configured_quota = None if kind == "UNLIMITED" else int(override["integer_value"])

        # Billing/package rights are real-plan facts.  Overrides can affect
        # effective access but can never turn BASIC into a WL billable plan.
        real_limited_wl = plan["wl_mode"] == "LIMITED"
        package_eligible = bool(
            active and plan["billing_required"] and plan["plan_kind"] == "COMMERCIAL"
            and real_limited_wl
        )

        package_state = self._db.wl_packages.package_state(account_id=account_id, now=now)
        package_buckets = [
            {
                "id": int(bucket["id"]),
                "status": bucket["status"],
                "sku": bucket["sku_snapshot"],
                "product_version": int(bucket["product_version_snapshot"]),
                "catalog_version": bucket["catalog_version_snapshot"],
                "price_channel": bucket["price_channel"],
                "granted_at": int(bucket["granted_at"]),
                "granted_bytes": int(bucket["granted_bytes"]),
                "derived_consumed_bytes": int(bucket["derived_consumed_bytes"]),
                "derived_remaining_bytes": int(bucket["derived_remaining_bytes"]),
            }
            for bucket in package_state["buckets"]
        ]
        active_package_remaining = sum(
            item["derived_remaining_bytes"]
            for item in package_buckets if item["status"] == "ACTIVE"
        )

        period = connection.execute(
            "SELECT id FROM mgboost_wl_periods WHERE account_id=? "
            "AND starts_at<=? AND ends_at>? AND status!='CLOSED' "
            "ORDER BY starts_at DESC,id DESC LIMIT 1",
            (account_id, now, now),
        ).fetchone()
        pool = (
            compute_parent_wl_pool(connection, account_id=account_id, wl_period_id=period["id"])
            if period is not None and active and real_limited_wl
            else None
        )

        if pool is None:
            base_remaining = None
            consumed_bytes = None
            effective_remaining = None
            current_period = None
        else:
            base_remaining = int(pool["remaining_bytes"])
            consumed_bytes = int(pool["consumed_bytes"])
            # PH5-03's derived package remainder is account-level rollover
            # credit.  It is intentionally added only after the canonical
            # current-period base quota, matching base-first semantics.
            effective_remaining = base_remaining + active_package_remaining
            current_period = {
                "id": int(pool["wl_period_id"]),
                "sequence_no": int(pool["sequence_no"]),
                "starts_at": int(pool["starts_at"]),
                "ends_at": int(pool["ends_at"]),
                "status": pool["status"],
                "quota_mode": pool["quota_mode"],
            }

        plan_payload = {
            "id": int(plan["id"]),
            "code": plan["plan_code"],
            "version": int(plan["version"]),
            "display_name": plan["display_name"],
            "kind": plan["plan_kind"],
            "billing_required": bool(plan["billing_required"]),
            "terms": _json(plan["terms_json"]),
        }
        override_payloads = [_override_payload(row) for row in overrides]
        device_source = "PLAN" if not any(
            row["entitlement_key"] == "DEVICE_LIMIT" for row in overrides
        ) else "OVERRIDE"

        result = {
            "calculation_version": CALCULATION_VERSION,
            "calculated_at": now,
            "account": {
                "id": account_id,
                "public_id": account["public_id"],
                "status": account["status"],
                "source": account["account_source"],
            },
            "subscription": {
                "id": int(subscription["id"]),
                "stored_status": subscription["status"],
                "effective_status": effective_status,
                "started_at": subscription["started_at"],
                "effective_expiry": subscription["current_expiry"],
                "active": active,
            },
            "plan": plan_payload,
            "device": {
                "limit_mode": device_mode,
                "limit": None if device_mode == "UNLIMITED" else int(device_limit),
                "technical_cap": TECHNICAL_DEVICE_CAP if device_mode == "UNLIMITED" else None,
                "source": device_source,
                "slot_addon_state": "NONE",
                "additional_slots": 0,
            },
            "wl": {
                "real_plan_mode": plan["wl_mode"],
                "effective_mode": effective_wl_mode,
                "access_override_mode": access_override_mode,
                "access_eligible": active and effective_wl_mode != "NONE",
                "package_eligible": package_eligible,
                "current_period": current_period,
                "base_quota_bytes": None if pool is None else int(pool["base_quota_bytes"]),
                "configured_quota_bytes": configured_quota,
                "consumed_bytes": consumed_bytes,
                "base_remaining_bytes": base_remaining,
                "package_remaining_bytes": active_package_remaining if pool is not None else None,
                "effective_remaining_bytes": effective_remaining,
                "contributing_children": None if pool is None else int(pool["contributing_children"]),
                "package_state": "ACTIVE" if package_state["eligible_now"] else "FROZEN",
                "packages": package_buckets,
                # PH6-08 does not exist yet.  No hidden balance/adjustment
                # calculation is smuggled in merely because an override row
                # exists; the durable rows remain visible above.
                "adjustment_state": "NONE",
                "adjustment_bytes": 0,
            },
            "overrides": {
                "mode": "AUTO" if not override_payloads else "EXPLICIT",
                "active": override_payloads,
                "applied_ids": applied_override_ids,
            },
        }
        result["components"] = self._components(result)
        return result

    def _without_subscription(self, account: sqlite3.Row, now: int) -> dict[str, Any]:
        result = {
            "calculation_version": CALCULATION_VERSION,
            "calculated_at": now,
            "account": {
                "id": int(account["id"]), "public_id": account["public_id"],
                "status": account["status"], "source": account["account_source"],
            },
            "subscription": {"id": None, "stored_status": "NONE", "effective_status": "NONE", "started_at": None, "effective_expiry": None, "active": False},
            "plan": None,
            "device": {"limit_mode": "NONE", "limit": 0, "technical_cap": None, "source": "NONE", "slot_addon_state": "NONE", "additional_slots": 0},
            "wl": {"real_plan_mode": "NONE", "effective_mode": "NONE", "access_override_mode": "AUTO", "access_eligible": False, "package_eligible": False, "current_period": None, "base_quota_bytes": None, "configured_quota_bytes": None, "consumed_bytes": None, "base_remaining_bytes": None, "package_remaining_bytes": None, "effective_remaining_bytes": None, "contributing_children": None, "package_state": "NOT_APPLICABLE", "packages": [], "adjustment_state": "NONE", "adjustment_bytes": 0},
            "overrides": {"mode": "AUTO", "active": [], "applied_ids": []},
        }
        result["components"] = self._components(result)
        return result

    @staticmethod
    def _components(result: dict[str, Any]) -> list[dict[str, Any]]:
        """Stable, machine-readable provenance; no display text or usernames."""
        return [
            {"kind": "SUBSCRIPTION", "effective_status": result["subscription"]["effective_status"], "expiry": result["subscription"]["effective_expiry"]},
            {"kind": "PLAN", "plan_version_id": None if result["plan"] is None else result["plan"]["id"]},
            {"kind": "DEVICE_LIMIT", "source": result["device"]["source"], "mode": result["device"]["limit_mode"], "limit": result["device"]["limit"]},
            {"kind": "WL_ACCESS", "real_plan_mode": result["wl"]["real_plan_mode"], "effective_mode": result["wl"]["effective_mode"], "package_eligible": result["wl"]["package_eligible"]},
            {"kind": "WL_PERIOD", "period_id": None if result["wl"]["current_period"] is None else result["wl"]["current_period"]["id"], "base_quota_bytes": result["wl"]["base_quota_bytes"], "consumed_bytes": result["wl"]["consumed_bytes"]},
            {"kind": "WL_PACKAGES", "state": result["wl"]["package_state"], "bucket_ids": [bucket["id"] for bucket in result["wl"]["packages"]], "remaining_bytes": result["wl"]["package_remaining_bytes"]},
            {"kind": "ADMIN_OVERRIDES", "mode": result["overrides"]["mode"], "override_ids": result["overrides"]["applied_ids"]},
            {"kind": "SLOT_ADDON", "state": "NONE", "additional_slots": 0},
            {"kind": "WL_ADJUSTMENTS", "state": "NONE", "bytes": 0},
        ]


def calculate_effective_entitlement(db, *, account_id: int, now: int | None = None) -> dict[str, Any]:
    """Convenience public API; callers should not calculate entitlements themselves."""
    return EntitlementEngine(db).calculate(account_id=account_id, now=now)
