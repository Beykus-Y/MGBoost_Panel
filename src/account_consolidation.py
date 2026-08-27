"""PH7-13 account consolidation (merge/supersession) orchestration -- DL-057.

Reuses the exact cross-store composition idiom `legacy_paid_compat.py`/
`legacy_grace_registration.py` already use: plain functions taking `db`
directly (`db._conn`, `db._lock`, `db.primary_admin_authority`,
`db.provenance`), not a dedicated constructor-injected store class -- this
module's job is orchestration across `mgboost_accounts`,
`mgboost_telegram_identities`, `mgboost_child_user_intents`,
`mgboost_device_slot_generations`, `mgboost_subscriptions` and its own three
new PH7-13 tables, not single-table CRUD.

Nothing here ever mutates, deletes or reassigns an absorbed account's
pre-existing rows. `resolve_account_id()` is the one shared canonicalizer
every legacy-username/account resolver must call so that an absorbed
account's identity (its legacy alias, its Telegram registration attempt,
its admin-expiry route) transparently redirects to the survivor for any
*new* operation, while its own history stays exactly where it is, forever
attributed to its original account_id.
"""

from __future__ import annotations

import sqlite3
import time

from .admin_authority import PrimaryAdminAuthorizationError


class AccountConsolidationError(RuntimeError):
    pass


class PrimaryAdminRequired(AccountConsolidationError):
    pass


class SelfMergeError(AccountConsolidationError):
    pass


class AbsorbedNotClosed(AccountConsolidationError):
    pass


class SurvivorNotActive(AccountConsolidationError):
    pass


class MergeChainConflict(AccountConsolidationError):
    pass


class MergeConflict(AccountConsolidationError):
    pass


class MergeNotFound(AccountConsolidationError):
    pass


class ClosedAccountError(AccountConsolidationError):
    pass


class TelegramOwnerStillActive(AccountConsolidationError):
    pass


class NonTerminalChildExists(AccountConsolidationError):
    pass


class ActiveGenerationExists(AccountConsolidationError):
    pass


class AccountNotFound(AccountConsolidationError):
    pass


def _require_primary(db, capability) -> str:
    try:
        return db.primary_admin_authority.require(capability)
    except PrimaryAdminAuthorizationError:
        raise PrimaryAdminRequired("primary MGBoost admin capability required")


def _clean(value, *, minlen, maxlen, field) -> str:
    text = (value or "").strip()
    if not minlen <= len(text) <= maxlen:
        raise AccountConsolidationError(f"{field} must be {minlen}..{maxlen} characters")
    return text


def _get_account(db, account_id: int) -> dict | None:
    row = db._conn.execute(
        "SELECT id,status,row_version FROM mgboost_accounts WHERE id=?", (int(account_id),),
    ).fetchone()
    return dict(row) if row else None


# --- resolution: the one shared canonicalizer -------------------------------

def resolve_account_id(db, account_id: int) -> int:
    """Canonicalize `account_id` through any ACTIVE merge -- returns the
    survivor's id if `account_id` is an absorbed account, else `account_id`
    unchanged. Read-only, no capability required: every resolver (legacy
    bridge, grace-registration Telegram bind, admin read models, admin
    expiry ops) calls this before treating a resolved `account_id` as the
    target of a *new* operation."""
    row = db._conn.execute(
        "SELECT survivor_account_id FROM mgboost_account_merges "
        "WHERE absorbed_account_id=? AND status='ACTIVE'",
        (int(account_id),),
    ).fetchone()
    return int(row["survivor_account_id"]) if row else int(account_id)


def get_active_merge(db, absorbed_account_id: int) -> dict | None:
    row = db._conn.execute(
        "SELECT * FROM mgboost_account_merges WHERE absorbed_account_id=? AND status='ACTIVE'",
        (int(absorbed_account_id),),
    ).fetchone()
    return dict(row) if row else None


def get_merge(db, absorbed_account_id: int) -> dict | None:
    row = db._conn.execute(
        "SELECT * FROM mgboost_account_merges WHERE absorbed_account_id=?",
        (int(absorbed_account_id),),
    ).fetchone()
    return dict(row) if row else None


# --- close/reopen ------------------------------------------------------------

