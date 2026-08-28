"""PH6-06 -- exact inbound-only WL quota-enforcement state machine.

Dormant, on-demand library + runner (matching the PH6-01..04 precedent):
nothing schedules this automatically yet, nothing routes admin traffic
through it yet. PH6-07 owns transactional-outbox wiring / periodic
reconciliation; PH6-09 owns outage fail-safe policy. This slice provides
the CORRECT MACHINE and the exact remote mutation semantics:

    ACTIVE -> DISABLE_PENDING -> DISABLED      (quota exhausted)
    DISABLED -> ENABLE_PENDING -> ACTIVE       (quota available again /
                                                reset / new period)
    mismatch/failure -> ERROR_RECONCILE        (never blindly mutated away)

Local DB is the source of DESIRED state. One `mgboost_wl_enforcement_states`
row per parent account records the current decision EPOCH plus the machine
state; every desired-direction change opens a fresh epoch and durably mints
operation rows -- `UNIQUE(account_id, epoch, child_intent_id)` per current
child. A claimed operation re-checks the stamped epoch against the LIVE
state row immediately before dispatch (the same anti-staleness guarantee as
PH3-08's parent-revision check): a newer decision has already superseded an
older-epoch op, which therefore can never be dispatched. This is what stops
a stale queued disable/enable from winning after the opposite decision.

Remote mutation semantics (single narrow broker op `child.user.wl.set`,
reusing the EXISTING narrow broker -- no second child-sync engine):

    reread child -> verify identity + UUID-verifier (fail closed)
    -> compute the exact target vless member list from authoritative live
       state and the static PH0-05 allowlist ONLY
    -> minimal partial update `{"inbounds": {"vless": target}}`
    -> reread/verify: membership == target, UUID/status/expire byte-stable

Proxies/UUID/expire/data_limit/status are never part of any payload.
Convergence claims come from LIVE state -- repeated/replayed dispatches
against an already-converged remote return ALREADY_IN_SYNC and perform zero
mutations, so "quota exhausted -> WL removed exactly once" holds by
observation, not by bookkeeping (the decisive lesson of the a68e265
false-convergence review: never answer convergence from a replayed key).

Crash/retry boundary. Between durable steps every op carries a bounded
lease (`IN_FLIGHT`, owner+expiry) mirroring the established outbox shape;
an expired lease is reclaimable:

  restart between local desired commit and remote mutation:
      op stays PENDING -> next cycle claims it and proceeds;
  restart after remote success but before local ACK:
      rediscovery re-freezes NOTHING (first-writer-wins manifest), finds by
      observation that the remote already matches the FROZEN target, and
      settles exactly once (the op row is terminal after its single
      APPLIED flip);
  Marzban outage / broker timeout / unknown response:
      exceptions map to RETRY with capped attempts; exhaustion lands the
      ACCOUNT in ERROR_RECONCILE -- recoverable only through the same
      verify-first paths, never by blind mutation.

Sibling children of one parent are handled independently (per-op lease,
per-op evidence, per-child error isolation); a partial/offline situation
converges the reachable children and flags the rest via ERROR_RECONCILE.
UNLIMITED-quota periods and accounts without any WL period produce NO
decision at all -- quota enforcement can never reach them (PH6-04's
`resolve_current_parent_wl_pool()` returning `None` means abstain, not
zero).

Interplay with device lifecycle: revoked children (terminal generation) are
structurally excluded by the same ACTIVE-generation join PH3-08 uses; slot-
paused children receive enforcement ops like everyone else (their status is
PH7-05's concern; inbound membership stays consistent either way); rebind
successors are new child rows picked up by the late-arrival rule.

Topology: THE one destructive capability of this module is gated behind a
FRESH PH6-01 assertion (`fetch_live_topology_observation` + run_assertion +
require_topology_ok()) executed at cycle start, before anything else --
a mismatch, a stale config version, or an unreachable topology check aborts
with zero transitions minted; unknown/mismatched topology can therefore
never produce a disable or enable.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from urllib.error import HTTPError

from .wl_enforcement_contract import (
    build_wl_target,
    derive_wl_operation_id,
    normalize_observed_vless,
    validate_wl_set_request,
)
from . import wl_topology as _wl_topology
from .wl_freshness import usage_freshness
from .wl_topology_guard import fetch_live_topology_observation
from .wl_topology_versions import tags_added_since
from .wl_parent_pool import resolve_current_parent_wl_pool

# Kept as a module-level name so the versioned PH0-05 baseline stays
# patchable/visible at this layer exactly as before.
WL_INBOUND_TAGS = _wl_topology.WL_INBOUND_TAGS


class WLEnforcementError(RuntimeError):
    pass


class WLEnforcementConflict(WLEnforcementError):
    pass


class RemoteChildMissing(WLEnforcementError):
    """The remote child user does not exist -- never auto-created here."""


MANIFEST_FROZEN = "FROZEN"
MANIFEST_EXISTING = "EXISTING"
MAX_ATTEMPTS = 8
DEFAULT_LEASE_SECONDS = 120
RETRY_DELAY_SECONDS = 60

_TERMINAL_FOR_DIRECTION = {"EXCLUDED": "DISABLED", "INCLUDED": "ACTIVE"}
_PENDING_FOR_DIRECTION = {"EXCLUDED": "DISABLE_PENDING", "INCLUDED": "ENABLE_PENDING"}
_SOURCE_FOR_DIRECTION = {
    "EXCLUDED": "QUOTA_EXCEEDED",
    "INCLUDED": "QUOTA_AVAILABLE",
}


def _canonical(value: dict) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def decide_direction_from_pool(pool: dict | None) -> str | None:
    """Pure policy: the desired WL inbound direction for one account.

    Returns None (= abstain) unless there is an enforceable LIMITED-quota
    signal: no active WL period at all, or an UNLIMITED-quota period, are
    both abstentions. Quota enforcement can consequently never reach a
    Non-WL or unlimited account."""
    if pool is None:
        return None
    if pool.get("quota_mode") != "LIMITED":
        return None
    return "EXCLUDED" if pool.get("exceeded") else "INCLUDED"


class WLEnforcementStore:
    def __init__(self, connection: sqlite3.Connection, lock):
        self._conn = connection
        self._lock = lock

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------
    def get_state(self, account_id: int) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM mgboost_wl_enforcement_states WHERE account_id=?",
            (int(account_id),),
        ).fetchone()
        return dict(row) if row is not None else None

    def get_op(self, operation_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM mgboost_wl_enforcement_ops WHERE operation_id=?",
            (operation_id,),
        ).fetchone()
        return dict(row) if row is not None else None

    def list_candidate_children(self, account_id: int) -> list[dict]:
        """Every *current* (non-terminal, live-generation) child of the
        account -- the exact structural join PH3-08 enqueues for, INCLUDING
        slot-paused children (their status belongs to PH7-05; enforcement
        keeps their inbound membership consistent regardless)."""
        rows = self._conn.execute(
            "SELECT ci.id AS child_intent_id, ci.account_id, ci.child_username, "
            "ci.uuid_verifier "
            "FROM mgboost_child_user_intents AS ci "
            "JOIN mgboost_device_slot_generations AS g ON g.id=ci.slot_generation_id "
            "WHERE ci.account_id=? AND g.status='ACTIVE' AND ci.desired_state!='REVOKED'",
            (int(account_id),),
        ).fetchall()
        return [dict(row) for row in rows]

    def epoch_ops(self, account_id: int, epoch: int) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM mgboost_wl_enforcement_ops WHERE account_id=? AND epoch=?",
            (int(account_id), int(epoch)),
        ).fetchall()
        return [dict(row) for row in rows]

    def latest_exclude_baseline(self, child_intent_id: int) -> tuple[list[str], str | None] | None:
        """The frozen baseline_full (+ the topology version it was frozen
        under) of the most recent prior EXCLUDED op for this child -- the
        only reference data an INCLUDED restore may use."""
        rows = self._conn.execute(
            "SELECT manifest_json FROM mgboost_wl_enforcement_ops "
            "WHERE child_intent_id=? AND direction='EXCLUDED' AND manifest_json IS NOT NULL "
            "ORDER BY epoch DESC, id DESC LIMIT 1",
            (int(child_intent_id),),
        ).fetchall()
        if not rows:
            return None
        manifest = json.loads(rows[0]["manifest_json"])
        return sorted(manifest["baseline_full"]), manifest.get("topology_version")

    def latest_include_baseline(self, child_intent_id: int) -> tuple[list[str], str | None] | None:
        """The frozen baseline_full (+ the topology version it was frozen
        under) of the most recent prior INCLUDED op for this child (PH6-07
        drift repair): a child that was INCLUDED from its very first epoch
        has no prior disable, so its own frozen pre-include observation is
        the only durable evidence of its legitimate full membership. Still
        only ever filtered through the static allowlist before anything is
        restored."""
        rows = self._conn.execute(
            "SELECT manifest_json FROM mgboost_wl_enforcement_ops "
            "WHERE child_intent_id=? AND direction='INCLUDED' AND manifest_json IS NOT NULL "
            "ORDER BY epoch DESC, id DESC LIMIT 1",
            (int(child_intent_id),),
        ).fetchall()
        if not rows:
            return None
        manifest = json.loads(rows[0]["manifest_json"])
        return sorted(manifest["baseline_full"]), manifest.get("topology_version")

    def unsettled_ops(self, account_id: int, epoch: int, *, now: int) -> list[dict]:
        """Every non-terminal op of the given epoch: PENDING/RETRY, plus
        leases whose owner died mid-flight. The exact set the full cycle
        drives; PH6-07 repair reuses it verbatim."""
        return [
            dict(row) for row in self._conn.execute(
                "SELECT * FROM mgboost_wl_enforcement_ops "
                "WHERE account_id=? AND epoch=? "
                "AND (state IN ('PENDING','RETRY') "
                "     OR (state='IN_FLIGHT' AND lease_expires_at<=?))",
                (int(account_id), int(epoch), int(now)),
            )
        ]

    def open_repair_epoch(self, account_id: int, *, direction: str, children: list[dict],
                          pool: dict | None, now: int) -> dict:
        """PH6-07 post-terminal drift repair (driven by
        `src.wl_reconciliation`, NOT a second engine): open a fresh epoch for
        the account's CURRENT desired direction and mint ops ONLY for the
        provably-drifted children (the late-arrival shape over a terminal
        state). Refuses anything but a terminal state already matching the
        direction with zero unsettled ops -- a mid-transition or errored
        account is the regular machine's business, never a repair's."""
        if direction not in ("EXCLUDED", "INCLUDED"):
            raise WLEnforcementError("invalid repair direction")
        if not children:
            raise WLEnforcementError("repair requires at least one drifted child")
        account_id = int(account_id)
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                state = self.get_state(account_id)
                if state is None or state["state"] not in ("ACTIVE", "DISABLED"):
                    raise WLEnforcementConflict(
                        "drift repair requires a terminal machine state"
                    )
                if state["last_direction"] != direction:
                    raise WLEnforcementConflict(
                        "drift repair direction must match the terminal state"
                    )
                if self.unsettled_ops(account_id, state["epoch"], now=now):
                    raise WLEnforcementConflict(
                        "drift repair refused while the live epoch has unsettled ops"
                    )
                self._open_epoch_locked(state, direction=direction, pool=pool, now=int(now))
                state = self.get_state(account_id)
                prepared = [
                    self._prepare_op_locked(state, child, now=int(now))
                    for child in children
                ]
                self._conn.commit()
                return {"state": self.get_state(account_id), "prepared": prepared}
            except Exception:
                self._conn.rollback()
                raise

    # ------------------------------------------------------------------
    # Decision application -- the only place epochs/states are opened
    # ------------------------------------------------------------------
    def apply_decision(self, account_id: int, *, pool: dict | None, now: int) -> dict | None:
        """One atomic decide-and-enqueue transaction for one account.

        Transition rules (S = machine row, D = decided direction):

          no row ............... D is None -> nothing at all is created
                                 (Non-WL/UNLIMITED accounts never even get a
                                 machine row); D given -> insert the row at
                                 epoch 1 straight into <D>_PENDING with ops
                                 for ALL current children.
          D is None ............ abstain: nothing touched, returns None.
          S terminal (ACTIVE /
           DISABLED) ........... terminal matches D and every current child
                                 has an op of the live epoch -> drive-only
                                 no-op (any unsettled ops are still handed
                                 back for retrying). Otherwise open a FRESH
                                 epoch for D: a real flip mints ops for ALL
                                 children, a late arrival (new/rebound child
                                 after convergence) only for the missing one.
          S <X>_PENDING ......... D == S.last_direction: the SAME transition
                                 continues in the SAME epoch -- unsettled ops
                                 are returned again and truly missing
                                 children get minted into it.
                                 D != S.last_direction: genuine flip ->
                                 fresh epoch for D over ALL children (old-
                                 epoch ops become undispatchable via the
                                 claim-time epoch guard).
          S ERROR_RECONCILE ..... identical to the PENDING cases: same
                                 direction continues the same epoch (its
                                 recovery path is verification-only, see
                                 `finalize_account`), opposite direction
                                 opens a fresh epoch.
        """
        timestamp = int(now)
        account_id = int(account_id)
        direction = decide_direction_from_pool(pool)
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                state = self.get_state(account_id)
                children = self.list_candidate_children(account_id)

                if state is None:
                    if direction is None:
                        # Minimal-footprint rule: an account with no
                        # enforceable LIMITED-quota signal never gets a
                        # machine row at all -- Non-WL/UNLIMITED accounts are
                        # structurally invisible to this module, they are
                        # only re-scanned cheaply each cycle.
                        self._conn.commit()
                        return None
                    self._insert_state_locked(
                        account_id, epoch=1, state=_PENDING_FOR_DIRECTION[direction],
                        direction=direction, pool=pool, now=timestamp,
                    )
                    state = self.get_state(account_id)
                    prepared = [
                        self._prepare_op_locked(state, child, now=timestamp)
                        for child in children
                    ]
                    self._conn.commit()
                    refreshed = self.get_state(account_id)
                    return {"state": refreshed, "prepared": prepared}

                if direction is None:
                    self._conn.commit()
                    return None

                live_epoch = state["epoch"]
                existing = {
                    int(row["child_intent_id"])
                    for row in self._conn.execute(
                        "SELECT child_intent_id FROM mgboost_wl_enforcement_ops "
                        "WHERE account_id=? AND epoch=?",
                        (account_id, live_epoch),
                    )
                }
                missing = [c for c in children if int(c["child_intent_id"]) not in existing]

                # Every non-terminal op of the CURRENT epoch -- PENDING/RETRY,
                # plus leases whose owner died mid-flight -- is always part of
                # what this evaluation hands back to the driver. Without this,
                # an op interrupted by a crash/outage between two invocations
                # could sit unclaimed forever.
                prepared = [
                    dict(row) for row in self._conn.execute(
                        "SELECT * FROM mgboost_wl_enforcement_ops "
                        "WHERE account_id=? AND epoch=? "
                        "AND (state IN ('PENDING','RETRY') "
                        "     OR (state='IN_FLIGHT' AND lease_expires_at<=?))",
                        (account_id, live_epoch, timestamp),
                    )
                ]

                if state["state"] in ("ACTIVE", "DISABLED"):
                    direction_matches = state["last_direction"] == direction
                    if direction_matches and not missing:
                        self._conn.commit()
                        return {"state": state, "prepared": prepared}
                    mintable = children if not direction_matches else missing
                    epoch_opened = True
                elif state["last_direction"] == direction:
                    if not missing:
                        self._conn.commit()
                        return {"state": state, "prepared": prepared}
                    # Late arrival mid-transition (PENDING/ERROR_RECONCILE,
                    # same direction): mint into the SAME epoch, never bump
                    # it. Bumping here would supersede any still-open
                    # sibling op of this epoch (PENDING/RETRY/IN_FLIGHT) via
                    # claim()'s epoch guard, letting finalize_account
                    # terminal-flip on a smaller op set than the one that
                    # must actually converge -- a real defect caught by
                    # `test_late_arrival_mid_transition_never_orphans_...`.
                    mintable = missing
                    epoch_opened = False
                else:
                    mintable = children
                    epoch_opened = True

                if epoch_opened:
                    self._open_epoch_locked(state, direction=direction, pool=pool, now=timestamp)
                    state = self.get_state(account_id)
                prepared += [
                    self._prepare_op_locked(state, child, now=timestamp)
                    for child in mintable
                ]
                self._conn.commit()
                refreshed = self.get_state(account_id)
                result = {"state": refreshed, "prepared": prepared}
                if epoch_opened:
                    result["epoch_opened"] = True
                return result
            except Exception:
                self._conn.rollback()
                raise

    def _insert_state_locked(self, account_id, *, epoch, state, direction, pool, source=None,
                             now=int(time.time())) -> None:
        self._conn.execute(
            "INSERT INTO mgboost_wl_enforcement_states "
            "(account_id,epoch,state,last_direction,wl_period_id,decision_source,"
            "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (
                int(account_id), int(epoch), state, direction,
                pool["wl_period_id"] if pool else None,
                source or _SOURCE_FOR_DIRECTION.get(direction, "NONE_QUOTA_SIGNAL"),
                int(now), int(now),
            ),
        )

    def _open_epoch_locked(self, state: dict, *, direction, pool, now: int) -> None:
        updated = self._conn.execute(
            "UPDATE mgboost_wl_enforcement_states SET epoch=?,state=?,"
            "last_direction=?,wl_period_id=?,decision_source=?,updated_at=?,"
            "row_version=row_version+1 WHERE account_id=? AND epoch=? AND row_version=?",
            (
                state["epoch"] + 1, _PENDING_FOR_DIRECTION[direction], direction,
                pool["wl_period_id"] if pool else None, _SOURCE_FOR_DIRECTION[direction],
                int(now), state["account_id"], state["epoch"], state["row_version"],
            ),
        ).rowcount
        if updated != 1:
            raise WLEnforcementConflict("wl enforcement state changed concurrently")

    def _set_machine_state_locked(self, state: dict, new_state: str, now: int) -> None:
        updated = self._conn.execute(
            "UPDATE mgboost_wl_enforcement_states SET state=?,updated_at=?,"
            "row_version=row_version+1 WHERE account_id=? AND epoch=? AND row_version=?",
            (new_state, int(now), state["account_id"], state["epoch"], state["row_version"]),
        ).rowcount
        if updated != 1:
            raise WLEnforcementConflict("wl enforcement state changed concurrently")

    def mark_reconcile_error(self, account_id: int, *, safe_error_class: str, now: int) -> None:
        """Idempotently flag the account ERROR_RECONCILE (entry point for
        verified failures surfaced outside an op row)."""
        safe_error = (safe_error_class or "").strip()
        if not safe_error or len(safe_error) > 128:
            raise WLEnforcementError("safe error class is required")
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                state = self.get_state(int(account_id))
                if state is not None and state["state"] != "ERROR_RECONCILE":
                    self._set_machine_state_locked(state, "ERROR_RECONCILE", now)
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    # ------------------------------------------------------------------
    # Durable per-(epoch, child) operations
    # ------------------------------------------------------------------
    def _prepare_op_locked(self, state: dict, child: dict, *, now: int) -> dict:
        direction = state["last_direction"]
        epoch = state["epoch"]
        operation_id = derive_wl_operation_id(child["child_username"], epoch, direction)
        payload = {
            "operation_id": operation_id,
            "child_username": child["child_username"],
            "uuid_verifier": child["uuid_verifier"],
            "direction": direction,
            "baseline_wl_tags": None,
        }
        payload_json = _canonical(payload)
        request_hash = _sha(payload_json)
        existing = self._conn.execute(
            "SELECT * FROM mgboost_wl_enforcement_ops WHERE operation_id=?",
            (operation_id,),
        ).fetchone()
        if existing:
            if existing["request_hash"] != request_hash:
                raise WLEnforcementConflict(
                    "an enforcement op with this identity exists with different content"
                )
            return dict(existing)
        cursor = self._conn.execute(
            "INSERT INTO mgboost_wl_enforcement_ops "
            "(account_id,epoch,child_intent_id,direction,operation_id,state,"
            "payload_json,request_hash,next_attempt_at,created_at,updated_at) "
            "VALUES (?,?,?,?,?,'PENDING',?,?,?,?,?)",
            (
                state["account_id"], epoch, child["child_intent_id"], direction,
                operation_id, payload_json, request_hash, now, now, now,
            ),
        )
        row = self._conn.execute(
            "SELECT * FROM mgboost_wl_enforcement_ops WHERE id=?", (cursor.lastrowid,)
        ).fetchone()
        return dict(row)

    def _event(self, op_row_id, account_id, epoch, attempt_no, event_type, *,
               outcome=None, safe_error_class=None, now):
        self._conn.execute(
            "INSERT INTO mgboost_wl_enforcement_events "
            "(op_row_id,account_id,epoch,attempt_no,event_type,outcome,"
            "safe_error_class,created_at) VALUES (?,?,?,?,?,?,?,?)",
            (op_row_id, account_id, epoch, max(1, int(attempt_no)), event_type, outcome,
             safe_error_class, int(now)),
        )

    def claim(self, operation_id: str, *, worker_id: str, now: int,
              lease_seconds: int = DEFAULT_LEASE_SECONDS) -> dict | None:
        if not isinstance(worker_id, str) or not 3 <= len(worker_id) <= 128:
            raise WLEnforcementError("invalid worker identity")
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                row = self._conn.execute(
                    "SELECT * FROM mgboost_wl_enforcement_ops WHERE operation_id=?",
                    (operation_id,),
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
                live_state = self.get_state(row["account_id"])
                superseded = (
                    live_state is None
                    or live_state["epoch"] != row["epoch"]
                    or live_state["last_direction"] != row["direction"]
                )
                if superseded:
                    # A newer decision epoch supersedes this op. It is NEVER
                    # dispatched -- the anti-stale-mutation guarantee for both
                    # stale-disable and stale-enable races. (The schema CHECK
                    # admits exactly PENDING/IN_FLIGHT/RETRY/APPLIED/ERROR, so
                    # supersession shows up as an event, not a fifth state.)
                    self._conn.execute(
                        "UPDATE mgboost_wl_enforcement_ops SET lease_owner=NULL,"
                        "lease_expires_at=NULL,updated_at=?,row_version=row_version+1 "
                        "WHERE id=?",
                        (now, row["id"]),
                    )
                    self._event(row["id"], row["account_id"], row["epoch"],
                                row["attempts"], "SUPERSEDED", now=now)
                    self._conn.commit()
                    return None
                attempt = row["attempts"] + 1
                if attempt > MAX_ATTEMPTS:
                    # Bounded retry: attempt N+1 beyond the cap converts the
                    # op to permanent ERROR instead of parking it forever --
                    # finalization then flags ERROR_RECONCILE honestly.
                    self._conn.execute(
                        "UPDATE mgboost_wl_enforcement_ops SET state='ERROR',"
                        "last_error_class=COALESCE(last_error_class,"
                        "'ATTEMPTS_EXHAUSTED'),lease_owner=NULL,lease_expires_at=NULL,"
                        "updated_at=?,row_version=row_version+1 WHERE id=?",
                        (now, row["id"]),
                    )
                    self._event(row["id"], row["account_id"], row["epoch"],
                                row["attempts"], "FAILED",
                                safe_error_class="ATTEMPTS_EXHAUSTED", now=now)
                    self._conn.commit()
                    return None
                self._conn.execute(
                    "UPDATE mgboost_wl_enforcement_ops SET state='IN_FLIGHT',attempts=?,"
                    "lease_owner=?,lease_expires_at=?,updated_at=?,row_version=row_version+1 "
                    "WHERE id=?",
                    (attempt, worker_id, now + max(5, int(lease_seconds)), now, row["id"]),
                )
                self._event(row["id"], row["account_id"], row["epoch"], attempt,
                            "STARTED", now=now)
                claimed = self._conn.execute(
                    "SELECT * FROM mgboost_wl_enforcement_ops WHERE id=?", (row["id"],)
                ).fetchone()
                self._conn.commit()
                result = dict(claimed)
                result["payload"] = json.loads(result.pop("payload_json"))
                raw_manifest = result.pop("manifest_json")
                result["manifest"] = json.loads(raw_manifest) if raw_manifest else None
                return result
            except Exception:
                self._conn.rollback()
                raise

    def record_manifest(self, operation_id: str, *, worker_id: str, manifest: dict,
                        now: int) -> str:
        """Freeze the observed baseline/target for this op. FIRST WRITER
        WINS: later attempts (crash-retry, second worker after lease expiry)
        reuse the frozen values verbatim instead of re-deriving them from
        possibly-drifted remote state."""
        encoded = _canonical(manifest)
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                row = self._conn.execute(
                    "SELECT * FROM mgboost_wl_enforcement_ops "
                    "WHERE operation_id=? AND state='IN_FLIGHT' AND lease_owner=?",
                    (operation_id, worker_id),
                ).fetchone()
                if not row:
                    raise WLEnforcementConflict("enforcement lease is not owned by worker")
                if row["manifest_json"] is not None:
                    self._conn.commit()
                    return MANIFEST_EXISTING
                self._conn.execute(
                    "UPDATE mgboost_wl_enforcement_ops SET manifest_json=?,updated_at=?,"
                    "row_version=row_version+1 WHERE id=? AND manifest_json IS NULL",
                    (encoded, now, row["id"]),
                )
                self._event(row["id"], row["account_id"], row["epoch"], row["attempts"],
                            "MANIFEST_FROZEN", now=now)
                self._conn.commit()
                return MANIFEST_FROZEN
            except Exception:
                self._conn.rollback()
                raise

    def acknowledge(self, operation_id: str, *, worker_id: str, outcome: str,
                    now: int) -> dict:
        if outcome not in {"SYNCED", "ALREADY_IN_SYNC"}:
            raise WLEnforcementError("invalid enforcement outcome")
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                row = self._conn.execute(
                    "SELECT * FROM mgboost_wl_enforcement_ops "
                    "WHERE operation_id=? AND state='IN_FLIGHT' AND lease_owner=?",
                    (operation_id, worker_id),
                ).fetchone()
                if not row:
                    raise WLEnforcementConflict("enforcement lease is not owned by worker")
                self._conn.execute(
                    "UPDATE mgboost_wl_enforcement_ops SET state='APPLIED',"
                    "lease_owner=NULL,lease_expires_at=NULL,last_error_class=NULL,"
                    "updated_at=?,row_version=row_version+1 WHERE id=?",
                    (now, row["id"]),
                )
                self._event(row["id"], row["account_id"], row["epoch"], row["attempts"],
                            "SUCCEEDED", outcome=outcome, now=now)
                result = self._conn.execute(
                    "SELECT * FROM mgboost_wl_enforcement_ops WHERE id=?", (row["id"],)
                ).fetchone()
                self._conn.commit()
                return dict(result)
            except Exception:
                self._conn.rollback()
                raise

    def record_error(self, operation_id: str, *, error_class: str, now: int) -> dict:
        safe_error = (error_class or "").strip()
        if not safe_error or len(safe_error) > 128:
            raise WLEnforcementError("safe error class is required")
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                row = self._conn.execute(
                    "SELECT * FROM mgboost_wl_enforcement_ops "
                    "WHERE operation_id=? AND state='IN_FLIGHT'",
                    (operation_id,),
                ).fetchone()
                if not row:
                    raise WLEnforcementConflict("no in-flight enforcement op to fail")
                self._conn.execute(
                    "UPDATE mgboost_wl_enforcement_ops SET state='ERROR',"
                    "last_error_class=?,lease_owner=NULL,lease_expires_at=NULL,"
                    "updated_at=?,row_version=row_version+1 WHERE id=?",
                    (safe_error, now, row["id"]),
                )
                self._event(row["id"], row["account_id"], row["epoch"], row["attempts"],
                            "FAILED", safe_error_class=safe_error, now=now)
                result = self._conn.execute(
                    "SELECT * FROM mgboost_wl_enforcement_ops WHERE id=?", (row["id"],)
                ).fetchone()
                self._conn.commit()
                return dict(result)
            except Exception:
                self._conn.rollback()
                raise

    def retry_later(self, operation_id: str, *, delay_seconds: int, now: int) -> dict:
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                row = self._conn.execute(
                    "SELECT * FROM mgboost_wl_enforcement_ops "
                    "WHERE operation_id=? AND state='IN_FLIGHT'",
                    (operation_id,),
                ).fetchone()
                if not row:
                    raise WLEnforcementConflict("no in-flight enforcement op to retry")
                self._conn.execute(
                    "UPDATE mgboost_wl_enforcement_ops SET state='RETRY',"
                    "lease_owner=NULL,lease_expires_at=NULL,next_attempt_at=?,"
                    "updated_at=?,row_version=row_version+1 WHERE id=?",
                    (now + max(1, int(delay_seconds)), now, row["id"]),
                )
                self._event(row["id"], row["account_id"], row["epoch"], row["attempts"],
                            "FAILED", now=now)
                result = self._conn.execute(
                    "SELECT * FROM mgboost_wl_enforcement_ops WHERE id=?", (row["id"],)
                ).fetchone()
                self._conn.commit()
                return dict(result)
            except Exception:
                self._conn.rollback()
                raise

    def finalize_account(self, account_id: int, *, verify_fn, now: int) -> dict:
        """Drive the machine row to terminal (or ERROR_RECONCILE) based ONLY
        on (a) every current-epoch op being APPLIED and (b) an independent
        live reread of each touched child matching its frozen target.

        `verify_fn(op_row_dict) -> bool` performs the reread; returning
        False or raising counts as unverifiable drift, which can only ever
        FLAG the account (ERROR_RECONCILE), never mutate anything."""
        account_id = int(account_id)
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                state = self.get_state(account_id)
                if state is None or state["state"] in ("ACTIVE", "DISABLED"):
                    self._conn.commit()
                    return {"flipped": None}
                epoch = state["epoch"]
                ops = self.epoch_ops(account_id, epoch)
                if not ops:
                    # A flip opened with zero candidate children converges
                    # trivially (nothing remote belongs to this account).
                    terminal = _TERMINAL_FOR_DIRECTION[state["last_direction"]]
                    self._set_machine_state_locked(state, terminal, now)
                    self._conn.commit()
                    return {"flipped": terminal, "reason": "no_current_children"}
                non_applied = [o for o in ops if o["state"] != "APPLIED"]
                errored = [o for o in non_applied if o["state"] == "ERROR"]
                if errored:
                    if state["state"] != "ERROR_RECONCILE":
                        self._set_machine_state_locked(state, "ERROR_RECONCILE", now)
                        for op in errored:
                            self._event(op["id"], account_id, epoch, op["attempts"],
                                        "VERIFY_FAILED", safe_error_class="OP_ERRORED",
                                        now=now)
                    self._conn.commit()
                    return {"flipped": None, "reason": "ops_errored"}
                if non_applied:
                    self._conn.commit()
                    return {"flipped": None, "reason": "ops_not_settled"}
                drifted = []
                for op in ops:
                    try:
                        ok = bool(verify_fn(dict(op)))
                    except Exception:  # noqa: BLE001 -- unverifiable ≠ converged
                        ok = False
                    if not ok:
                        drifted.append(op)
                if drifted:
                    if state["state"] != "ERROR_RECONCILE":
                        self._set_machine_state_locked(state, "ERROR_RECONCILE", now)
                    for op in drifted:
                        self._event(op["id"], account_id, epoch, op["attempts"],
                                    "VERIFY_FAILED", safe_error_class="LIVE_REREAD_DRIFT",
                                    now=now)
                    self._conn.commit()
                    return {"flipped": None, "reason": "live_reread_drift"}
                terminal = _TERMINAL_FOR_DIRECTION[state["last_direction"]]
                self._set_machine_state_locked(state, terminal, now)
                self._conn.commit()
                return {"flipped": terminal}
            except Exception:
                self._conn.rollback()
                raise


# ----------------------------------------------------------------------
# Observation helper: strict-shape a live child through the EXISTING
# read-only `legacy.user.get` broker surface (zero new reads introduced).
# ----------------------------------------------------------------------

def observe_child_vless(service_marzban, child_username: str) -> list[str]:
    try:
        raw = service_marzban.get_user(child_username)
    except HTTPError as exc:
        if exc.code == 404:
            raise RemoteChildMissing(f"remote child absent: {child_username[:4]}...")
        raise
    return normalize_observed_vless(raw)


# ----------------------------------------------------------------------
# One operation end-to-end: observe -> freeze manifest -> mutate -> settle.
# Every step is safe to repeat; convergence comes from live remote state.
# ----------------------------------------------------------------------

def process_wl_op(db, operation_id: str, *, worker_id: str, service_marzban, now: int) -> dict | None:
    store: WLEnforcementStore = db.wl_enforcement
    claimed = store.claim(operation_id, worker_id=worker_id, now=now)
    if claimed is None:
        return None
    payload = claimed["payload"]

    if claimed["manifest"] is not None:
        manifest = claimed["manifest"]
        try:
            result = _dispatch_frozen_manifest(store, claimed, payload, manifest,
                                               service_marzban, worker_id=worker_id, now=now)
        except RemoteChildMissing:
            store.record_error(operation_id, error_class="REMOTE_MISSING", now=now)
            return None
        except Exception as exc:  # noqa: BLE001 -- broker timeout/unknown response
            error_class = type(exc).__name__[:120] or "UNKNOWN"
            store.retry_later(operation_id, delay_seconds=RETRY_DELAY_SECONDS, now=now)
            return None
    else:
        try:
            observed = observe_child_vless(service_marzban, payload["child_username"])
            result = _derive_freeze_and_dispatch(
                store, claimed, payload, observed, service_marzban, worker_id=worker_id, now=now,
            )
            if result is None:  # degenerate-but-permanent local verdicts
                return None
        except RemoteChildMissing:
            store.record_error(operation_id, error_class="REMOTE_MISSING", now=now)
            return None
        except Exception as exc:  # noqa: BLE001 -- outage/timeout on read path too
            error_class = type(exc).__name__[:120] or "UNKNOWN"
            store.retry_later(operation_id, delay_seconds=RETRY_DELAY_SECONDS, now=now)
            return None
    if result["outcome"] == "REMOTE_MISSING":
        store.record_error(operation_id, error_class="REMOTE_MISSING", now=now)
        return None
    return store.acknowledge(
        operation_id, worker_id=worker_id, outcome=result["outcome"], now=now,
    )


def _dispatch_frozen_manifest(store, claimed, payload, manifest, service_marzban, *,
                              worker_id, now):
    """Replay/recovery path: the frozen manifest IS the intent. The broker
    still verifies everything against live state before touching anything;
    an already-converged remote answers ALREADY_IN_SYNC with zero writes."""
    request_payload = {
        "operation_id": payload["operation_id"],
        "child_username": payload["child_username"],
        "uuid_verifier": payload["uuid_verifier"],
        "direction": payload["direction"],
        "baseline_wl_tags": (
            sorted(manifest["removed_wl"]) if payload["direction"] == "INCLUDED" else None
        ),
    }
    request = validate_wl_set_request(request_payload)
    return service_marzban.set_child_wl_state(request)


def _derive_freeze_and_dispatch(store, claimed, payload, observed, service_marzban, *,
                                worker_id, now) -> dict | None:
    """Fresh-dispatch path: derive the target from live observation + the
    static allowlist, freeze it first-writer-wins, then dispatch."""
    operation_id = claimed["operation_id"]
    baseline_full = sorted(observed)
    if payload["direction"] == "EXCLUDED":
        removed_wl = sorted(set(observed) & set(WL_INBOUND_TAGS))
        target = sorted(set(observed) - set(WL_INBOUND_TAGS))
        if not target:
            # Fail closed BEFORE freezing anything: removing every vless
            # inbound from a child can never be what quota enforcement meant.
            store.record_error(
                operation_id, error_class="WOULD_REMOVE_ALL_INBOUNDS", now=now,
            )
            return None
    else:
        baseline_full_prior, prior_topology_version = (
            store.latest_exclude_baseline(claimed["child_intent_id"])
            or (None, None)
        )
        if baseline_full_prior is None:
            # PH6-07 repair path for a child that was INCLUDED from its very
            # first epoch (no prior disable exists): the child's own frozen
            # INCLUDED observation is the only durable evidence of its
            # legitimate membership. Same discipline as the EXCLUDED baseline:
            # frozen evidence + allowlist filter, never live-derived trust.
            baseline_full_prior, prior_topology_version = (
                store.latest_include_baseline(claimed["child_intent_id"])
                or (None, None)
            )
        if baseline_full_prior is None:
            store.record_error(
                operation_id, error_class="NO_BASELINE_FOR_INCLUDE", now=now,
            )
            return None
        # The recorded baseline is the child's FULL pre-disable member list;
        # only its PH0-05 allowlisted subset may ever be restored. On top of
        # that proven subset, EXACTLY the tags an operator-approved versioned
        # baseline update added since this child last converged (DL-059) join
        # the target -- legitimate topology convergence, provably scoped by
        # the durable version registry; anything unverifiable adds nothing.
        newly_approved = tags_added_since(
            store._conn, _wl_topology.WL_INBOUND_TAGS, prior_topology_version,
        )
        baseline_wl_tags = sorted(
            (set(baseline_full_prior) & set(WL_INBOUND_TAGS)) | newly_approved
        )
        if not baseline_wl_tags:
            # The source never carried any WL inbound: INCLUDE is
            # definitionally a no-op -- settle honestly by observation.
            store.record_manifest(
                operation_id, worker_id=worker_id,
                manifest={
                    "baseline_full": baseline_full,
                    "target": sorted(observed),
                    "removed_wl": [],
                },
                now=now,
            )
            return {"outcome": "ALREADY_IN_SYNC"}
        target = build_wl_target(observed, "INCLUDED", baseline_wl_tags)
        removed_wl = baseline_wl_tags
    manifest = {
        "baseline_full": baseline_full,
        "target": target,
        "removed_wl": removed_wl,
        # PH6-09: the PH0-05 config version this frozen evidence was derived
        # under -- what makes a later topology expansion provably scoped
        # (tags_added_since) instead of guessed.
        "topology_version": _wl_topology.WL_TOPOLOGY_VERSION,
    }
    store.record_manifest(operation_id, worker_id=worker_id, manifest=manifest, now=now)
    return _dispatch_frozen_manifest(store, claimed, payload, manifest,
                                     service_marzban, worker_id=worker_id, now=now)


# ----------------------------------------------------------------------
# Full cycle: fresh topology gate -> per-account decisions -> drive ops ->
# live-verified finalization. Never wired to a scheduler inside this slice.
# ----------------------------------------------------------------------

def _accounts_in_scope(conn: sqlite3.Connection) -> list[int]:
    rows = conn.execute(
        "SELECT DISTINCT ci.account_id FROM mgboost_child_user_intents AS ci "
        "JOIN mgboost_device_slot_generations AS g ON g.id=ci.slot_generation_id "
        "WHERE g.status='ACTIVE' AND ci.desired_state!='REVOKED'"
    ).fetchall()
    ids = {int(row["account_id"]) for row in rows}
    for row in conn.execute("SELECT account_id FROM mgboost_wl_enforcement_states"):
        ids.add(int(row["account_id"]))
    return sorted(ids)


def _op_username(op: dict) -> str:
    return json.loads(op["payload_json"])["child_username"]


def _op_target(op: dict) -> list[str]:
    manifest = json.loads(op["manifest_json"]) if op["manifest_json"] else {}
    return sorted(manifest.get("target", []))


def drive_account_ops(db, service_marzban, account_id: int, *, worker_id: str,
                      now: int) -> dict:
    """Dispatch every unsettled op of the account's LIVE epoch and drive the
    machine row to terminal (or ERROR_RECONCILE) with the usual independent
    live reread. The full cycle uses this after each account decision; PH6-07
    drift repair reuses it verbatim so repaired accounts converge through the
    EXACT same engine path (same claim guard, same manifest discipline, same
    verify) instead of a parallel one."""
    store: WLEnforcementStore = db.wl_enforcement
    result = {"ops_applied": 0, "ops_errored": 0, "flipped": None}

    def verify_one(op: dict) -> bool:
        try:
            observed = observe_child_vless(service_marzban, _op_username(op))
        except Exception:  # noqa: BLE001 -- unverifiable is not convergence
            return False
        return observed == _op_target(op)

    state = store.get_state(account_id)
    if state is None:
        return result
    for op in store.unsettled_ops(account_id, state["epoch"], now=now):
        process_wl_op(
            db, op["operation_id"], worker_id=worker_id,
            service_marzban=service_marzban, now=now,
        )
        fresh = store.get_op(op["operation_id"])
        if fresh and fresh["state"] == "APPLIED":
            result["ops_applied"] += 1
        elif fresh and fresh["state"] == "ERROR":
            result["ops_errored"] += 1
    finalized = store.finalize_account(account_id, verify_fn=verify_one, now=now)
    result["flipped"] = finalized.get("flipped")
    return result


def run_wl_enforcement_cycle(
    *,
    db,
    service_marzban,
    worker_id: str,
    now: int | None = None,
    topology_observer=None,
) -> dict:
    """One enforcement pass across every account in scope. All-or-nothing on
    the topology gate (fresh assertion + require_topology_ok BEFORE anything
    else): unknown/mismatched/unreachable topology aborts with zero
    transitions minted. Per-account failures afterwards are isolated and
    counted, mirroring the PH6-03 collector's error discipline."""
    timestamp = int(time.time()) if now is None else int(now)
    observer = topology_observer or (
        lambda: fetch_live_topology_observation(
            service_marzban, service_marzban.get_admin_token_from_env(),
        )
    )
    observed_tags, observed_nodes = observer()
    db.wl_topology_guard.run_assertion(observed_tags, observed_nodes, now=timestamp)
    db.wl_topology_guard.require_topology_ok()

    summary = {
        "accounts_evaluated": 0,
        "accounts_abstained": 0,
        "accounts_skipped_stale_usage": 0,
        "usage_fresh": None,
        "epochs_opened": 0,
        "ops_prepared": 0,
        "ops_applied": 0,
        "ops_errored": 0,
        "accounts_disabled": 0,
        "accounts_enabled": 0,
        "accounts_error_reconcile": 0,
        "errors": [],
        "outcome": "OK",
    }
    store: WLEnforcementStore = db.wl_enforcement

    # PH6-09 freshness contract, evaluated ONCE per cycle: access-increasing
    # (INCLUDED) decisions need a fresh trusted collector observation.
    # Access-decreasing (EXCLUDED) decisions are intentionally NOT gated --
    # a stale ledger can only under-count, never fabricate quota exhaustion,
    # so telemetry loss can never mass-disable already-active users.
    freshness = usage_freshness(db, now=timestamp)
    summary["usage_fresh"] = bool(freshness["fresh"])

    for account_id in _accounts_in_scope(db._conn):
        summary["accounts_evaluated"] += 1
        try:
            pool = resolve_current_parent_wl_pool(db, account_id=account_id, now=timestamp)
            if (
                decide_direction_from_pool(pool) == "INCLUDED"
                and not freshness["fresh"]
            ):
                # Fail closed: no enable/restore on stale or unknown usage.
                summary["accounts_skipped_stale_usage"] += 1
                continue
            decision = store.apply_decision(account_id, pool=pool, now=timestamp)
            if decision is None:
                summary["accounts_abstained"] += 1
                continue
            if decision.pop("epoch_opened", False):
                summary["epochs_opened"] += 1
            summary["ops_prepared"] += len(decision["prepared"])
            driven = drive_account_ops(
                db, service_marzban, account_id, worker_id=worker_id, now=timestamp,
            )
            summary["ops_applied"] += driven["ops_applied"]
            summary["ops_errored"] += driven["ops_errored"]
            flipped = driven["flipped"]
            if flipped == "DISABLED":
                summary["accounts_disabled"] += 1
            elif flipped == "ACTIVE":
                summary["accounts_enabled"] += 1
            state_after = store.get_state(account_id)
            if state_after and state_after["state"] == "ERROR_RECONCILE":
                summary["accounts_error_reconcile"] += 1
        except Exception as exc:  # noqa: BLE001 -- per-account isolation
            summary["errors"].append(type(exc).__name__)
    if summary["errors"]:
        summary["outcome"] = "PARTIAL"
    return summary
