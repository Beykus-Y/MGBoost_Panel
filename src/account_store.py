"""Dormant PH3-01 repository for the additive parent-account schema.

The store is deliberately not wired into legacy HTTP, bot or worker paths.
All reads that address a child resource require the owning account id so the
future API has an explicit tenant/IDOR boundary from its first implementation.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import time


class AccountSchemaError(ValueError):
    pass


class IdentityConflict(AccountSchemaError):
    pass


class AccountStore:
    ACCOUNT_SOURCES = {"DIRECT", "INTERNAL", "UNKNOWN_LEGACY"}
    IDENTITY_PROVENANCE = {
        "DIRECT_BIND", "ADMIN_REBIND", "MIGRATION", "UNKNOWN_LEGACY",
    }

    def __init__(self, connection: sqlite3.Connection, lock):
        self._conn = connection
        self._lock = lock

    def get_active_account_by_telegram_id(self, telegram_id: int) -> dict | None:
        """Resolve only the canonical, non-revoked owner identity.

        Legacy ``tg_users``/Marzban username links are deliberately not a
        substitute for this proof: Stars PH5-05 sells a parent-account
        product, never an inferred username entitlement.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT a.*,i.id AS telegram_identity_id,i.telegram_id,i.provenance "
                "FROM mgboost_telegram_identities i JOIN mgboost_accounts a ON a.id=i.account_id "
                "WHERE i.telegram_id=? AND i.role='OWNER' AND i.revoked_at IS NULL AND a.status='ACTIVE'",
                (int(telegram_id),),
            ).fetchone()
        return dict(row) if row else None

    def create_account(self, account_source: str, *, now: int | None = None) -> dict:
        if account_source not in self.ACCOUNT_SOURCES:
            raise AccountSchemaError("unsupported account source")
        timestamp = int(time.time()) if now is None else int(now)
        with self._lock:
            for _ in range(5):
                public_id = "acct_" + secrets.token_urlsafe(18)
                try:
                    cursor = self._conn.execute(
                        "INSERT INTO mgboost_accounts "
                        "(public_id,status,account_source,created_at,updated_at) "
                        "VALUES (?,'ACTIVE',?,?,?)",
                        (public_id, account_source, timestamp, timestamp),
                    )
                    self._conn.commit()
                    return self.get_account(cursor.lastrowid)
                except sqlite3.IntegrityError:
                    self._conn.rollback()
            raise RuntimeError("could not allocate unique account id")

    def get_account(self, account_id: int) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM mgboost_accounts WHERE id=?", (int(account_id),)
            ).fetchone()
        return dict(row) if row else None

    def link_telegram_owner(
        self,
        account_id: int,
        telegram_id: int,
        *,
        provenance: str,
        actor: str | None = None,
        now: int | None = None,
    ) -> dict:
        if provenance not in self.IDENTITY_PROVENANCE:
            raise AccountSchemaError("unsupported identity provenance")
        if isinstance(telegram_id, bool) or not isinstance(telegram_id, int) or telegram_id <= 0:
            raise AccountSchemaError("telegram_id must be a positive integer")
        timestamp = int(time.time()) if now is None else int(now)
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                if not self._conn.execute(
                    "SELECT 1 FROM mgboost_accounts WHERE id=? AND status!='CLOSED'",
                    (int(account_id),),
                ).fetchone():
                    raise AccountSchemaError("account not found or closed")
                existing = self._conn.execute(
                    "SELECT * FROM mgboost_telegram_identities "
                    "WHERE telegram_id=? AND revoked_at IS NULL",
                    (telegram_id,),
                ).fetchone()
                if existing:
                    if existing["account_id"] == int(account_id):
                        self._conn.commit()
                        return dict(existing)
                    raise IdentityConflict("telegram identity already belongs to another account")
                if self._conn.execute(
                    "SELECT 1 FROM mgboost_telegram_identities "
                    "WHERE account_id=? AND role='OWNER' AND revoked_at IS NULL",
                    (int(account_id),),
                ).fetchone():
                    raise IdentityConflict("account already has an active owner")
                cursor = self._conn.execute(
                    "INSERT INTO mgboost_telegram_identities "
                    "(account_id,telegram_id,role,provenance,linked_at,linked_by_actor) "
                    "VALUES (?,?,'OWNER',?,?,?)",
                    (int(account_id), telegram_id, provenance, timestamp, actor),
                )
                row = self._conn.execute(
                    "SELECT * FROM mgboost_telegram_identities WHERE id=?",
                    (cursor.lastrowid,),
                ).fetchone()
                self._conn.commit()
                return dict(row)
            except Exception:
                self._conn.rollback()
                raise

    def get_account_for_telegram(self, telegram_id: int) -> dict | None:
        """Identity lookup only; possession of the numeric ID is not auth."""
        with self._lock:
            row = self._conn.execute(
                "SELECT a.* FROM mgboost_accounts AS a "
                "JOIN mgboost_telegram_identities AS i ON i.account_id=a.id "
                "WHERE i.telegram_id=? AND i.revoked_at IS NULL AND i.role='OWNER'",
                (int(telegram_id),),
            ).fetchone()
        return dict(row) if row else None

    def get_subscription_for_account(
        self, account_id: int, subscription_id: int
    ) -> dict | None:
        """Account-scoped lookup; never fetch a subscription by id alone."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM mgboost_subscriptions WHERE id=? AND account_id=?",
                (int(subscription_id), int(account_id)),
            ).fetchone()
        return dict(row) if row else None

    def create_plan_version(self, values: dict, *, now: int | None = None) -> dict:
        timestamp = int(time.time()) if now is None else int(now)
        terms = values.get("terms") or {}
        with self._lock:
            try:
                cursor = self._conn.execute(
                    "INSERT INTO mgboost_plan_versions ("
                    "plan_code,version,display_name,plan_kind,billing_required,"
                    "non_wl_unlimited,device_limit_mode,device_limit,wl_mode,"
                    "wl_quota_bytes,wl_period_days,created_at,terms_json"
                    ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        values["plan_code"], int(values["version"]),
                        values["display_name"], values["plan_kind"],
                        1 if values["billing_required"] else 0,
                        1, values["device_limit_mode"], values.get("device_limit"),
                        values["wl_mode"], values.get("wl_quota_bytes"),
                        values.get("wl_period_days"), timestamp,
                        json.dumps(terms, ensure_ascii=False, sort_keys=True,
                                   separators=(",", ":")),
                    ),
                )
                self._conn.commit()
                row = self._conn.execute(
                    "SELECT * FROM mgboost_plan_versions WHERE id=?", (cursor.lastrowid,)
                ).fetchone()
            except Exception:
                self._conn.rollback()
                raise
        return dict(row)

    def add_plan_duration(
        self,
        plan_version_id: int,
        duration_days: int,
        *,
        duration_version: int = 1,
        metadata: dict | None = None,
        now: int | None = None,
    ) -> dict:
        timestamp = int(time.time()) if now is None else int(now)
        with self._lock:
            try:
                cursor = self._conn.execute(
                    "INSERT INTO mgboost_plan_durations "
                    "(plan_version_id,duration_days,duration_version,created_at,metadata_json) "
                    "VALUES (?,?,?,?,?)",
                    (
                        int(plan_version_id), int(duration_days), int(duration_version),
                        timestamp,
                        json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True,
                                   separators=(",", ":")),
                    ),
                )
                self._conn.commit()
                row = self._conn.execute(
                    "SELECT * FROM mgboost_plan_durations WHERE id=?", (cursor.lastrowid,)
                ).fetchone()
            except Exception:
                self._conn.rollback()
                raise
        return dict(row)
