"""PH6-07 -- continuous convergence around the EXISTING PH6-06 engine.

This module does NOT reimplement any enforcement: there is no second
desired-state derivation, no second outbox, no second remote-mutation path.
It wraps the existing `run_wl_enforcement_cycle` / `WLEnforcementStore` /
epoch/op/lease/manifest machinery with exactly the three things the on-demand
PH6-06 slice deliberately did not have:

1. **Orchestrated cycle lifecycle** (`run_wl_reconciliation_cycle`) -- the
   single entry point shared by the systemd timer (SCHEDULED) and manual
   operator runs (MANUAL). One bounded invocation: acquire the process-wide
   cycle lock (non-blocking flock -- overlap is refused, never queued), then
   the EXISTING engine cycle (fresh PH6-01 topology assertion inside,
   fail-closed), then the terminal drift scan, then same-cycle convergence
   of anything the scan armed via the engine's own drive path. Every cycle
   is durably recorded for the operator read model.

2. **Post-terminal drift detection** (`scan_terminal_drift`) -- an account
   whose machine row is already ACTIVE/DISABLED with every live-epoch op
   APPLIED is re-observed on cadence (the PH6-06 verify only ever fired for
   NEWLY minted ops). The desired direction is re-derived from the canonical
   PH6-04 read model each scan; a terminal account whose fresh decision is
   absent (period over) or flipped (engine's business) is skipped, so the
   scan only ever judges accounts whose entitlement still proves the frozen
   direction. Findings are classified exactly, against the static PH0-05
   allowlist only:

       WL_PRESENT_WHILE_EXCLUDED      -> repairable (the suspended child's
                                         membership gained an exact WL tag:
                                         manual re-add, or Marzban's
                                         persistent excluded_inbounds
                                         silently including a NEWLY-ADDED
                                         approved WL inbound)
       WL_MISSING_WHILE_INCLUDED      -> repairable (entitled WL tag gone;
                                         target proven by the child's own
                                         frozen APPLIED manifest)
       WL_UNEXPECTED_WHILE_INCLUDED   -> flagged only (entitled direction,
                                         but membership changed in an
                                         unproven way -- never guessed at)
       NON_WL_MEMBERSHIP_LOST         -> flagged only (this machinery can
                                         never restore non-WL inbounds)
       REMOTE_MISSING                 -> flagged only, never auto-created
       UUID_MISMATCH / REMOTE_UNREADABLE -> flagged only, zero mutation

   Repair is the EXISTING machine, not a new one:
   `WLEnforcementStore.open_repair_epoch` opens a fresh same-direction epoch
   over ONLY the provably drifted children (the late-arrival shape), and the
   ops converge through `drive_account_ops` -- the exact claim guard /
   manifest freeze / observe-mutate-verify / bounded-retry path every other
   enforcement op takes. Exactly-once stays observational: a replayed repair
   against an already-converged remote is ALREADY_IN_SYNC with zero writes.

3. **Backlog / observability** (`backlog_snapshot` + the two reconciliation
   tables) -- an operator-grade, identifier-free read model: cycle history
   and outcomes, topology assertion status, account state counts, op/backlog
   counts and age, drift counters, last error class, scheduler heartbeat.

Safety invariants inherited unchanged: UNLIMITED-quota and no-WL-signal
accounts produce no decision, no state row, no op, no scan action (the P0
legacy abstain contract); STANDARD (`wl_mode='NONE'`) never enters scope
because it never has a WL period; topology unknown/mismatched/unreachable
blocks the WHOLE cycle (engine and scan) before any observation is judged.
"""

from __future__ import annotations

import fcntl
import json
import sqlite3
import time
import uuid as uuid_module
from urllib.error import HTTPError

from . import wl_enforcement as _wl_enforcement
from .child_contract import credential_verifier
from .wl_enforcement import (
    RemoteChildMissing,
    WLEnforcementError,
    decide_direction_from_pool,
    drive_account_ops,
    normalize_observed_vless,
    run_wl_enforcement_cycle,
)
from .wl_reconciliation_schema import apply_wl_reconciliation_schema
from .wl_topology_guard import (
    TopologyMismatchError,
    fetch_live_topology_observation,
)


