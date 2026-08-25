"""PH4-03 reviewed DIRECT account enrollment foundation.

This is the DIRECT-cohort counterpart to PH3-06's `InternalEntitlementStore`.
It never touches `mgboost_internal_account_reviews` (INTERNAL-only, enforced
by its own DB trigger) -- reviewed DIRECT ownership lives in the sibling
`mgboost_direct_account_reviews` table instead. It reuses the already-generic
PH3-03 `mgboost_legacy_alias_groups`/`mgboost_legacy_account_aliases` tables
unchanged.

Crash-safety model: `enroll_direct_account()` is split into three durable,
independently-idempotent phases (mirrors PH4-02's
prepare/record-once/transition style):

  1. claim a `mgboost_direct_enrollment_intents` row keyed by the caller's
     idempotency key -- BEFORE any account is created. A cross-account
     conflict on `legacy_username` is rejected here, before anything else
     exists.
  2. `AccountStore.create_account('DIRECT')` is called at most once per
     intent -- the intent's `account_id` is filled exactly once (a DB
     trigger enforces fill-once) so a retry after a crash right after step 2
     reuses the same account instead of allocating an orphan second one.
  3. write the alias + review audit row (idempotent: skipped if already
     present), then link the Telegram owner if ownership is PROVEN
     (`AccountStore.link_telegram_owner` is itself idempotent).

Retrying the whole call with the same idempotency key at any point converges
to exactly one account, one reviewed alias, and one Telegram link -- never a
duplicate.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time

from .admin_authority import PrimaryAdminAuthorizationError


PAYABLE_STARS_STATUSES = {"paid", "plan_committed", "applied"}
LEGACY_STATUSES = {"ACTIVE", "DISABLED", "EXPIRED", "UNLIMITED"}
ALIAS_PROVENANCE = {"OWNER_APPROVED", "EVIDENCE_PROVEN"}


class DirectEnrollmentError(RuntimeError):
    pass


class PrimaryAdminRequired(DirectEnrollmentError):
    pass


class AmbiguousOwnershipRejected(DirectEnrollmentError):
    pass


class AliasConflict(DirectEnrollmentError):
    pass


class IdempotencyConflict(DirectEnrollmentError):
    pass


class InvoiceNotPayable(DirectEnrollmentError):
    pass


class PayerMismatch(DirectEnrollmentError):
    pass


class OwnerAttestationConflict(DirectEnrollmentError):
    pass


class TelegramMappingConflict(DirectEnrollmentError):
    pass


def _canonical(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _idempotency_hash(scope: str, raw_key: str) -> str:
    if not isinstance(raw_key, str) or not 16 <= len(raw_key) <= 512:
        raise DirectEnrollmentError("idempotency key length is invalid")
    return hashlib.sha256((scope + "\0" + raw_key).encode("utf-8")).hexdigest()


class DirectEnrollmentStore:
    def __init__(self, connection: sqlite3.Connection, lock, accounts, primary_admin_authority):
        self._conn = connection
        self._lock = lock
        self._accounts = accounts
        self._authority = primary_admin_authority

    def _require_primary(self, capability) -> str:
        try:
            return self._authority.require(capability)
        except PrimaryAdminAuthorizationError:
            raise PrimaryAdminRequired("primary MGBoost admin capability required")

    # --- phase 1: durable, pre-account-creation idempotent claim -----------

    def _claim_intent(self, *, username: str, request_hash: str, idem_hash: str, now: int) -> dict:
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                prior = self._conn.execute(
                    "SELECT * FROM mgboost_direct_enrollment_intents WHERE idempotency_key_hash=?",
                    (idem_hash,),
                ).fetchone()
                if prior:
                    if prior["request_hash"] != request_hash:
                        raise IdempotencyConflict("idempotency key reused with a different enrollment request")
                    self._conn.commit()
                    return dict(prior)
                conflicting = self._conn.execute(
                    "SELECT account_id FROM mgboost_legacy_account_aliases WHERE legacy_username=?",
                    (username,),
                ).fetchone()
                if conflicting is not None:
                    raise AliasConflict(
                        "legacy username is already bound to another account -- "
                        "one legacy username can never be linked to two accounts"
                    )
                cursor = self._conn.execute(
                    "INSERT INTO mgboost_direct_enrollment_intents "
                    "(idempotency_key_hash,legacy_username,request_hash,account_id,created_at,updated_at) "
                    "VALUES (?,?,?,NULL,?,?)",
                    (idem_hash, username, request_hash, now, now),
                )
                row = self._conn.execute(
                    "SELECT * FROM mgboost_direct_enrollment_intents WHERE id=?", (cursor.lastrowid,),
                ).fetchone()
                self._conn.commit()
                return dict(row)
            except sqlite3.IntegrityError as exc:
                self._conn.rollback()
                raise AliasConflict(
                    "legacy username is already bound to another account"
                ) from exc
            except Exception:
                self._conn.rollback()
                raise

    def _fill_intent_account(self, intent_id: int, account_id: int, *, now: int) -> dict:
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                self._conn.execute(
                    "UPDATE mgboost_direct_enrollment_intents SET account_id=?,updated_at=? "
                    "WHERE id=? AND account_id IS NULL",
                    (account_id, now, intent_id),
                )
                row = self._conn.execute(
                    "SELECT * FROM mgboost_direct_enrollment_intents WHERE id=?", (intent_id,),
                ).fetchone()
                self._conn.commit()
                return dict(row)
            except Exception:
                self._conn.rollback()
                raise

    # --- top-level reviewed DIRECT enrollment -------------------------------

    def enroll_direct_account(
        self,
        *,
        capability,
        legacy_username: str,
        decision_ref: str,
        ownership_evidence: str,
        telegram_id: int | None,
        alias_provenance: str,
        legacy_status: str,
        legacy_expiry: int | None,
        observed_device_count: int,
        observed_hwid_count: int,
        evidence: dict,
        idempotency_key: str,
        now: int | None = None,
    ) -> dict:
        actor = self._require_primary(capability)

        # Ambiguous ownership fails closed -- nothing is written for anything
        # other than an explicit, already-classified PROVEN/ABSENT result.
        if ownership_evidence not in {"PROVEN", "ABSENT"}:
            raise AmbiguousOwnershipRejected(
                "ambiguous ownership cannot be enrolled -- classify as PROVEN or "
                "ABSENT first, or do not enroll"
            )
        if ownership_evidence == "PROVEN":
            if isinstance(telegram_id, bool) or not isinstance(telegram_id, int) or telegram_id <= 0:
                raise AmbiguousOwnershipRejected("proven ownership requires a Telegram ID")
        elif telegram_id is not None:
            raise AmbiguousOwnershipRejected("Telegram binding requires proven ownership")
        if alias_provenance not in ALIAS_PROVENANCE:
            raise DirectEnrollmentError("alias provenance is invalid")
        if legacy_status not in LEGACY_STATUSES:
            raise DirectEnrollmentError("legacy status is invalid")
        if legacy_expiry is not None and (
            isinstance(legacy_expiry, bool) or not isinstance(legacy_expiry, int) or legacy_expiry < 0
        ):
            raise DirectEnrollmentError("legacy expiry is invalid")
        if any(
            isinstance(v, bool) or not isinstance(v, int) or v < 0
            for v in (observed_device_count, observed_hwid_count)
        ):
            raise DirectEnrollmentError("evidence counts are invalid")
        if not isinstance(evidence, dict):
            raise DirectEnrollmentError("evidence must be an object")
        username = (legacy_username or "").strip()
        decision_ref = (decision_ref or "").strip()
        if not username or len(username) > 128:
            raise DirectEnrollmentError("legacy username is invalid")
        if not 3 <= len(decision_ref) <= 128:
            raise DirectEnrollmentError("a bounded decision reference is required")

        # Reuse the existing bot Telegram-linkage table (`tg_users`, populated
        # by the existing subscription-link bot flow in `bot_support.py`) as a
        # cross-check, never a second linking mechanism. A username the bot
        # has bound to more than one distinct Telegram ID is inherently
        # ambiguous; a caller asserting a Telegram ID that contradicts the
        # single bot-recorded one is a conflicting mapping. Both fail closed.
        if ownership_evidence == "PROVEN":
            bot_rows = self._conn.execute(
                "SELECT DISTINCT telegram_id FROM tg_users WHERE marzban_username=?",
                (username,),
            ).fetchall()
            bot_telegram_ids = {int(row["telegram_id"]) for row in bot_rows}
            if len(bot_telegram_ids) > 1:
                raise AmbiguousOwnershipRejected(
                    "existing bot Telegram linkage for this legacy username is "
                    "ambiguous (multiple distinct Telegram IDs) -- cannot enroll as PROVEN"
                )
            if bot_telegram_ids and telegram_id not in bot_telegram_ids:
                raise TelegramMappingConflict(
                    "asserted Telegram ID does not match the existing bot-linked "
                    "mapping for this legacy username"
                )

        timestamp = int(time.time()) if now is None else int(now)
        payload = {
            "legacy_username": username, "decision_ref": decision_ref,
            "ownership_evidence": ownership_evidence, "telegram_id": telegram_id,
            "alias_provenance": alias_provenance, "legacy_status": legacy_status,
            "legacy_expiry": legacy_expiry, "observed_device_count": observed_device_count,
            "observed_hwid_count": observed_hwid_count, "evidence": evidence,
        }
        request_hash = hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()
        idem_hash = _idempotency_hash("direct-enrollment-v1", idempotency_key)

        intent = self._claim_intent(
            username=username, request_hash=request_hash, idem_hash=idem_hash, now=timestamp,
        )

        account_id = intent["account_id"]
        if account_id is None:
            account = self._accounts.create_account("DIRECT", now=timestamp)
            intent = self._fill_intent_account(intent["id"], account["id"], now=timestamp)
            account_id = intent["account_id"]

        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                review_row = self._conn.execute(
                    "SELECT id FROM mgboost_direct_account_reviews WHERE account_id=?",
                    (account_id,),
                ).fetchone()
                if review_row is None:
                    self._conn.execute(
                        "INSERT INTO mgboost_legacy_alias_groups "
                        "(account_id,mapping_key,decision_ref,created_by_actor,created_at) "
                        "VALUES (?,?,?,?,?)",
                        (account_id, f"direct-enroll-v1:{username}", decision_ref, actor, timestamp),
                    )
                    self._conn.execute(
                        "INSERT INTO mgboost_legacy_account_aliases "
                        "(account_id,legacy_username,alias_role,ownership_provenance,legacy_status,"
                        "legacy_expiry,observed_device_count,observed_hwid_count,evidence_json,created_at) "
                        "VALUES (?,?,'PRIMARY',?,?,?,?,?,?,?)",
                        (account_id, username, alias_provenance, legacy_status, legacy_expiry,
                         observed_device_count, observed_hwid_count, _canonical(evidence), timestamp),
                    )
                    self._conn.execute(
                        "INSERT INTO mgboost_direct_account_reviews "
                        "(account_id,legacy_username,ownership_evidence,decision_ref,reviewed_by_actor,"
                        "evidence_json,created_at) VALUES (?,?,?,?,?,?,?)",
                        (account_id, username, ownership_evidence, decision_ref, actor,
                         _canonical(evidence), timestamp),
                    )
                self._conn.commit()
            except sqlite3.IntegrityError as exc:
                self._conn.rollback()
                raise AliasConflict("legacy username is already bound to another account") from exc
            except Exception:
                self._conn.rollback()
                raise

        if ownership_evidence == "PROVEN":
            self._accounts.link_telegram_owner(
                account_id, telegram_id, provenance="DIRECT_BIND", actor=actor, now=timestamp,
            )

        return self._result(account_id)

    def _result(self, account_id: int) -> dict:
        row = self._conn.execute(
            "SELECT a.id AS account_id, a.public_id, a.account_source, "
            "r.legacy_username, r.ownership_evidence, r.decision_ref, "
            "r.reviewed_by_actor, r.created_at AS reviewed_at "
            "FROM mgboost_accounts a "
            "JOIN mgboost_direct_account_reviews r ON r.account_id=a.id "
            "WHERE a.id=?", (int(account_id),),
        ).fetchone()
        return dict(row)

    def _reviewed_direct_account(self, account_id: int) -> dict:
        row = self._conn.execute(
            "SELECT a.id AS account_id, a.account_source, r.legacy_username "
            "FROM mgboost_accounts a "
            "JOIN mgboost_direct_account_reviews r ON r.account_id=a.id "
            "WHERE a.id=?", (int(account_id),),
        ).fetchone()
        if row is None or row["account_source"] != "DIRECT":
            raise DirectEnrollmentError("account is not a reviewed DIRECT enrollment")
        return dict(row)

    def _owner_telegram_id(self, account_id: int) -> int | None:
        row = self._conn.execute(
            "SELECT telegram_id FROM mgboost_telegram_identities "
            "WHERE account_id=? AND role='OWNER' AND revoked_at IS NULL",
            (int(account_id),),
        ).fetchone()
        return int(row["telegram_id"]) if row else None

    # --- TELEGRAM_STARS: real paid invoice -> canonical payment record -----

    def record_stars_payment(
        self, db, *, invoice: dict, account_id: int, actor_ref: str, now: int | None = None,
    ) -> dict:
        timestamp = int(time.time()) if now is None else int(now)
        review = self._reviewed_direct_account(account_id)
        if invoice.get("status") not in PAYABLE_STARS_STATUSES:
            raise InvoiceNotPayable(
                f"invoice status {invoice.get('status')!r} is not a confirmed payment "
                "-- refused/refunded/pending invoices are never treated as payment"
            )
        charge_id = invoice.get("telegram_payment_charge_id")
        if not charge_id:
            raise InvoiceNotPayable("a paid invoice must carry a Telegram payment charge id")
        if invoice.get("marzban_username") != review["legacy_username"]:
            raise DirectEnrollmentError("invoice legacy username does not match the reviewed account")
        owner_id = self._owner_telegram_id(account_id)
        payer_id = invoice.get("payer_telegram_id")
        if owner_id is None or payer_id is None or int(payer_id) != owner_id:
            raise PayerMismatch(
                "Stars payer does not match the account's reviewed Telegram owner"
            )
        idem_key = f"stars-invoice-v1:{int(invoice['id'])}:{charge_id}"
        return db.provenance.record_payment(
            account_id,
            payment_channel="TELEGRAM_STARS",
            record_status="CONFIRMED",
            amount_minor=invoice.get("total_amount"),
            currency=invoice.get("payment_currency") or "XTR",
            payment_method="TELEGRAM_STARS",
            external_reference=charge_id,
            actor_type="SYSTEM",
            actor_ref=actor_ref,
            evidence={"invoice_id": invoice["id"], "tariff_name": invoice.get("tariff_name")},
            idempotency_key=idem_key,
            now=timestamp,
        )

    def process_direct_stars_enrollment(
        self, db, *, capability, invoice: dict, decision_ref, ownership_evidence, telegram_id,
        alias_provenance, legacy_status, legacy_expiry, observed_device_count,
        observed_hwid_count, evidence, idempotency_key, actor_ref, now: int | None = None,
    ) -> dict:
        """The one crash-safe orchestration flow this module provides: a
        retried call (same `idempotency_key`, same `invoice`) always
        converges to exactly one DIRECT account, one reviewed alias and one
        payment record, however many times it is repeated or wherever a
        prior attempt was interrupted."""
        timestamp = int(time.time()) if now is None else int(now)
        account = self.enroll_direct_account(
            capability=capability,
            legacy_username=invoice["marzban_username"],
            decision_ref=decision_ref,
            ownership_evidence=ownership_evidence,
            telegram_id=telegram_id,
            alias_provenance=alias_provenance,
            legacy_status=legacy_status,
            legacy_expiry=legacy_expiry,
            observed_device_count=observed_device_count,
            observed_hwid_count=observed_hwid_count,
            evidence=evidence,
            idempotency_key=idempotency_key,
            now=timestamp,
        )
        payment = self.record_stars_payment(
            db, invoice=invoice, account_id=account["account_id"], actor_ref=actor_ref, now=timestamp,
        )
        return {"account": account, "payment": payment}

    # --- EXTERNAL_PAYMENT: minimal admin-only manual-payment primitive -----
    #
    # This is only the low-level PH5-09 prerequisite PH4-03 needs (a typed,
    # idempotent way to record a manually-confirmed external payment against
    # an already-reviewed DIRECT account). It intentionally does not touch
    # subscriptions/renewal -- that is PH5-09's own scope.

    def record_external_payment(
        self, db, *, capability, account_id: int, external_reference: str,
        amount_minor: int | None, currency: str | None, reason: str, evidence: dict,
        idempotency_key: str, now: int | None = None,
    ) -> dict:
        actor = self._require_primary(capability)
        timestamp = int(time.time()) if now is None else int(now)
        self._reviewed_direct_account(account_id)
        reason = (reason or "").strip()
        if not 8 <= len(reason) <= 1000:
            raise DirectEnrollmentError("a bounded manual-payment reason is required")
        payment = db.provenance.record_payment(
            account_id,
            payment_channel="EXTERNAL_PAYMENT",
            record_status="CONFIRMED",
            amount_minor=amount_minor,
            currency=currency,
            payment_method="MANUAL",
            external_reference=external_reference,
            actor_type="PRIMARY_ADMIN",
            actor_ref=actor,
            evidence=evidence,
            idempotency_key=idempotency_key,
            now=timestamp,
        )
        db.provenance.record_mutation(
            account_id,
            subscription_id=None,
            operation="EXTERNAL_PAYMENT_MANUAL_APPLY",
            payment_channel="EXTERNAL_PAYMENT",
            mutation_source="MANUAL_PAYMENT",
            actor_type="PRIMARY_ADMIN",
            actor_ref=actor,
            reason=reason,
            external_reference=external_reference,
            before=None,
            after={"payment_id": payment["id"], "external_reference": external_reference},
            idempotency_key=f"external-payment-mutation-v1:{idempotency_key}",
            payment_id=payment["id"],
            now=timestamp,
        )
        return payment

    # --- OWNER_ATTESTED_LEGACY_EXTERNAL_PAYMENT: historical fact, no invented details ---
    #
    # Owner decision (2026-08-26): every real legacy paying user historically
    # paid the owner directly (never Stars), and no canonical payment ledger
    # existed at the time. This never invents amount/date/reference. It is a
    # distinct fact from a real new EXTERNAL_PAYMENT with known details
    # (`record_external_payment` above, `mgboost_payment_records`,
    # `record_status='CONFIRMED'`) -- deliberately a sibling additive table,
    # not a relaxed/edited `mgboost_payment_records` (whose CHECK constraints
    # are part of an already-deployed, checksum-locked PH3-09 migration that
    # must never be edited in place).

    def record_owner_attested_legacy_payment(
        self, db, *, capability, account_id: int, decision_ref: str,
        attestation_note: str, evidence: dict, now: int | None = None,
    ) -> dict:
        actor = self._require_primary(capability)
        self._reviewed_direct_account(account_id)
        decision_ref = (decision_ref or "").strip()
        attestation_note = (attestation_note or "").strip()
        if not 3 <= len(decision_ref) <= 128:
            raise DirectEnrollmentError("a bounded decision reference is required")
        if not 8 <= len(attestation_note) <= 1000:
            raise DirectEnrollmentError("a bounded attestation note is required")
        if not isinstance(evidence, dict):
            raise DirectEnrollmentError("evidence must be an object")
        timestamp = int(time.time()) if now is None else int(now)
        canonical_payload = _canonical({
            "account_id": int(account_id), "decision_ref": decision_ref,
            "attestation_note": attestation_note, "evidence": evidence,
        })
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                existing = self._conn.execute(
                    "SELECT * FROM mgboost_owner_attested_legacy_payments WHERE account_id=?",
                    (int(account_id),),
                ).fetchone()
                if existing is not None:
                    existing_payload = _canonical({
                        "account_id": existing["account_id"], "decision_ref": existing["decision_ref"],
                        "attestation_note": existing["attestation_note"],
                        "evidence": json.loads(existing["evidence_json"]),
                    })
                    if existing_payload != canonical_payload:
                        raise OwnerAttestationConflict(
                            "an owner-attested legacy external payment already exists for "
                            "this account with different details"
                        )
                    self._conn.commit()
                    result = dict(existing)
                else:
                    cursor = self._conn.execute(
                        "INSERT INTO mgboost_owner_attested_legacy_payments "
                        "(account_id,payment_channel,decision_ref,attestation_note,"
                        "attested_by_actor,evidence_json,created_at) "
                        "VALUES (?,'EXTERNAL_PAYMENT',?,?,?,?,?)",
                        (int(account_id), decision_ref, attestation_note, actor,
                         _canonical(evidence), timestamp),
                    )
                    row = self._conn.execute(
                        "SELECT * FROM mgboost_owner_attested_legacy_payments WHERE id=?",
                        (cursor.lastrowid,),
                    ).fetchone()
                    self._conn.commit()
                    result = dict(row)
            except sqlite3.IntegrityError as exc:
                self._conn.rollback()
                raise OwnerAttestationConflict(
                    "an owner-attested legacy external payment already exists for this account"
                ) from exc
            except Exception:
                self._conn.rollback()
                raise
        db.provenance.record_mutation(
            int(account_id),
            subscription_id=None,
            operation="OWNER_ATTESTED_LEGACY_EXTERNAL_PAYMENT",
            payment_channel="EXTERNAL_PAYMENT",
            mutation_source="MANUAL_PAYMENT",
            actor_type="PRIMARY_ADMIN",
            actor_ref=actor,
            reason=attestation_note,
            external_reference=None,
            before=None,
            after={"owner_attested_legacy_payment_id": result["id"], "decision_ref": decision_ref},
            idempotency_key=f"owner-attested-legacy-external-payment-v1:{int(account_id)}",
            now=timestamp,
        )
        return result
