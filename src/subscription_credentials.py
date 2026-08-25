"""Durable PH2-01 opaque subscription credential repository.

Dormant: no route imports this module for issuance/rotation/revoke; the
public resolver (`src/opaque_resolver.py`) only ever calls the read-only
`resolve()` method.

Design deviation from `docs/PHASE2_OPAQUE_TOKEN_DESIGN.md`'s "recommended"
AEAD delivery envelope: this store never persists the raw token anywhere,
not even encrypted. `prepare()` generates it, hands it back once in the
synchronous return value of the call itself (delivered to the caller inside
a single authenticated HTTP response, over TLS, matching the design's own
"returns/delivers the raw token" API wording), and never stores or logs it.
This satisfies the identical hard requirement ("raw tokens are absent from
the credential table, audit log, backups, ... exception text") without
inventing new symmetric-encryption infrastructure this project does not
otherwise have a dependency on. The cost is explicit and documented: a
`prepare()` call whose response never reaches the caller (crash/timeout
before receipt) cannot be recovered -- the design doc itself allows this
("If delivery ultimately fails, admin issues another generation"); the
abandoned `PENDING_DELIVERY` row must be explicitly revoked
(`revoke_reason='ABANDONED_PENDING'`) before a fresh `prepare()` can issue a
new generation for the same account.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import sqlite3
import time


class SubscriptionCredentialError(RuntimeError):
    pass


class SubscriptionCredentialConflict(SubscriptionCredentialError):
    pass


_REVOKE_REASONS = frozenset({
    "ROTATED", "COMPROMISE_SUSPECTED", "ADMIN_MANUAL", "ABANDONED_PENDING",
})


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _idem_hash(scope: str, raw_key: str) -> str:
    if not isinstance(raw_key, str) or not 16 <= len(raw_key) <= 512:
        raise SubscriptionCredentialError("invalid idempotency key")
    return _sha(f"subscription-credential-{scope}-v1\0{raw_key}")


def generate_opaque_token() -> str:
    """Exactly 32 CSPRNG bytes, base64url without padding -- 43 chars."""
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii").rstrip("=")


def token_verifier(raw_token: str) -> str:
    return _sha(raw_token)


class SubscriptionCredentialStore:
    def __init__(self, connection: sqlite3.Connection, lock):
        self._conn = connection
        self._lock = lock

    # --- issuance ------------------------------------------------------------

    def prepare(
        self, *, account_id: int, actor_ref: str, reason: str, idempotency_key: str,
        now: int | None = None,
    ) -> dict:
        """Generate a new PENDING_DELIVERY credential and return it together
        with the raw token -- the ONLY place the raw value ever exists.
        Never call this twice with the same idempotency_key: a pending
        generation cannot be re-delivered (see module docstring)."""
        timestamp = int(time.time()) if now is None else int(now)
        actor_ref = (actor_ref or "").strip()
        reason = (reason or "").strip()
        if not actor_ref or len(actor_ref) > 200:
            raise SubscriptionCredentialError("invalid actor reference")
        if not 3 <= len(reason) <= 300:
            raise SubscriptionCredentialError("a bounded human-readable reason is required")
        idem_hash = _idem_hash("prepare", idempotency_key)
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                account = self._conn.execute(
                    "SELECT id FROM mgboost_accounts WHERE id=?", (int(account_id),),
                ).fetchone()
                if not account:
                    raise SubscriptionCredentialError("account does not exist")
                prior = self._conn.execute(
                    "SELECT id FROM mgboost_subscription_credentials WHERE idempotency_key_hash=?",
                    (idem_hash,),
                ).fetchone()
                if prior:
                    raise SubscriptionCredentialConflict(
                        "idempotency key already used for a prepare call -- a pending "
                        "generation cannot be re-delivered; revoke it and use a fresh key"
                    )
                last_generation = self._conn.execute(
                    "SELECT COALESCE(MAX(generation), 0) FROM mgboost_subscription_credentials "
                    "WHERE account_id=?",
                    (int(account_id),),
                ).fetchone()[0]
                generation = int(last_generation) + 1
                raw_token = generate_opaque_token()
                token_hash = token_verifier(raw_token)
                cursor = self._conn.execute(
                    "INSERT INTO mgboost_subscription_credentials "
                    "(account_id,token_hash,generation,status,idempotency_key_hash,created_at) "
                    "VALUES (?,?,?,'PENDING_DELIVERY',?,?)",
                    (int(account_id), token_hash, generation, idem_hash, timestamp),
                )
                credential_id = cursor.lastrowid
                self._event(
                    credential_id, int(account_id), "PREPARED", actor_ref=actor_ref,
                    reason=reason, idempotency_key_hash=idem_hash, now=timestamp,
                )
                row = self._conn.execute(
                    "SELECT * FROM mgboost_subscription_credentials WHERE id=?", (credential_id,),
                ).fetchone()
                self._conn.commit()
                result = dict(row)
                result["raw_token"] = raw_token
                return result
            except Exception:
                self._conn.rollback()
                raise

    def activate(
        self, *, credential_id: int, account_id: int, expected_generation: int,
        actor_ref: str, idempotency_key: str, now: int | None = None,
    ) -> dict:
        """CAS-activate a PENDING_DELIVERY credential and atomically revoke
        the previously ACTIVE one (if any) in the same transaction."""
        timestamp = int(time.time()) if now is None else int(now)
        actor_ref = (actor_ref or "").strip()
        if not actor_ref or len(actor_ref) > 200:
            raise SubscriptionCredentialError("invalid actor reference")
        idem_hash = _idem_hash("activate", idempotency_key)
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                prior_event = self._conn.execute(
                    "SELECT credential_id FROM mgboost_subscription_credential_events "
                    "WHERE idempotency_key_hash=? AND event_type='ACTIVATED'",
                    (idem_hash,),
                ).fetchone()
                if prior_event:
                    if prior_event["credential_id"] != int(credential_id):
                        raise SubscriptionCredentialConflict(
                            "idempotency key reused for a different credential"
                        )
                    row = self._conn.execute(
                        "SELECT * FROM mgboost_subscription_credentials WHERE id=?",
                        (prior_event["credential_id"],),
                    ).fetchone()
                    self._conn.commit()
                    return dict(row)
                row = self._conn.execute(
                    "SELECT * FROM mgboost_subscription_credentials "
                    "WHERE id=? AND account_id=?", (int(credential_id), int(account_id)),
                ).fetchone()
                if not row:
                    raise SubscriptionCredentialError("credential does not belong to this account")
                if row["status"] != "PENDING_DELIVERY":
                    raise SubscriptionCredentialConflict("credential is not pending delivery")
                if row["generation"] != int(expected_generation):
                    raise SubscriptionCredentialConflict("credential generation mismatch (stale CAS)")
                previous_active = self._conn.execute(
                    "SELECT id FROM mgboost_subscription_credentials "
                    "WHERE account_id=? AND status='ACTIVE'", (int(account_id),),
                ).fetchone()
                if previous_active:
                    self._conn.execute(
                        "UPDATE mgboost_subscription_credentials SET status='REVOKED',"
                        "revoked_at=?,revoke_reason='ROTATED',row_version=row_version+1 WHERE id=?",
                        (timestamp, previous_active["id"]),
                    )
                    self._event(
                        previous_active["id"], int(account_id), "REVOKED", actor_ref=actor_ref,
                        reason="superseded by rotation", idempotency_key_hash=idem_hash, now=timestamp,
                    )
                self._conn.execute(
                    "UPDATE mgboost_subscription_credentials SET status='ACTIVE',"
                    "activated_at=?,row_version=row_version+1 WHERE id=?",
                    (timestamp, row["id"]),
                )
                self._event(
                    row["id"], int(account_id), "ACTIVATED", actor_ref=actor_ref,
                    reason="activated", idempotency_key_hash=idem_hash, now=timestamp,
                )
                result = self._conn.execute(
                    "SELECT * FROM mgboost_subscription_credentials WHERE id=?", (row["id"],),
                ).fetchone()
                self._conn.commit()
                return dict(result)
            except Exception:
                self._conn.rollback()
                raise

    def revoke(
        self, *, credential_id: int, account_id: int, reason_code: str, actor_ref: str,
        idempotency_key: str, now: int | None = None,
    ) -> dict:
        if reason_code not in _REVOKE_REASONS:
            raise SubscriptionCredentialError("invalid revoke reason code")
        timestamp = int(time.time()) if now is None else int(now)
        actor_ref = (actor_ref or "").strip()
        if not actor_ref or len(actor_ref) > 200:
            raise SubscriptionCredentialError("invalid actor reference")
        idem_hash = _idem_hash("revoke", idempotency_key)
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                prior_event = self._conn.execute(
                    "SELECT credential_id FROM mgboost_subscription_credential_events "
                    "WHERE idempotency_key_hash=? AND event_type='REVOKED'",
                    (idem_hash,),
                ).fetchone()
                if prior_event:
                    if prior_event["credential_id"] != int(credential_id):
                        raise SubscriptionCredentialConflict(
                            "idempotency key reused for a different credential"
                        )
                    row = self._conn.execute(
                        "SELECT * FROM mgboost_subscription_credentials WHERE id=?",
                        (prior_event["credential_id"],),
                    ).fetchone()
                    self._conn.commit()
                    return dict(row)
                row = self._conn.execute(
                    "SELECT * FROM mgboost_subscription_credentials "
                    "WHERE id=? AND account_id=?", (int(credential_id), int(account_id)),
                ).fetchone()
                if not row:
                    raise SubscriptionCredentialError("credential does not belong to this account")
                if row["status"] not in ("PENDING_DELIVERY", "ACTIVE"):
                    raise SubscriptionCredentialConflict(
                        "a terminal credential can never be revoked again"
                    )
                self._conn.execute(
                    "UPDATE mgboost_subscription_credentials SET status='REVOKED',"
                    "revoked_at=?,revoke_reason=?,row_version=row_version+1 WHERE id=?",
                    (timestamp, reason_code, row["id"]),
                )
                self._event(
                    row["id"], int(account_id), "REVOKED", actor_ref=actor_ref,
                    reason=reason_code, idempotency_key_hash=idem_hash, now=timestamp,
                )
                result = self._conn.execute(
                    "SELECT * FROM mgboost_subscription_credentials WHERE id=?", (row["id"],),
                ).fetchone()
                self._conn.commit()
                return dict(result)
            except Exception:
                self._conn.rollback()
                raise

    def _event(self, credential_id, account_id, event_type, *, actor_ref, reason,
               idempotency_key_hash, now):
        self._conn.execute(
            "INSERT INTO mgboost_subscription_credential_events "
            "(credential_id,account_id,event_type,actor_ref,reason,idempotency_key_hash,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (credential_id, account_id, event_type, actor_ref, reason, idempotency_key_hash, now),
        )

    # --- read-only resolution (the only method the public resolver calls) ----

    def abandon_pending(
        self, *, account_id: int, actor_ref: str, idempotency_key: str, now: int | None = None,
    ) -> dict | None:
        """PH4-04: explicitly abandon a `PENDING_DELIVERY` credential whose
        raw token is definitively unrecoverable (delivery failed, or a prior
        process crashed between `prepare()` and confirmed delivery). Returns
        None if there was nothing pending to abandon (idempotent no-op --
        never an error). Never reactivates or guesses; the old `ACTIVE`
        credential, if any, is completely untouched."""
        with self._lock:
            row = self._conn.execute(
                "SELECT id FROM mgboost_subscription_credentials "
                "WHERE account_id=? AND status='PENDING_DELIVERY'", (int(account_id),),
            ).fetchone()
        if row is None:
            return None
        return self.revoke(
            credential_id=row["id"], account_id=account_id, reason_code="ABANDONED_PENDING",
            actor_ref=actor_ref, idempotency_key=idempotency_key, now=now,
        )

    def resolve(self, raw_token: str, *, now: int | None = None) -> dict | None:
        """Verifier lookup only. Returns None for anything that is not an
        exactly-matching, currently ACTIVE credential -- unknown, malformed,
        pending, revoked and expired are all indistinguishable to the caller
        (uniform failure at the HTTP layer)."""
        if not isinstance(raw_token, str) or not raw_token:
            return None
        token_hash = token_verifier(raw_token)
        timestamp = int(time.time()) if now is None else int(now)
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM mgboost_subscription_credentials "
                "WHERE token_hash=? AND status='ACTIVE'", (token_hash,),
            ).fetchone()
            if not row:
                return None
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                self._conn.execute(
                    "UPDATE mgboost_subscription_credentials SET last_used_at=? WHERE id=?",
                    (timestamp, row["id"]),
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
            return {
                "credential_id": row["id"], "account_id": row["account_id"],
                "generation": row["generation"],
            }