class WLReconciliationError(RuntimeError):
    pass


def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


class WLReconciliationStore:
    def __init__(self, connection: sqlite3.Connection, lock):
        self._conn = connection
        self._lock = lock

    def record_cycle_start(self, trigger: str, *, now: int) -> int:
        if trigger not in ("SCHEDULED", "MANUAL"):
            raise WLReconciliationError("invalid cycle trigger")
        cursor = self._conn.execute(
            "INSERT INTO mgboost_wl_reconciliation_cycles (trigger,started_at) "
            "VALUES (?,?)",
            (trigger, int(now)),
        )
        self._conn.commit()
        return int(cursor.lastrowid)

    def record_cycle_finish(self, cycle_id: int, *, outcome: str, now: int,
                            config_version: str | None = None, topology_ok: int = 1,
                            engine_json: str | None = None,
                            drift_detected: int = 0, drift_repaired: int = 0,
                            drift_flagged: int = 0, last_error_class: str | None = None,
                            summary_json: str | None = None) -> None:
        if outcome not in ("OK", "PARTIAL", "BLOCKED_TOPOLOGY", "ERROR"):
            raise WLReconciliationError("invalid cycle outcome")
        if topology_ok not in (0, 1):
            raise WLReconciliationError("invalid topology flag")
        updated = self._conn.execute(
            "UPDATE mgboost_wl_reconciliation_cycles SET finished_at=?,outcome=?,"
            "config_version=?,topology_ok=?,engine_json=?,drift_detected=?,"
            "drift_repaired=?,drift_flagged=?,last_error_class=?,summary_json=? "
            "WHERE id=? AND finished_at IS NULL",
            (
                int(now), outcome, config_version, topology_ok, engine_json,
                int(drift_detected), int(drift_repaired), int(drift_flagged),
                last_error_class, summary_json, int(cycle_id),
            ),
        ).rowcount
        self._conn.commit()
        if updated != 1:
            raise WLReconciliationError("cycle row already finished")

    def record_drift(self, account_id: int, *, child_intent_id: int | None,
                     drift_class: str, action: str, epoch: int | None, now: int) -> None:
        self._conn.execute(
            "INSERT INTO mgboost_wl_reconciliation_drift "
            "(account_id,child_intent_id,drift_class,action,epoch,detected_at) "
            "VALUES (?,?,?,?,?,?)",
            (
                int(account_id),
                int(child_intent_id) if child_intent_id is not None else None,
                drift_class, action,
                int(epoch) if epoch is not None else None, int(now),
            ),
        )
        self._conn.commit()


# ----------------------------------------------------------------------
# Post-terminal drift scan
# ----------------------------------------------------------------------

def _observe_child_identity(service_marzban, username: str) -> tuple[list[str], str]:
    """One read-only observation through the EXISTING `legacy.user.get`
    surface: the exact vless membership plus the remote UUID (for the local
    verifier check). Raises RemoteChildMissing untouched; any unreadable
    shape raises ValueError."""
    try:
        raw = service_marzban.get_user(username)
    except HTTPError as exc:
        if exc.code == 404:
            raise RemoteChildMissing(f"remote child absent: {username[:4]}...")
        raise
    observed = normalize_observed_vless(raw)
    uuid_raw = raw["proxies"]["vless"]["id"]
    remote_uuid = str(uuid_module.UUID(uuid_raw)).lower()
    return observed, remote_uuid


def _expected_membership(store, state: dict, child: dict) -> dict | None:
    """The frozen proof of what this child's membership was when the account
    converged: the manifest of its APPLIED op in the live epoch. Without that
    manifest there is nothing durably proven to judge drift against -- the
    child is skipped (never judged from live state alone)."""
    for op in store.epoch_ops(state["account_id"], state["epoch"]):
        if int(op["child_intent_id"]) != int(child["child_intent_id"]):
            continue
        if op["state"] != "APPLIED" or not op["manifest_json"]:
            continue
        manifest = json.loads(op["manifest_json"])
        return {
            "target": sorted(manifest.get("target", [])),
            "baseline_full": sorted(manifest.get("baseline_full", [])),
        }
    return None


