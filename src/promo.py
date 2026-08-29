"""PH5-13 promo codes: EXTEND_SUBSCRIPTION and TRIAL_GRANT effects.

`PURCHASE_DISCOUNT` (reservation/redemption against a manual-RUB payment)
is a separate, not-yet-built slice -- `create_definition` accepts the
`effect_kind` for forward-compatibility, but `redeem_extend_or_trial`
only ever handles `EXTEND_SUBSCRIPTION`/`TRIAL_GRANT`.

Design invariants (owner-reviewed, see ROADMAP.md DL-060/DL-061 and the
plan this module implements):

* No second subscription engine. LIMITED-plan effects go through
  `subscription_renewal.append_promo_wl_period`; STANDARD/NONE-plan
  `EXTEND_SUBSCRIPTION` goes through the existing
  `subscription_admin_ops.apply_adjustment(EXTEND_DAYS)` -- both already
  reviewed/deployed primitives, never duplicated here.
* Crash-consistency: a `mgboost_promo_redemptions` row with
  `status='PENDING_APPLY'` is written and COMMITTED *before* the effect
  engine is ever called (durable intent). The effect engine's own
  `idempotency_key` is DERIVED from that durable row's id
  (`f"promo-redemption-v1:{redemption_id:012d}"`), never re-randomized --
  a crash at any point converges to applying the effect exactly once on
  retry (the exact `manual_payment.py` PENDING->APPLIED pattern).
* Anti-abuse: TRIAL_GRANT uniqueness is enforced at the DB level by
  `ux_promo_trial_class_identity` on `(trial_class, owner_telegram_id)`
  spanning EVERY promo code sharing a `trial_class` -- `owner_telegram_id`
  is the canonical Telegram OWNER identity (`mgboost_telegram_identities`),
  never `account_id`, so it survives account rebind and a fresh account
  never grants a second trial for the same person.
* Exact DL-060 prorating: `compute_promo_quota_bytes` is pure integer
  arithmetic (no floats) implementing
  `ceil(base_quota_bytes * days / 30 / 10_000_000_000) * 10_000_000_000`.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time

from .admin_authority import PrimaryAdminAuthorizationError
from .subscription_renewal import RenewalError

__all__ = [
    "PromoError", "PromoConflict", "PromoNotFound", "PromoIneligible",
    "PromoStore", "compute_promo_quota_bytes", "ensure_wl_trial_plan_version",
]

WL_TRIAL_PLAN_CODE = "WL_TRIAL"

_QUOTA_ROUND_BYTES = 10_000_000_000  # DL-060: round up to the nearest 10 GB decimal.
_NAMESPACE = "ph5-13-promo-v1\0"

PAYMENT_CHANNEL = "ADMIN_GRANT"
MUTATION_SOURCE = "ADMIN"
ACTOR_TYPE = "PRIMARY_ADMIN"

EFFECT_KINDS = ("EXTEND_SUBSCRIPTION", "TRIAL_GRANT", "PURCHASE_DISCOUNT")


class PromoError(ValueError):
    pass


class PromoConflict(PromoError):
    pass


class PromoNotFound(PromoError):
    pass


class PromoIneligible(PromoError):
    pass


def compute_promo_quota_bytes(base_quota_bytes: int, days: int) -> int:
    """DL-060 exact prorating: `ceil(base_quota_bytes/30*days / 10GB) * 10GB`,
    pure integer arithmetic (no float rounding surprises)."""
    if not isinstance(base_quota_bytes, int) or base_quota_bytes <= 0:
        raise PromoError("base_quota_bytes must be a positive integer")
    if not isinstance(days, int) or isinstance(days, bool) or days <= 0:
        raise PromoError("days must be a positive integer")
    numerator = base_quota_bytes * days
    denominator = 30 * _QUOTA_ROUND_BYTES
    units = (numerator + denominator - 1) // denominator
    return units * _QUOTA_ROUND_BYTES


def _idempotency_hash(idempotency_key: str) -> str:
    if not isinstance(idempotency_key, str) or not 16 <= len(idempotency_key) <= 512:
        raise PromoError("idempotency_key must be a string between 16 and 512 characters")
    return hashlib.sha256((_NAMESPACE + idempotency_key).encode("utf-8")).hexdigest()


def _request_hash(payload: dict) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _clean_reason(reason: str) -> str:
    if not isinstance(reason, str):
        raise PromoError("reason must be a string")
    reason = reason.strip()
    if not 8 <= len(reason) <= 1000:
        raise PromoError("reason must be between 8 and 1000 characters")
    return reason


def _clean_code(code: str) -> str:
    if not isinstance(code, str):
        raise PromoError("code must be a string")
    code = code.strip().upper()
    if not 3 <= len(code) <= 64:
        raise PromoError("code must be between 3 and 64 characters")
    return code


class PromoStore:
    def __init__(
        self, connection, lock, accounts, admin_grants, subscription_renewal,
        subscription_admin_ops, entitlements, primary_admin_authority,
    ):
        self._conn = connection
        self._lock = lock
        self._accounts = accounts
        self._admin_grants = admin_grants
        self._subscription_renewal = subscription_renewal
        self._subscription_admin_ops = subscription_admin_ops
        self._entitlements = entitlements
        self._authority = primary_admin_authority

    def _require_primary(self, capability) -> str:
        try:
            return self._authority.require(capability)
        except PrimaryAdminAuthorizationError:
            raise

    # --- definitions ---------------------------------------------------------

    def create_definition(
        self, capability, *, code: str, effect_kind: str, trial_class: str | None,
        effect_params: dict, reason: str, idempotency_key: str, now: int | None = None,
    ) -> dict:
        actor = self._require_primary(capability)
        clean_reason = _clean_reason(reason)
        clean_code = _clean_code(code)
        if effect_kind not in EFFECT_KINDS:
            raise PromoError(f"unknown effect_kind {effect_kind!r}")
        if effect_kind == "TRIAL_GRANT":
            if not isinstance(trial_class, str) or not 1 <= len(trial_class.strip()) <= 64:
                raise PromoError("trial_class is required for TRIAL_GRANT")
            trial_class = trial_class.strip()
        elif trial_class is not None:
            raise PromoError("trial_class only applies to TRIAL_GRANT")
        _validate_effect_params(effect_kind, effect_params)
        timestamp = int(time.time()) if now is None else int(now)
        idem_hash = _idempotency_hash(idempotency_key)
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                existing = self._conn.execute(
                    "SELECT id FROM mgboost_promo_definitions WHERE code=?", (clean_code,),
                ).fetchone()
                if existing is not None:
                    self._conn.rollback()
                    raise PromoConflict(f"promo code {clean_code!r} already exists")
                cursor = self._conn.execute(
                    "INSERT INTO mgboost_promo_definitions "
                    "(code,effect_kind,trial_class,status,created_by_actor,created_at,updated_at) "
                    "VALUES (?,?,?,'ACTIVE',?,?,?)",
                    (clean_code, effect_kind, trial_class, actor, timestamp, timestamp),
                )
                promo_id = cursor.lastrowid
                self._conn.execute(
                    "INSERT INTO mgboost_promo_versions "
                    "(promo_id,version,effect_params_json,status,created_by_actor,created_at) "
                    "VALUES (?,1,?,'ACTIVE',?,?)",
                    (promo_id, json.dumps(effect_params, sort_keys=True, separators=(",", ":")),
                     actor, timestamp),
                )
                self._conn.commit()
                return {
                    "promo_id": promo_id, "code": clean_code, "effect_kind": effect_kind,
                    "trial_class": trial_class, "version": 1,
                }
            except Exception:
                self._conn.rollback()
                raise

    def disable_definition(self, capability, *, code: str, reason: str, now: int | None = None) -> dict:
        self._require_primary(capability)
        _clean_reason(reason)
        clean_code = _clean_code(code)
        timestamp = int(time.time()) if now is None else int(now)
        with self._lock:
            updated = self._conn.execute(
                "UPDATE mgboost_promo_definitions SET status='DISABLED',updated_at=? "
                "WHERE code=? AND status='ACTIVE'",
                (timestamp, clean_code),
            )
            self._conn.commit()
        if updated.rowcount != 1:
            raise PromoNotFound(f"no ACTIVE promo code {clean_code!r} to disable")
        return {"code": clean_code, "status": "DISABLED"}

    def get_definition(self, code: str) -> dict | None:
        clean_code = _clean_code(code)
        row = self._conn.execute(
            "SELECT d.*, v.version AS active_version, v.effect_params_json "
            "FROM mgboost_promo_definitions d "
            "JOIN mgboost_promo_versions v ON v.promo_id=d.id AND v.status='ACTIVE' "
            "WHERE d.code=?", (clean_code,),
        ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["effect_params"] = json.loads(result.pop("effect_params_json"))
        return result

    def list_definitions(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT d.*, v.version AS active_version, v.effect_params_json "
            "FROM mgboost_promo_definitions d "
            "JOIN mgboost_promo_versions v ON v.promo_id=d.id AND v.status='ACTIVE' "
            "ORDER BY d.id"
        ).fetchall()
        results = []
        for row in rows:
            item = dict(row)
            item["effect_params"] = json.loads(item.pop("effect_params_json"))
            results.append(item)
        return results

    # --- redemption: EXTEND_SUBSCRIPTION / TRIAL_GRANT ------------------------

    def _owner_telegram_id(self, account_id: int) -> int | None:
        row = self._conn.execute(
            "SELECT telegram_id FROM mgboost_telegram_identities "
            "WHERE account_id=? AND role='OWNER' AND revoked_at IS NULL",
            (account_id,),
        ).fetchone()
        return int(row["telegram_id"]) if row is not None else None

    def _replay_redemption(self, row) -> dict:
        mutation = None
        if row["applied_mutation_id"] is not None:
            mutation = self._conn.execute(
                "SELECT after_json FROM mgboost_entitlement_mutations WHERE id=?",
                (row["applied_mutation_id"],),
            ).fetchone()
        return {
            "redemption_id": row["id"], "account_id": row["account_id"],
            "status": row["status"], "applied_mutation_id": row["applied_mutation_id"],
            "effect_result": json.loads(mutation["after_json"]) if mutation else None,
            "already_applied": True,
        }

    def redeem_extend_or_trial(
        self, capability, *, code: str, telegram_id: int, reason: str,
        idempotency_key: str, now: int | None = None,
    ) -> dict:
        actor = self._require_primary(capability)
        clean_reason = _clean_reason(reason)
        clean_code = _clean_code(code)
        if isinstance(telegram_id, bool) or not isinstance(telegram_id, int) or telegram_id <= 0:
            raise PromoError("telegram_id must be a positive integer")
        timestamp = int(time.time()) if now is None else int(now)
        idem_hash = _idempotency_hash(idempotency_key)
        request_hash = _request_hash({
            "code": clean_code, "telegram_id": int(telegram_id), "reason": clean_reason,
        })

        # Idempotency replay check FIRST, read-only, no transaction held --
        # `_resolve_redemption_target` below may call `AdminGrantStore.
        # create_account_only`, which opens its OWN `BEGIN IMMEDIATE` on
        # this same connection; SQLite has no nested transactions, so
        # nothing here may hold one open while calling it. The DB-level
        # `UNIQUE(idempotency_key_hash)`/`ux_promo_trial_class_identity`
        # constraints are the real race-safety backstop (see the INSERT's
        # `except sqlite3.IntegrityError` below), not this pre-check alone.
        existing = self._conn.execute(
            "SELECT * FROM mgboost_promo_redemptions WHERE idempotency_key_hash=?",
            (idem_hash,),
        ).fetchone()
        if existing is not None:
            if existing["request_hash"] != request_hash:
                raise PromoConflict("idempotency key reused with a different request")
            if existing["status"] == "REDEEMED":
                return self._replay_redemption(existing)
            redemption_id = existing["id"]
            account_id = existing["account_id"]
            definition = self._conn.execute(
                "SELECT * FROM mgboost_promo_definitions WHERE id=?", (existing["promo_id"],),
            ).fetchone()
            version_row = self._conn.execute(
                "SELECT effect_params_json FROM mgboost_promo_versions "
                "WHERE promo_id=? AND version=?", (existing["promo_id"], existing["promo_version"]),
            ).fetchone()
            effect_params = json.loads(version_row["effect_params_json"])
        else:
            definition = self._conn.execute(
                "SELECT * FROM mgboost_promo_definitions WHERE code=?", (clean_code,),
            ).fetchone()
            if definition is None or definition["status"] != "ACTIVE":
                raise PromoNotFound(f"no ACTIVE promo code {clean_code!r}")
            if definition["effect_kind"] not in ("EXTEND_SUBSCRIPTION", "TRIAL_GRANT"):
                raise PromoError(
                    f"{definition['effect_kind']} is not redeemable through "
                    "redeem_extend_or_trial"
                )
            version_row = self._conn.execute(
                "SELECT * FROM mgboost_promo_versions WHERE promo_id=? AND status='ACTIVE'",
                (definition["id"],),
            ).fetchone()
            if version_row is None:
                raise PromoNotFound(f"promo code {clean_code!r} has no active version")
            effect_params = json.loads(version_row["effect_params_json"])

            # Resolve/create the account BEFORE opening our own write
            # transaction (see note above) -- its own primitives are
            # independently durable/idempotent (AccountStore.create_account,
            # AdminGrantStore.create_account_only), so a crash here just
            # means the retry (same idempotency_key) re-resolves the same
            # already-created account, never a duplicate.
            account_id, trial_class, owner_telegram_id = self._resolve_redemption_target(
                capability=capability, definition=definition, telegram_id=telegram_id,
                reason=clean_reason, idem_hash=idem_hash, timestamp=timestamp,
            )
            if definition["effect_kind"] == "TRIAL_GRANT":
                conflict = self._conn.execute(
                    "SELECT id FROM mgboost_promo_redemptions "
                    "WHERE trial_class=? AND owner_telegram_id=? "
                    "AND status IN ('PENDING_APPLY','REDEEMED')",
                    (trial_class, owner_telegram_id),
                ).fetchone()
                if conflict is not None:
                    raise PromoIneligible(
                        f"trial_class {trial_class!r} already redeemed by this "
                        "Telegram identity"
                    )

            # --- Phase 1: durable intent, own transaction, committed
            # before any effect mutation (crash-consistency -- see module
            # docstring). --------------------------------------------------
            with self._lock:
                try:
                    self._conn.execute("BEGIN IMMEDIATE")
                    cursor = self._conn.execute(
                        "INSERT INTO mgboost_promo_redemptions "
                        "(promo_id,promo_version,trial_class,owner_telegram_id,account_id,"
                        "status,idempotency_key_hash,request_hash,actor_type,actor_ref,reason,"
                        "created_at,updated_at) "
                        "VALUES (?,?,?,?,?,'PENDING_APPLY',?,?,?,?,?,?,?)",
                        (definition["id"], version_row["version"], trial_class, owner_telegram_id,
                         account_id, idem_hash, request_hash, ACTOR_TYPE, actor, clean_reason,
                         timestamp, timestamp),
                    )
                    redemption_id = cursor.lastrowid
                    self._conn.commit()
                except sqlite3.IntegrityError as exc:
                    self._conn.rollback()
                    raise PromoConflict(
                        "a concurrent identical redemption or trial_class conflict "
                        "was already committed"
                    ) from exc
                except Exception:
                    self._conn.rollback()
                    raise

        # --- Phase 2: apply the effect through the derived, durable key. ---
        effect_idem_key = f"promo-redemption-v1:{redemption_id:012d}"
        effect_result = self._apply_effect(
            capability=capability, definition=definition, effect_params=effect_params,
            account_id=account_id, actor=actor, reason=clean_reason,
            idempotency_key=effect_idem_key, now=timestamp,
        )

        # --- Phase 3: mark redeemed, separate transaction. -----------------
        with self._lock:
            self._conn.execute(
                "UPDATE mgboost_promo_redemptions SET status='REDEEMED',"
                "applied_mutation_id=?,updated_at=? WHERE id=? AND status='PENDING_APPLY'",
                (effect_result.get("mutation_id"), timestamp, redemption_id),
            )
            self._conn.commit()

        return {
            "redemption_id": redemption_id, "account_id": account_id,
            "status": "REDEEMED", "applied_mutation_id": effect_result.get("mutation_id"),
            "effect_result": effect_result, "already_applied": False,
        }

    def _resolve_redemption_target(
        self, *, capability, definition, telegram_id, reason, idem_hash, timestamp,
    ) -> tuple[int, str | None, int]:
        """Resolves account_id + (trial_class, owner_telegram_id) snapshot
        for a NEW redemption (never called on replay). TRIAL_GRANT may
        create the account (reuses the public, already-reviewed
        `AdminGrantStore.create_account_only` -- no duplicated bootstrap
        wiring); EXTEND_SUBSCRIPTION requires an existing account."""
        account = self._accounts.get_active_account_by_telegram_id(int(telegram_id))
        if definition["effect_kind"] == "TRIAL_GRANT":
            if account is None:
                created = self._admin_grants.create_account_only(
                    capability, telegram_id=telegram_id, reason=reason,
                    idempotency_key=f"promo-trial-account-v1:{idem_hash[:40]}", now=timestamp,
                )
                account_id = created["account_id"]
            else:
                account_id = account["id"]
            entitlement = self._entitlements.calculate(account_id=account_id, now=timestamp)
            if entitlement["subscription"]["active"]:
                raise PromoIneligible(
                    "account already has an active subscription -- TRIAL_GRANT requires none"
                )
            owner_telegram_id = self._owner_telegram_id(account_id)
            if owner_telegram_id is None:
                raise PromoError("account has no active Telegram owner identity")
            return account_id, definition["trial_class"], owner_telegram_id
        # EXTEND_SUBSCRIPTION
        if account is None:
            raise PromoNotFound(
                f"no active account for telegram_id={telegram_id}; EXTEND_SUBSCRIPTION "
                "requires an existing account"
            )
        return account["id"], None, int(telegram_id)

    def _apply_effect(
        self, *, capability, definition, effect_params, account_id, actor, reason,
        idempotency_key, now,
    ) -> dict:
        days = int(effect_params["days"])
        if definition["effect_kind"] == "TRIAL_GRANT":
            plan_version_id = self._require_trial_plan_version_id()
            base_quota_bytes = int(effect_params.get("base_quota_bytes", 100_000_000_000))
            quota_bytes = compute_promo_quota_bytes(base_quota_bytes, days)
            return self._subscription_renewal.append_promo_wl_period(
                account_id=account_id, days=days, quota_bytes=quota_bytes,
                operation="PROMO_TRIAL_GRANT", plan_version_id=plan_version_id,
                mutation_source=MUTATION_SOURCE, payment_channel=PAYMENT_CHANNEL,
                actor_type=ACTOR_TYPE, actor_ref=actor, reason=reason,
                idempotency_key=idempotency_key, now=now,
            )
        # EXTEND_SUBSCRIPTION
        entitlement = self._entitlements.calculate(account_id=account_id, now=now)
        if entitlement["plan"] is None:
            raise PromoIneligible("account has no subscription to extend")
        wl_quota_gb = entitlement["plan"]["terms"].get("wl_quota_gb")
        if wl_quota_gb:
            base_quota_bytes = int(wl_quota_gb) * 1_000_000_000
            quota_bytes = compute_promo_quota_bytes(base_quota_bytes, days)
            try:
                return self._subscription_renewal.append_promo_wl_period(
                    account_id=account_id, days=days, quota_bytes=quota_bytes,
                    operation="PROMO_EXTEND_WL_PERIOD", plan_version_id=None,
                    mutation_source=MUTATION_SOURCE, payment_channel=PAYMENT_CHANNEL,
                    actor_type=ACTOR_TYPE, actor_ref=actor, reason=reason,
                    idempotency_key=idempotency_key, now=now,
                )
            except RenewalError as exc:
                raise PromoError(str(exc)) from exc
        # STANDARD/NONE: reuse the existing, already-reviewed PH7-01 writer
        # unchanged -- it never touches WL periods, exactly right for a
        # non-WL plan (DL-060: "STANDARD/NONE — обычное продление expiry").
        result = self._subscription_admin_ops.apply_adjustment(
            capability, account_id=account_id, adjustment_kind="EXTEND_DAYS", value=days,
            reason=reason, idempotency_key=idempotency_key, now=now,
        )
        return result

    def _require_trial_plan_version_id(self) -> int:
        row = self._conn.execute(
            "SELECT id FROM mgboost_plan_versions WHERE plan_code='WL_TRIAL' "
            "ORDER BY version DESC LIMIT 1",
        ).fetchone()
        if row is None:
            raise PromoError(
                "WL_TRIAL plan_version is not registered -- run "
                "scripts/seed_promo_wl_trial_plan.py once before granting trials"
            )
        return int(row["id"])


def _validate_effect_params(effect_kind: str, effect_params: dict) -> None:
    if not isinstance(effect_params, dict):
        raise PromoError("effect_params must be an object")
    if effect_kind in ("EXTEND_SUBSCRIPTION", "TRIAL_GRANT"):
        days = effect_params.get("days")
        if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 3650:
            raise PromoError("effect_params.days must be an integer between 1 and 3650")
    elif effect_kind == "PURCHASE_DISCOUNT":
        percent = effect_params.get("discount_percent")
        minor = effect_params.get("discount_minor")
        if (percent is None) == (minor is None):
            raise PromoError("exactly one of discount_percent/discount_minor is required")
        if percent is not None and not (isinstance(percent, int) and 1 <= percent <= 100):
            raise PromoError("discount_percent must be an integer between 1 and 100")
        if minor is not None and not (isinstance(minor, int) and minor > 0):
            raise PromoError("discount_minor must be a positive integer")


def ensure_wl_trial_plan_version(accounts, *, now: int | None = None) -> dict:
    """Idempotently register the `WL_TRIAL` `mgboost_plan_versions` row that
    legitimizes TRIAL_GRANT's entitlement (DL-060/plan review point 2):
    `plan_kind='COMMERCIAL'` (already-legal CHECK value, no schema change),
    `billing_required=0` -- this is what makes `apply_same_plan_purchase`
    itself refuse to ever sell/renew it (same `billing_required` gate that
    already protects `WL_PACKAGE_*`), while `subscription_renewal.
    append_promo_wl_period` can still set it as a subscription's real plan
    identity. No `mgboost_plan_durations` row -- the trial's initial grant
    never goes through the catalog/duration-validated purchase path.
    `accounts` is the shared `AccountStore` (same object `db.accounts`)."""
    existing = accounts._conn.execute(
        "SELECT * FROM mgboost_plan_versions WHERE plan_code=? ORDER BY version DESC LIMIT 1",
        (WL_TRIAL_PLAN_CODE,),
    ).fetchone()
    if existing is not None:
        return dict(existing)
    return accounts.create_plan_version(
        {
            "plan_code": WL_TRIAL_PLAN_CODE, "version": 1, "display_name": "WL Trial",
            "plan_kind": "COMMERCIAL", "billing_required": False,
            "device_limit_mode": "LIMITED", "device_limit": 1,
            "wl_mode": "LIMITED", "wl_quota_bytes": 10_000_000_000, "wl_period_days": 1,
            "terms": {"catalog": "ph5-13-promo-wl-trial-v1", "device_limit": 1, "wl_quota_gb": 10},
        },
        now=now,
    )
