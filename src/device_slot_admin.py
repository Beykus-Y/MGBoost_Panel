"""PH7-05 reversible slot pause (admin Disable/Enable) -- the previously
missing standalone backend primitive.

Semantics, exactly as scoped by ADMIN-UX-02 plus the owner instruction of
2026-08-27 (preview + mandatory reason + confirmation; audit evidence):

  * Disable is a REVERSIBLE PAUSE of a slot's CURRENT generation. The slot
    stays occupied (capacity accounting unchanged), generation/UUID/HWID
    history is never touched, no lifecycle row becomes terminal.
  * The local truth is the schema-blessed ``mgboost_device_slots
    .desired_state='DISABLED'`` value; ``DeviceSlotAdminStore`` is the ONLY
    writer of that transition (Enable writes it back to 'ACTIVE').
  * The remote effect is NOT invented here: pausing bumps the account's
    PH3-08 desired-state revision in the same transaction, and convergence
    happens through the existing durable revision-stamped parent-sync outbox
    (`child.user.state.sync` PUTs {status:"disabled"} and asserts UUID
    stability). Because `enqueue_current_children` derives each child's
    target from its own slot row inside every enqueue, no later renewal,
    expiry change or parent transition can resurrect a paused device, and
    stale ENABLE/DISABLE sync ops die through the standard supersede path.
  * Enable returns THE SAME generation/child/UUID to service, narrowed by
    the parent's own state (an expired subscription keeps the child off).
  * Every successful toggle appends an immutable evidence row to the
    EXISTING PH3-09/PH7-08 ledger (`mgboost_entitlement_mutations`, free-text
    operation, mutation_source='ADMIN') carrying actor/reason/before-after,
    so the already-deployed Audit timeline shows it without any second
    audit framework.

This store never talks to Marzban: the caller drives
`parent_sync.run_account_sync_cycle` after commit and reports convergence.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time

_NAMESPACE = "ph7-05-slot-pause-v1\0"


class SlotAdminError(RuntimeError):
    pass


class SlotAdminConflict(SlotAdminError):
    pass


def _idempotency_hash(idempotency_key: str) -> str:
    key = idempotency_key if isinstance(idempotency_key, str) else ""
    if not 16 <= len(key) <= 512:
        raise SlotAdminError("idempotency_key must be a string of 16..512 characters")
    return hashlib.sha256((_NAMESPACE + key).encode("utf-8")).hexdigest()


def _clean_reason(reason) -> str:
    text = (reason or "").strip()
    if not 3 <= len(text) <= 300:
        raise SlotAdminError("a bounded human-readable reason (3..300) is required")
    return text


class DeviceSlotAdminStore:
    def __init__(self, connection: sqlite3.Connection, lock, primary_admin_authority):
        self._conn = connection
        self._lock = lock
        self._authority = primary_admin_authority

    # --- read model used by routes/read-models -------------------------------

    def current_pause_state(self, account_id: int, slot_number: int) -> dict | None:
        """Safe per-slot pause projection for route bodies: whether the slot's
        CURRENT active generation is administratively paused."""
        with self._lock:
            row = self._conn.execute(
                "SELECT s.desired_state,s.observed_state,g.id AS generation_row_id,"
                "g.generation,c.desired_state AS child_desired "
                "FROM mgboost_device_slots s "
                "JOIN mgboost_device_slot_generations g "
                "ON g.slot_id=s.id AND g.account_id=s.account_id AND g.status='ACTIVE' "
                "LEFT JOIN mgboost_child_user_intents c ON c.slot_generation_id=g.id "
                "WHERE s.account_id=? AND s.slot_number=?",
                (int(account_id), int(slot_number)),
            ).fetchone()
            if not row:
                return None
            return {
                "slot_number": int(slot_number),
                "generation": row["generation"],
                "paused": row["desired_state"] == "DISABLED",
                "slot_desired_state": row["desired_state"],
                "slot_observed_state": row["observed_state"],
                "child_desired_state": row["child_desired"],
                "has_provisioned_child": row["child_desired"] is not None,
            }

    # --- mutation -------------------------------------------------------------

    def set_paused(
        self, capability, *, account_id: int, slot_number: int, paused: bool,
        reason: str, idempotency_key: str | None = None, now: int | None = None,
    ) -> dict:
        """Pause (paused=True) or resume (paused=False) one slot's current
        provisioned generation.

        Idempotency/convergence is decided ONLY by the slot's live state read
        inside THIS transaction: an action whose end state already holds
        returns {"converged": True} without writing anything. A never-
        trusted-by-hash shortcut is deliberate -- a deterministic key repeats
        across legitimate re-toggles of the same generation
        (Disable -> Enable -> Disable again), so replaying "prior result"
        off `idempotency_key_hash` alone would falsely report convergence
        against a contradictory present (the exact a68e265-review defect
        class this design refuses to reproduce). The first occurrence of a
        client key is additionally stamped into the evidence row when its
        hash slot is free; repeats append their own honest evidence rows."""
        timestamp = int(time.time()) if now is None else int(now)
        reason = _clean_reason(reason)
        actor_ref = self._authority.require(capability)
        target_state = "DISABLED" if paused else "ACTIVE"
        operation_kind = "SLOT_DISABLE" if paused else "SLOT_ENABLE"
        account_id = int(account_id)
        slot_number = int(slot_number)
        if idempotency_key is not None:
            _idempotency_hash(idempotency_key)  # validate shape even though
            # the hash itself is only ever advisory metadata here.
        idem_hash = (
            hashlib.sha256((_NAMESPACE + idempotency_key).encode("utf-8")).hexdigest()
            if isinstance(idempotency_key, str) and idempotency_key else None
        )

        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                slot = self._conn.execute(
                    "SELECT s.*,g.id AS generation_row_id,g.generation,"
                    "c.id AS child_intent_id,c.desired_state AS child_desired "
                    "FROM mgboost_device_slots s "
                    "LEFT JOIN mgboost_device_slot_generations g "
                    "ON g.slot_id=s.id AND g.account_id=s.account_id AND g.status='ACTIVE' "
                    "AND g.generation=s.current_generation "
                    "LEFT JOIN mgboost_child_user_intents c ON c.slot_generation_id=g.id "
                    "WHERE s.account_id=? AND s.slot_number=?",
                    (account_id, slot_number),
                ).fetchone()
                if not slot:
                    raise SlotAdminError("device slot does not exist")
                # Guards are scoped to THIS slot's CURRENT generation/intent --
                # never to the slot's lifetime history (the a68e265 review P0
                # class of scoping mistake must stay impossible here).
                if slot["generation_row_id"] is None or slot["child_intent_id"] is None:
                    raise SlotAdminConflict(
                        "this slot has no live provisioned generation to pause or resume"
                    )
                if slot["child_desired"] == "REVOKED":
                    raise SlotAdminConflict(
                        "the current generation is revoked; pause/resume do not apply"
                    )
                if slot["desired_state"] == target_state:
                    # Convergence decision from LIVE state, inside the txn.
                    self._conn.commit()
                    return {
                        "converged": True, "mutation_id": None,
                        "slot_number": slot_number, "generation": slot["generation"],
                        "operation_kind": operation_kind,
                        "slot_desired_state": target_state,
                    }
                if slot["desired_state"] != "ACTIVE" and paused:
                    raise SlotAdminConflict(
                        "only an ACTIVE slot can be paused; resolve its pending "
                        "lifecycle state first"
                    )

                # Optimistic CAS on the exact row just read: a concurrent
                # claim/release/rebind toggling this slot makes us fail loudly
                # instead of pausing/resuming the wrong generation.
                updated = self._conn.execute(
                    "UPDATE mgboost_device_slots SET desired_state=?,"
                    "observed_state='UNKNOWN',updated_at=?,row_version=row_version+1 "
                    "WHERE id=? AND account_id=? AND row_version=? AND current_generation=?",
                    (
                        target_state, timestamp,
                        slot["id"], account_id, slot["row_version"], slot["current_generation"],
                    ),
                )
                if updated.rowcount != 1:
                    raise SlotAdminConflict("slot changed concurrently; retry with fresh state")
                before_json = '{"slot_number":%d,"generation":%d,"slot_desired_state":"%s"}' % (
                    slot_number, slot["generation"], "DISABLED" if paused else "ACTIVE",
                )
                after_json = '{"slot_number":%d,"generation":%d,"slot_desired_state":"%s"}' % (
                    slot_number, slot["generation"], target_state,
                )
                try:
                    cursor = self._conn.execute(
                        "INSERT INTO mgboost_entitlement_mutations "
                        "(account_id,subscription_id,operation,payment_channel,"
                        "mutation_source,actor_type,actor_ref,reason,idempotency_key_hash,"
                        "before_json,after_json,created_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            account_id,
                            self._conn.execute(
                                "SELECT id FROM mgboost_subscriptions WHERE account_id=? "
                                "ORDER BY id DESC LIMIT 1", (account_id,),
                            ).fetchone()[0],
                            operation_kind, "NOT_APPLICABLE", "ADMIN", "PRIMARY_ADMIN",
                            actor_ref, reason, idem_hash, before_json, after_json, timestamp,
                        ),
                    )
                    mutation_id = cursor.lastrowid
                except sqlite3.IntegrityError:
                    # The same client key was stamped by an earlier occurrence
                    # of this repeatable state action; audit the repeat under
                    # its own row without stealing the UNIQUE hash slot.
                    cursor = self._conn.execute(
                        "INSERT INTO mgboost_entitlement_mutations "
                        "(account_id,subscription_id,operation,payment_channel,"
                        "mutation_source,actor_type,actor_ref,reason,"
                        "before_json,after_json,created_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            account_id,
                            self._conn.execute(
                                "SELECT id FROM mgboost_subscriptions WHERE account_id=? "
                                "ORDER BY id DESC LIMIT 1", (account_id,),
                            ).fetchone()[0],
                            operation_kind, "NOT_APPLICABLE", "ADMIN", "PRIMARY_ADMIN",
                            actor_ref, reason, before_json, after_json, timestamp,
                        ),
                    )
                    mutation_id = cursor.lastrowid
                self._bump_parent_revision_locked(account_id, timestamp)
                self._conn.commit()
                return {
                    "converged": False, "mutation_id": mutation_id,
                    "slot_number": slot_number, "generation": slot["generation"],
                    "operation_kind": operation_kind,
                    "slot_desired_state": target_state,
                }
            except Exception:
                self._conn.rollback()
                raise

    def mark_observed(self, account_id: int, slot_number: int, *, now: int | None = None) -> None:
        """After a successful convergence drive, align the slot's observed
        state with its desired state (pause semantics only; caller decides)."""
        timestamp = int(time.time()) if now is None else int(now)
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                self._conn.execute(
                    "UPDATE mgboost_device_slots SET observed_state=desired_state,"
                    "updated_at=?,row_version=row_version+1 WHERE account_id=? AND slot_number=?",
                    (timestamp, int(account_id), int(slot_number)),
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    # --- internals ------------------------------------------------------------

    def _bump_parent_revision_locked(self, account_id: int, now: int) -> None:
        """Force a new PH3-08 desired-state revision so fresh revision-stamped
        sync ops are issued for every current child. A missing entitlement
        state row is created from the canonical pure policy first (identical
        inputs to ParentSyncStore.refresh_desired_state's initial insert), but
        all reads happen here inside OUR transaction: the bump and the pause
        flag become visible atomically, so no enqueue can ever stamp ops for
        the old revision against the new per-slot reality."""
        from .parent_sync import compute_desired_status

        row = self._conn.execute(
            "SELECT es.revision,s.status AS subscription_status,s.current_expiry,"
            "s.id AS subscription_id,a.status AS account_status "
            "FROM mgboost_accounts a "
            "JOIN mgboost_subscriptions s ON s.account_id=a.id "
            "LEFT JOIN mgboost_entitlement_state es ON es.account_id=a.id "
            "WHERE a.id=? ORDER BY s.id DESC LIMIT 1",
            (account_id,),
        ).fetchone()
        if not row:
            raise SlotAdminError("account has no subscription to converge")
        if row["revision"] is None:
            desired = compute_desired_status(
                row["account_status"], row["subscription_status"], row["current_expiry"], now,
            )
            self._conn.execute(
                "INSERT INTO mgboost_entitlement_state "
                "(account_id,subscription_id,desired_status,revision,updated_at,desired_expire) "
                "VALUES (?,?,?,2,?,?)",
                (account_id, row["subscription_id"], desired, now, row["current_expiry"]),
            )
            return
        self._conn.execute(
            "UPDATE mgboost_entitlement_state SET revision=revision+1,updated_at=? "
            "WHERE account_id=?",
            (now, account_id),
        )
