"""Admin-grant domain primitive: a non-financial entitlement grant of an
existing commercial plan product, gated by PrimaryAdmin capability.

This is not a canary-only mechanism and carries no canary-specific logic --
it is the general "admin grants a canonical commercial product with no money
moving" primitive a future admin UI / promo-grant layer can reuse as-is. The
current caller (a controlled no-payment WL canary account) is simply the
first consumer.

Design constraints, all deliberate:

* Reuses -- never duplicates -- the exact PH5-02 engine
  (`subscription_renewal.apply_same_plan_purchase`) that both the Stars
  (PH5-05) and manual-RUB (PH5-09) purchase paths already apply through.
  That engine is also what a brand-new account's FIRST grant goes through
  (its `subscription is None` branch is exactly PH5-11's own "CREATE"
  case for a fresh Stars signup) -- so it is equally correct for an
  initial grant, not only a renewal.  This module adds no second
  subscription/period engine.
* `payment_channel='ADMIN_GRANT'` / `mutation_source='ADMIN'` is not new
  vocabulary: both values already exist in PH3-09's `provenance.py`
  (`PAYMENT_CHANNELS`, `MUTATION_SOURCES`, and the
  `ADMIN: {ADMIN_GRANT, NOT_APPLICABLE}` combination) and in the
  `mgboost_entitlement_mutations` CHECK constraints -- this module is the
  first caller to actually exercise that already-provisioned combination
  for a commercial plan.
* Zero financial rows: no `mgboost_payment_records`, no `stars_invoices`,
  no `mgboost_manual_payment_records` row is ever created here. The applied
  mutation is not revenue and is not refundable.
* Idempotent: `apply_same_plan_purchase`'s own idempotency-key uniqueness is
  the crash-safe replay boundary, exactly like every other PH5 caller.
* No implicit plan change: granting an EXISTING account routes straight
  into the engine's own same-plan-only guard (`PlanMismatch`) -- a
  different-plan grant on a live subscription is refused, matching every
  other caller of this engine (upgrade/downgrade is explicitly out of
  scope, PH5-06).
* A brand-new account also needs the SAME system-owned provisioning
  wiring PH5-11's self-service signup creates (`mgboost_legacy_alias_
  groups` / `mgboost_legacy_account_aliases` PRIMARY `tpl-<public_id>` /
  `mgboost_direct_account_reviews`) -- without it, first-device bootstrap
  fails closed (`opaque_resolver.resolve_account_device` requires a
  PRIMARY alias for every account, migrated or not). This module reuses
  those exact tables/values, only substituting `ownership_provenance=
  OWNER_APPROVED` (an admin grant, not payment evidence) for PH5-11's
  `EVIDENCE_PROVEN`. Remote template provisioning itself is queued in a
  NEW small table (`mgboost_admin_grant_template_jobs`, PH7-14) rather
  than PH5-11's `mgboost_signup_template_jobs`, whose `invoice_id` is
  `NOT NULL` by design (payment-anchored) -- an ADMIN_GRANT account has no
  invoice and must never be given a fabricated one. The actual remote
  provisioning call (`commercial_signup.ensure_template_for_account`) is
  reused unchanged; only the job queue differs.
"""

from __future__ import annotations

import json
import time

from .admin_authority import PrimaryAdminAuthorizationError
from .commercial_signup import derive_template_username
from .subscription_renewal import (
    PlanMismatch, RenewalError, UnknownPlan, UnlimitedSubscriptionConflict,
)

__all__ = [
    "AdminGrantError", "AdminGrantConflict", "PlanMismatch", "RenewalError",
    "UnknownPlan", "UnlimitedSubscriptionConflict", "AdminGrantStore",
]

PAYMENT_CHANNEL = "ADMIN_GRANT"
MUTATION_SOURCE = "ADMIN"
ACTOR_TYPE = "PRIMARY_ADMIN"
_TELEGRAM_IDENTITY_PROVENANCE = "ADMIN_REBIND"
_ALIAS_OWNERSHIP_PROVENANCE = "OWNER_APPROVED"
_REVIEW_OWNERSHIP_EVIDENCE = "PROVEN"


class AdminGrantError(ValueError):
    pass


class AdminGrantConflict(AdminGrantError):
    pass


def _clean_reason(reason: str) -> str:
    if not isinstance(reason, str):
        raise AdminGrantError("reason must be a string")
    reason = reason.strip()
    if not 8 <= len(reason) <= 1000:
        raise AdminGrantError("reason must be between 8 and 1000 characters")
    return reason


