"""PH5-13 promo codes: EXTEND_SUBSCRIPTION and TRIAL_GRANT effects.

`PURCHASE_DISCOUNT` is deliberately **TELEGRAM_STARS-only in v1**. Its
reservation/invoice lifecycle lives in ``StarsPurchaseStore``; there is no
MANUAL_RUB binding or discount accounting path, so no caller may represent a
manual payment as promo-discounted. ``redeem_extend_or_trial`` only ever
handles ``EXTEND_SUBSCRIPTION``/``TRIAL_GRANT``.

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


def _discount_from_effect_params(effect_params: dict, catalog_price: int) -> int:
    """Pure helper: exactly one of discount_percent/discount_minor (the same
    rule `_validate_effect_params` enforces), final price clamped to >= 1
    (a Stars invoice cannot be free)."""
    percent = effect_params.get("discount_percent")
    minor = effect_params.get("discount_minor")
    if (percent is None) == (minor is None):
        raise PromoError("exactly one of discount_percent/discount_minor is required")
    if isinstance(catalog_price, bool) or not isinstance(catalog_price, int):
        raise PromoError("catalog_price must be an integer")
    if percent is not None:
        if isinstance(percent, bool) or not isinstance(percent, int) or not 1 <= percent <= 100:
            raise PromoError("discount_percent must be an integer between 1 and 100")
        return max(1, catalog_price - catalog_price * percent // 100)
    if isinstance(minor, bool) or not isinstance(minor, int) or minor <= 0:
        raise PromoError("discount_minor must be a positive integer")
    return max(1, int(catalog_price) - int(minor))


class _RedemptionTxMixin:
    """Transactional PURCHASE_DISCOUNT helpers. Placed on PromoStore via
    inheritance-free composition: these methods assume the caller already
    holds `self._lock` AND an open BEGIN IMMEDIATE on `self._conn`."""

    def purchase_reservation_locked(self, redemption_id: int):
        """Public transaction-bound interface for payment adapters.

        The caller must hold this store's shared lock and have an active
        ``BEGIN IMMEDIATE`` on the shared connection.
        """
        return self._conn.execute(
            "SELECT r.*,v.effect_params_json,d.effect_kind "
            "FROM mgboost_promo_redemptions r "
            "JOIN mgboost_promo_versions v ON v.promo_id=r.promo_id AND v.version=r.promo_version "
            "JOIN mgboost_promo_definitions d ON d.id=r.promo_id "
            "WHERE r.id=?", (int(redemption_id),),
        ).fetchone()

    def bind_purchase_reservation_locked(self, *, redemption_id: int, telegram_id: int,
                                         bound_kind: str, bound_invoice_id: int, now: int) -> dict:
        """Bind a live RESERVED purchase reservation to a specific invoice,
        inside the invoice's own create transaction. The reservation stays
        RESERVED -- binding only fixes its invoice; TTL cleanup stops
        touching it while that invoice is alive and payable."""
        row = self.purchase_reservation_locked(int(redemption_id))
        if row is None or row["effect_kind"] != "PURCHASE_DISCOUNT":
            raise PromoNotFound("no such purchase reservation")
        if int(row["owner_telegram_id"] or -1) != int(telegram_id):
            raise PromoIneligible("reservation does not belong to this payer")
        if row["status"] != "RESERVED":
            raise PromoConflict(f"reservation is {row['status']}, not RESERVED")
        updated = self._conn.execute(
            "UPDATE mgboost_promo_redemptions SET bound_kind=?,bound_invoice_id=?,updated_at=? "
            "WHERE id=? AND status='RESERVED'",
            (bound_kind, int(bound_invoice_id), int(now), int(redemption_id)),
        )
        if updated.rowcount != 1:
            raise PromoConflict("reservation was concurrently modified")
        return {"effect_params": json.loads(row["effect_params_json"]),
                "reserved_until": row["reserved_until"]}

    def commit_purchase_reservation_locked(self, *, redemption_id: int, invoice_id: int, now: int) -> None:
        """pre_checkout acceptance gate: CAS RESERVED->COMMITTED. Failure
        means the reservation was cancelled (cleanup raced ahead of the
        delayed checkout) -- the caller must reject the checkout so no money
        moves against a pool-returned promo."""
        updated = self._conn.execute(
            "UPDATE mgboost_promo_redemptions SET status='COMMITTED',updated_at=? "
            "WHERE id=? AND bound_invoice_id=? AND status='RESERVED' AND "
            "(reserved_until IS NULL OR reserved_until > ?)",
            (int(now), int(redemption_id), int(invoice_id), int(now)),
        )
        if updated.rowcount != 1:
            raise PromoConflict("reservation is no longer valid for checkout")

    def redeem_purchase_reservation_locked(self, *, redemption_id: int, invoice_id: int, now: int) -> None:
        """Payment-capture gate: CAS -> REDEEMED inside the capture
        transaction. Zero rows aborts the caller's transaction (one payment
        == one redemption). A late successful_payment on a CANCELLED
        reservation is impossible by construction: reaching checkout
        required the COMMITTED gate, after which CANCELLED is unreachable."""
        updated = self._conn.execute(
            "UPDATE mgboost_promo_redemptions SET status='REDEEMED',updated_at=? "
            "WHERE id=? AND bound_invoice_id=? AND status IN ('RESERVED','COMMITTED')",
            (int(now), int(redemption_id), int(invoice_id)),
        )
        if updated.rowcount != 1:
            raise PromoConflict("reservation cannot be redeemed for this invoice")

    def release_expired_reservations(self, *, now: int | None = None, limit: int = 500) -> int:
        """Sweeper (own transaction). Two rules:
        1. unbound: TTL passed -> CANCELLED;
        2. bound: only when the bound invoice is canonically unpayable --
           STARS `created` past its own expires_at, MANUAL_RUB CANCELLED or
           REJECTED. A bound reservation whose invoice is live or paid is
           NEVER touched by TTL (COMMITTED ones never, at all)."""
        timestamp = int(time.time()) if now is None else int(now)
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                cursor = self._conn.execute(
                    "UPDATE mgboost_promo_redemptions SET status='CANCELLED',updated_at=? "
                    "WHERE status='RESERVED' AND bound_invoice_id IS NULL "
                    "AND reserved_until IS NOT NULL AND reserved_until <= ? "
                    "AND id IN (SELECT id FROM mgboost_promo_redemptions "
                    "WHERE status='RESERVED' AND bound_invoice_id IS NULL "
                    "AND reserved_until IS NOT NULL AND reserved_until <= ? LIMIT ?)",
                    (timestamp, timestamp, timestamp, int(limit)),
                )
                unbound = cursor.rowcount
                bound_stars = self._conn.execute(
                    "UPDATE mgboost_promo_redemptions SET status='CANCELLED',updated_at=? "
                    "WHERE id IN (SELECT r.id FROM mgboost_promo_redemptions r "
                    "JOIN stars_invoices i ON i.id=r.bound_invoice_id "
                    "WHERE r.bound_kind='STARS' AND r.status='RESERVED' "
                    "AND i.status='created' AND i.expires_at <= ? LIMIT ?)",
                    (timestamp, timestamp, int(limit)),
                ).rowcount
                bound_manual = self._conn.execute(
                    "UPDATE mgboost_promo_redemptions SET status='CANCELLED',updated_at=? "
                    "WHERE id IN (SELECT r.id FROM mgboost_promo_redemptions r "
                    "JOIN mgboost_manual_payment_records m ON m.id=r.bound_invoice_id "
                    "WHERE r.bound_kind='MANUAL_RUB' AND r.status='RESERVED' "
                    "AND m.status IN ('CANCELLED','REJECTED') LIMIT ?)",
                    (timestamp, int(limit)),
                ).rowcount
                self._conn.commit()
                return unbound + bound_stars + bound_manual
            except Exception:
                self._conn.rollback()
                raise

class PromoStore(_RedemptionTxMixin):
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
        effect_params: dict, reason: str, idempotency_key: str,
        per_user_limit: int = 1, now: int | None = None,
    ) -> dict:
        actor = self._require_primary(capability)
        clean_reason = _clean_reason(reason)
        clean_code = _clean_code(code)
        if isinstance(per_user_limit, bool) or not isinstance(per_user_limit, int) \
                or per_user_limit < 1:
            raise PromoError("per_user_limit must be a positive integer")
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
                    "(code,effect_kind,trial_class,per_user_limit,status,"
                    "created_by_actor,created_at,updated_at) "
                    "VALUES (?,?,?,?,'ACTIVE',?,?,?)",
                    (clean_code, effect_kind, trial_class, per_user_limit,
                     actor, timestamp, timestamp),
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
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                updated = self._conn.execute(
                    "UPDATE mgboost_promo_definitions SET status='DISABLED',updated_at=? "
                    "WHERE code=? AND status='ACTIVE'",
                    (timestamp, clean_code),
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
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
        """Support/admin-driven redemption (full primary-admin capability).
        Unlike `redeem_for_telegram_user` this path may bootstrap a fresh
        account for TRIAL_GRANT and may EXTEND a non-WL (STANDARD/NONE)
        plan via `subscription_admin_ops.apply_adjustment`."""
        actor = self._require_primary(capability)
        return self._redeem(
            capability=capability,
            code=code, telegram_id=telegram_id, reason=reason,
            idempotency_key=idempotency_key, now=now,
            actor_type=ACTOR_TYPE, actor_ref=actor, self_service=False,
        )

    def redeem_for_telegram_user(
        self, *, code: str, telegram_id: int, idempotency_key: str,
        now: int | None = None,
    ) -> dict:
        """PH5-13 user self-service redemption (bot / LK ingress). NO admin
        capability is involved: the acting principal IS the proven Telegram
        OWNER identity (`mgboost_telegram_identities`, the same canonical
        lookup the Stars/`/newsub` flows trust), carried in by the
        Telegram-authenticated transport -- eligibility is enforced by the
        promo rules (ACTIVE code, trial-class uniqueness, account state),
        not by an admin session. Deliberately narrower than the admin path:
        the account must already exist (no bootstrap creation) and only
        effects routed through `append_promo_wl_period` are reachable
        (TRIAL_GRANT; EXTEND_SUBSCRIPTION on a WL/LIMITED plan). A
        STANDARD/NONE-plan EXTEND needs the support flow
        (`redeem_extend_or_trial`)."""
        if isinstance(telegram_id, bool) or not isinstance(telegram_id, int) or telegram_id <= 0:
            raise PromoError("telegram_id must be a positive integer")
        return self._redeem(
            capability=None,
            code=code, telegram_id=telegram_id,
            reason="telegram user self-service promo redemption",
            idempotency_key=idempotency_key, now=now,
            actor_type="TELEGRAM_USER", actor_ref=f"telegram:{telegram_id}",
            self_service=True,
        )

    def _redeem(
        self, *, capability, code: str, telegram_id: int, reason: str,
        idempotency_key: str, now: int | None, actor_type: str, actor_ref: str,
        self_service: bool,
    ) -> dict:
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
                self_service=self_service,
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
            # The count is intentionally made inside the write transaction
            # below. BEGIN IMMEDIATE serializes this check and insertion in
            # every local worker and across SQLite processes.
            per_user_limit = int(definition["per_user_limit"])
            # --- Phase 1: durable intent, own transaction, committed
            # before any effect mutation (crash-consistency -- see module
            # docstring). --------------------------------------------------
            with self._lock:
                try:
                    self._conn.execute("BEGIN IMMEDIATE")
                    prior = self._conn.execute(
                        "SELECT COUNT(*) c FROM mgboost_promo_redemptions "
                        "WHERE promo_id=? AND owner_telegram_id=? AND status!='CANCELLED'",
                        (definition["id"], owner_telegram_id),
                    ).fetchone()["c"]
                    if prior >= per_user_limit:
                        raise PromoConflict(
                            "promo code has already been redeemed the maximum number "
                            "of times by this Telegram identity"
                        )
                    cursor = self._conn.execute(
                        "INSERT INTO mgboost_promo_redemptions "
                        "(promo_id,promo_version,trial_class,owner_telegram_id,account_id,"
                        "status,reserved_until,per_user_limit_snapshot,"
                        "idempotency_key_hash,request_hash,actor_type,actor_ref,reason,"
                        "created_at,updated_at) "
                        "VALUES (?,?,?,?,?,'PENDING_APPLY',NULL,?,?,?,?,?,?,?,?)",
                        (definition["id"], version_row["version"], trial_class, owner_telegram_id,
                         account_id, per_user_limit, idem_hash, request_hash, actor_type,
                         actor_ref, clean_reason, timestamp, timestamp),
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
            account_id=account_id, self_service=self_service, actor_type=actor_type,
            actor=actor_ref, reason=clean_reason,
            idempotency_key=effect_idem_key, now=timestamp,
        )

        # --- Phase 3: mark redeemed, separate transaction. -----------------
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                self._conn.execute(
                    "UPDATE mgboost_promo_redemptions SET status='REDEEMED',"
                    "applied_mutation_id=?,updated_at=? WHERE id=? AND status='PENDING_APPLY'",
                    (effect_result.get("mutation_id"), timestamp, redemption_id),
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

        return {
            "redemption_id": redemption_id, "account_id": account_id,
            "status": "REDEEMED", "applied_mutation_id": effect_result.get("mutation_id"),
            "effect_result": effect_result, "already_applied": False,
        }

    def reserve_purchase_for_telegram_user(
        self, *, code: str, telegram_id: int, ttl_seconds: int = 3600,
        idempotency_key: str, now: int | None = None,
    ) -> dict:
        """PH5-13 self-service PURCHASE_DISCOUNT reservation (bot ingress).
        Durable hold with status='RESERVED' and `reserved_until`; no effect
        is applied at reserve time -- the discount lands on the invoice the
        reservation is later bound to. Same proven-Telegram-identity
        principal as `redeem_for_telegram_user`; existing account required.
        Idempotent by the caller-supplied per-event key (replay returns the
        original reservation)."""
        if isinstance(telegram_id, bool) or not isinstance(telegram_id, int) or telegram_id <= 0:
            raise PromoError("telegram_id must be a positive integer")
        if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int) \
                or not 60 <= ttl_seconds <= 86400:
            raise PromoError("ttl_seconds must be an integer between 60 and 86400")
        clean_code = _clean_code(code)
        timestamp = int(time.time()) if now is None else int(now)
        idem_hash = _idempotency_hash(idempotency_key)
        request_hash = _request_hash({"code": clean_code, "telegram_id": int(telegram_id)})

        # Every eligibility read below is part of the same BEGIN IMMEDIATE
        # transaction as the limit check and reservation write.  This is
        # important not only for cross-process SQLite serialization, but for
        # the shared connection: a second thread must never run a read in the
        # middle of this transaction and then roll back its owner.
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                existing = self._conn.execute(
                    "SELECT * FROM mgboost_promo_redemptions WHERE idempotency_key_hash=?",
                    (idem_hash,),
                ).fetchone()
                if existing is not None:
                    if existing["request_hash"] != request_hash:
                        raise PromoConflict("idempotency key reused with a different request")
                    if existing["status"] == "CANCELLED":
                        raise PromoConflict("reservation was cancelled -- request a new one")
                    self._conn.commit()
                    return self._reservation_result(existing, already=True)
                definition = self._conn.execute(
                    "SELECT * FROM mgboost_promo_definitions WHERE code=?", (clean_code,),
                ).fetchone()
                if definition is None or definition["status"] != "ACTIVE":
                    raise PromoNotFound(f"no ACTIVE promo code {clean_code!r}")
                if definition["effect_kind"] != "PURCHASE_DISCOUNT":
                    raise PromoError(f"{definition['effect_kind']} is not a purchase discount")
                version_row = self._conn.execute(
                    "SELECT * FROM mgboost_promo_versions WHERE promo_id=? AND status='ACTIVE'",
                    (definition["id"],),
                ).fetchone()
                if version_row is None:
                    raise PromoNotFound(f"promo code {clean_code!r} has no active version")
                account = self._accounts.get_active_account_by_telegram_id(int(telegram_id))
                if account is None:
                    raise PromoNotFound(f"no active account for telegram_id={telegram_id}")
                per_user_limit = int(definition["per_user_limit"])
                prior = self._conn.execute(
                    "SELECT COUNT(*) c FROM mgboost_promo_redemptions "
                    "WHERE promo_id=? AND owner_telegram_id=? AND status!='CANCELLED'",
                    (definition["id"], int(telegram_id)),
                ).fetchone()["c"]
                if prior >= per_user_limit:
                    raise PromoConflict(
                        "promo code has already been redeemed the maximum number "
                        "of times by this Telegram identity"
                    )
                cursor = self._conn.execute(
                    "INSERT INTO mgboost_promo_redemptions "
                    "(promo_id,promo_version,trial_class,owner_telegram_id,account_id,"
                    "status,reserved_until,per_user_limit_snapshot,"
                    "idempotency_key_hash,request_hash,actor_type,actor_ref,reason,"
                    "created_at,updated_at) "
                    "VALUES (?,?,NULL,?,?,'RESERVED',?,?,?,?,?,?,?,?,?)",
                    (definition["id"], version_row["version"], int(telegram_id),
                     account["id"], timestamp + int(ttl_seconds), per_user_limit,
                     idem_hash, request_hash,
                     "TELEGRAM_USER", f"telegram:{telegram_id}",
                     "telegram user purchase-discount reservation", timestamp, timestamp),
                )
                redemption_id = cursor.lastrowid
                self._conn.commit()
            except sqlite3.IntegrityError as exc:
                self._conn.rollback()
                raise PromoConflict(
                    "a concurrent identical reservation was already committed"
                ) from exc
            except Exception:
                self._conn.rollback()
                raise
        return {
            "redemption_id": redemption_id, "account_id": account["id"],
            "status": "RESERVED", "code": clean_code,
            "effect_params": json.loads(version_row["effect_params_json"]),
            "reserved_until": timestamp + int(ttl_seconds),
            "already": False,
        }

    def _reservation_result(self, row, *, already: bool) -> dict:
        return {
            "redemption_id": row["id"], "account_id": row["account_id"],
            "status": row["status"],
            "reserved_until": row["reserved_until"],
            "already": already,
        }

    def list_recent_redemptions(self, *, limit: int = 100) -> list[dict]:
        """Support read model; routes do not reach into the shared connection."""
        rows = self._conn.execute(
            "SELECT r.id,r.promo_id,d.code AS promo_code,r.promo_version,r.trial_class,"
            "r.owner_telegram_id,r.account_id,r.status,r.reserved_until,"
            "r.per_user_limit_snapshot,r.bound_kind,r.bound_invoice_id,"
            "r.actor_type,r.actor_ref,r.reason,r.created_at,r.updated_at "
            "FROM mgboost_promo_redemptions r "
            "JOIN mgboost_promo_definitions d ON d.id=r.promo_id "
            "ORDER BY r.id DESC LIMIT ?",
            (max(1, min(int(limit), 500)),),
        ).fetchall()
        return [dict(row) for row in rows]

    def _resolve_redemption_target(
        self, *, capability, definition, telegram_id, self_service, reason,
        idem_hash, timestamp,
    ) -> tuple[int, str | None, int]:
        """Resolves account_id + (trial_class, owner_telegram_id) snapshot
        for a NEW redemption (never called on replay). TRIAL_GRANT may
        create the account (reuses the public, already-reviewed
        `AdminGrantStore.create_account_only` -- no duplicated bootstrap
        wiring) EXCEPT on the self-service path, where the account must
        already exist (no admin capability to authorize the bootstrap);
        EXTEND_SUBSCRIPTION requires an existing account."""
        account = self._accounts.get_active_account_by_telegram_id(int(telegram_id))
        if definition["effect_kind"] == "TRIAL_GRANT":
            if account is None:
                if self_service:
                    raise PromoNotFound(
                        f"no active account for telegram_id={telegram_id}; a self-service "
                        "TRIAL_GRANT requires an existing account -- contact support"
                    )
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
        self, *, capability, definition, effect_params, account_id, self_service,
        actor_type, actor, reason, idempotency_key, now,
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
                actor_type=actor_type, actor_ref=actor, reason=reason,
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
                    actor_type=actor_type, actor_ref=actor, reason=reason,
                    idempotency_key=idempotency_key, now=now,
                )
            except RenewalError as exc:
                raise PromoError(str(exc)) from exc
        # STANDARD/NONE: reuse the existing, already-reviewed PH7-01 writer
        # unchanged -- it never touches WL periods, exactly right for a
        # non-WL plan (DL-060: "STANDARD/NONE — обычное продление expiry").
        # Admin-capability-gated, therefore unreachable on the self-service
        # path by construction (checked, not assumed).
        if self_service:
            raise PromoIneligible(
                "self-service promo EXTEND requires a WL/LIMITED plan -- "
                "contact support for a non-WL extension"
            )
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
                "WL_TRIAL plan_version is not registered -- complete normal "
                "application startup before granting trials"
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
    expected = {
            "plan_code": WL_TRIAL_PLAN_CODE, "version": 1, "display_name": "WL Trial",
            "plan_kind": "COMMERCIAL", "billing_required": False,
            "device_limit_mode": "LIMITED", "device_limit": 1,
            "wl_mode": "LIMITED", "wl_quota_bytes": 10_000_000_000, "wl_period_days": 1,
            "terms": {"catalog": "ph5-13-promo-wl-trial-v1", "device_limit": 1, "wl_quota_gb": 10},
    }
    if existing is None:
        return accounts.create_plan_version(expected, now=now)
    result = dict(existing)
    required = {
        "version": 1, "plan_kind": "COMMERCIAL", "billing_required": 0,
        "device_limit_mode": "LIMITED", "device_limit": 1, "wl_mode": "LIMITED",
        "wl_quota_bytes": 10_000_000_000, "wl_period_days": 1,
    }
    if any(result.get(key) != value for key, value in required.items()) or json.loads(result["terms_json"]) != expected["terms"]:
        raise PromoError("existing WL_TRIAL plan_version does not match ph5-13-promo-wl-trial-v1")
    return result


# --- PURCHASE_DISCOUNT: reservation lifecycle ---------------------------------
#
# States: RESERVED -> COMMITTED -> REDEEMED, terminal CANCELLED.
#   RESERVED   durable hold written by `reserve_purchase_for_telegram_user`
#              (own transaction, before any invoice exists).
#   bind       at invoice creation: the caller's invoice transaction sets
#              bound_kind/bound_invoice_id (`bind_purchase_reservation_locked`); the
#              reservation stays RESERVED -- its TTL no longer decides its
#              fate once the invoice is alive and payable.
#   COMMITTED  at pre_checkout acceptance (`commit_purchase_reservation_locked` CAS
#              RESERVED->COMMITTED). After this point the promo can never
#              return to the pool -- the financial double-spend window
#              (checkout accepted -> TTL -> code reused -> late payment) is
#              closed by construction.
#   REDEEMED   inside the payment capture transaction
#              (`redeem_purchase_reservation_locked` CAS RESERVED/COMMITTED ->
#              REDEEMED); zero rows roll back the whole capture.
#   CANCELLED  terminal, never revived: unbound TTL expiry, or the bound
#              invoice reaching a canonical unpayable state
#              (`release_expired_reservations`).
#
# All `*_locked` helpers below run strictly inside the CALLER's write
# transaction (SQLite same-connection BEGIN IMMEDIATE) and must never take
# `self._lock` themselves -- nested acquisition would deadlock.
