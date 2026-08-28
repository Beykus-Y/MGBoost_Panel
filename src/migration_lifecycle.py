"""PH4-02 durable migration state machine.

`LEGACY -> MIGRATING -> MIGRATED -> LEGACY_REVOKE_PENDING -> LEGACY_REVOKED`,
plus `ERROR_RECONCILE`. `LEGACY` is the implicit absence of a
`mgboost_migration_bindings` row (mirrors PH4-01's own "no binding = fall
through" pattern) -- a row is created only once `resolve_legacy_bridge()` has
already returned a non-fall-through outcome, i.e. only once a slot has
already been durably claimed by the unmodified PH3-02/03 machinery. This
module never writes a second resolver: `process_migration_bridge_request()`
below wraps `legacy_bridge_resolver.resolve_legacy_bridge()` unchanged and
adds only a durable per-device lifecycle record on top of it.

Transition allowlist (`_ALLOWED_TRANSITIONS`) is the single source of truth
for which state changes are legal; every mutating method enforces it plus an
optimistic-concurrency `revision` CAS, so a stale worker/request can never
overwrite a newer decision, and `LEGACY_REVOKED` is additionally enforced
terminal by a DB trigger (belt-and-suspenders, not just application logic).
"""

from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
import time

from .admin_authority import PrimaryAdminAuthorizationError
from .device_slots import InvalidHWID, privacy_safe_hwid
from .legacy_bridge_resolver import is_fall_through_outcome, resolve_legacy_bridge
from .opaque_resolver import (
    OUTCOME_INTERNAL_ERROR,
    OUTCOME_OK,
    OUTCOME_PROVISIONING_FAILED_PERMANENT,
)


class MigrationLifecycleError(RuntimeError):
    pass


class MigrationConflict(MigrationLifecycleError):
    pass


class MigrationStaleRevision(MigrationLifecycleError):
    pass


class MigrationTransitionError(MigrationLifecycleError):
    pass


class PrimaryAdminRequired(MigrationLifecycleError):
    pass


# Explicit allowlist -- no arbitrary `UPDATE state=?` anywhere in this module.
_ALLOWED_TRANSITIONS = {
    "MIGRATING": {"MIGRATING", "MIGRATED", "ERROR_RECONCILE"},
    "ERROR_RECONCILE": {"MIGRATING", "MIGRATED", "ERROR_RECONCILE"},
    "MIGRATED": {"MIGRATED", "LEGACY_REVOKE_PENDING"},
    "LEGACY_REVOKE_PENDING": {"LEGACY_REVOKE_PENDING", "LEGACY_REVOKED", "ERROR_RECONCILE"},
    "LEGACY_REVOKED": set(),  # terminal: never rolls back, never re-enables the shared credential
}


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical(value: dict) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _base32_128(raw: bytes) -> str:
    return base64.b32encode(raw[:16]).decode("ascii").lower().rstrip("=")


def _derive_operation_id(idem_hash: str) -> str:
    return "mg_" + _base32_128(bytes.fromhex(idem_hash))


