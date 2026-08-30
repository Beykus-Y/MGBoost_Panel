"""Shared DIRECT-account bootstrap domain primitive: create-or-reuse exactly
one canonical DIRECT account for a Telegram identity, with the full
first-device-bootstrap-capable provisioning wiring and the OWNER telegram
identity bound.

Extracted from `AdminGrantStore` (PH7-14) so that MORE THAN ONE caller can
bootstrap canonical accounts WITHOUT duplicating the account-creation SQL or
weaving a second account engine, and WITHOUT turning the capability-gated
admin API into a user API:

* `AdminGrantStore` (admin boundary) -- unchanged behavior, same constants,
  now delegating here. `PrimaryAdminAuthority` stays exactly where it was:
  in the admin store's public methods. This module itself carries NO
  authorization boundary -- it is a deterministic domain primitive; whoever
  may call it for a new principal is decided by the caller's own module.
* `PromoStore` self-service `TRIAL_GRANT` bootstrap -- the ONLY
  non-admin caller, restricted to the narrow, owner-reviewed policy
  (`trial_class == "WL_TRIAL"`, see promo.py) and reached exclusively with
  the Telegram-authenticated principal already used by every
  self-service redemption.
* `commercial_signup.ensure_signup_account` deliberately stays as-is: it is
  invoice-anchored (payment provenance lives in its own tables) and its
  fill-once invoice binding is a different concern.

Crash-safety contract (state F of the trial slice): the provisioning wiring
is (re-)built for BOTH a freshly created and a reused account, so a crash
anywhere between account creation and wiring cannot leave a permanently
un-wired account behind a retry. `create_account`, the wiring transaction
and `link_telegram_owner` are each independently durable and idempotent;
retrying the whole call converges to exactly one account, one PRIMARY alias,
one review row, one template job and one OWNER identity. The process lock is
held across create+wiring+link (the `commercial_signup.ensure_signup_account`
pattern): releasing it in between would let a concurrent bootstrap for the
SAME brand-new telegram_id observe "no OWNER yet" and allocate a second,
permanently-orphaned account.
"""

from __future__ import annotations

import json

from .commercial_signup import derive_template_username

__all__ = [
    "ensure_direct_provisioning_wiring",
    "ensure_direct_account",
]


def ensure_direct_provisioning_wiring(
    connection, lock, *,
    account_id: int, public_id: str, decision_ref: str,
    actor: str, now: int,
    alias_mapping_prefix: str, alias_ownership_provenance: str,
    review_ownership_evidence: str, evidence: dict,
) -> None:
    """Idempotently ensure the PH5-11-shaped provisioning wiring for one
    DIRECT account: the `mgboost_legacy_alias_groups` row, the system-owned
    PRIMARY `tpl-<public_id>` alias, the `mgboost_direct_account_reviews`
    audit row and the non-payment-origin template-provisioning job
    (`mgboost_admin_grant_template_jobs` -- the queue the stars.py worker
    already drives to convergence). No-op if the account already has any
    PRIMARY-capable alias. The caller's `evidence` dict gains the
    `account_id` key; JSON is canonical (sort_keys, tight separators)."""
    existing = connection.execute(
        "SELECT id FROM mgboost_legacy_account_aliases WHERE account_id=?",
        (account_id,),
    ).fetchone()
    if existing is not None:
        return
    template_username = derive_template_username(public_id)
    evidence_json = json.dumps(
        {**evidence, "account_id": account_id},
        sort_keys=True, separators=(",", ":"),
    )
    with lock:
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO mgboost_legacy_alias_groups "
                "(account_id,mapping_key,decision_ref,created_by_actor,created_at) "
                "VALUES (?,?,?,?,?)",
                (account_id, f"{alias_mapping_prefix}:{public_id}", decision_ref, actor, now),
            )
            connection.execute(
                "INSERT INTO mgboost_legacy_account_aliases "
                "(account_id,legacy_username,alias_role,ownership_provenance,legacy_status,"
                "legacy_expiry,observed_device_count,observed_hwid_count,evidence_json,created_at) "
                "VALUES (?,?,'PRIMARY',?,'ACTIVE',NULL,0,0,?,?)",
                (account_id, template_username, alias_ownership_provenance, evidence_json, now),
            )
            connection.execute(
                "INSERT INTO mgboost_direct_account_reviews "
                "(account_id,legacy_username,ownership_evidence,decision_ref,reviewed_by_actor,"
                "evidence_json,created_at) VALUES (?,?,?,?,?,?,?)",
                (account_id, template_username, review_ownership_evidence, decision_ref, actor,
                 evidence_json, now),
            )
            connection.execute(
                "INSERT OR IGNORE INTO mgboost_admin_grant_template_jobs "
                "(account_id,decision_ref,state,created_at,updated_at) "
                "VALUES (?,?,'PENDING',?,?)",
                (account_id, decision_ref, now, now),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise


def ensure_direct_account(
    connection, lock, accounts, *,
    telegram_id: int, actor: str, decision_ref: str, now: int,
    identity_provenance: str,
    alias_ownership_provenance: str,
    alias_mapping_prefix: str,
    review_ownership_evidence: str,
    evidence: dict,
) -> tuple[dict, bool]:
    """Create-or-reuse exactly one ACTIVE DIRECT account owned by
    `telegram_id` and guarantee the full provisioning wiring + OWNER
    identity. Returns `(account, reused)`. Never a second account for one
    identity (`get_active_account_by_telegram_id` is the reuse boundary) and
    never a second OWNER identity (`link_telegram_owner` uniqueness). See
    the module docstring for the crash-safety contract."""
    with lock:
        account = accounts.get_active_account_by_telegram_id(int(telegram_id))
        reused = account is not None
        if account is None:
            account = accounts.create_account("DIRECT", now=now)
        ensure_direct_provisioning_wiring(
            connection, lock,
            account_id=account["id"], public_id=account["public_id"],
            decision_ref=decision_ref, actor=actor, now=now,
            alias_mapping_prefix=alias_mapping_prefix,
            alias_ownership_provenance=alias_ownership_provenance,
            review_ownership_evidence=review_ownership_evidence,
            evidence=evidence,
        )
        accounts.link_telegram_owner(
            account["id"], int(telegram_id),
            provenance=identity_provenance, actor=actor, now=now,
        )
        return account, reused
