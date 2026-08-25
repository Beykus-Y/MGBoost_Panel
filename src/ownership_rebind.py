"""Durable PH2-05 Telegram ownership recovery/rebind.

Dormant: no route imports this module. Fixed first-rollout policy
(OPD-39/DL-041): only the primary MGBoost admin can rebind, no self-service,
no recovery codes, HWID/subscription-URL possession is never proof of
ownership. This module never determines *whether* a rebind should happen --
that is an out-of-band admin decision -- it only durably, atomically and
idempotently executes one already-decided rebind.

    prepare()  -- validated, capability-gated, idempotent-insert request
    process_rebind() -- claim -> for COMPROMISE only, PH2-01 credential
        rotation *first* -> atomic identity mutation (old REVOKED, new
        ACTIVE, in the same transaction PH3-01's own schema already makes
        exclusive) -> finish. Credential rotation runs before the identity
        swap specifically so a crash between the two durable steps can
        never leave "new owner active while the compromised credential is
        still active" -- see the ordering note on `process_rebind` itself.

Ownership rebind is emphatically not a device rebind: it never calls
`src/child_lifecycle.py`'s REVOKE/FREE/REBIND, never touches
`mgboost_device_slot_generations`/`mgboost_child_user_intents`, and a
successful rebind changes zero bytes in any of the PH3-02/03/08 tables
(proven in tests and the isolated Marzban gate, not just asserted here).
"""

from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
import time

from .admin_authority import PrimaryAdminAuthorizationError


class OwnershipRebindError(RuntimeError):
    pass


class OwnershipRebindConflict(OwnershipRebindError):
    pass


class PrimaryAdminRequired(OwnershipRebindError):
    pass


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical(value: dict) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _base32_128(raw: bytes) -> str:
    return base64.b32encode(raw[:16]).decode("ascii").lower().rstrip("=")


def _derive_operation_id(idem_hash: str) -> str:
    return "rb_" + _base32_128(bytes.fromhex(idem_hash))