def close_account(
    db, *, capability, account_id: int, decision_ref: str, reason: str, now: int | None = None,
) -> dict:
    """Preconditions (all fail closed, never silently skipped): no active
    Telegram OWNER identity, no non-terminal child intent, no ACTIVE device
    slot generation. On success: any live subscription -> CANCELLED
    (immutable evidence row appended) while the account is still ACTIVE --
    `ProvenanceStore.record_mutation()` itself refuses to write evidence
    against an already-CLOSED account, so the subscription cancellation +
    its evidence must land first -- then, as a separate step, account ->
    CLOSED. Never a physical delete of anything. Idempotent throughout: a
    retry after a crash between the two steps converges (a subscription
    that is already CANCELLED is left alone; an account already CLOSED
    short-circuits immediately)."""
    actor = _require_primary(db, capability)
    account_id = int(account_id)
    decision_ref = _clean(decision_ref, minlen=3, maxlen=128, field="decision_ref")
    reason = _clean(reason, minlen=3, maxlen=500, field="reason")
    timestamp = int(time.time()) if now is None else int(now)

    with db._lock:
        try:
            db._conn.execute("BEGIN IMMEDIATE")
            account = _get_account(db, account_id)
            if account is None:
                raise AccountNotFound(f"account {account_id} not found")
            if account["status"] == "CLOSED":
                db._conn.commit()
                return {"account_id": account_id, "status": "CLOSED", "already_applied": True}
            if account["status"] != "ACTIVE":
                raise AccountConsolidationError(
                    f"cannot close an account in status {account['status']!r}"
                )

            owner = db._conn.execute(
                "SELECT 1 FROM mgboost_telegram_identities "
                "WHERE account_id=? AND role='OWNER' AND revoked_at IS NULL",
                (account_id,),
            ).fetchone()
            if owner is not None:
                raise TelegramOwnerStillActive(
                    "account still has an active Telegram OWNER identity -- revoke it "
                    "(PH2-05 ownership_rebind) before closing"
                )

            non_terminal_child = db._conn.execute(
                "SELECT 1 FROM mgboost_child_user_intents WHERE account_id=? "
                "AND (desired_state!='REVOKED' OR observed_state NOT IN ('REVOKED','NOT_CREATED'))",
                (account_id,),
            ).fetchone()
            if non_terminal_child is not None:
                raise NonTerminalChildExists(
                    "account has a non-terminal child intent -- revoke and free it first"
                )

            active_generation = db._conn.execute(
                "SELECT 1 FROM mgboost_device_slot_generations "
                "WHERE account_id=? AND status='ACTIVE'",
                (account_id,),
            ).fetchone()
            if active_generation is not None:
                raise ActiveGenerationExists(
                    "account has an ACTIVE device slot generation -- free it first"
                )

            live_sub = db._conn.execute(
                "SELECT * FROM mgboost_subscriptions WHERE account_id=? "
                "AND status IN ('PENDING','ACTIVE','DISABLED','UNLIMITED','UNKNOWN_LEGACY')",
                (account_id,),
            ).fetchone()
            if live_sub is not None:
                sub_updated = db._conn.execute(
                    "UPDATE mgboost_subscriptions SET status='CANCELLED',updated_at=?,"
                    "row_version=row_version+1 WHERE id=? AND account_id=? AND row_version=?",
                    (timestamp, live_sub["id"], account_id, live_sub["row_version"]),
                )
                if sub_updated.rowcount != 1:
                    raise AccountConsolidationError("concurrent subscription modification detected")
            db._conn.commit()
        except Exception:
            db._conn.rollback()
            raise

    if live_sub is not None:
        # Outside the lock/transaction above (opens its own `BEGIN
        # IMMEDIATE`, exactly like `legacy_paid_compat.py` does) and, just
        # as importantly, still while the account is ACTIVE --
        # `record_mutation()` hard-refuses evidence for a CLOSED account.
        db.provenance.record_mutation(
            account_id, subscription_id=live_sub["id"],
            operation="ACCOUNT_CLOSED_SUBSCRIPTION_CANCELLED",
            payment_channel="NOT_APPLICABLE", mutation_source="ADMIN",
            actor_type="PRIMARY_ADMIN", actor_ref=actor, reason=decision_ref,
            external_reference=None,
            before={"status": live_sub["status"]},
            after={"status": "CANCELLED", "reason": reason},
            idempotency_key=f"account-close-v1:{account_id}", now=timestamp,
        )

    with db._lock:
        try:
            db._conn.execute("BEGIN IMMEDIATE")
            account = _get_account(db, account_id)
            if account["status"] == "CLOSED":
                db._conn.commit()
                return {"account_id": account_id, "status": "CLOSED", "already_applied": True}
            updated = db._conn.execute(
                "UPDATE mgboost_accounts SET status='CLOSED',updated_at=?,"
                "row_version=row_version+1 WHERE id=? AND row_version=?",
                (timestamp, account_id, account["row_version"]),
            )
            if updated.rowcount != 1:
                raise AccountConsolidationError("concurrent account modification detected")
            db._conn.commit()
        except Exception:
            db._conn.rollback()
            raise

    return {
        "account_id": account_id, "status": "CLOSED",
        "subscription_cancelled": live_sub is not None, "already_applied": False,
    }