class AdminGrantStore:
    def __init__(self, connection, lock, accounts, subscription_renewal, primary_admin_authority):
        self._conn = connection
        self._lock = lock
        self._accounts = accounts
        self._subscription_renewal = subscription_renewal
        self._authority = primary_admin_authority

    def _require_primary(self, capability) -> str:
        try:
            return self._authority.require(capability)
        except PrimaryAdminAuthorizationError:
            raise

    def _ensure_direct_provisioning_wiring(
        self, *, account_id: int, public_id: str, decision_ref: str,
        actor: str, now: int,
    ) -> None:
        """Idempotent: no-op if this account already has a PRIMARY alias
        (mirrors `commercial_signup.ensure_signup_account`'s own
        `if alias is None` guard byte-for-byte, same tables, same PRIMARY
        `tpl-<public_id>` username -- only the ownership_provenance/
        ownership_evidence values and the job-queue table differ)."""
        existing = self._conn.execute(
            "SELECT id FROM mgboost_legacy_account_aliases WHERE account_id=?",
            (account_id,),
        ).fetchone()
        if existing is not None:
            return
        template_username = derive_template_username(public_id)
        evidence = {"account_id": account_id, "origin": PAYMENT_CHANNEL, "decision_ref": decision_ref}
        evidence_json = json.dumps(evidence, sort_keys=True, separators=(",", ":"))
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                self._conn.execute(
                    "INSERT INTO mgboost_legacy_alias_groups "
                    "(account_id,mapping_key,decision_ref,created_by_actor,created_at) "
                    "VALUES (?,?,?,?,?)",
                    (account_id, f"admin-grant-v1:{public_id}", decision_ref, actor, now),
                )
                self._conn.execute(
                    "INSERT INTO mgboost_legacy_account_aliases "
                    "(account_id,legacy_username,alias_role,ownership_provenance,legacy_status,"
                    "legacy_expiry,observed_device_count,observed_hwid_count,evidence_json,created_at) "
                    "VALUES (?,?,'PRIMARY',?,'ACTIVE',NULL,0,0,?,?)",
                    (account_id, template_username, _ALIAS_OWNERSHIP_PROVENANCE, evidence_json, now),
                )
                self._conn.execute(
                    "INSERT INTO mgboost_direct_account_reviews "
                    "(account_id,legacy_username,ownership_evidence,decision_ref,reviewed_by_actor,"
                    "evidence_json,created_at) VALUES (?,?,?,?,?,?,?)",
                    (account_id, template_username, _REVIEW_OWNERSHIP_EVIDENCE, decision_ref, actor,
                     evidence_json, now),
                )
                self._conn.execute(
                    "INSERT OR IGNORE INTO mgboost_admin_grant_template_jobs "
                    "(account_id,decision_ref,state,created_at,updated_at) "
                    "VALUES (?,?,'PENDING',?,?)",
                    (account_id, decision_ref, now, now),
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def _resolve_or_create_account(
        self, *, actor: str, telegram_id: int, reason: str, now: int,
    ) -> tuple[dict, bool]:
        """Shared create-or-reuse step for `create_account_only` and
        `grant_new_account`: never a second account for the same
        `telegram_id`. Returns `(account, reused)`."""
        account = self._accounts.get_active_account_by_telegram_id(int(telegram_id))
        if account is not None:
            return account, True
        account = self._accounts.create_account("DIRECT", now=now)
        self._ensure_direct_provisioning_wiring(
            account_id=account["id"], public_id=account["public_id"],
            decision_ref=reason, actor=actor, now=now,
        )
        self._accounts.link_telegram_owner(
            account["id"], int(telegram_id),
            provenance=_TELEGRAM_IDENTITY_PROVENANCE, actor=actor, now=now,
        )
        return account, False

    def create_account_only(
        self, capability, *, telegram_id: int, reason: str,
        idempotency_key: str, now: int | None = None,
    ) -> dict:
        """Create-or-reuse exactly one DIRECT account owned by `telegram_id`,
        with the full first-device-bootstrap-capable provisioning wiring,
        but grant NO plan. For the "create the account now, decide
        ADMIN_GRANT vs MANUAL_RUB afterward" flow -- `ManualPaymentStore.
        create_record` requires an existing `account_id`, and this is the
        only public way to create one with working wiring outside a paid
        signup. `idempotency_key` is accepted for API symmetry with the
        grant methods but is not itself load-bearing here: `telegram_id`
        reuse is already the real idempotency boundary (`_resolve_or_create_
        account`), and account creation carries no engine-level replay
        state to key on."""
        actor = self._require_primary(capability)
        clean_reason = _clean_reason(reason)
        if not isinstance(idempotency_key, str) or not 16 <= len(idempotency_key) <= 512:
            raise AdminGrantError("idempotency_key must be a string between 16 and 512 characters")
        timestamp = int(time.time()) if now is None else int(now)
        account, reused = self._resolve_or_create_account(
            actor=actor, telegram_id=telegram_id, reason=clean_reason, now=timestamp,
        )
        return {
            "account_id": account["id"], "account_public_id": account["public_id"],
            "reused": reused,
        }

    def repair_missing_provisioning_wiring(
        self, capability, *, account_id: int, reason: str, now: int | None = None,
    ) -> bool:
        """Idempotent repair primitive for an account granted BEFORE this
        wiring existed (or any other account that somehow reached ACTIVE
        without a PRIMARY alias): backfills the exact same PH5-11-shaped
        wiring `grant_new_account` now creates inline for brand-new
        accounts. No-op (`False`) if the account already has a PRIMARY
        alias -- never re-wires or duplicates. `account_source` must be
        `DIRECT` (the same account-source `mgboost_direct_account_reviews`
        itself requires by trigger)."""
        actor = self._require_primary(capability)
        clean_reason = _clean_reason(reason)
        timestamp = int(time.time()) if now is None else int(now)
        account = self._accounts.get_account(int(account_id))
        if account is None or account["status"] == "CLOSED":
            raise AdminGrantError("account not found or closed")
        if account["account_source"] != "DIRECT":
            raise AdminGrantError("provisioning wiring repair requires a DIRECT account")
        existing = self._conn.execute(
            "SELECT id FROM mgboost_legacy_account_aliases WHERE account_id=?",
            (int(account_id),),
        ).fetchone()
        if existing is not None:
            return False
        self._ensure_direct_provisioning_wiring(
            account_id=int(account_id), public_id=account["public_id"],
            decision_ref=clean_reason, actor=actor, now=timestamp,
        )
        return True

    def pending_template_jobs(self) -> list[dict]:
        """Same shape/contract as `commercial_signup.pending_template_jobs`
        -- read by the same `_tick` worker loop, a separate queue only
        because the job row itself carries no invoice."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM mgboost_admin_grant_template_jobs WHERE state='PENDING' "
                "ORDER BY account_id"
            ).fetchall()
        return [dict(row) for row in rows]

    def record_template_result(
        self, account_id: int, *, state: str, error_class: str | None = None,
        now: int | None = None,
    ) -> None:
        if state not in {"PENDING", "READY", "MANUAL_REVIEW"}:
            raise AdminGrantError("invalid admin-grant template job state")
        timestamp = int(time.time()) if now is None else int(now)
        with self._lock:
            self._conn.execute(
                "UPDATE mgboost_admin_grant_template_jobs SET state=?,attempts=attempts+1,"
                "last_error_class=?,last_attempt_at=?,ready_at=?,updated_at=? "
                "WHERE account_id=? AND state='PENDING'",
                (state, error_class, timestamp, timestamp if state == "READY" else None,
                 timestamp, int(account_id)),
            )
            self._conn.commit()

    def grant_existing_account(
        self, capability, *, account_id: int, plan_code: str, duration_days: int,
        reason: str, idempotency_key: str, now: int | None = None,
    ) -> dict:
        """Grant an exact commercial plan/duration product to an EXISTING
        account with no money moving. Refuses (`PlanMismatch`) rather than
        implicitly changing an account already on a different live plan --
        upgrade/downgrade is out of scope here (PH5-06)."""
        actor = self._require_primary(capability)
        clean_reason = _clean_reason(reason)
        account = self._accounts.get_account(int(account_id))
        if account is None or account["status"] == "CLOSED":
            raise AdminGrantError("account not found or closed")
        try:
            renewal = self._subscription_renewal.apply_same_plan_purchase(
                account_id=int(account_id), plan_code=plan_code,
                duration_days=int(duration_days),
                payment_channel=PAYMENT_CHANNEL, mutation_source=MUTATION_SOURCE,
                actor_type=ACTOR_TYPE, actor_ref=actor,
                reason=clean_reason, external_reference=None,
                idempotency_key=idempotency_key, now=now,
            )
        except (UnknownPlan, RenewalError):
            # PlanMismatch and UnlimitedSubscriptionConflict are RenewalError
            # subclasses and are re-raised as-is; UnknownPlan too. This
            # except/raise is deliberate so callers can catch the specific
            # subtype (see tests) while this module still fails closed on
            # any other engine-level rejection.
            raise
        return {
            "account_id": int(account_id),
            "account_public_id": account["public_id"],
            **renewal,
        }

    def grant_new_account(
        self, capability, *, telegram_id: int, plan_code: str, duration_days: int,
        reason: str, idempotency_key: str, now: int | None = None,
    ) -> dict:
        """Create-or-reuse exactly one DIRECT account owned by `telegram_id`
        and grant it an exact commercial plan/duration product with no money
        moving. If this exact `telegram_id` already owns an account (e.g. a
        prior call, or a renewal grant), that account is reused -- never a
        second account for the same identity. A DIFFERENT plan on an
        already-live subscription for that account still fails closed via
        `grant_existing_account`'s own `PlanMismatch` guard."""
        # Capability + reason are validated before any write, matching every
        # other PH3/PH5 capability-gated store's fail-fast shape.
        actor = self._require_primary(capability)
        clean_reason = _clean_reason(reason)
        timestamp = int(time.time()) if now is None else int(now)
        account, _reused = self._resolve_or_create_account(
            actor=actor, telegram_id=telegram_id, reason=clean_reason, now=timestamp,
        )
        return self.grant_existing_account(
            capability, account_id=account["id"], plan_code=plan_code,
            duration_days=duration_days, reason=reason,
            idempotency_key=idempotency_key, now=timestamp,
        )
