"""Typed, explicit PH3-09 provenance writer; not connected to legacy flows."""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import time


PAYMENT_CHANNELS = {
    "TELEGRAM_STARS", "EXTERNAL_PAYMENT", "ADMIN_GRANT", "UNKNOWN_LEGACY",
}
MUTATION_SOURCES = {
    "DIRECT_PURCHASE", "MANUAL_PAYMENT", "ADMIN", "MIGRATION", "INTERNAL",
    "UNKNOWN_LEGACY",
}


class ProvenanceError(RuntimeError):
    pass


class ProvenanceConflict(ProvenanceError):
    pass


def _canonical(value: dict | None) -> str:
    return json.dumps(value or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _key_hash(scope: str, key: str) -> str:
    if not isinstance(key, str) or not 16 <= len(key) <= 512:
        raise ProvenanceError("idempotency key length is invalid")
    return hashlib.sha256((scope + "\0" + key).encode()).hexdigest()


def _request_hash(payload: dict) -> str:
    return hashlib.sha256(_canonical(payload).encode()).hexdigest()


class ProvenanceStore:
    def __init__(self, connection: sqlite3.Connection, lock):
        self._conn = connection
        self._lock = lock

    def _require_account(self, account_id: int) -> None:
        if not self._conn.execute(
            "SELECT 1 FROM mgboost_accounts WHERE id=? AND status!='CLOSED'",
            (int(account_id),),
        ).fetchone():
            raise ProvenanceError("account not found or closed")

    def record_payment(
        self,
        account_id: int,
        *,
        payment_channel: str,
        record_status: str,
        amount_minor: int | None,
        currency: str | None,
        payment_method: str | None,
        external_reference: str | None,
        actor_type: str,
        actor_ref: str | None,
        evidence: dict | None,
        idempotency_key: str,
        now: int | None = None,
    ) -> dict:
        if payment_channel not in PAYMENT_CHANNELS:
            raise ProvenanceError("payment channel must be explicit")
        if not isinstance(actor_type, str) or not 1 <= len(actor_type.strip()) <= 64:
            raise ProvenanceError("actor type is required")
        if actor_ref is not None and len(actor_ref) > 256:
            raise ProvenanceError("actor reference is too long")
        if external_reference is not None and not 1 <= len(external_reference) <= 512:
            raise ProvenanceError("external reference length is invalid")
        if payment_method is not None and len(payment_method) > 64:
            raise ProvenanceError("payment method is too long")
        allowed_status = {
            "TELEGRAM_STARS": {"CONFIRMED"},
            "EXTERNAL_PAYMENT": {"CONFIRMED"},
            "ADMIN_GRANT": {"ADMIN_GRANTED"},
            "UNKNOWN_LEGACY": {"UNKNOWN_LEGACY"},
        }
        if record_status not in allowed_status[payment_channel]:
            raise ProvenanceError("payment status/channel combination is invalid")
        if amount_minor is not None and (
            isinstance(amount_minor, bool) or not isinstance(amount_minor, int) or amount_minor < 0
        ):
            raise ProvenanceError("amount_minor must be nonnegative")
        if (amount_minor is None) != (currency is None):
            raise ProvenanceError("amount and currency must be supplied together")
        if payment_channel != "UNKNOWN_LEGACY" and not external_reference:
            raise ProvenanceError("confirmed payment/grant requires external reference")
        payload = {
            "account_id": int(account_id), "payment_channel": payment_channel,
            "record_status": record_status, "amount_minor": amount_minor,
            "currency": currency, "payment_method": payment_method,
            "external_reference": external_reference, "actor_type": actor_type,
            "actor_ref": actor_ref, "evidence": evidence or {},
        }
        key_hash = _key_hash("payment-v1", idempotency_key)
        request_hash = _request_hash(payload)
        timestamp = int(time.time()) if now is None else int(now)
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                self._require_account(int(account_id))
                prior = self._conn.execute(
                    "SELECT * FROM mgboost_payment_records WHERE idempotency_key_hash=?",
                    (key_hash,),
                ).fetchone()
                if prior:
                    if prior["account_id"] != int(account_id) or prior["request_hash"] != request_hash:
                        raise ProvenanceConflict("idempotency key reused for another payment")
                    self._conn.commit()
                    return dict(prior)
                cursor = self._conn.execute(
                    "INSERT INTO mgboost_payment_records "
                    "(public_id,account_id,payment_channel,record_status,amount_minor,currency,"
                    "payment_method,external_reference,actor_type,actor_ref,evidence_json,"
                    "idempotency_key_hash,request_hash,created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    ("pay_" + secrets.token_urlsafe(18), int(account_id), payment_channel,
                     record_status, amount_minor, currency, payment_method,
                     external_reference, actor_type, actor_ref, _canonical(evidence),
                     key_hash, request_hash, timestamp),
                )
                row = self._conn.execute(
                    "SELECT * FROM mgboost_payment_records WHERE id=?", (cursor.lastrowid,)
                ).fetchone()
                self._conn.commit()
                return dict(row)
            except sqlite3.IntegrityError as exc:
                self._conn.rollback()
                raise ProvenanceConflict("duplicate immutable payment reference") from exc
            except Exception:
                self._conn.rollback()
                raise

    def record_mutation(
        self,
        account_id: int,
        *,
        subscription_id: int | None,
        operation: str,
        payment_channel: str,
        mutation_source: str,
        actor_type: str,
        actor_ref: str | None,
        reason: str | None,
        external_reference: str | None,
        before: dict | None,
        after: dict | None,
        idempotency_key: str,
        payment_id: int | None = None,
        now: int | None = None,
    ) -> dict:
        if payment_channel not in PAYMENT_CHANNELS | {"NOT_APPLICABLE"}:
            raise ProvenanceError("payment channel must be explicit")
        if mutation_source not in MUTATION_SOURCES:
            raise ProvenanceError("mutation source must be explicit")
        if not isinstance(operation, str) or not 1 <= len(operation.strip()) <= 128:
            raise ProvenanceError("mutation operation is required")
        if not isinstance(actor_type, str) or not 1 <= len(actor_type.strip()) <= 64:
            raise ProvenanceError("actor type is required")
        if actor_ref is not None and len(actor_ref) > 256:
            raise ProvenanceError("actor reference is too long")
        if external_reference is not None and not 1 <= len(external_reference) <= 512:
            raise ProvenanceError("external reference length is invalid")
        combinations = {
            "DIRECT_PURCHASE": {"TELEGRAM_STARS"},
            "MANUAL_PAYMENT": {"EXTERNAL_PAYMENT"},
            "ADMIN": {"ADMIN_GRANT", "NOT_APPLICABLE"},
            "MIGRATION": {"UNKNOWN_LEGACY", "NOT_APPLICABLE"},
            "INTERNAL": {"ADMIN_GRANT", "NOT_APPLICABLE"},
            "UNKNOWN_LEGACY": {"UNKNOWN_LEGACY"},
        }
        if payment_channel not in combinations[mutation_source]:
            raise ProvenanceError("payment channel/mutation source mismatch")
        payload = {
            "account_id": int(account_id), "subscription_id": subscription_id,
            "operation": operation, "payment_channel": payment_channel,
            "mutation_source": mutation_source, "actor_type": actor_type,
            "actor_ref": actor_ref, "reason": reason,
            "external_reference": external_reference, "before": before or {},
            "after": after or {}, "payment_id": payment_id,
        }
        key_hash = _key_hash("mutation-v1", idempotency_key)
        canonical_payload = _canonical(payload)
        timestamp = int(time.time()) if now is None else int(now)
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                self._require_account(int(account_id))
                if subscription_id is not None and not self._conn.execute(
                    "SELECT 1 FROM mgboost_subscriptions WHERE id=? AND account_id=?",
                    (int(subscription_id), int(account_id)),
                ).fetchone():
                    raise ProvenanceError("subscription does not belong to account")
                prior = self._conn.execute(
                    "SELECT * FROM mgboost_entitlement_mutations WHERE idempotency_key_hash=?",
                    (key_hash,),
                ).fetchone()
                if prior:
                    if prior["account_id"] != int(account_id) or prior["after_json"] != canonical_payload:
                        raise ProvenanceConflict("idempotency key reused for another mutation")
                    self._conn.commit()
                    return dict(prior)
                if payment_id is not None:
                    payment = self._conn.execute(
                        "SELECT * FROM mgboost_payment_records WHERE id=? AND account_id=?",
                        (int(payment_id), int(account_id)),
                    ).fetchone()
                    if not payment or payment["payment_channel"] != payment_channel:
                        raise ProvenanceError("payment does not belong to account/channel")
                mutation_id = self._conn.execute(
                    "INSERT INTO mgboost_entitlement_mutations "
                    "(account_id,subscription_id,operation,payment_channel,mutation_source,"
                    "actor_type,actor_ref,reason,external_reference,idempotency_key_hash,"
                    "before_json,after_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (int(account_id), subscription_id, operation, payment_channel,
                     mutation_source, actor_type, actor_ref, reason, external_reference,
                     key_hash, _canonical(before), canonical_payload, timestamp),
                ).lastrowid
                if payment_id is not None:
                    self._conn.execute(
                        "INSERT INTO mgboost_mutation_payment_links "
                        "(mutation_id,payment_id,account_id,created_at) VALUES (?,?,?,?)",
                        (mutation_id, int(payment_id), int(account_id), timestamp),
                    )
                row = self._conn.execute(
                    "SELECT * FROM mgboost_entitlement_mutations WHERE id=?", (mutation_id,)
                ).fetchone()
                self._conn.commit()
                return dict(row)
            except sqlite3.IntegrityError as exc:
                self._conn.rollback()
                raise ProvenanceConflict("duplicate immutable mutation reference") from exc
            except Exception:
                self._conn.rollback()
                raise