class MigrationLifecycleStore:
    def __init__(self, connection: sqlite3.Connection, lock, primary_admin_authority=None):
        self._conn = connection
        self._lock = lock
        self._authority = primary_admin_authority

    def _require_primary(self, capability) -> str:
        if self._authority is None:
            raise PrimaryAdminRequired("primary admin authority not configured")
        try:
            return self._authority.require(capability)
        except PrimaryAdminAuthorizationError:
            raise PrimaryAdminRequired("primary MGBoost admin capability required")

    def _event(self, binding_id, account_id, attempt_no, event_type, *, from_state=None,
               to_state=None, safe_error_class=None, reason=None, now):
        self._conn.execute(
            "INSERT INTO mgboost_migration_binding_events "
            "(migration_binding_id,account_id,attempt_no,event_type,from_state,to_state,"
            "safe_error_class,reason,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (binding_id, account_id, attempt_no, event_type, from_state, to_state,
             safe_error_class, reason, now),
        )

    # --- read-only lookups -------------------------------------------------

    def find_by_device(self, account_id: int, hwid_verifier: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM mgboost_migration_bindings WHERE account_id=? AND hwid_verifier=?",
            (int(account_id), hwid_verifier),
        ).fetchone()
        return dict(row) if row else None

    def find_by_operation_id(self, operation_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM mgboost_migration_bindings WHERE operation_id=?", (operation_id,),
        ).fetchone()
        return dict(row) if row else None

    # --- prepare (idempotent insert, only after a durable slot claim) -----

    def prepare_migration(
        self, *, account_id: int, legacy_alias_id: int, hwid_verifier: str, actor_ref: str,
        reason: str, idempotency_key: str, now: int | None = None,
    ) -> dict:
        reason = (reason or "").strip()
        if not 3 <= len(reason) <= 300:
            raise MigrationLifecycleError("a bounded human-readable reason is required")
        if not isinstance(hwid_verifier, str) or len(hwid_verifier) != 76 or not hwid_verifier.startswith("hmac-sha256:"):
            raise MigrationLifecycleError("invalid hwid verifier")
        if not isinstance(idempotency_key, str) or not 16 <= len(idempotency_key) <= 512:
            raise MigrationLifecycleError("invalid idempotency key")
        timestamp = int(time.time()) if now is None else int(now)
        idem_hash = _sha("migration-lifecycle-v1\0" + idempotency_key)
        payload = {
            "account_id": int(account_id), "legacy_alias_id": int(legacy_alias_id),
            "hwid_verifier": hwid_verifier,
        }
        request_hash = _sha(_canonical(payload))
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                # One logical device -> one authoritative migration lineage.
                by_device = self._conn.execute(
                    "SELECT * FROM mgboost_migration_bindings WHERE account_id=? AND hwid_verifier=?",
                    (int(account_id), hwid_verifier),
                ).fetchone()
                if by_device:
                    self._conn.commit()
                    return dict(by_device)
                prior = self._conn.execute(
                    "SELECT * FROM mgboost_migration_bindings WHERE idempotency_key_hash=?",
                    (idem_hash,),
                ).fetchone()
                if prior:
                    if prior["request_hash"] != request_hash:
                        raise MigrationConflict("idempotency key reused with a different migration request")
                    self._conn.commit()
                    return dict(prior)
                operation_id = _derive_operation_id(idem_hash)
                cursor = self._conn.execute(
                    "INSERT INTO mgboost_migration_bindings "
                    "(operation_id,account_id,legacy_alias_id,hwid_verifier,state,revision,"
                    "idempotency_key_hash,request_hash,actor_ref,next_attempt_at,created_at,updated_at) "
                    "VALUES (?,?,?,?,'MIGRATING',1,?,?,?,?,?,?)",
                    (operation_id, int(account_id), int(legacy_alias_id), hwid_verifier,
                     idem_hash, request_hash, actor_ref, timestamp, timestamp, timestamp),
                )
                self._event(cursor.lastrowid, int(account_id), 0, "CREATED",
                            to_state="MIGRATING", reason=reason, now=timestamp)
                row = self._conn.execute(
                    "SELECT * FROM mgboost_migration_bindings WHERE id=?", (cursor.lastrowid,),
                ).fetchone()
                self._conn.commit()
                return dict(row)
            except Exception:
                self._conn.rollback()
                raise

    # --- generic CAS transition helper -------------------------------------

    def _transition(
        self, operation_id: str, *, expected_revision: int, from_states: set[str], to_state: str,
        event_type: str, extra_set: str = "", extra_params: tuple = (),
        safe_error_class: str | None = None, reason: str | None = None, now: int,
    ) -> dict:
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                row = self._conn.execute(
                    "SELECT * FROM mgboost_migration_bindings WHERE operation_id=?", (operation_id,),
                ).fetchone()
                if row is None:
                    raise MigrationLifecycleError("unknown migration operation_id")
                if row["state"] not in from_states:
                    raise MigrationTransitionError(
                        f"illegal transition {row['state']} -> {to_state}"
                    )
                if to_state not in _ALLOWED_TRANSITIONS.get(row["state"], set()):
                    raise MigrationTransitionError(
                        f"illegal transition {row['state']} -> {to_state}"
                    )
                if row["revision"] != expected_revision:
                    raise MigrationStaleRevision("stale revision -- a newer decision already applied")
                self._conn.execute(
                    "UPDATE mgboost_migration_bindings SET state=?,revision=revision+1,"
                    "attempts=attempts+1,updated_at=?,row_version=row_version+1"
                    + (", " + extra_set if extra_set else "")
                    + " WHERE id=? AND revision=?",
                    (to_state, now) + extra_params + (row["id"], expected_revision),
                )
                self._event(row["id"], row["account_id"], row["attempts"] + 1, event_type,
                            from_state=row["state"], to_state=to_state,
                            safe_error_class=safe_error_class, reason=reason, now=now)
                result = self._conn.execute(
                    "SELECT * FROM mgboost_migration_bindings WHERE id=?", (row["id"],),
                ).fetchone()
                self._conn.commit()
                return dict(result)
            except Exception:
                self._conn.rollback()
                raise

    # --- non-transitioning ref updates (idempotent, fill-once) -------------

    def record_slot(self, operation_id: str, *, expected_revision: int, slot_generation_id: int, now: int) -> dict:
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                row = self._conn.execute(
                    "SELECT * FROM mgboost_migration_bindings WHERE operation_id=?", (operation_id,),
                ).fetchone()
                if row is None:
                    raise MigrationLifecycleError("unknown migration operation_id")
                if row["state"] not in ("MIGRATING", "ERROR_RECONCILE"):
                    raise MigrationTransitionError("slot can only be recorded while MIGRATING/ERROR_RECONCILE")
                if row["revision"] != expected_revision:
                    raise MigrationStaleRevision("stale revision")
                if row["slot_generation_id"] is None:
                    self._conn.execute(
                        "UPDATE mgboost_migration_bindings SET slot_generation_id=?,revision=revision+1,"
                        "updated_at=?,row_version=row_version+1 WHERE id=? AND revision=?",
                        (slot_generation_id, now, row["id"], expected_revision),
                    )
                    self._event(row["id"], row["account_id"], row["attempts"], "SLOT_RECORDED", now=now)
                result = self._conn.execute(
                    "SELECT * FROM mgboost_migration_bindings WHERE id=?", (row["id"],),
                ).fetchone()
                self._conn.commit()
                return dict(result)
            except Exception:
                self._conn.rollback()
                raise

    def record_child(self, operation_id: str, *, expected_revision: int, child_intent_id: int, now: int) -> dict:
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                row = self._conn.execute(
                    "SELECT * FROM mgboost_migration_bindings WHERE operation_id=?", (operation_id,),
                ).fetchone()
                if row is None:
                    raise MigrationLifecycleError("unknown migration operation_id")
                if row["state"] not in ("MIGRATING", "ERROR_RECONCILE"):
                    raise MigrationTransitionError("child can only be recorded while MIGRATING/ERROR_RECONCILE")
                if row["revision"] != expected_revision:
                    raise MigrationStaleRevision("stale revision")
                if row["child_intent_id"] is None:
                    self._conn.execute(
                        "UPDATE mgboost_migration_bindings SET child_intent_id=?,revision=revision+1,"
                        "updated_at=?,row_version=row_version+1 WHERE id=? AND revision=?",
                        (child_intent_id, now, row["id"], expected_revision),
                    )
                    self._event(row["id"], row["account_id"], row["attempts"], "CHILD_RECORDED", now=now)
                result = self._conn.execute(
                    "SELECT * FROM mgboost_migration_bindings WHERE id=?", (row["id"],),
                ).fetchone()
                self._conn.commit()
                return dict(result)
            except Exception:
                self._conn.rollback()
                raise

    # --- forward-path transitions -------------------------------------------

    def mark_migrated(self, operation_id: str, *, expected_revision: int, now: int) -> dict:
        row = self.find_by_operation_id(operation_id)
        if row is None:
            raise MigrationLifecycleError("unknown migration operation_id")
        if row["slot_generation_id"] is None or row["child_intent_id"] is None:
            raise MigrationTransitionError("cannot mark MIGRATED without a recorded slot and child")
        return self._transition(
            operation_id, expected_revision=expected_revision, from_states={"MIGRATING", "ERROR_RECONCILE"},
            to_state="MIGRATED", event_type="MIGRATED", now=now,
        )

    def retry_migrating(self, operation_id: str, *, expected_revision: int, error_class: str, now: int) -> dict:
        return self._transition(
            operation_id, expected_revision=expected_revision, from_states={"MIGRATING"},
            to_state="MIGRATING", event_type="RETRY", safe_error_class=error_class, now=now,
        )

    def mark_error_reconcile(self, operation_id: str, *, expected_revision: int, error_class: str, now: int) -> dict:
        return self._transition(
            operation_id, expected_revision=expected_revision, from_states={"MIGRATING", "LEGACY_REVOKE_PENDING"},
            to_state="ERROR_RECONCILE", event_type="ERROR_RECONCILE", safe_error_class=error_class, now=now,
        )

    # --- reconciliation ------------------------------------------------------

    def reconcile_to_migrating(self, operation_id: str, *, expected_revision: int, reason: str, now: int) -> dict:
        return self._transition(
            operation_id, expected_revision=expected_revision, from_states={"ERROR_RECONCILE"},
            to_state="MIGRATING", event_type="RECONCILE_TO_MIGRATING", reason=reason, now=now,
        )

    def reconcile_to_migrated(self, operation_id: str, *, expected_revision: int, reason: str, now: int) -> dict:
        row = self.find_by_operation_id(operation_id)
        if row is None:
            raise MigrationLifecycleError("unknown migration operation_id")
        if row["slot_generation_id"] is None or row["child_intent_id"] is None:
            raise MigrationTransitionError("cannot reconcile to MIGRATED without a recorded slot and child")
        return self._transition(
            operation_id, expected_revision=expected_revision, from_states={"ERROR_RECONCILE"},
            to_state="MIGRATED", event_type="RECONCILE_TO_MIGRATED", reason=reason, now=now,
        )

    def stay_error_reconcile(self, operation_id: str, *, expected_revision: int, reason: str, now: int) -> dict:
        return self._transition(
            operation_id, expected_revision=expected_revision, from_states={"ERROR_RECONCILE"},
            to_state="ERROR_RECONCILE", event_type="RECONCILE_STALE", reason=reason, now=now,
        )

    # --- legacy revoke boundary (dormant: no route/worker calls this) -------

    def start_legacy_revoke_pending(
        self, operation_id: str, *, capability, expected_revision: int, reason: str, now: int,
    ) -> dict:
        actor = self._require_primary(capability)
        reason = (reason or "").strip()
        if not 3 <= len(reason) <= 300:
            raise MigrationLifecycleError("a bounded human-readable reason is required")
        return self._transition(
            operation_id, expected_revision=expected_revision, from_states={"MIGRATED"},
            to_state="LEGACY_REVOKE_PENDING", event_type="REVOKE_PENDING_STARTED",
            reason=f"{reason} (actor={actor})", now=now,
        )

    def mark_legacy_revoked(self, operation_id: str, *, expected_revision: int, now: int) -> dict:
        return self._transition(
            operation_id, expected_revision=expected_revision, from_states={"LEGACY_REVOKE_PENDING"},
            to_state="LEGACY_REVOKED", event_type="LEGACY_REVOKED", now=now,
        )