def reopen_account(
    db, *, capability, account_id: int, decision_ref: str, reason: str, now: int | None = None,
) -> dict:
    """The reversal counterpart to `close_account()`. Never resurrects a
    revoked/freed child or generation (PH3-05's own rollback policy: a new
    generation, never restoring a leaked UUID) -- only flips the account
    back to ACTIVE so ordinary re-provisioning can proceed from a clean
    slate. Idempotent on an already-ACTIVE account."""
    actor = _require_primary(db, capability)
    account_id = int(account_id)
    decision_ref = _clean(decision_ref, minlen=3, maxlen=128, field="decision_ref")
    reason = _clean(reason, minlen=3, maxlen=500, field="reason")
    timestamp = int(time.time()) if now is None else int(now)

    with db._lock:
        try:
            db._conn.execute("BEGIN IMMEDIATE")
            account = _get_account(db, account_id)
            if account is None:
                raise AccountNotFound(f"account {account_id} not found")
            if account["status"] == "ACTIVE":
                db._conn.commit()
                return {"account_id": account_id, "status": "ACTIVE", "already_applied": True}
            if account["status"] != "CLOSED":
                raise AccountConsolidationError(
                    f"cannot reopen an account in status {account['status']!r}"
                )
            active_merge = db._conn.execute(
                "SELECT 1 FROM mgboost_account_merges WHERE absorbed_account_id=? AND status='ACTIVE'",
                (account_id,),
            ).fetchone()
            if active_merge is not None:
                raise AccountConsolidationError(
                    "account is absorbed by an ACTIVE merge -- reverse the merge first"
                )
            updated = db._conn.execute(
                "UPDATE mgboost_accounts SET status='ACTIVE',updated_at=?,"
                "row_version=row_version+1 WHERE id=? AND row_version=?",
                (timestamp, account_id, account["row_version"]),
            )
            if updated.rowcount != 1:
                raise AccountConsolidationError("concurrent account modification detected")
            db._conn.commit()
        except Exception:
            db._conn.rollback()
            raise

    db.provenance.record_mutation(
        account_id, subscription_id=None, operation="ACCOUNT_REOPENED",
        payment_channel="NOT_APPLICABLE", mutation_source="ADMIN",
        actor_type="PRIMARY_ADMIN", actor_ref=actor, reason=decision_ref,
        external_reference=None, before={"status": "CLOSED"},
        after={"status": "ACTIVE", "reason": reason},
        idempotency_key=f"account-reopen-v1:{account_id}:{timestamp}", now=timestamp,
    )
    return {"account_id": account_id, "status": "ACTIVE", "already_applied": False}


# --- merge / reversal --------------------------------------------------------

