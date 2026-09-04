"""P0 hotfix -- audited, idempotent recovery primitive for poisoned
CHILD_USER_ENSURE operations.

Production incident (account #8, POCO Slot 3 / generation 49): PH5-11's
render-boundary backstop `fail_permanent`('WL_INBOUND_IN_STANDARD_CHILD')
killed provisioning AFTER the broker had already created the remote child,
because the backstop was scoped to no account policy at all. The durable
local state (outbox ERROR + intent observed_state=ERROR) is terminal -- the
worker never re-picks it and auto-recovery is impossible by design. This
module is the ONLY sanctioned way out of that state; direct SQL updates and
generic "retry any ERROR" endpoints are exactly what it must never become.

Semantics, narrowed on purpose:

  * Applies ONLY to a proven-owned child intent/outbox whose durable
    ``last_error_class`` is in ``RECOVERABLE_ERROR_CLASSES`` (today exactly
    the incident class ``WL_INBOUND_IN_STANDARD_CHILD``) -- everything else
    is refused.
  * Rereads the account's CURRENT canonical entitlement policy
    (`entitlement_engine.exact_wl_allowed_for_delivery`). If it still
    forbids exact WL, the repair MUST refuse -- policy, not the operator,
    decides.
  * Takes a FRESH typed remote observation through the existing read-only
    ``child.user.observe`` broker surface (caller-supplied ``observe_fn``).
    MATCH proves remote username/source-contract/inbound provenance against
    the EXACT ensure contract pinned in the durable outbox payload. It
    never ensures, never creates, never mutates remote state, never changes
    a UUID, never re-pins a source.
  * ABSENT is a typed non-mutation result (REMOTE_MISSING); MISMATCH and a
    local UUID-verifier contradiction are refusals for manual review --
    per the existing safe-repair conventions.
  * Local completion goes through ``ChildProvisioningStore
    .recovery_acknowledge`` (CAS from ERROR, full attempt-event evidence).
  * Every terminal decision (REPAIRED / REFUSED / REMOTE_MISSING) appends
    an immutable actor/reason/idempotency evidence row to the EXISTING
    ``mgboost_entitlement_mutations`` audit ledger -- no second audit
    framework. A repeat repair is a safe no-op (ALREADY_APPLIED, no new
    audit row, no state change).

This module talks to Marzban ONLY through the caller-injected read-only
``observe_fn``; wiring it to an admin route/surface is a later, separate
owner decision. Nothing schedules or invokes it automatically.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid

from .child_contract import validate_child_ensure_request
from .entitlement_engine import exact_wl_allowed_for_delivery
from .wl_topology import WL_INBOUND_TAGS


REPAIR_OPERATION = "CHILD_RECOVERY_REPAIR"

# Only proven poison classes are recoverable. Never grow this set without
# an explicit per-class proof that the remote child is verifiable for it.
RECOVERABLE_ERROR_CLASSES = frozenset({"WL_INBOUND_IN_STANDARD_CHILD"})

_IDEMPOTENCY_NAMESPACE = "p0-child-recovery-repair-v1\0"


class ChildRecoveryError(RuntimeError):
    pass


def _canonical(value: dict) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _clean_reason(reason) -> str:
    text = (reason or "").strip()
    if not 3 <= len(text) <= 300:
        raise ChildRecoveryError("a bounded human-readable reason (3..300) is required")
    return text


def _clean_idempotency_key(idempotency_key) -> str:
    if not isinstance(idempotency_key, str) or not 16 <= len(idempotency_key) <= 512:
        raise ChildRecoveryError("idempotency_key must be a string of 16..512 characters")
    return idempotency_key


_AUDIT_SAVEPOINT = "child_recovery_audit"


def _insert_audit_row_locked(db, *, account_id, actor_ref, reason, idem_hash,
                              before, after, now) -> int:
    """Append one immutable evidence row to the existing entitlement
    mutations ledger. Assumes ``db._lock`` is already held and a transaction
    is already open (``BEGIN IMMEDIATE`` already issued by the caller) --
    this function never begins, commits or rolls back the outer transaction
    itself. It uses its own SAVEPOINT only to retry past a UNIQUE
    idempotency-key collision (a repeated client key never steals the
    UNIQUE hash slot, mirroring the device-slot admin store's honest-repeat
    pattern) without disturbing whatever the caller already did in the same
    transaction."""

    def _insert(idem_column_value):
        return db._conn.execute(
            "INSERT INTO mgboost_entitlement_mutations "
            "(account_id,subscription_id,operation,payment_channel,"
            "mutation_source,actor_type,actor_ref,reason,idempotency_key_hash,"
            "before_json,after_json,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                account_id,
                db._conn.execute(
                    "SELECT id FROM mgboost_subscriptions WHERE account_id=? "
                    "ORDER BY id DESC LIMIT 1", (account_id,),
                ).fetchone()[0],
                REPAIR_OPERATION, "NOT_APPLICABLE", "ADMIN", "PRIMARY_ADMIN",
                actor_ref, reason, idem_column_value,
                _canonical(before), _canonical(after), now,
            ),
        )

    db._conn.execute(f"SAVEPOINT {_AUDIT_SAVEPOINT}")
    try:
        cursor = _insert(idem_hash)
        db._conn.execute(f"RELEASE SAVEPOINT {_AUDIT_SAVEPOINT}")
        return cursor.lastrowid
    except sqlite3.IntegrityError:
        db._conn.execute(f"ROLLBACK TO SAVEPOINT {_AUDIT_SAVEPOINT}")
        db._conn.execute(f"RELEASE SAVEPOINT {_AUDIT_SAVEPOINT}")
        cursor = _insert(None)
        return cursor.lastrowid
    except Exception:
        db._conn.execute(f"ROLLBACK TO SAVEPOINT {_AUDIT_SAVEPOINT}")
        db._conn.execute(f"RELEASE SAVEPOINT {_AUDIT_SAVEPOINT}")
        raise


def _audit(db, *, account_id, actor_ref, reason, idem_hash, before, after, now) -> int:
    """Standalone commit boundary for the non-mutating refuse path
    (``_refuse``): one immutable evidence row, its own transaction. The
    mutating REPAIRED path never calls this -- it uses
    ``_insert_audit_row_locked`` inside the same transaction as the local
    completion mutation so the two can never durably diverge."""
    with db._lock:
        try:
            db._conn.execute("BEGIN IMMEDIATE")
            mutation_id = _insert_audit_row_locked(
                db, account_id=account_id, actor_ref=actor_ref, reason=reason,
                idem_hash=idem_hash, before=before, after=after, now=now,
            )
            db._conn.commit()
            return mutation_id
        except Exception:
            db._conn.rollback()
            raise


def _repair_and_audit_atomic(
    db, *, operation_id, outcome, child_uuid, remote_result_verifier,
    account_id, actor_ref, reason, idem_hash, before, after, now,
) -> tuple[dict, int]:
    """The one durable transaction boundary for a successful repair: the
    local CAS completion mutation and the mandatory immutable audit row
    commit together or not at all. If the audit insert fails for any
    reason, the completion mutation is rolled back with it -- there is no
    state in which the child comes out of ERROR without durable
    actor+reason+audit evidence of exactly that."""
    with db._lock:
        try:
            db._conn.execute("BEGIN IMMEDIATE")
            repaired = db.child_provisioning._recovery_acknowledge_locked(
                operation_id, outcome=outcome, child_uuid=child_uuid,
                remote_result_verifier=remote_result_verifier, now=now,
            )
            mutation_id = _insert_audit_row_locked(
                db, account_id=account_id, actor_ref=actor_ref, reason=reason,
                idem_hash=idem_hash, before=before, after=after, now=now,
            )
            db._conn.commit()
            return repaired, mutation_id
        except Exception:
            db._conn.rollback()
            raise


def _snapshot(outbox, intent) -> dict:
    return {
        "outbox_state": outbox["state"],
        "outbox_last_error_class": outbox["last_error_class"],
        "intent_observed_state": None if intent is None else intent["observed_state"],
    }


def repair_child_ensure(
    db, *, operation_id: str, capability, reason: str, idempotency_key: str,
    observe_fn, now: int | None = None,
) -> dict:
    """Repair one poisoned CHILD_USER_ENSURE operation. See the module
    docstring for the full narrowed semantics. Returns a typed result dict:
    ``{"status": "REPAIRED"|"ALREADY_APPLIED"|"REFUSED"|"REMOTE_MISSING",
    "operation_id", "reason_class"?, "mutation_id"?}``."""
    timestamp = int(time.time()) if now is None else int(now)
    reason = _clean_reason(reason)
    _clean_idempotency_key(idempotency_key)
    if not callable(observe_fn):
        raise ChildRecoveryError("a read-only observe_fn is required")
    actor_ref = db.primary_admin_authority.require(capability)
    idem_hash = _sha(_IDEMPOTENCY_NAMESPACE + idempotency_key)

    # --- durable read + ownership proof (read-only) ------------------------
    outbox = db._conn.execute(
        "SELECT * FROM mgboost_outbox "
        "WHERE operation_id=? AND operation_kind='CHILD_USER_ENSURE'",
        (operation_id,),
    ).fetchone()
    if outbox is None:
        raise ChildRecoveryError("unknown CHILD_USER_ENSURE operation_id")
    account_id = int(outbox["account_id"])
    intent = db._conn.execute(
        "SELECT * FROM mgboost_child_user_intents WHERE id=? AND account_id=?",
        (outbox["child_intent_id"], account_id),
    ).fetchone()
    if intent is None:
        raise ChildRecoveryError("child intent does not belong to the operation's account")
    generation = db._conn.execute(
        "SELECT g.status FROM mgboost_device_slot_generations g "
        "WHERE g.id=? AND g.account_id=?",
        (intent["slot_generation_id"], account_id),
    ).fetchone()
    alias = db._conn.execute(
        "SELECT id FROM mgboost_legacy_account_aliases "
        "WHERE id=? AND account_id=? AND alias_role='PRIMARY'",
        (intent["source_alias_id"], account_id),
    ).fetchone()

    def _finish(status, *, reason_class=None, mutation_id=None) -> dict:
        return {
            "status": status, "operation_id": operation_id,
            **({"reason_class": reason_class} if reason_class else {}),
            **({"mutation_id": mutation_id} if mutation_id else {}),
        }

    if outbox["state"] == "APPLIED":
        # Idempotent repeat (or already consistent): safe no-op, no audit row.
        return _finish("ALREADY_APPLIED")
    if outbox["state"] != "ERROR":
        raise ChildRecoveryError(
            f"recovery applies only to terminal ERROR operations, not '{outbox['state']}'"
        )
    if generation is None or generation["status"] != "ACTIVE":
        return _refuse(db, account_id, actor_ref, reason, idem_hash, outbox, intent,
                       "GENERATION_NOT_ACTIVE", now=timestamp)
    if alias is None:
        return _refuse(db, account_id, actor_ref, reason, idem_hash, outbox, intent,
                       "OWNER_ALIAS_MISSING", now=timestamp)
    error_class = (outbox["last_error_class"] or "").strip()
    if error_class not in RECOVERABLE_ERROR_CLASSES:
        return _refuse(db, account_id, actor_ref, reason, idem_hash, outbox, intent,
                       "ERROR_CLASS_NOT_RECOVERABLE", now=timestamp)

    # --- CURRENT canonical policy decides (fail closed) ---------------------
    try:
        wl_allowed = exact_wl_allowed_for_delivery(db, account_id=account_id, now=timestamp)
    except Exception:
        wl_allowed = False
    if not wl_allowed:
        return _refuse(db, account_id, actor_ref, reason, idem_hash, outbox, intent,
                       "POLICY_STILL_FORBIDS_WL", now=timestamp)

    # --- fresh typed remote observation (read-only, outside local txns) ----
    payload = validate_child_ensure_request(json.loads(outbox["payload_json"]))
    observed = observe_fn(payload)
    presence = (observed or {}).get("presence")
    if presence == "ABSENT":
        return _refuse(db, account_id, actor_ref, reason, idem_hash, outbox, intent,
                       "REMOTE_MISSING", now=timestamp, status="REMOTE_MISSING")
    if presence != "MATCH":
        return _refuse(db, account_id, actor_ref, reason, idem_hash, outbox, intent,
                       "REMOTE_MISMATCH", now=timestamp)
    from .child_provisioning import ChildProvisioningError
    try:
        remote_uuid = str(uuid.UUID(observed.get("uuid"))).lower()
    except (ValueError, TypeError, AttributeError) as exc:
        raise ChildProvisioningError("invalid child UUID") from exc
    verifier = "sha256:" + _sha(remote_uuid)
    if intent["uuid_verifier"] is not None and intent["uuid_verifier"] != verifier:
        return _refuse(db, account_id, actor_ref, reason, idem_hash, outbox, intent,
                       "UUID_VERIFIER_MISMATCH", now=timestamp)
    # Belt-and-braces: the observed remote inbound set must itself be
    # permissible under the CURRENT entitlement (MATCH already proves it
    # equals the pinned source contract; the policy read above already
    # proved that contract's WL membership is allowed -- this keeps the two
    # proofs explicitly adjacent).
    observed_tags = set((observed.get("inbounds") or {}).get("vless") or [])
    if (observed_tags & set(WL_INBOUND_TAGS)) and not wl_allowed:
        return _refuse(db, account_id, actor_ref, reason, idem_hash, outbox, intent,
                       "POLICY_STILL_FORBIDS_WL", now=timestamp)
    if observed.get("protocols") != ["vless"]:
        raise ChildProvisioningError("unexpected child protocol credentials")

    # --- local completion + mandatory audit, ONE atomic transaction --------
    # Invariant: no successful recovery without durable actor+reason+audit
    # evidence. If the audit insert fails for any reason, the whole
    # transaction (including the CAS completion mutation) rolls back: the
    # child stays ERROR, the outbox stays ERROR/not APPLIED, and no
    # uuid_verifier is persisted. There is no partially-applied recovery.
    before = _snapshot(outbox, intent)
    observation_verifier = _sha(_canonical(
        {k: v for k, v in observed.items() if k != "uuid"}
    ))
    after = dict(before)
    after.update({"outbox_state": "APPLIED", "intent_observed_state": "ACTIVE"})
    try:
        repaired, mutation_id = _repair_and_audit_atomic(
            db, operation_id=operation_id, outcome="EXISTING", child_uuid=remote_uuid,
            remote_result_verifier=observation_verifier, account_id=account_id,
            actor_ref=actor_ref, reason=reason, idem_hash=idem_hash,
            before=before, after=after, now=timestamp,
        )
    except Exception as exc:
        refreshed = db._conn.execute(
            "SELECT state FROM mgboost_outbox WHERE operation_id=?", (operation_id,),
        ).fetchone()
        if refreshed is not None and refreshed["state"] == "APPLIED":
            return _finish("ALREADY_APPLIED")
        raise ChildRecoveryError("recovery completion failed") from exc
    return {
        "status": "REPAIRED", "operation_id": operation_id,
        "child_username": repaired["child_username"], "mutation_id": mutation_id,
    }


def preview_child_ensure_recovery(
    db, *, operation_id: str, observe_fn, now: int | None = None,
) -> dict:
    """Read-only, non-mutating, non-audited preview of the exact proof
    `repair_child_ensure` requires. Mirrors that function's checks in the
    same order and never diverges on what counts as proof -- but never
    writes an audit row (only a real repair attempt is audited) and never
    calls anything but the injected read-only ``observe_fn``. The raw
    remote UUID never leaves this function's stack frame; only a bounded
    safe-fact dict is returned."""
    timestamp = int(time.time()) if now is None else int(now)
    if not callable(observe_fn):
        raise ChildRecoveryError("a read-only observe_fn is required")

    outbox = db._conn.execute(
        "SELECT * FROM mgboost_outbox "
        "WHERE operation_id=? AND operation_kind='CHILD_USER_ENSURE'",
        (operation_id,),
    ).fetchone()
    if outbox is None:
        raise ChildRecoveryError("unknown CHILD_USER_ENSURE operation_id")
    account_id = int(outbox["account_id"])
    intent = db._conn.execute(
        "SELECT * FROM mgboost_child_user_intents WHERE id=? AND account_id=?",
        (outbox["child_intent_id"], account_id),
    ).fetchone()
    if intent is None:
        raise ChildRecoveryError("child intent does not belong to the operation's account")
    generation = db._conn.execute(
        "SELECT g.status,g.generation,g.slot_number,g.hwid_verifier "
        "FROM mgboost_device_slot_generations g WHERE g.id=? AND g.account_id=?",
        (intent["slot_generation_id"], account_id),
    ).fetchone()
    alias = db._conn.execute(
        "SELECT id FROM mgboost_legacy_account_aliases "
        "WHERE id=? AND account_id=? AND alias_role='PRIMARY'",
        (intent["source_alias_id"], account_id),
    ).fetchone()
    migration = None
    if generation is not None:
        migration = db._conn.execute(
            "SELECT state FROM mgboost_migration_bindings "
            "WHERE account_id=? AND hwid_verifier=?",
            (account_id, generation["hwid_verifier"]),
        ).fetchone()

    def _result(*, recoverable: bool, reason_class: str | None = None,
                remote_exists=None, username_match=None, contract_match=None,
                uuid_identity_provable=None) -> dict:
        return {
            "operation_id": operation_id,
            "slot_number": generation["slot_number"] if generation else None,
            "generation": generation["generation"] if generation else None,
            "child_username_masked": _mask_username(intent["child_username"]),
            "local_state": outbox["state"],
            "migration_state": migration["state"] if migration else None,
            "recoverable": recoverable,
            "reason_class": reason_class,
            "remote_exists": remote_exists,
            "username_match": username_match,
            "contract_match": contract_match,
            "uuid_identity_provable": uuid_identity_provable,
            "expected_action": "REPAIR" if recoverable else "NONE",
        }

    if outbox["state"] == "APPLIED":
        return _result(recoverable=False, reason_class="ALREADY_APPLIED")
    if outbox["state"] != "ERROR":
        return _result(recoverable=False, reason_class="NOT_ERROR_STATE")
    if generation is None or generation["status"] != "ACTIVE":
        return _result(recoverable=False, reason_class="GENERATION_NOT_ACTIVE")
    if alias is None:
        return _result(recoverable=False, reason_class="OWNER_ALIAS_MISSING")
    error_class = (outbox["last_error_class"] or "").strip()
    if error_class not in RECOVERABLE_ERROR_CLASSES:
        return _result(recoverable=False, reason_class="ERROR_CLASS_NOT_RECOVERABLE")

    try:
        wl_allowed = exact_wl_allowed_for_delivery(db, account_id=account_id, now=timestamp)
    except Exception:
        wl_allowed = False
    if not wl_allowed:
        return _result(recoverable=False, reason_class="POLICY_STILL_FORBIDS_WL")

    payload = validate_child_ensure_request(json.loads(outbox["payload_json"]))
    try:
        observed = observe_fn(payload)
    except Exception:
        return _result(recoverable=False, reason_class="REMOTE_OBSERVE_FAILED")
    presence = (observed or {}).get("presence")
    if presence == "ABSENT":
        return _result(recoverable=False, reason_class="REMOTE_MISSING", remote_exists=False)
    if presence != "MATCH":
        return _result(recoverable=False, reason_class="REMOTE_MISMATCH",
                       remote_exists=True, username_match=False)
    try:
        remote_uuid = str(uuid.UUID(observed.get("uuid"))).lower()
    except (ValueError, TypeError, AttributeError):
        return _result(recoverable=False, reason_class="REMOTE_MISMATCH", remote_exists=True)
    verifier = "sha256:" + _sha(remote_uuid)
    del remote_uuid  # never retained past this point
    if intent["uuid_verifier"] is not None and intent["uuid_verifier"] != verifier:
        return _result(recoverable=False, reason_class="UUID_VERIFIER_MISMATCH",
                       remote_exists=True, username_match=True, contract_match=True,
                       uuid_identity_provable=False)
    observed_tags = set((observed.get("inbounds") or {}).get("vless") or [])
    if (observed_tags & set(WL_INBOUND_TAGS)) and not wl_allowed:
        return _result(recoverable=False, reason_class="POLICY_STILL_FORBIDS_WL",
                       remote_exists=True, username_match=True, contract_match=True)
    if observed.get("protocols") != ["vless"]:
        return _result(recoverable=False, reason_class="REMOTE_MISMATCH",
                       remote_exists=True, username_match=True, contract_match=False)
    return _result(recoverable=True, remote_exists=True, username_match=True,
                   contract_match=True, uuid_identity_provable=True)


def _mask_username(username: str | None) -> str | None:
    if not username:
        return None
    if len(username) <= 4:
        return "***"
    return username[:2] + "***" + username[-2:]


def _refuse(db, account_id, actor_ref, reason, idem_hash, outbox, intent,
            reason_class: str, *, now: int, status: str = "REFUSED") -> dict:
    """Audit-and-refuse: no local state mutation, one honest evidence row."""
    before = _snapshot(outbox, intent)
    after = dict(before)
    after["repair_result"] = {"status": status, "reason_class": reason_class}
    mutation_id = _audit(db, account_id=account_id, actor_ref=actor_ref, reason=reason,
                         idem_hash=idem_hash, before=before, after=after, now=now)
    return {
        "status": status, "operation_id": outbox["operation_id"],
        "reason_class": reason_class, "mutation_id": mutation_id,
    }
