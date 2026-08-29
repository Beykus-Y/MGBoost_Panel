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
"""

from __future__ import annotations

from .admin_authority import PrimaryAdminAuthorizationError
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
        self._require_primary(capability)
        _clean_reason(reason)
        account = self._accounts.get_active_account_by_telegram_id(int(telegram_id))
        if account is None:
            account = self._accounts.create_account("DIRECT", now=now)
            self._accounts.link_telegram_owner(
                account["id"], int(telegram_id),
                provenance=_TELEGRAM_IDENTITY_PROVENANCE,
                actor=self._authority.require(capability), now=now,
            )
        return self.grant_existing_account(
            capability, account_id=account["id"], plan_code=plan_code,
            duration_days=duration_days, reason=reason,
            idempotency_key=idempotency_key, now=now,
        )