def create_merge(
    db, *, capability, absorbed_account_id: int, survivor_account_id: int,
    decision_ref: str, reason: str, now: int | None = None,
) -> dict:
    """Preconditions: absorbed account must already be CLOSED (the caller
    closes it first -- this function never closes anything itself);
    survivor must be ACTIVE; neither id may ever have played the other role
    anywhere in this table (a strict, permanent bipartition -- no chains, no
    cycles, bounded to depth 1, forever, even across a later reversal).
    Idempotent: replaying the exact same absorbed/survivor pair returns the
    existing row instead of erroring; a different survivor for an
    already-merged absorbed account is a hard `MergeConflict`."""
    actor = _require_primary(db, capability)
    absorbed_account_id = int(absorbed_account_id)
    survivor_account_id = int(survivor_account_id)
    decision_ref = _clean(decision_ref, minlen=3, maxlen=128, field="decision_ref")
    reason = _clean(reason, minlen=3, maxlen=500, field="reason")
    if absorbed_account_id == survivor_account_id:
        raise SelfMergeError("an account cannot be merged into itself")
    timestamp = int(time.time()) if now is None else int(now)

    with db._lock:
        try:
            db._conn.execute("BEGIN IMMEDIATE")
            existing = db._conn.execute(
                "SELECT * FROM mgboost_account_merges WHERE absorbed_account_id=?",
                (absorbed_account_id,),
            ).fetchone()
            if existing is not None:
                if existing["survivor_account_id"] != survivor_account_id:
                    raise MergeConflict(
                        "absorbed account is already merged into a different survivor"
                    )
                if existing["status"] == "ACTIVE":
                    db._conn.commit()
                    return {**dict(existing), "already_applied": True}
                # Same absorbed/survivor pair, previously reversed: re-activating
                # is not a new chain risk (identity pair is unchanged), just a
                # fresh decision -- append a new CREATED event, flip status back.
                updated = db._conn.execute(
                    "UPDATE mgboost_account_merges SET status='ACTIVE',updated_at=?,"
                    "row_version=row_version+1 WHERE id=? AND row_version=?",
                    (timestamp, existing["id"], existing["row_version"]),
                )
                if updated.rowcount != 1:
                    raise MergeConflict("concurrent merge modification detected")
                db._conn.execute(
                    "INSERT INTO mgboost_account_merge_events "
                    "(merge_id,event_type,actor_ref,reason,created_at) VALUES (?,'CREATED',?,?,?)",
                    (existing["id"], actor, reason, timestamp),
                )
                row = db._conn.execute(
                    "SELECT * FROM mgboost_account_merges WHERE id=?", (existing["id"],),
                ).fetchone()
                db._conn.commit()
                return {**dict(row), "already_applied": False}

            absorbed = _get_account(db, absorbed_account_id)
            if absorbed is None:
                raise AccountNotFound(f"absorbed account {absorbed_account_id} not found")
            if absorbed["status"] != "CLOSED":
                raise AbsorbedNotClosed(
                    "absorbed account must already be CLOSED before it can be merged -- "
                    "close it first"
                )
            survivor = _get_account(db, survivor_account_id)
            if survivor is None:
                raise AccountNotFound(f"survivor account {survivor_account_id} not found")
            if survivor["status"] != "ACTIVE":
                raise SurvivorNotActive("survivor account must be ACTIVE")

            if db._conn.execute(
                "SELECT 1 FROM mgboost_account_merges WHERE absorbed_account_id=?",
                (survivor_account_id,),
            ).fetchone():
                raise MergeChainConflict(
                    "survivor is itself an absorbed account elsewhere -- chained merges "
                    "are not allowed"
                )
            if db._conn.execute(
                "SELECT 1 FROM mgboost_account_merges WHERE survivor_account_id=?",
                (absorbed_account_id,),
            ).fetchone():
                raise MergeChainConflict(
                    "absorbed account is already a survivor of another merge elsewhere -- "
                    "chained merges are not allowed"
                )

            cursor = db._conn.execute(
                "INSERT INTO mgboost_account_merges "
                "(absorbed_account_id,survivor_account_id,status,decision_ref,"
                "created_by_actor,created_at,updated_at) VALUES (?,?,'ACTIVE',?,?,?,?)",
                (absorbed_account_id, survivor_account_id, decision_ref, actor, timestamp, timestamp),
            )
            merge_id = cursor.lastrowid
            db._conn.execute(
                "INSERT INTO mgboost_account_merge_events "
                "(merge_id,event_type,actor_ref,reason,created_at) VALUES (?,'CREATED',?,?,?)",
                (merge_id, actor, reason, timestamp),
            )
            row = db._conn.execute(
                "SELECT * FROM mgboost_account_merges WHERE id=?", (merge_id,),
            ).fetchone()
            db._conn.commit()
            return {**dict(row), "already_applied": False}
        except sqlite3.IntegrityError as exc:
            db._conn.rollback()
            raise MergeConflict("concurrent merge creation detected") from exc
        except Exception:
            db._conn.rollback()
            raise


