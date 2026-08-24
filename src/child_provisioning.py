"""Dormant transactional child intent/outbox repository.

This module creates desired local state only. It has no worker, route or
Marzban import, so importing it cannot provision a remote child.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import time
import uuid

from .child_contract import derive_child_username, derive_operation_id


class ChildProvisioningError(RuntimeError):
    pass


class ChildProvisioningConflict(ChildProvisioningError):
    pass


def _canonical(value: dict) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class ChildProvisioningStore:
    def __init__(self, connection: sqlite3.Connection, lock):
        self._conn = connection
        self._lock = lock

    def prepare_child_ensure(
        self,
        *,
        account_id: int,
        slot_generation_id: int,
        source_alias_id: int,
        source_contract_hash: str,
        expire: int,
        idempotency_key: str,
        now: int | None = None,
    ) -> dict:
        timestamp = int(time.time()) if now is None else int(now)
        if not isinstance(idempotency_key, str) or not 16 <= len(idempotency_key) <= 512:
            raise ChildProvisioningError("invalid idempotency key")
        if (
            not isinstance(source_contract_hash, str)
            or len(source_contract_hash) != 64
            or any(char not in "0123456789abcdef" for char in source_contract_hash)
        ):
            raise ChildProvisioningError("invalid source contract hash")
        if isinstance(expire, bool) or not isinstance(expire, int) or expire < 0:
            raise ChildProvisioningError("invalid child expiry")
        idem_hash = _sha("child-ensure-v1\0" + idempotency_key)
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                account = self._conn.execute(
                    "SELECT public_id,status FROM mgboost_accounts WHERE id=?",
                    (int(account_id),),
                ).fetchone()
                generation = self._conn.execute(
                    "SELECT g.id,g.account_id,g.slot_id,g.slot_number,g.generation,g.status,"
                    "s.desired_state FROM mgboost_device_slot_generations g "
                    "JOIN mgboost_device_slots s ON s.id=g.slot_id AND s.account_id=g.account_id "
                    "WHERE g.id=? AND g.account_id=?",
                    (int(slot_generation_id), int(account_id)),
                ).fetchone()
                alias = self._conn.execute(
                    "SELECT id,legacy_username FROM mgboost_legacy_account_aliases "
                    "WHERE id=? AND account_id=?",
                    (int(source_alias_id), int(account_id)),
                ).fetchone()
                if not account or account["status"] != "ACTIVE":
                    raise ChildProvisioningError("active parent account required")
                if (
                    not generation or generation["status"] != "ACTIVE"
                    or generation["desired_state"] != "ACTIVE"
                ):
                    raise ChildProvisioningError("active slot generation required")
                if not alias:
                    raise ChildProvisioningError("account-owned legacy alias required")
                child_username = derive_child_username(
                    account["public_id"], generation["slot_number"], generation["generation"]
                )
                operation_id = derive_operation_id(child_username)
                payload = {
                    "operation_id": operation_id,
                    "child_username": child_username,
                    "source_username": alias["legacy_username"],
                    "source_contract_hash": source_contract_hash,
                    "expire": expire,
                }
                payload_json = _canonical(payload)
                request_hash = _sha(payload_json)
                prior = self._conn.execute(
                    "SELECT o.*,c.child_username FROM mgboost_outbox o "
                    "JOIN mgboost_child_user_intents c ON c.id=o.child_intent_id "
                    "WHERE o.idempotency_key_hash=?", (idem_hash,),
                ).fetchone()
                if prior:
                    if prior["request_hash"] != request_hash:
                        raise ChildProvisioningConflict(
                            "idempotency key reused with different child request"
                        )
                    self._conn.commit()
                    return dict(prior)
                child_id = self._conn.execute(
                    "INSERT INTO mgboost_child_user_intents "
                    "(public_id,account_id,slot_id,slot_generation_id,slot_number,generation,"
                    "source_alias_id,child_username,source_contract_hash,desired_state,"
                    "observed_state,created_at,updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,'ACTIVE','NOT_CREATED',?,?)",
                    (
                        "child_" + secrets.token_urlsafe(18), int(account_id),
                        generation["slot_id"], generation["id"],
                        generation["slot_number"], generation["generation"],
                        int(source_alias_id), child_username, source_contract_hash,
                        timestamp, timestamp,
                    ),
                ).lastrowid
                outbox_id = self._conn.execute(
                    "INSERT INTO mgboost_outbox "
                    "(operation_id,account_id,child_intent_id,operation_kind,state,"
                    "idempotency_key_hash,request_hash,payload_json,next_attempt_at,"
                    "created_at,updated_at) "
                    "VALUES (?,?,?,'CHILD_USER_ENSURE','PENDING',?,?,?,?,?,?)",
                    (
                        operation_id, int(account_id), child_id, idem_hash,
                        request_hash, payload_json, timestamp, timestamp, timestamp,
                    ),
                ).lastrowid
                row = self._conn.execute(
                    "SELECT o.*,c.child_username FROM mgboost_outbox o "
                    "JOIN mgboost_child_user_intents c ON c.id=o.child_intent_id "
                    "WHERE o.id=?", (outbox_id,),
                ).fetchone()
                self._conn.commit()
                return dict(row)
            except Exception:
                self._conn.rollback()
                raise

    def claim(self, operation_id: str, *, worker_id: str, now: int, lease_seconds: int = 30) -> dict | None:
        if not isinstance(worker_id, str) or not 3 <= len(worker_id) <= 128:
            raise ChildProvisioningError("invalid worker identity")
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                row = self._conn.execute(
                    "SELECT * FROM mgboost_outbox WHERE operation_id=?", (operation_id,)
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
                    "UPDATE mgboost_outbox SET state='IN_FLIGHT',attempts=?,lease_owner=?,"
                    "lease_expires_at=?,updated_at=?,row_version=row_version+1 WHERE id=?",
                    (attempt, worker_id, now + max(5, int(lease_seconds)), now, row["id"]),
                )
                self._conn.execute(
                    "INSERT INTO mgboost_outbox_attempt_events "
                    "(outbox_id,account_id,attempt_no,event_type,created_at) "
                    "VALUES (?,?,?,'STARTED',?)",
                    (row["id"], row["account_id"], attempt, now),
                )
                claimed = self._conn.execute(
                    "SELECT * FROM mgboost_outbox WHERE id=?", (row["id"],)
                ).fetchone()
                self._conn.commit()
                result = dict(claimed)
                result["payload"] = json.loads(result.pop("payload_json"))
                return result
            except Exception:
                self._conn.rollback()
                raise

    def retry(self, operation_id: str, *, worker_id: str, error_class: str, now: int, delay: int = 5) -> None:
        safe_error = (error_class or "").strip()
        if not safe_error or len(safe_error) > 128:
            raise ChildProvisioningError("safe error class is required")
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                row = self._conn.execute(
                    "SELECT * FROM mgboost_outbox WHERE operation_id=? AND state='IN_FLIGHT' "
                    "AND lease_owner=?", (operation_id, worker_id),
                ).fetchone()
                if not row:
                    raise ChildProvisioningConflict("outbox lease is not owned by worker")
                self._conn.execute(
                    "UPDATE mgboost_outbox SET state='RETRY',next_attempt_at=?,lease_owner=NULL,"
                    "lease_expires_at=NULL,last_error_class=?,updated_at=?,"
                    "row_version=row_version+1 WHERE id=?",
                    (now + max(1, int(delay)), safe_error, now, row["id"]),
                )
                self._conn.execute(
                    "INSERT INTO mgboost_outbox_attempt_events "
                    "(outbox_id,account_id,attempt_no,event_type,safe_error_class,created_at) "
                    "VALUES (?,?,?,'FAILED',?,?)",
                    (row["id"], row["account_id"], row["attempts"], safe_error, now),
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def acknowledge(
        self, operation_id: str, *, worker_id: str, outcome: str,
        child_uuid: str, child_shadowsocks_password: str | None,
        remote_result: dict, now: int,
    ) -> dict:
        if outcome not in {"CREATED", "EXISTING"}:
            raise ChildProvisioningError("invalid broker ensure outcome")
        try:
            normalized_uuid = str(uuid.UUID(child_uuid)).lower()
        except (ValueError, TypeError, AttributeError) as exc:
            raise ChildProvisioningError("invalid child UUID") from exc
        uuid_verifier = "sha256:" + _sha(normalized_uuid)
        uuid_masked = "uuid_" + _sha("mask\0" + normalized_uuid)[:8]
        protocols = remote_result.get("protocols") if isinstance(remote_result, dict) else None
        if protocols == ["shadowsocks", "vless"]:
            if not isinstance(child_shadowsocks_password, str) or not child_shadowsocks_password:
                raise ChildProvisioningError("child Shadowsocks credential is required")
            shadowsocks_verifier = "sha256:" + _sha(child_shadowsocks_password)
            shadowsocks_masked = "ss_" + _sha(
                "mask\0" + child_shadowsocks_password
            )[:10]
        elif protocols == ["vless"] and child_shadowsocks_password is None:
            shadowsocks_verifier = shadowsocks_masked = None
        else:
            raise ChildProvisioningError("unexpected child protocol credentials")
        if any(key in remote_result for key in ("uuid", "shadowsocks_password")):
            raise ChildProvisioningError("raw credentials must be stripped before ACK")
        remote_verifier = _sha(_canonical(remote_result))
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                row = self._conn.execute(
                    "SELECT * FROM mgboost_outbox WHERE operation_id=? AND state='IN_FLIGHT' "
                    "AND lease_owner=?", (operation_id, worker_id),
                ).fetchone()
                if not row:
                    raise ChildProvisioningConflict("outbox lease is not owned by worker")
                child = self._conn.execute(
                    "SELECT uuid_verifier FROM mgboost_child_user_intents WHERE id=?",
                    (row["child_intent_id"],),
                ).fetchone()
                if child["uuid_verifier"] not in (None, uuid_verifier):
                    raise ChildProvisioningConflict("remote child UUID changed")
                self._conn.execute(
                    "UPDATE mgboost_child_user_intents SET observed_state='ACTIVE',"
                    "uuid_verifier=?,uuid_masked=?,shadowsocks_verifier=?,"
                    "shadowsocks_masked=?,updated_at=?,row_version=row_version+1 "
                    "WHERE id=?",
                    (
                        uuid_verifier, uuid_masked, shadowsocks_verifier,
                        shadowsocks_masked, now, row["child_intent_id"],
                    ),
                )
                self._conn.execute(
                    "UPDATE mgboost_outbox SET state='APPLIED',lease_owner=NULL,"
                    "lease_expires_at=NULL,last_error_class=NULL,updated_at=?,"
                    "row_version=row_version+1 WHERE id=?",
                    (now, row["id"]),
                )
                self._conn.execute(
                    "INSERT INTO mgboost_outbox_attempt_events "
                    "(outbox_id,account_id,attempt_no,event_type,outcome,"
                    "remote_effect_verifier,created_at) VALUES (?,?,?,?,?,?,?)",
                    (
                        row["id"], row["account_id"], row["attempts"],
                        "RECONCILED" if outcome == "EXISTING" else "SUCCEEDED",
                        outcome, remote_verifier, now,
                    ),
                )
                result = self._conn.execute(
                    "SELECT * FROM mgboost_child_user_intents WHERE id=?",
                    (row["child_intent_id"],),
                ).fetchone()
                self._conn.commit()
                return dict(result)
            except Exception:
                self._conn.rollback()
                raise

    def record_reconciliation_error(
        self, operation_id: str, *, error_class: str, now: int
    ) -> None:
        safe_error = (error_class or "").strip()
        if not safe_error or len(safe_error) > 128:
            raise ChildProvisioningError("safe reconciliation error is required")
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                row = self._conn.execute(
                    "SELECT * FROM mgboost_outbox WHERE operation_id=? AND state='APPLIED'",
                    (operation_id,),
                ).fetchone()
                if not row:
                    raise ChildProvisioningConflict("applied operation is required")
                self._conn.execute(
                    "UPDATE mgboost_child_user_intents SET observed_state='ERROR',"
                    "updated_at=?,row_version=row_version+1 WHERE id=?",
                    (now, row["child_intent_id"]),
                )
                self._conn.execute(
                    "UPDATE mgboost_outbox SET state='ERROR',last_error_class=?,"
                    "updated_at=?,row_version=row_version+1 WHERE id=?",
                    (safe_error, now, row["id"]),
                )
                self._conn.execute(
                    "INSERT INTO mgboost_outbox_attempt_events "
                    "(outbox_id,account_id,attempt_no,event_type,safe_error_class,created_at) "
                    "VALUES (?,?,?,'FAILED',?,?)",
                    (row["id"], row["account_id"], row["attempts"], safe_error, now),
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
