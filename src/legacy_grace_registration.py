"""PH4-05 mass-grace-campaign registration glue.

Owner decision (2026-08-26): a real legacy user's grace-period membership
must never wait on Telegram registration -- the grace clock is about an
already-real legacy subscription continuing to work, not about Telegram
identity. Telegram linking is something the user does *during* the 14-day
window; migration/opaque delivery follow once it happens.

This module adds exactly three small orchestrations, each composed
entirely from already-existing, already-tested primitives -- no new
resolver, no new ownership rule, no schema change:

  1. `bootstrap_grace_subject()` -- the "ABSENT ownership" bootstrap this
     session's investigation confirmed is architecturally safe:
     `DirectEnrollmentStore.enroll_direct_account(ownership_evidence='ABSENT',
     telegram_id=None)` (already supports this -- no caller anywhere had
     used it before) creates a DIRECT account + reviewed alias with zero
     Telegram claim at all, then reuses the existing owner-attested-payment
     + `legacy_paid_compat` primitives PH4-03 already proved twice in
     production. The account exists, has entitlement, and PH4-05's
     existing `LegacyGraceStore` can start its clock -- all before any
     Telegram identity exists. Creating this account is NOT itself
     evidence of Telegram ownership; nothing here ever asserts who the
     Telegram owner is.
  2. `bind_telegram_after_registration()` -- called once a real Telegram
     user links via the existing bot flow (`tg_users`, unchanged). Reuses
     the exact same ambiguity bar `enroll_direct_account(PROVEN)` already
     enforces (more than one distinct Telegram ID ever linked to this
     username is ambiguous, never auto-resolved) and the existing
     `AccountStore.link_telegram_owner` primitive (idempotent, rejects a
     telegram_id that already owns a different account, rejects a second
     owner for an already-owned account) -- never an automatic *rebind*
     (PH2-05's `ownership_rebind.py` territory, untouched), only a
     first-time assignment to an account that currently has none.
  3. `start_grace_cohort()` -- a thin batch wrapper over the existing
     `LegacyGraceStore.start()`, using one caller-supplied canonical
     timestamp for every member so the whole cohort shares one exact UTC
     boundary, never per-call `time.time()` drift. Idempotent per member
     (deterministic idempotency key derived from `cohort_ref` + account),
     so a partial batch (crash, or an owner-chosen smaller sub-batch) can
     always be safely re-run to complete the rest without re-touching
     already-started members.
"""

from __future__ import annotations

import time

from .legacy_grace import GraceAlreadyStarted


class GraceRegistrationError(RuntimeError):
    pass


class TelegramBindAmbiguous(GraceRegistrationError):
    """More than one distinct Telegram ID has ever linked this legacy
    username -- never auto-resolved. Stays a manual-review case."""


class TelegramBindConflict(GraceRegistrationError):
    """The single evidenced Telegram ID already owns a different account,
    or this account already has a different active owner. Fails closed --
    never a silent/automatic rebind."""


# --- 1. account bootstrap without any Telegram claim ------------------------

def bootstrap_grace_subject(
    db, *, capability, legacy_username: str, legacy_status: str, legacy_expiry: int | None,
    observed_device_count: int, observed_hwid_count: int, decision_ref: str,
    payment_decision_ref: str, payment_attestation_note: str, payment_evidence: dict,
    approved_extra_device_slots: int = 0, idempotency_key: str, now: int | None = None,
) -> dict:
    """Idempotent per legacy_username. Returns
    `{"account_id": int, "subscription": {...}}`. Creates no Telegram
    identity, no device slot, no child, no migration/bridge state -- purely
    the identity/entitlement layer PH4-05's `start()` needs to exist."""
    timestamp = int(time.time()) if now is None else int(now)

    account = db.direct_enrollment.enroll_direct_account(
        capability=capability,
        legacy_username=legacy_username,
        decision_ref=decision_ref,
        ownership_evidence="ABSENT",
        telegram_id=None,
        alias_provenance="EVIDENCE_PROVEN",
        legacy_status=legacy_status,
        legacy_expiry=legacy_expiry,
        observed_device_count=observed_device_count,
        observed_hwid_count=observed_hwid_count,
        evidence={"source": "mass-grace-campaign-bootstrap-v1", "legacy_username_ref": legacy_username[:2] + "***"},
        idempotency_key=idempotency_key,
        now=timestamp,
    )
    account_id = account["account_id"]

    db.direct_enrollment.record_owner_attested_legacy_payment(
        db, capability=capability, account_id=account_id,
        decision_ref=payment_decision_ref, attestation_note=payment_attestation_note,
        evidence=payment_evidence, now=timestamp,
    )

    from .legacy_paid_compat import ensure_legacy_paid_compat_entitlement

    subscription = ensure_legacy_paid_compat_entitlement(
        db, capability=capability, account_id=account_id,
        approved_extra_device_slots=approved_extra_device_slots,
        decision_ref=decision_ref, evidence=None, now=timestamp,
    )
    return {"account_id": account_id, "subscription": subscription}