class OwnershipRebindStore:
    def __init__(self, connection: sqlite3.Connection, lock, primary_admin_authority):
        self._conn = connection
        self._lock = lock
        self._authority = primary_admin_authority

    def _require_primary(self, capability) -> str:
        try:
            return self._authority.require(capability)
        except PrimaryAdminAuthorizationError:
            raise PrimaryAdminRequired("primary MGBoost admin capability required")

    # --- prepare (idempotent insert) ------------------------------------------

    def prepare(
        self, *, capability, account_id: int, expected_old_telegram_id: int,
        new_telegram_id: int, mode: str, reason: str, idempotency_key: str,
        now: int | None = None,
    ) -> dict:
        actor = self._require_primary(capability)
        if mode not in ("ORDINARY", "COMPROMISE"):
            raise OwnershipRebindError("invalid rebind mode")
        reason = (reason or "").strip()
        if not 3 <= len(reason) <= 300:
            raise OwnershipRebindError("a bounded human-readable reason is required")
        for label, value in (
            ("expected_old_telegram_id", expected_old_telegram_id),
            ("new_telegram_id", new_telegram_id),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise OwnershipRebindError(f"{label} must be a positive integer")
        if expected_old_telegram_id == new_telegram_id:
            raise OwnershipRebindError("new Telegram ID must differ from the expected old one")
        if not isinstance(idempotency_key, str) or not 16 <= len(idempotency_key) <= 512:
            raise OwnershipRebindError("invalid idempotency key")
        timestamp = int(time.time()) if now is None else int(now)
        idem_hash = _sha("ownership-rebind-v1\0" + idempotency_key)
        payload = {
            "account_id": int(account_id), "expected_old_telegram_id": int(expected_old_telegram_id),
            "new_telegram_id": int(new_telegram_id), "mode": mode,
        }
        request_hash = _sha(_canonical(payload))
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                account = self._conn.execute(
                    "SELECT id FROM mgboost_accounts WHERE id=? AND status!='CLOSED'",
                    (int(account_id),),
                ).fetchone()
                if not account:
                    raise OwnershipRebindError("account not found or closed")
                prior = self._conn.execute(
                    "SELECT * FROM mgboost_ownership_rebind_operations WHERE idempotency_key_hash=?",
                    (idem_hash,),
                ).fetchone()
                if prior:
                    if prior["request_hash"] != request_hash:
                        raise OwnershipRebindConflict(
                            "idempotency key reused with a different rebind request"
                        )
                    self._conn.commit()
                    return dict(prior)
                operation_id = _derive_operation_id(idem_hash)
                cursor = self._conn.execute(
                    "INSERT INTO mgboost_ownership_rebind_operations "
                    "(operation_id,account_id,expected_old_telegram_id,new_telegram_id,mode,"
                    "reason,state,idempotency_key_hash,request_hash,actor_ref,next_attempt_at,"
                    "created_at,updated_at) VALUES (?,?,?,?,?,?,'PENDING',?,?,?,?,?,?)",
                    (operation_id, int(account_id), int(expected_old_telegram_id),
                     int(new_telegram_id), mode, reason, idem_hash, request_hash, actor,
                     timestamp, timestamp, timestamp),
                )
                row = self._conn.execute(
                    "SELECT * FROM mgboost_ownership_rebind_operations WHERE id=?",
                    (cursor.lastrowid,),
                ).fetchone()
                self._conn.commit()
                return dict(row)
            except Exception:
                self._conn.rollback()
                raise

    # --- claim/lease ------------------------------------------------------------

    def claim(self, operation_id: str, *, worker_id: str, now: int, lease_seconds: int = 30) -> dict | None:
        if not isinstance(worker_id, str) or not 3 <= len(worker_id) <= 128:
            raise OwnershipRebindError("invalid worker identity")
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                row = self._conn.execute(
                    "SELECT * FROM mgboost_ownership_rebind_operations WHERE operation_id=?",
                    (operation_id,),
                ).fetchone()
                if not row or row["state"] in {"APPLIED", "ERROR"}:
                    self._conn.rollback()
                    return None
                claimable = (
                    (row["state"] in {"PENDING", "RETRY"} and row["next_attempt_at"] <= now)
                    or (row["state"] == "IN_FLIGHT" and row["lease_expires_at"] <= now)
                )
                if not claimable:
                    self._conn.rollback()
                    return None
                attempt = row["attempts"] + 1
                self._conn.execute(
                    "UPDATE mgboost_ownership_rebind_operations SET state='IN_FLIGHT',attempts=?,"
                    "lease_owner=?,lease_expires_at=?,updated_at=?,row_version=row_version+1 "
                    "WHERE id=?",
                    (attempt, worker_id, now + max(5, int(lease_seconds)), now, row["id"]),
                )
                self._event(row["id"], row["account_id"], attempt, "STARTED", now=now)
                claimed = self._conn.execute(
                    "SELECT * FROM mgboost_ownership_rebind_operations WHERE id=?", (row["id"],)
                ).fetchone()
                self._conn.commit()
                return dict(claimed)
            except Exception:
                self._conn.rollback()
                raise

    def _event(self, op_id, account_id, attempt_no, event_type, *, safe_error_class=None, now):
        self._conn.execute(
            "INSERT INTO mgboost_ownership_rebind_events "
            "(rebind_operation_id,account_id,attempt_no,event_type,safe_error_class,created_at) "
            "VALUES (?,?,?,?,?,?)",
            (op_id, account_id, attempt_no, event_type, safe_error_class, now),
        )

    # --- step 1: atomic identity mutation ---------------------------------------

    def apply_identity_mutation(self, operation_id: str, *, worker_id: str, now: int) -> dict:
        """Fails closed (OwnershipRebindConflict) on: no current active owner,
        current active owner != expected_old_telegram_id (stale request), or
        new_telegram_id already an active identity anywhere (including on
        this same account). Never touches PH3-02/03/08 tables."""
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                row = self._conn.execute(
                    "SELECT * FROM mgboost_ownership_rebind_operations "
                    "WHERE operation_id=? AND state='IN_FLIGHT' AND lease_owner=?",
                    (operation_id, worker_id),
                ).fetchone()
                if not row:
                    raise OwnershipRebindConflict("rebind lease is not owned by worker")
                if row["new_identity_id"] is not None:
                    self._conn.commit()
                    return dict(row)  # already applied by a prior attempt -- idempotent no-op
                current_owner = self._conn.execute(
                    "SELECT * FROM mgboost_telegram_identities "
                    "WHERE account_id=? AND role='OWNER' AND revoked_at IS NULL",
                    (row["account_id"],),
                ).fetchone()
                if not current_owner or current_owner["telegram_id"] != row["expected_old_telegram_id"]:
                    raise OwnershipRebindConflict(
                        "current owner does not match the expected old Telegram ID (stale request)"
                    )
                conflicting = self._conn.execute(
                    "SELECT 1 FROM mgboost_telegram_identities WHERE telegram_id=? AND revoked_at IS NULL",
                    (row["new_telegram_id"],),
                ).fetchone()
                if conflicting:
                    raise OwnershipRebindConflict(
                        "new Telegram ID already has an active identity (dual ownership denied)"
                    )
                self._conn.execute(
                    "UPDATE mgboost_telegram_identities SET revoked_at=?,revoke_reason=?,"
                    "revoked_by_actor=? WHERE id=?",
                    (now, f"ownership_rebind:{row['mode'].lower()}", row["actor_ref"], current_owner["id"]),
                )
                new_identity_cursor = self._conn.execute(
                    "INSERT INTO mgboost_telegram_identities "
                    "(account_id,telegram_id,role,provenance,linked_at,linked_by_actor) "
                    "VALUES (?,?,'OWNER','ADMIN_REBIND',?,?)",
                    (row["account_id"], row["new_telegram_id"], now, row["actor_ref"]),
                )
                self._conn.execute(
                    "UPDATE mgboost_ownership_rebind_operations SET old_identity_id=?,new_identity_id=?,"
                    "updated_at=?,row_version=row_version+1 WHERE id=?",
                    (current_owner["id"], new_identity_cursor.lastrowid, now, row["id"]),
                )
                self._event(row["id"], row["account_id"], row["attempts"], "IDENTITY_REBOUND", now=now)
                result = self._conn.execute(
                    "SELECT * FROM mgboost_ownership_rebind_operations WHERE id=?", (row["id"],)
                ).fetchone()
                self._conn.commit()
                return dict(result)
            except Exception:
                self._conn.rollback()
                raise

    def record_credential_rotation(
        self, operation_id: str, *, worker_id: str, old_credential_id: int,
        new_credential_id: int, now: int,
    ) -> dict:
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                row = self._conn.execute(
                    "SELECT * FROM mgboost_ownership_rebind_operations "
                    "WHERE operation_id=? AND state='IN_FLIGHT' AND lease_owner=?",
                    (operation_id, worker_id),
                ).fetchone()
                if not row:
                    raise OwnershipRebindConflict("rebind lease is not owned by worker")
                if row["new_credential_id"] is None:
                    self._conn.execute(
                        "UPDATE mgboost_ownership_rebind_operations SET old_credential_id=?,"
                        "new_credential_id=?,updated_at=?,row_version=row_version+1 WHERE id=?",
                        (old_credential_id, new_credential_id, now, row["id"]),
                    )
                    self._event(row["id"], row["account_id"], row["attempts"], "CREDENTIAL_ROTATED", now=now)
                result = self._conn.execute(
                    "SELECT * FROM mgboost_ownership_rebind_operations WHERE id=?", (row["id"],)
                ).fetchone()
                self._conn.commit()
                return dict(result)
            except Exception:
                self._conn.rollback()
                raise

    def finish(self, operation_id: str, *, worker_id: str, now: int) -> dict:
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                row = self._conn.execute(
                    "SELECT * FROM mgboost_ownership_rebind_operations "
                    "WHERE operation_id=? AND state='IN_FLIGHT' AND lease_owner=?",
                    (operation_id, worker_id),
                ).fetchone()
                if not row:
                    raise OwnershipRebindConflict("rebind lease is not owned by worker")
                self._conn.execute(
                    "UPDATE mgboost_ownership_rebind_operations SET state='APPLIED',"
                    "lease_owner=NULL,lease_expires_at=NULL,last_error_class=NULL,updated_at=?,"
                    "row_version=row_version+1 WHERE id=?",
                    (now, row["id"]),
                )
                self._event(row["id"], row["account_id"], row["attempts"], "SUCCEEDED", now=now)
                result = self._conn.execute(
                    "SELECT * FROM mgboost_ownership_rebind_operations WHERE id=?", (row["id"],)
                ).fetchone()
                self._conn.commit()
                return dict(result)
            except Exception:
                self._conn.rollback()
                raise

    def record_error(self, operation_id: str, *, error_class: str, now: int) -> None:
        safe_error = (error_class or "").strip()
        if not safe_error or len(safe_error) > 128:
            raise OwnershipRebindError("safe error class is required")
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                row = self._conn.execute(
                    "SELECT * FROM mgboost_ownership_rebind_operations "
                    "WHERE operation_id=? AND state='IN_FLIGHT'",
                    (operation_id,),
                ).fetchone()
                if not row:
                    raise OwnershipRebindConflict("no in-flight rebind operation to fail")
                self._conn.execute(
                    "UPDATE mgboost_ownership_rebind_operations SET state='ERROR',"
                    "last_error_class=?,lease_owner=NULL,lease_expires_at=NULL,updated_at=?,"
                    "row_version=row_version+1 WHERE id=?",
                    (safe_error, now, row["id"]),
                )
                self._event(row["id"], row["account_id"], row["attempts"], "FAILED",
                            safe_error_class=safe_error, now=now)
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise


# --- orchestration -----------------------------------------------------------

def process_rebind(db, operation_id: str, *, worker_id: str, now: int) -> dict | None:
    """Claim -> (COMPROMISE only) PH2-01 credential rotation first -> atomic
    identity mutation -> finish.

    Ordering is deliberate and security-load-bearing, not incidental: for
    COMPROMISE, the old opaque credential is revoked *before* the new
    Telegram owner is ever activated. Each step is its own durable SQLite
    transaction, so a crash between them is possible -- but because
    credential rotation runs first, the only resting states a crash can
    leave are (a) old owner still active, old credential already revoked
    (an availability gap for the legitimate old owner, closed by the next
    retry) or (b) both steps done. It can never leave "new owner active
    while the compromised credential is still active" -- the one outcome
    this module must never produce, since that would mean whoever holds
    the compromised token keeps working after the account has already been
    handed to someone else. ORDINARY mode has no credential step, so
    ordering is moot for it; only the identity mutation runs.

    Every step is independently idempotent, so a crash/retry at any point
    converges safely without a second rotation or a reactivated old
    credential."""
    from .subscription_credentials import SubscriptionCredentialConflict

    claimed = db.ownership_rebind.claim(operation_id, worker_id=worker_id, now=now)
    if claimed is None:
        return None
    try:
        if claimed["mode"] == "COMPROMISE" and claimed["new_credential_id"] is None:
            account_id = claimed["account_id"]
            actor_ref = claimed["actor_ref"]
            prepare_key = f"ownership-rebind-credential-{operation_id}"
            try:
                prepared = db.subscription_credentials.prepare(
                    account_id=account_id, actor_ref=actor_ref,
                    reason=f"ownership rebind compromise: {operation_id}",
                    idempotency_key=prepare_key, now=now,
                )
            except SubscriptionCredentialConflict:
                # The raw token from a prior prepare() was lost before this
                # operation could record it (crash/lost response). Never try
                # to recover the old raw value -- abandon that pending
                # generation and issue a fresh one, per PH2-01's own
                # documented abandoned/reissue semantics.
                pending = db._conn.execute(
                    "SELECT id,generation FROM mgboost_subscription_credentials "
                    "WHERE account_id=? AND status='PENDING_DELIVERY' ORDER BY generation DESC LIMIT 1",
                    (account_id,),
                ).fetchone()
                if pending:
                    db.subscription_credentials.revoke(
                        credential_id=pending["id"], account_id=account_id,
                        reason_code="ABANDONED_PENDING", actor_ref=actor_ref,
                        idempotency_key=f"{prepare_key}-abandon", now=now,
                    )
                prepared = db.subscription_credentials.prepare(
                    account_id=account_id, actor_ref=actor_ref,
                    reason=f"ownership rebind compromise reissue: {operation_id}",
                    idempotency_key=f"{prepare_key}-retry", now=now,
                )
            old_active = db._conn.execute(
                "SELECT id FROM mgboost_subscription_credentials "
                "WHERE account_id=? AND status='ACTIVE'", (account_id,),
            ).fetchone()
            activated = db.subscription_credentials.activate(
                credential_id=prepared["id"], account_id=account_id,
                expected_generation=prepared["generation"], actor_ref=actor_ref,
                idempotency_key=f"ownership-rebind-activate-{operation_id}", now=now,
            )
            db.ownership_rebind.record_credential_rotation(
                operation_id, worker_id=worker_id,
                old_credential_id=old_active["id"] if old_active else None,
                new_credential_id=activated["id"], now=now,
            )
        db.ownership_rebind.apply_identity_mutation(operation_id, worker_id=worker_id, now=now)
        return db.ownership_rebind.finish(operation_id, worker_id=worker_id, now=now)
    except Exception as exc:
        db.ownership_rebind.record_error(operation_id, error_class=type(exc).__name__, now=now)
        raise