def scan_terminal_drift(db, service_marzban, *, worker_id: str, now: int,
                        observer=None) -> dict:
    """Re-observe every already-terminal account on the fresh topology
    assertion and classify exact drift. Read-only EXCEPT for genuinely
    drifted children, which get a fresh same-direction epoch minted through
    the existing store (dispatch happens in the orchestrated cycle)."""
    timestamp = int(now)
    store = _wl_enforcement.WLEnforcementStore(db._conn, db._lock)
    if observer is None:
        observer = lambda: fetch_live_topology_observation(  # noqa: E731
            service_marzban, service_marzban.get_admin_token_from_env(),
        )
    observed_tags, observed_nodes = observer()
    db.wl_topology_guard.run_assertion(observed_tags, observed_nodes, now=timestamp)
    db.wl_topology_guard.require_topology_ok()

    wl_tags = set(_wl_enforcement.WL_INBOUND_TAGS)
    summary = {
        "scanned_accounts": 0,
        "scanned_children": 0,
        "detected": 0,
        "repaired": 0,
        "flagged": 0,
        "observation_errors": 0,
        "repair_accounts": [],
        "flagged_classes": [],
    }
    recon: WLReconciliationStore = db.wl_reconciliation

    rows = db._conn.execute(
        "SELECT * FROM mgboost_wl_enforcement_states WHERE state IN ('ACTIVE','DISABLED')"
    ).fetchall()
    for row in rows:
        state = dict(row)
        account_id = int(state["account_id"])
        summary["scanned_accounts"] += 1
        pool = _wl_enforcement.resolve_current_parent_wl_pool(
            db, account_id=account_id, now=timestamp,
        )
        desired = decide_direction_from_pool(pool)
        # No entitlement signal, or the fresh decision disagrees with the
        # frozen terminal direction: the regular decision path owns that
        # (it runs in the same cycle) -- a drift repair must never invent
        # an entitlement the canonical read model no longer proves.
        if desired is None or desired != state["last_direction"]:
            continue
        children = store.list_candidate_children(account_id)
        if not children:
            continue
        if store.unsettled_ops(account_id, state["epoch"], now=timestamp):
            continue  # the live epoch is still converging; not terminal drift

        summary["scanned_children"] += len(children)
        repairs: list[dict] = []
        flags: list[str] = []
        for child in children:
            child_id = int(child["child_intent_id"])
            try:
                observed, remote_uuid = _observe_child_identity(
                    service_marzban, child["child_username"],
                )
            except RemoteChildMissing:
                summary["detected"] += 1
                summary["flagged"] += 1
                flags.append("DRIFT_REMOTE_MISSING")
                recon.record_drift(
                    account_id, child_intent_id=child_id,
                    drift_class="REMOTE_MISSING", action="FLAGGED",
                    epoch=state["epoch"], now=timestamp,
                )
                continue
            except HTTPError:
                summary["observation_errors"] += 1
                continue  # transient remote failure: never counted as drift
            except ValueError:  # noqa: BLE001 -- unreadable/ambiguous remote shape
                summary["detected"] += 1
                summary["flagged"] += 1
                flags.append("DRIFT_REMOTE_UNREADABLE")
                recon.record_drift(
                    account_id, child_intent_id=child_id,
                    drift_class="REMOTE_UNREADABLE", action="FLAGGED",
                    epoch=state["epoch"], now=timestamp,
                )
                continue
            except Exception:  # noqa: BLE001 -- transient outage on the read path
                summary["observation_errors"] += 1
                continue  # never counted as drift; next cycle re-observes
            summary["observation"] = summary.get("observation", 0) + 1
            if credential_verifier(remote_uuid) != child["uuid_verifier"]:
                summary["detected"] += 1
                summary["flagged"] += 1
                flags.append("DRIFT_UUID_MISMATCH")
                recon.record_drift(
                    account_id, child_intent_id=child_id,
                    drift_class="UUID_MISMATCH", action="FLAGGED",
                    epoch=state["epoch"], now=timestamp,
                )
                continue
            expected = _expected_membership(store, state, child)
            if expected is None:
                continue  # nothing frozen to judge against: never guess
            target = set(expected["target"])
            observed_set = set(observed)
            if desired == "EXCLUDED":
                wl_present = observed_set & wl_tags
                if wl_present:
                    summary["detected"] += 1
                    summary["repaired"] += 1
                    repairs.append(child)
                    recon.record_drift(
                        account_id, child_intent_id=child_id,
                        drift_class="WL_PRESENT_WHILE_EXCLUDED",
                        action="REPAIR_QUEUED", epoch=state["epoch"], now=timestamp,
                    )
                elif target - observed_set:
                    summary["detected"] += 1
                    summary["flagged"] += 1
                    flags.append("DRIFT_NON_WL_MEMBERSHIP_LOST")
                    recon.record_drift(
                        account_id, child_intent_id=child_id,
                        drift_class="NON_WL_MEMBERSHIP_LOST", action="FLAGGED",
                        epoch=state["epoch"], now=timestamp,
                    )
            else:  # INCLUDED
                expected_wl = target & wl_tags
                if not expected_wl:
                    continue  # no WL entitlement ever proven for this child
                missing = expected_wl - observed_set
                unexpected = (observed_set & wl_tags) - expected_wl
                if missing:
                    summary["detected"] += 1
                    summary["repaired"] += 1
                    repairs.append(child)
                    recon.record_drift(
                        account_id, child_intent_id=child_id,
                        drift_class="WL_MISSING_WHILE_INCLUDED",
                        action="REPAIR_QUEUED", epoch=state["epoch"], now=timestamp,
                    )
                if unexpected:
                    summary["detected"] += 1
                    summary["flagged"] += 1
                    flags.append("DRIFT_WL_UNEXPECTED_WHILE_INCLUDED")
                    recon.record_drift(
                        account_id, child_intent_id=child_id,
                        drift_class="WL_UNEXPECTED_WHILE_INCLUDED",
                        action="FLAGGED", epoch=state["epoch"], now=timestamp,
                    )
                if (target - wl_tags) - observed_set:
                    summary["detected"] += 1
                    summary["flagged"] += 1
                    flags.append("DRIFT_NON_WL_MEMBERSHIP_LOST")
                    recon.record_drift(
                        account_id, child_intent_id=child_id,
                        drift_class="NON_WL_MEMBERSHIP_LOST", action="FLAGGED",
                        epoch=state["epoch"], now=timestamp,
                    )
        if flags:
            # Ambiguous/unverifiable findings win over repair: the account is
            # flagged ERROR_RECONCILE and left 100% untouched this cycle --
            # never a mixed repair-plus-guess against one account.
            for class_name in flags:
                summary["flagged_classes"].append(class_name)
                store.mark_reconcile_error(
                    account_id, safe_error_class=class_name, now=timestamp,
                )
        elif repairs:
            result = store.open_repair_epoch(
                account_id, direction=desired, children=repairs, pool=pool,
                now=timestamp,
            )
            if result["prepared"]:
                summary["repair_accounts"].append(account_id)
    return summary