# --- 2. Telegram linking after the user registers ---------------------------

def bind_telegram_after_registration(
    db, *, legacy_username: str, telegram_id: int, actor: str, now: int | None = None,
) -> str:
    """Returns one of: 'BOUND', 'ALREADY_BOUND', 'NO_ACCOUNT', 'AMBIGUOUS',
    'CONFLICT'. Never raises for the expected non-error outcomes above --
    callers (the bot handler, the daily report) branch on the string.
    `AMBIGUOUS`/`CONFLICT` never mutate anything; they are reported, not
    resolved automatically."""
    timestamp = int(time.time()) if now is None else int(now)

    alias_row = db._conn.execute(
        "SELECT account_id FROM mgboost_legacy_account_aliases WHERE legacy_username=?",
        (legacy_username,),
    ).fetchone()
    if alias_row is None:
        return "NO_ACCOUNT"
    account_id = alias_row["account_id"]

    already_owner = db._conn.execute(
        "SELECT telegram_id FROM mgboost_telegram_identities "
        "WHERE account_id=? AND role='OWNER' AND revoked_at IS NULL",
        (account_id,),
    ).fetchone()
    if already_owner is not None:
        return "ALREADY_BOUND" if int(already_owner["telegram_id"]) == int(telegram_id) else "CONFLICT"

    bot_rows = db._conn.execute(
        "SELECT DISTINCT telegram_id FROM tg_users WHERE marzban_username=?",
        (legacy_username,),
    ).fetchall()
    distinct_ids = {int(row["telegram_id"]) for row in bot_rows}
    if len(distinct_ids) > 1:
        return "AMBIGUOUS"
    if distinct_ids and int(telegram_id) not in distinct_ids:
        return "AMBIGUOUS"

    from .account_store import IdentityConflict

    try:
        db.accounts.link_telegram_owner(
            account_id, int(telegram_id), provenance="DIRECT_BIND", actor=actor, now=timestamp,
        )
    except IdentityConflict:
        return "CONFLICT"
    return "BOUND"


# --- 3. shared-boundary cohort start ----------------------------------------

def start_grace_cohort(
    db, *, capability, account_ids: list[int], cohort_ref: str, reason: str,
    cohort_start_at: int,
) -> dict:
    """Every member gets the exact same `now=cohort_start_at` -- one shared
    UTC boundary, never per-call `time.time()`. Idempotent per account
    (deterministic key from `cohort_ref`+account_id): re-running this for a
    partially-completed cohort only starts the accounts not already
    started, and never touches an already-started member's `started_at`/
    `current_end_at` (enforced by `LegacyGraceStore.start()`'s own
    `GraceAlreadyStarted`-on-mismatched-retry semantics one layer down)."""
    started: list[int] = []
    already: list[int] = []
    failed: dict[int, str] = {}
    for account_id in account_ids:
        idem_key = f"grace-cohort-v1:{cohort_ref}:{int(account_id)}"
        pre_existing = db.legacy_grace.find_by_account(account_id) is not None
        try:
            db.legacy_grace.start(
                account_id=account_id, cohort_ref=cohort_ref, capability=capability,
                reason=reason, idempotency_key=idem_key, now=cohort_start_at,
            )
            (already if pre_existing else started).append(account_id)
        except GraceAlreadyStarted:
            already.append(account_id)
        except Exception as exc:  # noqa: BLE001 -- collected, not swallowed
            failed[account_id] = type(exc).__name__
    return {"started": started, "already_started": already, "failed": failed}
