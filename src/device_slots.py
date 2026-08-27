"""Transactional PH3-02 device slots.

This repository is dormant: no legacy route imports or calls it. SQLite write
transactions and constraints, not the process-local connection lock, are the
capacity and generation correctness boundary.
"""

from __future__ import annotations

import hashlib
import hmac
import sqlite3
import time


TECHNICAL_SLOT_CAP = 99
# 3/6/12 are the catalog baseline (OPD-06). 4/8 are individually-reviewed
# PH4-03 legacy-compat values (owner decision 2026-08-26, mass-migration
# device-policy review) -- never inferred, only ever assigned per-account
# through `legacy_paid_compat.ensure_legacy_paid_compat_entitlement()`'s
# explicit, evidenced `approved_extra_device_slots` path. Extending this
# set was anticipated when PH4-03's `LEGACY_PAID_COMPAT_V1_D{n}` naming
# scheme was designed -- a plain code constant, not schema-locked.
PAID_BASELINE_LIMITS = frozenset({3, 4, 6, 8, 12})


class DeviceSlotError(RuntimeError):
    pass


class InvalidHWID(DeviceSlotError):
    pass


class EntitlementUnavailable(DeviceSlotError):
    pass


class CapacityReached(DeviceSlotError):
    pass


class CapacityConflict(DeviceSlotError):
    def __init__(self, active_count: int, effective_limit: int):
        self.active_count = active_count
        self.effective_limit = effective_limit
        super().__init__("active device count exceeds current entitlement")


class CrossAccountHWID(DeviceSlotError):
    pass


class StaleSlotGeneration(DeviceSlotError):
    pass


def privacy_safe_hwid(raw_hwid: str, hmac_key: bytes | str) -> tuple[str, str]:
    """Return stable keyed verifier and non-reversible short display mask."""
    if not isinstance(raw_hwid, str):
        raise InvalidHWID("HWID must be text")
    canonical = raw_hwid.strip()
    if not canonical or len(canonical) > 512:
        raise InvalidHWID("HWID length is invalid")
    key = hmac_key.encode("utf-8") if isinstance(hmac_key, str) else hmac_key
    if not isinstance(key, bytes) or len(key) < 32:
        raise InvalidHWID("HWID verifier key must contain at least 32 bytes")
    digest = hmac.new(key, canonical.encode("utf-8"), hashlib.sha256).hexdigest()
    return "hmac-sha256:" + digest, "hwid_" + digest[:12]


