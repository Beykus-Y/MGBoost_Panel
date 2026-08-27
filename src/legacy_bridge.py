"""Durable PH4-01 legacy subscription alias bridge repository.

Dormant: no legacy route imports this module. `resolve_account_for_legacy_username`
is the only read the (also dormant) bridge resolver ever calls; everything
else here is administrative (root-only, `PrimaryAdminAuthority`-gated,
mirroring `InternalEntitlementStore`/PH3-03's shadow-binding tool exactly).

The staged-rollout gate this module IS: no account is ever bridged unless an
explicit `enabled=1` binding row already exists for it, created ahead of
time by the primary admin. This is independent of and in addition to
`OPAQUE_SUBSCRIPTION_ENABLED`-style flags -- PH4-03 will later flip specific
canary accounts on through `enable()`, never a global switch.
"""

from __future__ import annotations

import sqlite3
import time

from .admin_authority import PrimaryAdminAuthorizationError


class LegacyBridgeError(RuntimeError):
    pass


class LegacyBridgeConflict(LegacyBridgeError):
    pass


class PrimaryAdminRequired(LegacyBridgeError):
    pass


class LegacyBridgeStore:
    def __init__(self, connection: sqlite3.Connection, lock, primary_admin_authority):
        self._conn = connection
        self._lock = lock
        self._authority = primary_admin_authority

    def _require_primary(self, capability) -> str:
        try:
            return self._authority.require(capability)
        except PrimaryAdminAuthorizationError:
            raise PrimaryAdminRequired("primary MGBoost admin capability required")

    def create_binding(
        self, *, capability, account_id: int, legacy_alias_id: int, enabled: bool,
        decision_ref: str, now: int | None = None,
    ) -> dict:
        actor = self._require_primary(capability)
        decision_ref = (decision_ref or "").strip()
        if not 3 <= len(decision_ref) <= 128:
            raise LegacyBridgeError("a bounded decision reference is required")
        timestamp = int(time.time()) if now is None else int(now)
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                account_id = int(account_id)
                alias = self._conn.execute(
                    "SELECT id FROM mgboost_legacy_account_aliases "
                    "WHERE id=? AND account_id=?", (int(legacy_alias_id), account_id),
                ).fetchone()
                if not alias:
                    raise LegacyBridgeError("legacy alias does not belong to this account")
                existing = self._conn.execute(
                    "SELECT id FROM mgboost_legacy_bridge_bindings WHERE account_id=?",
                    (account_id,),
                ).fetchone()
                if existing:
                    raise LegacyBridgeConflict(
                        "a bridge binding already exists for this account -- use "
                        "enable()/disable(), not a second create_binding() call"
                    )
                cursor = self._conn.execute(
                    "INSERT INTO mgboost_legacy_bridge_bindings "
                    "(account_id,legacy_alias_id,enabled,decision_ref,created_by_actor,"
                    "created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                    (account_id, int(legacy_alias_id), 1 if enabled else 0, decision_ref,
                     actor, timestamp, timestamp),
                )
                binding_id = cursor.lastrowid
                self._event(
                    binding_id, account_id, "CREATED", actor_ref=actor,
                    reason=decision_ref, now=timestamp,
                )
                if enabled:
                    self._event(binding_id, account_id, "ENABLED", actor_ref=actor,
                                reason=decision_ref, now=timestamp)
                row = self._conn.execute(
                    "SELECT * FROM mgboost_legacy_bridge_bindings WHERE id=?", (binding_id,),
                ).fetchone()
                self._conn.commit()
                return dict(row)
            except Exception:
                self._conn.rollback()
                raise

    def _set_enabled(self, *, capability, account_id, enabled: bool, reason: str, now):
        actor = self._require_primary(capability)
        reason = (reason or "").strip()
        if not 3 <= len(reason) <= 128:
            raise LegacyBridgeError("a bounded reason is required")
        timestamp = int(time.time()) if now is None else int(now)
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                row = self._conn.execute(
                    "SELECT * FROM mgboost_legacy_bridge_bindings WHERE account_id=?",
                    (int(account_id),),
                ).fetchone()
                if not row:
                    raise LegacyBridgeError("no bridge binding exists for this account")
                if bool(row["enabled"]) == enabled:
                    self._conn.commit()
                    return dict(row)
                self._conn.execute(
                    "UPDATE mgboost_legacy_bridge_bindings SET enabled=?,updated_at=?,"
                    "row_version=row_version+1 WHERE id=?",
                    (1 if enabled else 0, timestamp, row["id"]),
                )
                self._event(
                    row["id"], row["account_id"], "ENABLED" if enabled else "DISABLED",
                    actor_ref=actor, reason=reason, now=timestamp,
                )
                result = self._conn.execute(
                    "SELECT * FROM mgboost_legacy_bridge_bindings WHERE id=?", (row["id"],),
                ).fetchone()
                self._conn.commit()
                return dict(result)
            except Exception:
                self._conn.rollback()
                raise

    def enable(self, *, capability, account_id: int, reason: str, now: int | None = None) -> dict:
        return self._set_enabled(capability=capability, account_id=account_id, enabled=True,
                                  reason=reason, now=now)

    def disable(self, *, capability, account_id: int, reason: str, now: int | None = None) -> dict:
        return self._set_enabled(capability=capability, account_id=account_id, enabled=False,
                                  reason=reason, now=now)

    def _event(self, binding_id, account_id, event_type, *, actor_ref, reason, now):
        self._conn.execute(
            "INSERT INTO mgboost_legacy_bridge_binding_events "
            "(binding_id,account_id,event_type,actor_ref,reason,created_at) VALUES (?,?,?,?,?,?)",
            (binding_id, account_id, event_type, actor_ref, reason, now),
        )

    # --- read-only resolution (the only method the dormant bridge resolver calls) ---

    def resolve_account_for_legacy_username(self, legacy_username: str) -> int | None:
        """Deterministic, evidence-based only: an exact match against an
        already-reviewed immutable alias, AND an explicit enabled=1 binding.
        Never infers from username shape/prefix/similarity. Ambiguous or
        missing mapping (no alias, no binding, or a disabled binding) all
        return None -- the caller must fall through to the unmodified legacy
        response, never guess or auto-create.

        DL-057: if the resolved account was absorbed by an ACTIVE PH7-13
        merge, this returns the survivor's id instead -- the immutable
        alias/binding rows keep pointing at the absorbed account exactly as
        they always have, but any *new* device/child operation must land on
        the canonical survivor. A real device reconnecting on the absorbed
        legacy username transparently migrates onto the survivor's slot
        pool, never onto the closed account."""
        if not isinstance(legacy_username, str) or not legacy_username:
            return None
        row = self._conn.execute(
            "SELECT b.account_id FROM mgboost_legacy_account_aliases AS a "
            "JOIN mgboost_legacy_bridge_bindings AS b "
            "  ON b.account_id=a.account_id AND b.legacy_alias_id=a.id "
            "WHERE a.legacy_username=? AND b.enabled=1",
            (legacy_username,),
        ).fetchone()
        if row is None:
            return None
        account_id = row["account_id"]
        merge_row = self._conn.execute(
            "SELECT survivor_account_id FROM mgboost_account_merges "
            "WHERE absorbed_account_id=? AND status='ACTIVE'",
            (account_id,),
        ).fetchone()
        return merge_row["survivor_account_id"] if merge_row else account_id
