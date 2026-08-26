"""PH5-03 deterministic package buckets over the canonical PH6 parent ledger.

Consumption is never stored as a mutable counter: every calculation derives
period excess from PH6-03's immutable samples, spends base first, then FIFO
across package buckets by owner-approved ``(granted_at ASC, bucket_id ASC)``.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time

from .wl_topology import WL_NODE_IDS


class WLPackageError(RuntimeError): pass
class PackageEligibilityError(WLPackageError): pass
class PackagePaymentError(WLPackageError): pass
class PackageIdempotencyConflict(WLPackageError): pass
class PackageConsumed(WLPackageError): pass
class PackageAlreadyRefunded(WLPackageError): pass


def _hash(scope: str, value: str) -> str:
    if not isinstance(value, str) or not 16 <= len(value) <= 512:
        raise WLPackageError("idempotency key must be a string between 16 and 512 characters")
    return hashlib.sha256((scope + "\0" + value).encode()).hexdigest()


def _canonical(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class WLPackageStore:
    def __init__(self, connection: sqlite3.Connection, lock, catalog):
        self._conn, self._lock, self._catalog = connection, lock, catalog

    def _wl_eligible(self, account_id: int, now: int) -> bool:
        """Real plan terms only: a FORCE_ENABLED override never qualifies Base."""
        row = self._conn.execute(
            "SELECT s.status,s.current_expiry,pv.wl_mode FROM mgboost_subscriptions s "
            "JOIN mgboost_plan_versions pv ON pv.id=s.current_plan_version_id "
            "WHERE s.account_id=? ORDER BY s.id DESC LIMIT 1", (int(account_id),)
        ).fetchone()
        return bool(row and row["status"] == "ACTIVE" and row["current_expiry"] is not None
                    and int(row["current_expiry"]) > int(now) and row["wl_mode"] == "LIMITED")

    def _price_snapshot(self, sku: str, channel: str, catalog_version: str | None) -> dict:
        if channel not in {"TELEGRAM_STARS", "RUB"}:
            raise WLPackageError("unsupported package price channel")
        sql = (
            "SELECT pp.id AS package_price_id,pp.amount,p.id AS product_id,p.sku,p.version AS product_version,p.bytes,"
            "cv.id AS catalog_version_id,cv.catalog_version,cv.channel FROM mgboost_wl_package_prices pp "
            "JOIN mgboost_wl_package_products p ON p.id=pp.package_product_id "
            "JOIN mgboost_price_catalog_versions cv ON cv.id=pp.catalog_version_id "
            "WHERE p.sku=? AND cv.channel=?"
        )
        params: list[object] = [sku, channel]
        if catalog_version is None:
            sql += " AND cv.status='ACTIVE'"
        else:
            sql += " AND cv.catalog_version=?"
            params.append(catalog_version)
        row = self._conn.execute(sql + " ORDER BY p.version DESC LIMIT 1", params).fetchone()
        if row is None:
            raise WLPackageError("recorded package/catalog version is unknown")
        return dict(row)

    def _period_excesses(self, account_id: int) -> list[dict]:
        nodes = sorted(WL_NODE_IDS)
        placeholders = ",".join("?" for _ in nodes)
        rows = self._conn.execute(
            "SELECT p.id,p.starts_at,p.ends_at,p.base_quota_bytes,p.quota_mode,"
            f"COALESCE(SUM(CASE WHEN u.node_id IN ({placeholders}) THEN u.bytes_delta ELSE 0 END),0) AS consumed_bytes "
            "FROM mgboost_wl_periods p LEFT JOIN mgboost_wl_usage_samples u ON u.wl_period_id=p.id "
            "WHERE p.account_id=? GROUP BY p.id ORDER BY p.starts_at ASC,p.id ASC", (*nodes, int(account_id)),
        ).fetchall()
        return [{"id": int(r["id"]), "starts_at": int(r["starts_at"]), "ends_at": int(r["ends_at"]),
                 "consumed_bytes": int(r["consumed_bytes"]), "base_quota_bytes": r["base_quota_bytes"],
                 "package_demand_bytes": 0 if r["quota_mode"] == "UNLIMITED" else max(0, int(r["consumed_bytes"]) - int(r["base_quota_bytes"]))}
                for r in rows]

    def _derive(self, account_id: int, now: int) -> dict:
        grants = [dict(r) for r in self._conn.execute(
            "SELECT * FROM mgboost_wl_package_grants WHERE account_id=? ORDER BY granted_at ASC,id ASC", (int(account_id),)
        ).fetchall()]
        active = [g for g in grants if g["status"] == "ACTIVE"]
        consumed = {int(g["id"]): 0 for g in grants}
        periods = self._period_excesses(account_id)
        for period in periods:
            demand = period["package_demand_bytes"]
            for grant in active:
                if demand <= 0:
                    break
                # The durable PH6 interface is period-level. A bucket applies
                # only to a period ending after it was granted; freeze/resume
                # changes no bucket key or historical allocation.
                if int(grant["granted_at"]) >= period["ends_at"]:
                    continue
                remain = int(grant["granted_bytes"]) - consumed[int(grant["id"])]
                take = min(remain, demand)
                consumed[int(grant["id"])] += take
                demand -= take
        buckets = [{**g, "derived_consumed_bytes": consumed[int(g["id"])],
                    "derived_remaining_bytes": 0 if g["status"] == "REVOKED" else int(g["granted_bytes"]) - consumed[int(g["id"])]}
                   for g in grants]
        eligible = self._wl_eligible(account_id, now)
        return {"account_id": int(account_id), "eligible_now": eligible, "frozen": not eligible,
                "buckets": buckets, "periods": periods}

    def package_state(self, *, account_id: int, now: int | None = None) -> dict:
        with self._lock:
            return self._derive(int(account_id), int(time.time()) if now is None else int(now))

    def grant_paid_package(self, *, account_id: int, sku: str, price_channel: str, payment_id: int,
                           idempotency_key: str, catalog_version: str | None = None,
                           actor_type: str = "PAYMENT_CALLBACK", actor_ref: str | None = None,
                           now: int | None = None) -> dict:
        timestamp, key_hash = int(time.time()) if now is None else int(now), _hash("ph5-03-package-grant-v1", idempotency_key)
        payload = {"account_id": int(account_id), "sku": sku, "price_channel": price_channel,
                   "catalog_version": catalog_version, "payment_id": int(payment_id), "actor_type": actor_type, "actor_ref": actor_ref}
        request_hash = hashlib.sha256(_canonical(payload).encode()).hexdigest()
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                prior = self._conn.execute("SELECT * FROM mgboost_wl_package_grants WHERE idempotency_key_hash=?", (key_hash,)).fetchone()
                if prior:
                    if prior["request_hash"] != request_hash: raise PackageIdempotencyConflict("package grant idempotency key reused with another request")
                    self._conn.commit(); return {**dict(prior), "already_applied": True}
                if not self._wl_eligible(int(account_id), timestamp): raise PackageEligibilityError("package requires an active real WL-enabled plan")
                price = self._price_snapshot(sku, price_channel, catalog_version)
                payment = self._conn.execute("SELECT * FROM mgboost_payment_records WHERE id=? AND account_id=?", (int(payment_id), int(account_id))).fetchone()
                expected = "TELEGRAM_STARS" if price_channel == "TELEGRAM_STARS" else "EXTERNAL_PAYMENT"
                if not payment or payment["record_status"] != "CONFIRMED" or payment["payment_channel"] != expected or payment["amount_minor"] != price["amount"]:
                    raise PackagePaymentError("payment does not exactly confirm this package price/channel/account")
                if self._conn.execute("SELECT 1 FROM mgboost_wl_package_grants WHERE payment_id=?", (int(payment_id),)).fetchone():
                    raise PackageIdempotencyConflict("payment already granted a package")
                source = "DIRECT_PURCHASE" if expected == "TELEGRAM_STARS" else "MANUAL_PAYMENT"
                snapshot = {"package_sku": price["sku"], "product_version": price["product_version"], "catalog_version": price["catalog_version"], "granted_bytes": price["bytes"], "price_amount": price["amount"], "price_channel": price_channel, "payment_id": int(payment_id)}
                mutation_id = self._conn.execute(
                    "INSERT INTO mgboost_entitlement_mutations (account_id,subscription_id,operation,payment_channel,mutation_source,actor_type,actor_ref,reason,external_reference,idempotency_key_hash,before_json,after_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (int(account_id), None, "PACKAGE_GRANT", expected, source, actor_type, actor_ref, "PH5-03 paid package grant", payment["external_reference"], _hash("ph5-03-package-grant-mutation-v1", idempotency_key), "{}", _canonical(snapshot), timestamp)).lastrowid
                self._conn.execute("INSERT INTO mgboost_mutation_payment_links (mutation_id,payment_id,account_id,created_at) VALUES (?,?,?,?)", (mutation_id, int(payment_id), int(account_id), timestamp))
                grant_id = self._conn.execute(
                    "INSERT INTO mgboost_wl_package_grants (account_id,package_product_id,catalog_version_id,package_price_id,payment_id,grant_mutation_id,price_channel,sku_snapshot,product_version_snapshot,catalog_version_snapshot,granted_bytes,price_amount_snapshot,granted_at,status,idempotency_key_hash,request_hash,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'ACTIVE',?,?,?)",
                    (int(account_id), price["product_id"], price["catalog_version_id"], price["package_price_id"], int(payment_id), mutation_id, price_channel, price["sku"], price["product_version"], price["catalog_version"], price["bytes"], price["amount"], timestamp, key_hash, request_hash, timestamp)).lastrowid
                row = self._conn.execute("SELECT * FROM mgboost_wl_package_grants WHERE id=?", (grant_id,)).fetchone()
                self._conn.commit(); return {**dict(row), "already_applied": False}
            except Exception:
                self._conn.rollback(); raise

    def refund_unused_package(self, *, account_id: int, package_grant_id: int, refund_reference: str,
                              evidence: dict, idempotency_key: str, actor_type: str,
                              actor_ref: str | None = None, now: int | None = None) -> dict:
        if not isinstance(refund_reference, str) or not 1 <= len(refund_reference) <= 512: raise WLPackageError("refund reference is required")
        timestamp, key_hash = int(time.time()) if now is None else int(now), _hash("ph5-03-package-refund-v1", idempotency_key)
        payload = {"account_id": int(account_id), "package_grant_id": int(package_grant_id), "refund_reference": refund_reference, "evidence": evidence or {}, "actor_type": actor_type, "actor_ref": actor_ref}
        request_hash = hashlib.sha256(_canonical(payload).encode()).hexdigest()
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                prior = self._conn.execute("SELECT * FROM mgboost_wl_package_refunds WHERE idempotency_key_hash=?", (key_hash,)).fetchone()
                if prior:
                    if prior["request_hash"] != request_hash: raise PackageIdempotencyConflict("package refund idempotency key reused with another request")
                    self._conn.commit(); return {**dict(prior), "already_applied": True}
                grant = self._conn.execute("SELECT * FROM mgboost_wl_package_grants WHERE id=? AND account_id=?", (int(package_grant_id), int(account_id))).fetchone()
                if not grant: raise WLPackageError("package grant not found for account")
                if grant["status"] != "ACTIVE": raise PackageAlreadyRefunded("package grant has already been revoked")
                bucket = next(row for row in self._derive(int(account_id), timestamp)["buckets"] if row["id"] == int(package_grant_id))
                if bucket["derived_consumed_bytes"] != 0: raise PackageConsumed("package refund requires zero derived consumption")
                payment = self._conn.execute("SELECT * FROM mgboost_payment_records WHERE id=?", (grant["payment_id"],)).fetchone()
                source = "DIRECT_PURCHASE" if payment["payment_channel"] == "TELEGRAM_STARS" else "MANUAL_PAYMENT"
                mutation_id = self._conn.execute(
                    "INSERT INTO mgboost_entitlement_mutations (account_id,subscription_id,operation,payment_channel,mutation_source,actor_type,actor_ref,reason,external_reference,idempotency_key_hash,before_json,after_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (int(account_id), None, "PACKAGE_REFUND_REVOKE", payment["payment_channel"], source, actor_type, actor_ref, "PH5-03 unused-only package refund", refund_reference, _hash("ph5-03-package-refund-mutation-v1", idempotency_key), _canonical({"package_grant_id": int(package_grant_id), "granted_bytes": grant["granted_bytes"]}), _canonical({"package_grant_id": int(package_grant_id), "revoked": True, "derived_consumed_bytes": 0, "refund_reference": refund_reference}), timestamp)).lastrowid
                self._conn.execute("UPDATE mgboost_wl_package_grants SET status='REVOKED',revoked_at=?,revoked_by_mutation_id=? WHERE id=? AND status='ACTIVE'", (timestamp, mutation_id, int(package_grant_id)))
                refund_id = self._conn.execute("INSERT INTO mgboost_wl_package_refunds (account_id,package_grant_id,refund_mutation_id,refund_reference,evidence_json,idempotency_key_hash,request_hash,created_at) VALUES (?,?,?,?,?,?,?,?)", (int(account_id), int(package_grant_id), mutation_id, refund_reference, _canonical(evidence or {}), key_hash, request_hash, timestamp)).lastrowid
                row = self._conn.execute("SELECT * FROM mgboost_wl_package_refunds WHERE id=?", (refund_id,)).fetchone()
                self._conn.commit(); return {**dict(row), "already_applied": False}
            except Exception:
                self._conn.rollback(); raise