class DeviceSlotStore:
    def __init__(self, connection: sqlite3.Connection, lock):
        self._conn = connection
        self._lock = lock

    @staticmethod
    def _slot_result(slot, generation, *, result: str) -> dict:
        return {
            "result": result,
            "slot_id": slot["id"],
            "slot_number": slot["slot_number"],
            "slot_kind": slot["slot_kind"],
            "generation": generation["generation"],
            "generation_id": generation["id"],
            "hwid_masked": generation["hwid_masked"],
            "desired_state": slot["desired_state"],
            "observed_state": slot["observed_state"],
        }

    def _entitlement_capacity(self, account_id: int, now: int) -> dict:
        row = self._conn.execute(
            "SELECT a.account_source,a.status AS account_status,"
            "s.id AS subscription_id,s.status AS subscription_status,s.current_expiry,"
            "p.plan_kind,p.device_limit_mode,p.device_limit "
            "FROM mgboost_accounts AS a "
            "JOIN mgboost_subscriptions AS s ON s.account_id=a.id "
            "JOIN mgboost_plan_versions AS p ON p.id=s.current_plan_version_id "
            "WHERE a.id=? AND s.status IN ('ACTIVE','UNLIMITED')",
            (account_id,),
        ).fetchone()
        if not row or row["account_status"] != "ACTIVE":
            raise EntitlementUnavailable("active account entitlement is required")
        if (row["subscription_status"] != "UNLIMITED"
                and row["current_expiry"] is not None
                and int(row["current_expiry"]) <= now):
            raise EntitlementUnavailable("subscription is expired")

        source = row["account_source"]
        if source == "INTERNAL":
            if row["plan_kind"] != "INTERNAL":
                raise EntitlementUnavailable("internal account requires internal plan")
        elif source == "DIRECT":
            if row["plan_kind"] != "COMMERCIAL":
                raise EntitlementUnavailable("direct account requires commercial plan")
        else:
            raise EntitlementUnavailable("legacy account requires reviewed entitlement")

        mode = row["device_limit_mode"]
        limit = row["device_limit"]
        if source == "DIRECT":
            # UNLIMITED is never a catalog/self-service option for a DIRECT
            # plan -- every plan_version is immutable and only ever created
            # through a capability-gated, audited path (e.g.
            # `legacy_paid_compat.ensure_legacy_paid_compat_entitlement(
            # device_limit_exempt=True)`), never chosen by the customer.
            # Owner decision 2026-08-26: an individually-reviewed legacy
            # account may be granted this exact exemption.
            if mode == "UNLIMITED":
                if limit is not None:
                    raise EntitlementUnavailable("commercial unlimited plan must not carry a limit")
            elif mode != "LIMITED" or limit not in PAID_BASELINE_LIMITS:
                raise EntitlementUnavailable("commercial device baseline is not approved")
        elif mode == "LIMITED":
            if limit is None or not 1 <= int(limit) <= TECHNICAL_SLOT_CAP:
                raise EntitlementUnavailable("internal device limit is invalid")
        elif mode != "UNLIMITED":
            raise EntitlementUnavailable("internal device limit mode is invalid")

        override = self._conn.execute(
            "SELECT value_type,integer_value FROM mgboost_entitlement_overrides "
            "WHERE account_id=? AND entitlement_key='DEVICE_LIMIT' "
            "AND revoked_at IS NULL AND starts_at<=? AND expires_at>? "
            "AND (subscription_id IS NULL OR subscription_id=?) "
            "ORDER BY starts_at DESC,id DESC LIMIT 1",
            (account_id, now, now, row["subscription_id"]),
        ).fetchone()
        if override:
            if override["value_type"] == "UNLIMITED":
                if source != "INTERNAL":
                    raise EntitlementUnavailable("commercial unlimited override is disabled")
                mode, limit = "UNLIMITED", None
            elif override["value_type"] == "INTEGER":
                proposed = int(override["integer_value"])
                if not 1 <= proposed <= TECHNICAL_SLOT_CAP:
                    raise EntitlementUnavailable("device override exceeds technical cap")
                if source == "DIRECT" and proposed > int(limit):
                    raise EntitlementUnavailable(
                        "commercial capacity increase is not enabled in PH3-02"
                    )
                mode, limit = "LIMITED", proposed
            else:
                raise EntitlementUnavailable("device override type is invalid")

        active_count = int(self._conn.execute(
            "SELECT COUNT(*) FROM mgboost_device_slot_generations "
            "WHERE account_id=? AND status='ACTIVE'",
            (account_id,),
        ).fetchone()[0])
        effective_limit = TECHNICAL_SLOT_CAP if mode == "UNLIMITED" else int(limit)
        return {
            "account_source": source,
            "subscription_id": row["subscription_id"],
            "limit_mode": mode,
            "entitled_limit": None if mode == "UNLIMITED" else int(limit),
            "technical_limit": TECHNICAL_SLOT_CAP,
            "effective_limit": effective_limit,
            "active_count": active_count,
            "conflict": active_count > effective_limit,
            "overage": max(0, active_count - effective_limit),
        }

    def get_capacity_state(self, account_id: int, *, now: int | None = None) -> dict:
        timestamp = int(time.time()) if now is None else int(now)
        with self._lock:
            try:
                self._conn.execute("BEGIN")
                result = self._entitlement_capacity(int(account_id), timestamp)
                self._conn.commit()
                return result
            except Exception:
                self._conn.rollback()
                raise

    def claim(
        self,
        account_id: int,
        raw_hwid: str,
        hmac_key: bytes | str,
        *,
        verifier_version: int = 1,
        now: int | None = None,
    ) -> dict:
        verifier, masked = privacy_safe_hwid(raw_hwid, hmac_key)
        if (isinstance(verifier_version, bool) or not isinstance(verifier_version, int)
                or not 1 <= verifier_version <= 1_000_000):
            raise InvalidHWID("HWID verifier version is invalid")
        timestamp = int(time.time()) if now is None else int(now)
        account_id = int(account_id)
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                existing = self._conn.execute(
                    "SELECT g.*,s.slot_kind,s.current_generation,s.desired_state,"
                    "s.observed_state,s.updated_at,s.row_version "
                    "FROM mgboost_device_slot_generations AS g "
                    "JOIN mgboost_device_slots AS s ON s.id=g.slot_id "
                    "WHERE g.hwid_verifier_version=? AND g.hwid_verifier=? "
                    "AND g.status='ACTIVE'",
                    (verifier_version, verifier),
                ).fetchone()
                if existing:
                    if existing["account_id"] != account_id:
                        raise CrossAccountHWID(
                            "active HWID verifier belongs to another account"
                        )
                    slot = {
                        "id": existing["slot_id"],
                        "slot_number": existing["slot_number"],
                        "slot_kind": existing["slot_kind"],
                        "desired_state": existing["desired_state"],
                        "observed_state": existing["observed_state"],
                    }
                    self._conn.commit()
                    return self._slot_result(slot, existing, result="EXISTING")

                capacity = self._entitlement_capacity(account_id, timestamp)
                if capacity["conflict"]:
                    raise CapacityConflict(
                        capacity["active_count"], capacity["effective_limit"]
                    )
                if capacity["active_count"] >= capacity["effective_limit"]:
                    raise CapacityReached("device slot capacity reached")

                slot = self._conn.execute(
                    "SELECT * FROM mgboost_device_slots "
                    "WHERE account_id=? AND desired_state='FREE' "
                    "ORDER BY slot_number LIMIT 1",
                    (account_id,),
                ).fetchone()
                if slot is None:
                    used_numbers = {
                        int(row[0]) for row in self._conn.execute(
                            "SELECT slot_number FROM mgboost_device_slots WHERE account_id=?",
                            (account_id,),
                        )
                    }
                    slot_number = next(
                        (number for number in range(1, TECHNICAL_SLOT_CAP + 1)
                         if number not in used_numbers),
                        None,
                    )
                    if slot_number is None:
                        raise CapacityReached("technical device slot cap reached")
                    slot_kind = (
                        "INTERNAL" if capacity["account_source"] == "INTERNAL" else "BASE"
                    )
                    cursor = self._conn.execute(
                        "INSERT INTO mgboost_device_slots "
                        "(account_id,slot_number,slot_kind,current_generation,"
                        "desired_state,observed_state,created_at,updated_at) "
                        "VALUES (?,?,?,0,'FREE','FREE',?,?)",
                        (account_id, slot_number, slot_kind, timestamp, timestamp),
                    )
                    slot = self._conn.execute(
                        "SELECT * FROM mgboost_device_slots WHERE id=?",
                        (cursor.lastrowid,),
                    ).fetchone()

                next_generation = int(slot["current_generation"]) + 1
                generation_cursor = self._conn.execute(
                    "INSERT INTO mgboost_device_slot_generations "
                    "(account_id,slot_id,slot_number,generation,hwid_verifier_version,"
                    "hwid_verifier,hwid_masked,status,claimed_at) "
                    "VALUES (?,?,?,?,?,?,?,'ACTIVE',?)",
                    (
                        account_id, slot["id"], slot["slot_number"], next_generation,
                        verifier_version, verifier, masked, timestamp,
                    ),
                )
                updated = self._conn.execute(
                    "UPDATE mgboost_device_slots SET current_generation=?,"
                    "desired_state='ACTIVE',observed_state='ACTIVE',updated_at=?,"
                    "row_version=row_version+1 "
                    "WHERE id=? AND account_id=? AND desired_state='FREE' "
                    "AND current_generation=?",
                    (
                        next_generation, timestamp, slot["id"], account_id,
                        slot["current_generation"],
                    ),
                )
                if updated.rowcount != 1:
                    raise RuntimeError("slot generation compare-and-set failed")
                slot = self._conn.execute(
                    "SELECT * FROM mgboost_device_slots WHERE id=?", (slot["id"],)
                ).fetchone()
                generation = self._conn.execute(
                    "SELECT * FROM mgboost_device_slot_generations WHERE id=?",
                    (generation_cursor.lastrowid,),
                ).fetchone()
                self._conn.commit()
                return self._slot_result(slot, generation, result="CLAIMED")
            except Exception:
                self._conn.rollback()
                raise

    def rebind(
        self,
        account_id: int,
        slot_id: int,
        expected_generation: int,
        new_raw_hwid: str,
        hmac_key: bytes | str,
        *,
        reason: str,
        verifier_version: int = 1,
        now: int | None = None,
    ) -> dict:
        """Atomically release the current ACTIVE generation of one specific
        slot and claim the next generation on that exact same slot for a new
        HWID -- never a different/newly-picked slot. Combining release+claim
        in a single transaction avoids a TOCTOU race where an unrelated
        concurrent claim() could grab the slot the instant it is freed.

        The caller (PH3-05 lifecycle repository) must only call this after
        the old remote child credential is durably confirmed revoked; this
        method itself does not touch Marzban.
        """
        reason = str(reason or "").strip()
        if not reason or len(reason) > 500:
            raise DeviceSlotError("rebind reason is required and bounded")
        verifier, masked = privacy_safe_hwid(new_raw_hwid, hmac_key)
        if (isinstance(verifier_version, bool) or not isinstance(verifier_version, int)
                or not 1 <= verifier_version <= 1_000_000):
            raise InvalidHWID("HWID verifier version is invalid")
        timestamp = int(time.time()) if now is None else int(now)
        account_id = int(account_id)
        slot_id = int(slot_id)
        expected_generation = int(expected_generation)
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                slot = self._conn.execute(
                    "SELECT * FROM mgboost_device_slots WHERE id=? AND account_id=?",
                    (slot_id, account_id),
                ).fetchone()
                if not slot:
                    raise StaleSlotGeneration("slot does not belong to account")

                # Idempotent replay: the new generation was already created by
                # a prior attempt for this exact new HWID -- converge, do not
                # create a second (X+2) generation.
                existing_next = self._conn.execute(
                    "SELECT g.*,s.slot_kind,s.desired_state,s.observed_state "
                    "FROM mgboost_device_slot_generations AS g "
                    "JOIN mgboost_device_slots AS s ON s.id=g.slot_id "
                    "WHERE g.slot_id=? AND g.generation=? AND g.status='ACTIVE'",
                    (slot_id, expected_generation + 1),
                ).fetchone()
                if existing_next:
                    if (
                        existing_next["hwid_verifier_version"] != verifier_version
                        or existing_next["hwid_verifier"] != verifier
                    ):
                        raise StaleSlotGeneration(
                            "next generation already exists for a different HWID"
                        )
                    self._conn.commit()
                    return self._slot_result(
                        {
                            "id": existing_next["slot_id"], "slot_number": existing_next["slot_number"],
                            "slot_kind": existing_next["slot_kind"],
                            "desired_state": existing_next["desired_state"],
                            "observed_state": existing_next["observed_state"],
                        },
                        existing_next, result="EXISTING",
                    )

                if int(slot["current_generation"]) != expected_generation:
                    raise StaleSlotGeneration("slot generation changed")
                old_generation = self._conn.execute(
                    "SELECT * FROM mgboost_device_slot_generations "
                    "WHERE slot_id=? AND account_id=? AND generation=? AND status='ACTIVE'",
                    (slot_id, account_id, expected_generation),
                ).fetchone()
                if not old_generation:
                    raise StaleSlotGeneration("active slot generation changed")

                # Cross-account / cross-slot HWID isolation, same rule claim() enforces.
                active_verifier_row = self._conn.execute(
                    "SELECT account_id,slot_id FROM mgboost_device_slot_generations "
                    "WHERE hwid_verifier_version=? AND hwid_verifier=? AND status='ACTIVE'",
                    (verifier_version, verifier),
                ).fetchone()
                if active_verifier_row:
                    if active_verifier_row["account_id"] != account_id:
                        raise CrossAccountHWID(
                            "active HWID verifier belongs to another account"
                        )
                    if active_verifier_row["slot_id"] != slot_id:
                        raise DeviceSlotError(
                            "HWID is already active on a different slot of this account"
                        )

                updated = self._conn.execute(
                    "UPDATE mgboost_device_slot_generations "
                    "SET status='RELEASED',ended_at=?,end_reason=? "
                    "WHERE id=? AND status='ACTIVE'",
                    (timestamp, reason, old_generation["id"]),
                )
                if updated.rowcount != 1:
                    raise StaleSlotGeneration("active slot generation changed")

                next_generation = expected_generation + 1
                generation_cursor = self._conn.execute(
                    "INSERT INTO mgboost_device_slot_generations "
                    "(account_id,slot_id,slot_number,generation,hwid_verifier_version,"
                    "hwid_verifier,hwid_masked,status,claimed_at) "
                    "VALUES (?,?,?,?,?,?,?,'ACTIVE',?)",
                    (
                        account_id, slot_id, slot["slot_number"], next_generation,
                        verifier_version, verifier, masked, timestamp,
                    ),
                )
                updated = self._conn.execute(
                    "UPDATE mgboost_device_slots SET current_generation=?,"
                    "desired_state='ACTIVE',observed_state='ACTIVE',updated_at=?,"
                    "row_version=row_version+1 "
                    "WHERE id=? AND account_id=? AND current_generation=?",
                    (next_generation, timestamp, slot_id, account_id, expected_generation),
                )
                if updated.rowcount != 1:
                    raise RuntimeError("slot generation compare-and-set failed")
                slot = self._conn.execute(
                    "SELECT * FROM mgboost_device_slots WHERE id=?", (slot_id,)
                ).fetchone()
                generation = self._conn.execute(
                    "SELECT * FROM mgboost_device_slot_generations WHERE id=?",
                    (generation_cursor.lastrowid,),
                ).fetchone()
                self._conn.commit()
                return self._slot_result(slot, generation, result="REBOUND")
            except Exception:
                self._conn.rollback()
                raise

    def release(
        self,
        account_id: int,
        slot_id: int,
        expected_generation: int,
        *,
        reason: str,
        now: int | None = None,
    ) -> dict:
        reason = str(reason or "").strip()
        if not reason or len(reason) > 500:
            raise DeviceSlotError("release reason is required and bounded")
        timestamp = int(time.time()) if now is None else int(now)
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                slot = self._conn.execute(
                    "SELECT * FROM mgboost_device_slots WHERE id=? AND account_id=?",
                    (int(slot_id), int(account_id)),
                ).fetchone()
                if not slot:
                    raise StaleSlotGeneration("slot does not belong to account")
                generation = self._conn.execute(
                    "SELECT * FROM mgboost_device_slot_generations "
                    "WHERE slot_id=? AND account_id=? AND status='ACTIVE'",
                    (int(slot_id), int(account_id)),
                ).fetchone()
                if not generation or generation["generation"] != int(expected_generation):
                    raise StaleSlotGeneration("active slot generation changed")
                updated = self._conn.execute(
                    "UPDATE mgboost_device_slot_generations "
                    "SET status='RELEASED',ended_at=?,end_reason=? "
                    "WHERE id=? AND status='ACTIVE' AND generation=?",
                    (timestamp, reason, generation["id"], int(expected_generation)),
                )
                if updated.rowcount != 1:
                    raise StaleSlotGeneration("active slot generation changed")
                updated = self._conn.execute(
                    "UPDATE mgboost_device_slots SET desired_state='FREE',"
                    "observed_state='FREE',updated_at=?,row_version=row_version+1 "
                    "WHERE id=? AND account_id=? AND desired_state IN ('ACTIVE','DISABLED') "
                    "AND current_generation=?",
                    (timestamp, int(slot_id), int(account_id), int(expected_generation)),
                )
                if updated.rowcount != 1:
                    raise StaleSlotGeneration("slot state changed")
                self._conn.commit()
                return {
                    "slot_id": int(slot_id),
                    "slot_number": slot["slot_number"],
                    "released_generation": int(expected_generation),
                    "desired_state": "FREE",
                }
            except Exception:
                self._conn.rollback()
                raise

    def list_for_account(self, account_id: int) -> list[dict]:
        """Return account-scoped safe metadata; never expose verifier values."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT s.id,s.slot_number,s.slot_kind,s.current_generation,"
                "s.desired_state,s.observed_state,g.hwid_masked,g.status "
                "FROM mgboost_device_slots AS s "
                "LEFT JOIN mgboost_device_slot_generations AS g "
                "ON g.slot_id=s.id AND g.status='ACTIVE' "
                "WHERE s.account_id=? ORDER BY s.slot_number",
                (int(account_id),),
            ).fetchall()
        return [dict(row) for row in rows]
