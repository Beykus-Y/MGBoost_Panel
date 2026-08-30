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

Crash-safety contract (state F of the trial slice): account allocation,
wiring and OWNER binding are one `BEGIN IMMEDIATE` SQLite transaction. A
crash therefore leaves either no account or the complete local bootstrap;
retry converges to one account, one PRIMARY alias, one review row, one
template job and one OWNER identity. The transaction is also the lock across
separate processes; the process-local RLock only protects shared-connection
cursor use.
"""

from __future__ import annotations

import json
import secrets
import sqlite3

from .commercial_signup import derive_template_username

__all__ = [
    "ensure_direct_provisioning_wiring",
    "ensure_direct_account",
]

_ADMIN_GRANT = "ADMIN_GRANT"
_PROMO_TRIAL = "PROMO_TRIAL"
_BOOTSTRAP_POLICIES = {
    _ADMIN_GRANT: {
        "alias_mapping_prefix": "admin-grant-v1",
        "alias_ownership_provenance": "OWNER_APPROVED",
        "review_ownership_evidence": "PROVEN",
    },
    _PROMO_TRIAL: {
        "alias_mapping_prefix": "promo-trial-v1",
        "alias_ownership_provenance": "EVIDENCE_PROVEN",
        "review_ownership_evidence": "PROVEN",
    },
}


def ensure_direct_provisioning_wiring(
    connection, lock, *,
    account_id: int, public_id: str, decision_ref: str,
    actor: str, now: int, bootstrap_policy: str,
    telegram_id: int | None = None,
) -> None:
    """Idempotently ensure the PH5-11-shaped provisioning wiring for one
    DIRECT account: the `mgboost_legacy_alias_groups` row, the system-owned
    PRIMARY `tpl-<public_id>` alias, the `mgboost_direct_account_reviews`
    audit row and the non-payment-origin template-provisioning job
    (`mgboost_admin_grant_template_jobs` -- the queue the stars.py worker
    already drives to convergence). No-op if the account already has any
    PRIMARY-capable alias. The policy-owned evidence gains the `account_id`
    key; JSON is canonical (sort_keys, tight separators)."""
    with lock:
        try:
            connection.execute("BEGIN IMMEDIATE")
            _ensure_direct_provisioning_wiring_locked(
                connection,
                account_id=account_id, public_id=public_id,
                decision_ref=decision_ref, actor=actor, now=now,
                bootstrap_policy=bootstrap_policy, telegram_id=telegram_id,
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise


def ensure_direct_account(
    connection, lock, *,
    telegram_id: int, actor: str, decision_ref: str, now: int,
    bootstrap_policy: str,
) -> tuple[dict, bool]:
    """Create-or-reuse exactly one ACTIVE DIRECT account owned by
    `telegram_id` and guarantee the full provisioning wiring + OWNER
    identity. Returns `(account, reused)`. Never a second account for one
    identity (`get_active_account_by_telegram_id` is the reuse boundary) and
    never a second OWNER identity (`link_telegram_owner` uniqueness). See
    the module docstring for the crash-safety contract."""
    # This deliberately is one SQLite write transaction, rather than the
    # old sequence `create_account()` -> wiring transaction ->
    # `link_telegram_owner()` transactions.  The RLock only serializes one
    # Database instance; BEGIN IMMEDIATE is the cross-process boundary.
    # Without it, two processes can each create an ACTIVE DIRECT account
    # before one loses the OWNER unique-index race, leaving an orphan.
    if isinstance(telegram_id, bool) or not isinstance(telegram_id, int) or telegram_id <= 0:
        raise ValueError("telegram_id must be a positive integer")
    if bootstrap_policy not in _BOOTSTRAP_POLICIES:
        raise ValueError("unsupported direct-account bootstrap policy")
    with lock:
        try:
            connection.execute("BEGIN IMMEDIATE")
            identity = connection.execute(
                "SELECT a.* FROM mgboost_telegram_identities i "
                "JOIN mgboost_accounts a ON a.id=i.account_id "
                "WHERE i.telegram_id=? AND i.role='OWNER' AND i.revoked_at IS NULL",
                (int(telegram_id),),
            ).fetchone()
            if identity is not None:
                if identity["status"] != "ACTIVE":
                    raise ValueError("telegram identity is bound to a non-active account")
                account = dict(identity)
                reused = True
            else:
                account = None
                for _ in range(5):
                    public_id = "acct_" + secrets.token_urlsafe(18)
                    try:
                        cursor = connection.execute(
                            "INSERT INTO mgboost_accounts "
                            "(public_id,status,account_source,created_at,updated_at) "
                            "VALUES (?,'ACTIVE','DIRECT',?,?)",
                            (public_id, int(now), int(now)),
                        )
                        account = {
                            "id": cursor.lastrowid, "public_id": public_id,
                            "status": "ACTIVE", "account_source": "DIRECT",
                        }
                        break
                    except sqlite3.IntegrityError:
                        # A public-id collision is cryptographically remote;
                        # retry exactly as AccountStore.create_account does.
                        continue
                if account is None:
                    raise RuntimeError("could not allocate unique account id")
                reused = False

            _ensure_direct_provisioning_wiring_locked(
                connection,
                account_id=account["id"], public_id=account["public_id"],
                decision_ref=decision_ref, actor=actor, now=now,
                bootstrap_policy=bootstrap_policy, telegram_id=telegram_id,
            )
            if not reused:
                connection.execute(
                    "INSERT INTO mgboost_telegram_identities "
                    "(account_id,telegram_id,role,provenance,linked_at,linked_by_actor) "
                    "VALUES (?,?,'OWNER',?,?,?)",
                    (account["id"], int(telegram_id), _identity_provenance(bootstrap_policy),
                     int(now), actor),
                )
            connection.commit()
            return account, reused
        except Exception:
            connection.rollback()
            raise


def _ensure_direct_provisioning_wiring_locked(
    connection, *, account_id: int, public_id: str, decision_ref: str,
    actor: str, now: int, bootstrap_policy: str, telegram_id: int | None,
) -> None:
    """Write the provisioning rows inside the caller's open transaction."""
    existing = connection.execute(
        "SELECT id FROM mgboost_legacy_account_aliases WHERE account_id=?",
        (account_id,),
    ).fetchone()
    if existing is not None:
        return
    policy = _BOOTSTRAP_POLICIES.get(bootstrap_policy)
    if policy is None:
        raise ValueError("unsupported direct-account bootstrap policy")
    template_username = derive_template_username(public_id)
    evidence_json = json.dumps(
        {**_evidence(bootstrap_policy, decision_ref, telegram_id), "account_id": account_id},
        sort_keys=True, separators=(",", ":"),
    )
    connection.execute(
        "INSERT INTO mgboost_legacy_alias_groups "
        "(account_id,mapping_key,decision_ref,created_by_actor,created_at) "
        "VALUES (?,?,?,?,?)",
        (account_id, f"{policy['alias_mapping_prefix']}:{public_id}", decision_ref, actor, now),
    )
    connection.execute(
        "INSERT INTO mgboost_legacy_account_aliases "
        "(account_id,legacy_username,alias_role,ownership_provenance,legacy_status,"
        "legacy_expiry,observed_device_count,observed_hwid_count,evidence_json,created_at) "
        "VALUES (?,?,'PRIMARY',?,'ACTIVE',NULL,0,0,?,?)",
        (account_id, template_username, policy["alias_ownership_provenance"], evidence_json, now),
    )
    connection.execute(
        "INSERT INTO mgboost_direct_account_reviews "
        "(account_id,legacy_username,ownership_evidence,decision_ref,reviewed_by_actor,"
        "evidence_json,created_at) VALUES (?,?,?,?,?,?,?)",
        (account_id, template_username, policy["review_ownership_evidence"], decision_ref, actor,
         evidence_json, now),
    )
    connection.execute(
        "INSERT OR IGNORE INTO mgboost_admin_grant_template_jobs "
        "(account_id,decision_ref,state,created_at,updated_at) "
        "VALUES (?,?,'PENDING',?,?)",
        (account_id, decision_ref, now, now),
    )


def _identity_provenance(bootstrap_policy: str) -> str:
    return "ADMIN_REBIND" if bootstrap_policy == _ADMIN_GRANT else "DIRECT_BIND"


def _evidence(bootstrap_policy: str, decision_ref: str, telegram_id: int | None) -> dict:
    if bootstrap_policy == _ADMIN_GRANT:
        return {"origin": _ADMIN_GRANT, "decision_ref": decision_ref}
    if bootstrap_policy == _PROMO_TRIAL and isinstance(telegram_id, int) and telegram_id > 0:
        return {
            "origin": "PROMO_TRIAL_SELF_SERVICE", "telegram_id": telegram_id,
            "trial_class": "WL_TRIAL",
        }
    raise ValueError("promo-trial bootstrap requires a Telegram principal")
