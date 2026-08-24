"""Dormant PH3-06 internal account and entitlement service.

No legacy route imports this module.  It deliberately requires an explicit
primary-admin actor configured server-side before any write can occur.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
import time


TECHNICAL_DEVICE_CAP = 99
MAX_OVERRIDE_SECONDS = 90 * 86400


class InternalEntitlementError(RuntimeError):
    pass


class PrimaryAdminRequired(InternalEntitlementError):
    pass


class ReviewedEvidenceRequired(InternalEntitlementError):
    pass


class IdempotencyConflict(InternalEntitlementError):
    pass


def _canonical(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _idempotency_hash(scope: str, raw_key: str) -> str:
    if not isinstance(raw_key, str) or not 16 <= len(raw_key) <= 512:
        raise InternalEntitlementError("idempotency key length is invalid")
    return hashlib.sha256((scope + "\0" + raw_key).encode("utf-8")).hexdigest()


class InternalEntitlementStore:
    def __init__(self, connection: sqlite3.Connection, lock, primary_admin_actor_id: str):
        self._conn = connection
        self._lock = lock
        self._primary_actor = (primary_admin_actor_id or "").strip()

    def _require_primary(self, actor_id: str) -> str:
        actor = (actor_id or "").strip()
        if not self._primary_actor or not actor or not hmac.compare_digest(
            actor, self._primary_actor
        ):
            raise PrimaryAdminRequired("primary MGBoost admin capability required")
        return actor

    def create_internal_plan(
        self,
        *,
        actor_id: str,
        plan_code: str,
        version: int,
        display_name: str,
        device_limit_mode: str,
        device_limit: int | None,
        wl_mode: str = "UNLIMITED",
        terms: dict | None = None,
        now: int | None = None,
    ) -> dict:
        self._require_primary(actor_id)
        if device_limit_mode == "LIMITED":
            if isinstance(device_limit, bool) or not isinstance(device_limit, int):
                raise InternalEntitlementError("limited internal plan requires device limit")
            if not 1 <= device_limit <= TECHNICAL_DEVICE_CAP:
                raise InternalEntitlementError("internal device limit exceeds technical cap")
        elif device_limit_mode == "UNLIMITED":
            if device_limit is not None:
                raise InternalEntitlementError("unlimited internal plan has no numeric limit")
        else:
            raise InternalEntitlementError("invalid internal device limit mode")
        if wl_mode not in {"NONE", "UNLIMITED"}:
            raise InternalEntitlementError("PH3-06 internal WL mode must be NONE or UNLIMITED")
        if not isinstance(version, int) or isinstance(version, bool) or version <= 0:
            raise InternalEntitlementError("plan version is invalid")
        if not isinstance(plan_code, str) or not plan_code.strip():
            raise InternalEntitlementError("plan code is required")
        timestamp = int(time.time()) if now is None else int(now)
        terms_json = _canonical(terms or {"schema": 1, "internal": True})
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                existing = self._conn.execute(
                    "SELECT * FROM mgboost_plan_versions WHERE plan_code=? AND version=?",
                    (plan_code.strip(), version),
                ).fetchone()
                expected = (
                    display_name, "INTERNAL", 0, device_limit_mode, device_limit,
                    wl_mode, terms_json,
                )
                if existing:
                    actual = (
                        existing["display_name"], existing["plan_kind"],
                        existing["billing_required"], existing["device_limit_mode"],
                        existing["device_limit"], existing["wl_mode"], existing["terms_json"],
                    )
                    if actual != expected:
                        raise IdempotencyConflict("plan version already exists with other terms")
                    self._conn.commit()
                    return dict(existing)
                cursor = self._conn.execute(
                    "INSERT INTO mgboost_plan_versions (plan_code,version,display_name,"
                    "plan_kind,billing_required,non_wl_unlimited,device_limit_mode,"
                    "device_limit,wl_mode,wl_quota_bytes,wl_period_days,created_at,terms_json) "
                    "VALUES (?,?,?,'INTERNAL',0,1,?,?,?,NULL,NULL,?,?)",
                    (plan_code.strip(), version, display_name, device_limit_mode,
                     device_limit, wl_mode, timestamp, terms_json),
                )
                row = self._conn.execute(
                    "SELECT * FROM mgboost_plan_versions WHERE id=?", (cursor.lastrowid,)
                ).fetchone()
                self._conn.commit()
                return dict(row)
            except Exception:
                self._conn.rollback()
                raise

    def create_reviewed_account(
        self,
        *,
        actor_id: str,
        plan_version_id: int,
        legacy_username: str,
        ownership_evidence: str,
        telegram_id: int | None,
        legacy_status: str,
        legacy_expiry: int | None,
        device_evidence_count: int,
        hwid_evidence_count: int,
        internal_reason: str,
        migration_confidence: str,
        evidence: dict,
        idempotency_key: str,
        now: int | None = None,
    ) -> dict:
        actor = self._require_primary(actor_id)
        if ownership_evidence not in {"PROVEN", "ABSENT"}:
            raise ReviewedEvidenceRequired("ambiguous ownership cannot be auto-bound")
        if ownership_evidence == "PROVEN":
            if isinstance(telegram_id, bool) or not isinstance(telegram_id, int) or telegram_id <= 0:
                raise ReviewedEvidenceRequired("proven ownership requires Telegram ID")
        elif telegram_id is not None:
            raise ReviewedEvidenceRequired("Telegram binding requires proven ownership")
        if legacy_status not in {"ACTIVE", "DISABLED", "EXPIRED", "UNLIMITED"}:
            raise ReviewedEvidenceRequired("legacy status is invalid")
        if migration_confidence not in {"HIGH", "MEDIUM", "LOW"}:
            raise ReviewedEvidenceRequired("migration confidence is invalid")
        username = (legacy_username or "").strip()
        reason = (internal_reason or "").strip()
        if not username or len(username) > 128 or not 8 <= len(reason) <= 1000:
            raise ReviewedEvidenceRequired("reviewed username/reason is invalid")
        if any(isinstance(v, bool) or not isinstance(v, int) or v < 0
               for v in (device_evidence_count, hwid_evidence_count)):
            raise ReviewedEvidenceRequired("evidence counts are invalid")
        timestamp = int(time.time()) if now is None else int(now)
        idem_hash = _idempotency_hash("internal-account-v1", idempotency_key)
        review_payload = {
            "legacy_username": username,
            "ownership_evidence": ownership_evidence,
            "telegram_id": telegram_id,
            "legacy_status": legacy_status,
            "legacy_expiry": legacy_expiry,
            "device_evidence_count": device_evidence_count,
            "hwid_evidence_count": hwid_evidence_count,
            "internal_reason": reason,
            "migration_confidence": migration_confidence,
            "plan_version_id": int(plan_version_id),
            "evidence": evidence,
        }
        after_json = _canonical(review_payload)
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                prior = self._conn.execute(
                    "SELECT m.*,r.id AS review_id FROM mgboost_entitlement_mutations m "
                    "JOIN mgboost_internal_account_reviews r ON r.mutation_id=m.id "
                    "WHERE m.idempotency_key_hash=?", (idem_hash,),
                ).fetchone()
                if prior:
                    if prior["operation"] != "INTERNAL_ACCOUNT_CREATE" or prior["after_json"] != after_json:
                        raise IdempotencyConflict("idempotency key reused with other review")
                    result = self._review_result(prior["account_id"])
                    self._conn.commit()
                    return result
                plan = self._conn.execute(
                    "SELECT * FROM mgboost_plan_versions WHERE id=? AND plan_kind='INTERNAL' "
                    "AND billing_required=0", (int(plan_version_id),),
                ).fetchone()
                if not plan:
                    raise ReviewedEvidenceRequired("review requires a versioned internal plan")
                public_id = "acct_" + secrets.token_urlsafe(18)
                account_id = self._conn.execute(
                    "INSERT INTO mgboost_accounts "
                    "(public_id,status,account_source,created_at,updated_at) "
                    "VALUES (?,'ACTIVE','INTERNAL',?,?)",
                    (public_id, timestamp, timestamp),
                ).lastrowid
                subscription_status = legacy_status
                if subscription_status == "ACTIVE" and legacy_expiry is not None and legacy_expiry <= timestamp:
                    subscription_status = "EXPIRED"
                subscription_id = self._conn.execute(
                    "INSERT INTO mgboost_subscriptions "
                    "(account_id,current_plan_version_id,status,started_at,current_expiry,"
                    "created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                    (account_id, plan_version_id, subscription_status, timestamp,
                     legacy_expiry, timestamp, timestamp),
                ).lastrowid
                mutation_id = self._conn.execute(
                    "INSERT INTO mgboost_entitlement_mutations "
                    "(account_id,subscription_id,operation,payment_channel,mutation_source,"
                    "actor_type,actor_ref,reason,idempotency_key_hash,after_json,created_at) "
                    "VALUES (?,?,?,'NOT_APPLICABLE','INTERNAL','PRIMARY_ADMIN',?,?,?,?,?)",
                    (account_id, subscription_id, "INTERNAL_ACCOUNT_CREATE", actor,
                     reason, idem_hash, after_json, timestamp),
                ).lastrowid
                if ownership_evidence == "PROVEN":
                    self._conn.execute(
                        "INSERT INTO mgboost_telegram_identities "
                        "(account_id,telegram_id,role,provenance,linked_at,linked_by_actor) "
                        "VALUES (?,?,'OWNER','MIGRATION',?,?)",
                        (account_id, telegram_id, timestamp, actor),
                    )
                self._conn.execute(
                    "INSERT INTO mgboost_internal_account_reviews "
                    "(account_id,legacy_username,ownership_evidence,legacy_status,legacy_expiry,"
                    "device_evidence_count,hwid_evidence_count,internal_reason,"
                    "migration_confidence,proposed_plan_version_id,reviewed_by_actor,"
                    "mutation_id,evidence_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (account_id, username, ownership_evidence, legacy_status, legacy_expiry,
                     device_evidence_count, hwid_evidence_count, reason,
                     migration_confidence, plan_version_id, actor, mutation_id,
                     _canonical(evidence), timestamp),
                )
                self._conn.execute(
                    "INSERT INTO mgboost_internal_entitlement_revisions "
                    "(account_id,revision,updated_at) VALUES (?,1,?)",
                    (account_id, timestamp),
                )
                self._conn.commit()
                return self._review_result(account_id)
            except Exception:
                self._conn.rollback()
                raise

    def _review_result(self, account_id: int) -> dict:
        row = self._conn.execute(
            "SELECT a.id AS account_id,a.public_id,a.account_source,s.id AS subscription_id,"
            "s.status,s.current_expiry,r.ownership_evidence,r.migration_confidence,"
            "r.proposed_plan_version_id FROM mgboost_accounts a "
            "JOIN mgboost_subscriptions s ON s.account_id=a.id "
            "JOIN mgboost_internal_account_reviews r ON r.account_id=a.id "
            "WHERE a.id=?", (int(account_id),),
        ).fetchone()
        return dict(row)

    def effective_entitlements(self, account_id: int, *, now: int | None = None) -> dict:
        timestamp = int(time.time()) if now is None else int(now)
        with self._lock:
            row = self._conn.execute(
                "SELECT a.account_source,a.status AS account_status,s.id AS subscription_id,"
                "s.status AS subscription_status,s.current_expiry,p.* "
                "FROM mgboost_accounts a JOIN mgboost_subscriptions s ON s.account_id=a.id "
                "JOIN mgboost_plan_versions p ON p.id=s.current_plan_version_id "
                "WHERE a.id=?", (int(account_id),),
            ).fetchone()
            if not row:
                raise InternalEntitlementError("account entitlement not found")
            if row["account_source"] != "INTERNAL" or row["plan_kind"] != "INTERNAL":
                raise InternalEntitlementError("ordinary account has no internal entitlement")
            result = {
                "account_id": int(account_id),
                "billing_required": False,
                "device_limit_mode": row["device_limit_mode"],
                "device_limit": row["device_limit"],
                "effective_device_cap": TECHNICAL_DEVICE_CAP if row["device_limit_mode"] == "UNLIMITED" else row["device_limit"],
                "wl_mode": row["wl_mode"],
                "wl_quota_bytes": row["wl_quota_bytes"],
                "override_mode": "AUTO",
            }
            overrides = self._conn.execute(
                "SELECT * FROM mgboost_entitlement_overrides WHERE account_id=? "
                "AND revoked_at IS NULL AND starts_at<=? AND expires_at>? "
                "AND (subscription_id IS NULL OR subscription_id=?) "
                "ORDER BY starts_at,id", (int(account_id), timestamp, timestamp,
                                           row["subscription_id"]),
            ).fetchall()
            for override in overrides:
                result["override_mode"] = "EXPLICIT"
                if override["entitlement_key"] == "DEVICE_LIMIT":
                    result["device_limit_mode"] = (
                        "UNLIMITED" if override["value_type"] == "UNLIMITED"
                        else "LIMITED"
                    )
                    result["device_limit"] = override["integer_value"]
                    result["effective_device_cap"] = (
                        TECHNICAL_DEVICE_CAP if override["value_type"] == "UNLIMITED"
                        else override["integer_value"]
                    )
                elif override["entitlement_key"] == "WL_ACCESS":
                    result["wl_mode"] = (
                        "UNLIMITED" if override["value_type"] == "UNLIMITED"
                        else ("UNLIMITED" if override["boolean_value"] else "NONE")
                    )
                elif override["entitlement_key"] == "WL_QUOTA_BYTES":
                    result["wl_quota_bytes"] = (
                        None if override["value_type"] == "UNLIMITED"
                        else override["integer_value"]
                    )
            return result

    def add_override(
        self,
        account_id: int,
        *,
        actor_id: str,
        entitlement_key: str,
        value_type: str,
        value: int | bool | None,
        reason: str,
        expires_at: int,
        idempotency_key: str,
        now: int | None = None,
    ) -> dict:
        actor = self._require_primary(actor_id)
        timestamp = int(time.time()) if now is None else int(now)
        reason = (reason or "").strip()
        if not 8 <= len(reason) <= 1000:
            raise InternalEntitlementError("override reason is required")
        if not timestamp < int(expires_at) <= timestamp + MAX_OVERRIDE_SECONDS:
            raise InternalEntitlementError("override expiry must be within 90 days")
        allowed = {
            "DEVICE_LIMIT": {"INTEGER", "UNLIMITED"},
            "WL_ACCESS": {"BOOLEAN", "UNLIMITED"},
            "WL_QUOTA_BYTES": {"INTEGER", "UNLIMITED"},
        }
        if entitlement_key not in allowed or value_type not in allowed[entitlement_key]:
            raise InternalEntitlementError("unsupported internal override")
        boolean_value = integer_value = None
        if value_type == "BOOLEAN":
            if not isinstance(value, bool):
                raise InternalEntitlementError("boolean override requires boolean value")
            boolean_value = 1 if value else 0
        elif value_type == "INTEGER":
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise InternalEntitlementError("integer override requires nonnegative integer")
            if entitlement_key == "DEVICE_LIMIT" and not 1 <= value <= TECHNICAL_DEVICE_CAP:
                raise InternalEntitlementError("device override exceeds technical cap")
            integer_value = value
        elif value is not None:
            raise InternalEntitlementError("unlimited override has no value")
        payload = {
            "account_id": int(account_id), "entitlement_key": entitlement_key,
            "value_type": value_type, "value": value, "reason": reason,
            "starts_at": timestamp, "expires_at": int(expires_at),
        }
        after_json = _canonical(payload)
        idem_hash = _idempotency_hash("internal-override-v1", idempotency_key)
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                account = self._conn.execute(
                    "SELECT a.account_source,s.id AS subscription_id FROM mgboost_accounts a "
                    "JOIN mgboost_subscriptions s ON s.account_id=a.id WHERE a.id=?",
                    (int(account_id),),
                ).fetchone()
                if not account or account["account_source"] != "INTERNAL":
                    raise InternalEntitlementError("ordinary account cannot receive internal override")
                prior = self._conn.execute(
                    "SELECT * FROM mgboost_entitlement_mutations WHERE idempotency_key_hash=?",
                    (idem_hash,),
                ).fetchone()
                if prior:
                    if prior["account_id"] != int(account_id) or prior["after_json"] != after_json:
                        raise IdempotencyConflict("idempotency key reused with other override")
                    override = self._conn.execute(
                        "SELECT * FROM mgboost_entitlement_overrides WHERE mutation_id=?",
                        (prior["id"],),
                    ).fetchone()
                    self._conn.commit()
                    return dict(override)
                mutation_id = self._conn.execute(
                    "INSERT INTO mgboost_entitlement_mutations "
                    "(account_id,subscription_id,operation,payment_channel,mutation_source,"
                    "actor_type,actor_ref,reason,idempotency_key_hash,after_json,created_at) "
                    "VALUES (?,?,?,'ADMIN_GRANT','ADMIN','PRIMARY_ADMIN',?,?,?,?,?)",
                    (int(account_id), account["subscription_id"], "INTERNAL_OVERRIDE",
                     actor, reason, idem_hash, after_json, timestamp),
                ).lastrowid
                override_id = self._conn.execute(
                    "INSERT INTO mgboost_entitlement_overrides "
                    "(account_id,subscription_id,entitlement_key,value_type,boolean_value,"
                    "integer_value,starts_at,expires_at,reason,mutation_id,created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (int(account_id), account["subscription_id"], entitlement_key,
                     value_type, boolean_value, integer_value, timestamp,
                     int(expires_at), reason, mutation_id, timestamp),
                ).lastrowid
                self._conn.execute(
                    "INSERT INTO mgboost_internal_entitlement_revisions "
                    "(account_id,revision,updated_at) VALUES (?,1,?) "
                    "ON CONFLICT(account_id) DO UPDATE SET revision=revision+1,updated_at=excluded.updated_at",
                    (int(account_id), timestamp),
                )
                row = self._conn.execute(
                    "SELECT * FROM mgboost_entitlement_overrides WHERE id=?", (override_id,)
                ).fetchone()
                self._conn.commit()
                return dict(row)
            except Exception:
                self._conn.rollback()
                raise