# --- reconciliation orchestration -------------------------------------------

def reconcile_binding(db, binding: dict, *, now: int) -> dict:
    """Compares durable desired state against the authoritative PH3-02/03
    tables -- never infers from a single signal. Never creates a second
    child, never resurrects a terminal generation."""
    if binding["state"] != "ERROR_RECONCILE":
        return binding
    store = db.migration_lifecycle
    generation_row = None
    if binding["slot_generation_id"] is not None:
        generation_row = db._conn.execute(
            "SELECT * FROM mgboost_device_slot_generations WHERE id=?",
            (binding["slot_generation_id"],),
        ).fetchone()
    else:
        generation_row = db._conn.execute(
            "SELECT * FROM mgboost_device_slot_generations "
            "WHERE account_id=? AND hwid_verifier=? AND status='ACTIVE'",
            (binding["account_id"], binding["hwid_verifier"]),
        ).fetchone()

    if generation_row is None or generation_row["status"] != "ACTIVE":
        # The slot this migration attempt anchored to is gone/superseded
        # (revoked, rebound to a different device). Never blindly reassign
        # or resurrect it -- stays ERROR_RECONCILE for manual review.
        return store.stay_error_reconcile(
            binding["operation_id"], expected_revision=binding["revision"],
            reason="anchored slot generation is no longer ACTIVE", now=now,
        )

    if binding["slot_generation_id"] is None:
        binding = store.record_slot(
            binding["operation_id"], expected_revision=binding["revision"],
            slot_generation_id=generation_row["id"], now=now,
        )

    intent_row = db._conn.execute(
        "SELECT * FROM mgboost_child_user_intents WHERE slot_generation_id=?",
        (generation_row["id"],),
    ).fetchone()

    if intent_row is None:
        return store.reconcile_to_migrating(
            binding["operation_id"], expected_revision=binding["revision"],
            reason="no child intent yet -- safe to retry provisioning", now=now,
        )

    if binding["child_intent_id"] is None:
        binding = store.record_child(
            binding["operation_id"], expected_revision=binding["revision"],
            child_intent_id=intent_row["id"], now=now,
        )

    if intent_row["observed_state"] == "ACTIVE":
        return store.reconcile_to_migrated(
            binding["operation_id"], expected_revision=binding["revision"],
            reason="remote child already ACTIVE -- lost ACK, not a real failure", now=now,
        )

    # P0 hotfix: a terminal child operation (durable outbox ERROR, e.g. the
    # PH5-11 WL_INBOUND_IN_STANDARD_CHILD poison) must never be resurrected
    # into the MIGRATING retry machine by reconciliation -- that is the
    # infinite RETRY loop class. It stays ERROR_RECONCILE until the explicit
    # audited recovery primitive repairs it.
    outbox_row = db._conn.execute(
        "SELECT state FROM mgboost_outbox WHERE child_intent_id=?", (intent_row["id"],),
    ).fetchone()
    if outbox_row is not None and outbox_row["state"] == "ERROR":
        return store.stay_error_reconcile(
            binding["operation_id"], expected_revision=binding["revision"],
            reason="child operation is terminally ERROR -- recovery primitive required, "
                   "not provisioning retry", now=now,
        )

    return store.reconcile_to_migrating(
        binding["operation_id"], expected_revision=binding["revision"],
        reason="child intent not yet ACTIVE -- safe to retry provisioning", now=now,
    )