def reverse_merge(
    db, *, capability, absorbed_account_id: int, decision_ref: str, reason: str,
    now: int | None = None,
) -> dict:
    """Append-only reversal: never a DELETE of the merge row, only a new
    `REVERSED` event plus a CAS flip of `status`. Does NOT reopen the
    absorbed account and does NOT restore its revoked genesis child/
    generation -- those are separate, deliberate steps (`reopen_account()`
    and, if a device is needed again, ordinary fresh provisioning).
    Idempotent on an already-REVERSED merge."""
    actor = _require_primary(db, capability)
    absorbed_account_id = int(absorbed_account_id)
    decision_ref = _clean(decision_ref, minlen=3, maxlen=128, field="decision_ref")
    reason = _clean(reason, minlen=3, maxlen=500, field="reason")
    timestamp = int(time.time()) if now is None else int(now)

    with db._lock:
        try:
            db._conn.execute("BEGIN IMMEDIATE")
            row = db._conn.execute(
                "SELECT * FROM mgboost_account_merges WHERE absorbed_account_id=?",
                (absorbed_account_id,),
            ).fetchone()
            if row is None:
                raise MergeNotFound(f"no merge exists for absorbed account {absorbed_account_id}")
            if row["status"] == "REVERSED":
                db._conn.commit()
                return {**dict(row), "already_applied": True}
            updated = db._conn.execute(
                "UPDATE mgboost_account_merges SET status='REVERSED',updated_at=?,"
                "row_version=row_version+1 WHERE id=? AND row_version=?",
                (timestamp, row["id"], row["row_version"]),
            )
            if updated.rowcount != 1:
                raise MergeConflict("concurrent merge modification detected")
            db._conn.execute(
                "INSERT INTO mgboost_account_merge_events "
                "(merge_id,event_type,actor_ref,reason,created_at) VALUES (?,'REVERSED',?,?,?)",
                (row["id"], actor, reason, timestamp),
            )
            updated_row = db._conn.execute(
                "SELECT * FROM mgboost_account_merges WHERE id=?", (row["id"],),
            ).fetchone()
            db._conn.commit()
            return {**dict(updated_row), "already_applied": False}
        except Exception:
            db._conn.rollback()
            raise


# --- display name -------------------------------------------------------------

def get_display_name(db, account_id: int) -> str | None:
    row = db._conn.execute(
        "SELECT display_name FROM mgboost_account_display_names "
        "WHERE account_id=? AND revoked_at IS NULL",
        (int(account_id),),
    ).fetchone()
    return row["display_name"] if row else None


def set_display_name(
    db, *, capability, account_id: int, display_name: str, decision_ref: str,
    now: int | None = None,
) -> dict:
    """Purely cosmetic, owner-set human label -- unrelated to any legacy
    alias. Modeled like `mgboost_telegram_identities`: at most one active
    row per account, changing it revokes the old row and inserts a new one,
    never an UPDATE of an existing label. Idempotent if the exact same name
    is already active."""
    actor = _require_primary(db, capability)
    account_id = int(account_id)
    display_name = (display_name or "").strip()
    if not 1 <= len(display_name) <= 64:
        raise AccountConsolidationError("display_name must be 1..64 characters")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in display_name):
        raise AccountConsolidationError("display_name must not contain control characters")
    decision_ref = _clean(decision_ref, minlen=3, maxlen=128, field="decision_ref")
    timestamp = int(time.time()) if now is None else int(now)

    with db._lock:
        try:
            db._conn.execute("BEGIN IMMEDIATE")
            account = _get_account(db, account_id)
            if account is None:
                raise AccountNotFound(f"account {account_id} not found")
            if account["status"] != "ACTIVE":
                raise ClosedAccountError(
                    "cannot set a display name on a non-ACTIVE account"
                )
            current = db._conn.execute(
                "SELECT id,display_name FROM mgboost_account_display_names "
                "WHERE account_id=? AND revoked_at IS NULL",
                (account_id,),
            ).fetchone()
            if current is not None and current["display_name"] == display_name:
                db._conn.commit()
                return {"account_id": account_id, "display_name": display_name, "already_applied": True}
            if current is not None:
                db._conn.execute(
                    "UPDATE mgboost_account_display_names SET revoked_at=? WHERE id=?",
                    (timestamp, current["id"]),
                )
            db._conn.execute(
                "INSERT INTO mgboost_account_display_names "
                "(account_id,display_name,set_at,set_by_actor,decision_ref) "
                "VALUES (?,?,?,?,?)",
                (account_id, display_name, timestamp, actor, decision_ref),
            )
            db._conn.commit()
            return {"account_id": account_id, "display_name": display_name, "already_applied": False}
        except Exception:
            db._conn.rollback()
            raise