# ----------------------------------------------------------------------
# Orchestrated cycle: the ONE entry point for timer + manual runs
# ----------------------------------------------------------------------

class CycleLockBusy(RuntimeError):
    pass


class _CycleLock:
    """Process-wide mutual exclusion for one enforcement cycle (flock on a
    lock file next to the database). A crashed holder releases it with the
    process; a concurrent timer/manual invocation is refused immediately --
    overlap is never queued, and the durable op rows make skipping safe."""

    def __init__(self, path: str):
        self._path = path
        self._fh = None

    def __enter__(self):
        self._fh = open(self._path, "a")
        try:
            fcntl.flock(self._fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._fh.close()
            self._fh = None
            raise CycleLockBusy(self._path)
        return self

    def __exit__(self, *exc):
        if self._fh is not None:
            fcntl.flock(self._fh, fcntl.LOCK_UN)
            self._fh.close()
            self._fh = None
        return False


def run_wl_reconciliation_cycle(*, db, service_marzban, worker_id: str,
                                now: int | None = None, trigger: str = "SCHEDULED",
                                topology_observer=None, lock_file: str | None = None) -> dict:
    timestamp = int(time.time()) if now is None else int(now)
    recon: WLReconciliationStore = db.wl_reconciliation
    try:
        lock = _CycleLock(lock_file) if lock_file else None
        if lock is not None:
            lock.__enter__()
    except CycleLockBusy:
        return {"outcome": "SKIPPED_BUSY", "trigger": trigger, "finished_at": timestamp}
    try:
        observer = topology_observer
        cycle_id = recon.record_cycle_start(trigger, now=timestamp)
        try:
            if observer is None:
                observer = lambda: fetch_live_topology_observation(  # noqa: E731
                    service_marzban, service_marzban.get_admin_token_from_env(),
                )
            # The engine cycle performs its own FRESH topology assertion and
            # fails closed before anything else; the drift scan below runs
            # another fresh one. A mismatch blocks the whole cycle here.
            observed_tags, observed_nodes = observer()
            assertion = db.wl_topology_guard.run_assertion(
                observed_tags, observed_nodes, now=timestamp,
            )
            db.wl_topology_guard.require_topology_ok()

            engine = run_wl_enforcement_cycle(
                db=db, service_marzban=service_marzban, worker_id=worker_id,
                now=timestamp, topology_observer=observer,
            )
            drift = scan_terminal_drift(
                db, service_marzban, worker_id=worker_id, now=timestamp,
                observer=observer,
            )
            # Converge whatever the scan armed through the EXISTING engine
            # path (same claim/manifest/verify/bounded-retry discipline).
            repaired_now = 0
            for account_id in drift["repair_accounts"]:
                driven = drive_account_ops(
                    db, service_marzban, account_id,
                    worker_id=worker_id, now=timestamp,
                )
                repaired_now += driven["ops_applied"]

            outcome = "OK" if engine["outcome"] == "OK" else "PARTIAL"
            last_error_class = None
            if drift["flagged_classes"]:
                last_error_class = drift["flagged_classes"][0]
            elif engine["errors"]:
                last_error_class = str(engine["errors"][0])[:128]
            recon.record_cycle_finish(
                cycle_id, outcome=outcome, now=timestamp,
                config_version=assertion["config_version"], topology_ok=1,
                engine_json=_canonical(engine),
                drift_detected=drift["detected"],
                drift_repaired=drift["repaired"],
                drift_flagged=drift["flagged"],
                last_error_class=last_error_class,
                summary_json=_canonical(backlog_snapshot(db, now=timestamp)),
            )
            return {
                "outcome": outcome,
                "trigger": trigger,
                "cycle_id": cycle_id,
                "engine": engine,
                "drift": drift,
                "repairs_converged": repaired_now,
                "started_at": timestamp,
                "finished_at": int(time.time()),
            }
        except TopologyMismatchError:
            recon.record_cycle_finish(
                cycle_id, outcome="BLOCKED_TOPOLOGY", now=timestamp,
                config_version=assertion["config_version"], topology_ok=0,
                last_error_class="TOPOLOGY_MISMATCH",
            )
            return {
                "outcome": "BLOCKED_TOPOLOGY",
                "trigger": trigger,
                "cycle_id": cycle_id,
                "started_at": timestamp,
                "finished_at": int(time.time()),
            }
        except Exception as exc:  # noqa: BLE001 -- one bounded invocation
            try:
                recon.record_cycle_finish(
                    cycle_id, outcome="ERROR", now=timestamp, topology_ok=0,
                    last_error_class=type(exc).__name__[:128],
                )
            except Exception:  # noqa: BLE001 -- never mask the original failure
                pass
            return {
                "outcome": "ERROR",
                "trigger": trigger,
                "cycle_id": cycle_id,
                "last_error_class": type(exc).__name__[:128],
                "started_at": timestamp,
                "finished_at": int(time.time()),
            }
    finally:
        if lock is not None:
            lock.__exit__()


# ----------------------------------------------------------------------
# Operator read model: aggregate counts only, never identifiers
# ----------------------------------------------------------------------

def backlog_snapshot(db, *, now: int | None = None) -> dict:
    timestamp = int(time.time()) if now is None else int(now)
    conn = db._conn

    last = conn.execute(
        "SELECT id,trigger,started_at,finished_at,outcome,config_version,"
        "topology_ok,drift_detected,drift_repaired,drift_flagged,last_error_class "
        "FROM mgboost_wl_reconciliation_cycles ORDER BY id DESC LIMIT 1"
    ).fetchone()
    last_successful = conn.execute(
        "SELECT id,trigger,started_at,finished_at,outcome FROM mgboost_wl_reconciliation_cycles "
        "WHERE outcome='OK' ORDER BY id DESC LIMIT 1"
    ).fetchone()

    account_states = {
        row["state"]: int(row["n"])
        for row in conn.execute(
            "SELECT state, COUNT(*) AS n FROM mgboost_wl_enforcement_states GROUP BY state"
        )
    }
    op_counts = {
        row["state"]: int(row["n"])
        for row in conn.execute(
            "SELECT state, COUNT(*) AS n FROM mgboost_wl_enforcement_ops GROUP BY state"
        )
    }
    oldest = conn.execute(
        "SELECT MIN(created_at) AS oldest FROM mgboost_wl_enforcement_ops "
        "WHERE state IN ('PENDING','RETRY','IN_FLIGHT')"
    ).fetchone()
    oldest_age = (
        max(0, timestamp - int(oldest["oldest"])) if oldest["oldest"] is not None else None
    )

    drift = {
        "detected": int(conn.execute(
            "SELECT COUNT(*) AS n FROM mgboost_wl_reconciliation_drift"
        ).fetchone()["n"]),
        "repaired": int(conn.execute(
            "SELECT COUNT(*) AS n FROM mgboost_wl_reconciliation_drift "
            "WHERE action='REPAIR_QUEUED'"
        ).fetchone()["n"]),
        "flagged": int(conn.execute(
            "SELECT COUNT(*) AS n FROM mgboost_wl_reconciliation_drift "
            "WHERE action='FLAGGED'"
        ).fetchone()["n"]),
    }

    topology = db.wl_topology_guard.latest_assertion()

    def _cycle_dict(row) -> dict | None:
        if row is None:
            return None
        return {
            "cycle_id": int(row["id"]),
            "trigger": row["trigger"] if "trigger" in row.keys() else None,
            "started_at": int(row["started_at"]),
            "finished_at": int(row["finished_at"]) if row["finished_at"] is not None else None,
            "outcome": row["outcome"],
            "topology_ok": bool(row["topology_ok"]) if "topology_ok" in row.keys() else None,
            "config_version": row["config_version"] if "config_version" in row.keys() else None,
        }

    last_error_class = last["last_error_class"] if last is not None else None
    return {
        "last_cycle": _cycle_dict(last),
        "last_successful_cycle": _cycle_dict(last_successful),
        "topology": {
            "ok": bool(topology["ok"]) if topology else None,
            "config_version": topology["config_version"] if topology else None,
            "checked_at": int(topology["checked_at"]) if topology else None,
        },
        "account_states": account_states,
        "op_counts": op_counts,
        "oldest_backlog_age_seconds": oldest_age,
        "drift": drift,
        "last_error_class": last_error_class,
        "worker_health": {
            "last_cycle_finished_at": _cycle_dict(last)["finished_at"] if last else None,
            "last_cycle_outcome": _cycle_dict(last)["outcome"] if last else None,
            "scheduler_seen": _cycle_dict(last) is not None,
        },
    }