# --- PH4-01 integration: durable lifecycle wrapper, no second resolver -----

def process_migration_bridge_request(
    db, legacy_username: str, device_metadata: dict, *, hmac_key, ensure_fn, subscription_fn,
    worker_id: str, now: int | None = None,
):
    """Wraps `resolve_legacy_bridge()` unchanged and adds a durable
    per-(account, hwid) migration lineage on top of it. Once a lineage
    exists in MIGRATING (or beyond), a child/provisioning outage never
    silently falls back to the legacy shared credential -- the caller
    (`src/routes/sub.py::_try_legacy_bridge`) already fails closed on any
    non-OK, non-fall-through outcome; this module only adds the durable
    audit/state layer, never a second decision path."""
    timestamp = int(time.time()) if now is None else int(now)

    raw_hwid = device_metadata.get("device_id")
    hwid_verifier = None
    if isinstance(raw_hwid, str) and raw_hwid:
        try:
            hwid_verifier, _masked = privacy_safe_hwid(raw_hwid, hmac_key)
        except InvalidHWID:
            hwid_verifier = None

    existing = None
    if hwid_verifier is not None:
        account_id_probe = db.legacy_bridge.resolve_account_for_legacy_username(legacy_username)
        if account_id_probe is not None:
            existing = db.migration_lifecycle.find_by_device(account_id_probe, hwid_verifier)
            if existing is not None and existing["state"] == "ERROR_RECONCILE":
                existing = reconcile_binding(db, existing, now=timestamp)

    result = resolve_legacy_bridge(
        db, legacy_username, device_metadata, hmac_key=hmac_key, ensure_fn=ensure_fn,
        subscription_fn=subscription_fn, worker_id=worker_id, now=timestamp,
    )

    if hwid_verifier is None or is_fall_through_outcome(result.outcome):
        return result

    account_id = db.legacy_bridge.resolve_account_for_legacy_username(legacy_username)
    if account_id is None:
        return result

    if existing is not None and existing["state"] in ("LEGACY_REVOKED", "LEGACY_REVOKE_PENDING", "MIGRATED"):
        return result

    alias_row = db._conn.execute(
        "SELECT id FROM mgboost_legacy_account_aliases WHERE account_id=? AND alias_role='PRIMARY'",
        (account_id,),
    ).fetchone()
    if alias_row is None:
        return result

    if existing is None:
        idem_key = f"migration-bridge-v1:{account_id}:{hwid_verifier}"
        existing = db.migration_lifecycle.prepare_migration(
            account_id=account_id, legacy_alias_id=alias_row["id"], hwid_verifier=hwid_verifier,
            actor_ref="mgboost-migration-resolver",
            reason="durable migration decision from legacy bridge request",
            idempotency_key=idem_key, now=timestamp,
        )

    if existing["state"] not in ("MIGRATING", "ERROR_RECONCILE"):
        return result

    if existing["slot_generation_id"] is None:
        # P0 hotfix diagnostics: the resolver's result carries slot/child
        # identity only on OUTCOME_OK, but the slot claim and the child
        # intent are durable local rows that exist even when provisioning
        # terminally failed. Binding them here gives admin diagnostics,
        # repair and audit the real entities involved. This never declares
        # a failed provisioning migrated -- the state transitions below
        # stay strictly outcome-driven.
        slot_row = db._conn.execute(
            "SELECT g.id FROM mgboost_device_slot_generations AS g "
            "WHERE g.account_id=? AND g.hwid_verifier=? AND g.status='ACTIVE'",
            (account_id, hwid_verifier),
        ).fetchone()
        if slot_row is not None:
            existing = db.migration_lifecycle.record_slot(
                existing["operation_id"], expected_revision=existing["revision"],
                slot_generation_id=slot_row["id"], now=timestamp,
            )

    if existing["child_intent_id"] is None and existing["slot_generation_id"] is not None:
        intent_row = db._conn.execute(
            "SELECT id FROM mgboost_child_user_intents WHERE slot_generation_id=?",
            (existing["slot_generation_id"],),
        ).fetchone()
        if intent_row is not None:
            existing = db.migration_lifecycle.record_child(
                existing["operation_id"], expected_revision=existing["revision"],
                child_intent_id=intent_row["id"], now=timestamp,
            )

    if existing["state"] == "MIGRATING" and result.outcome == OUTCOME_OK:
        if existing["slot_generation_id"] is not None and existing["child_intent_id"] is not None:
            db.migration_lifecycle.mark_migrated(
                existing["operation_id"], expected_revision=existing["revision"], now=timestamp,
            )
    elif existing["state"] in ("MIGRATING", "ERROR_RECONCILE") and result.outcome != OUTCOME_OK:
        if result.outcome == OUTCOME_INTERNAL_ERROR:
            # Ambiguous -- cannot tell from this single signal whether the
            # remote side actually committed. Never blindly retry.
            db.migration_lifecycle.mark_error_reconcile(
                existing["operation_id"], expected_revision=existing["revision"],
                error_class=result.outcome, now=timestamp,
            )
        elif result.outcome == OUTCOME_PROVISIONING_FAILED_PERMANENT:
            # Terminal provisioning failure (P0 hotfix): the typed root
            # cause from the durable outbox row is surfaced to operators
            # through the binding event instead of an endless MIGRATING ->
            # RETRY loop. Recovery is the explicit audited repair primitive.
            root_class = _terminal_root_error_class(db, existing["child_intent_id"]) or result.outcome
            if existing["state"] == "MIGRATING":
                db.migration_lifecycle.mark_error_reconcile(
                    existing["operation_id"], expected_revision=existing["revision"],
                    error_class=root_class, now=timestamp,
                )
            else:
                db.migration_lifecycle.stay_error_reconcile(
                    existing["operation_id"], expected_revision=existing["revision"],
                    reason=f"terminal provisioning failure persists ({root_class})",
                    now=timestamp,
                )
        else:
            db.migration_lifecycle.retry_migrating(
                existing["operation_id"], expected_revision=existing["revision"],
                error_class=result.outcome, now=timestamp,
            )

    return result


def _terminal_root_error_class(db, child_intent_id: int | None) -> str | None:
    """The durable typed root cause of a terminal child operation, for
    operator-facing binding events (already a bounded safe error class)."""
    if child_intent_id is None:
        return None
    row = db._conn.execute(
        "SELECT last_error_class FROM mgboost_outbox WHERE child_intent_id=?",
        (int(child_intent_id),),
    ).fetchone()
    if row is None:
        return None
    value = (row["last_error_class"] or "").strip()
    return value[:128] or None
