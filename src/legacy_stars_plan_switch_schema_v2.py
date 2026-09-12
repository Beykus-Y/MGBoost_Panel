"""DL-063 v2: additive REFUNDED terminal state for legacy Stars plan switches.

The v1 migration (`legacy_stars_plan_switch_schema.py`,
`dl063_legacy_stars_plan_switch_v1`) already shipped in commit `0832fe3` --
its `_S` tuple and `SCHEMA_CHECKSUM` must never change again, or every
database that already ran v1 (including any test DB built against that
commit) fails `apply_legacy_stars_plan_switch_schema`'s own checksum-mismatch
guard on next startup. A follow-up Astra review needed a `REFUNDED` terminal
state (a paid-but-never-applied switch, refunded via the existing kind-
agnostic Stars refund tool, must reach a terminal state that frees the
live-switch UNIQUE slot without being confused with `CANCELLED`, which
elsewhere implies "never charged"). Widening the `state`/`event_type` CHECK
constraints those two tables were created with is not something SQLite lets
you `ALTER ... ADD CHECK` -- so, following exactly the rename/copy/verify
table-rebuild discipline already established by `wl_usage_ledger_schema_v2.py`/
`_v3.py` and used again for the identical CHECK-widening shape in
`manual_payment_schema_v2.py` (BUG-001's `APPLYING` status add), this
migration rebuilds both tables under new temporary names, copies every
existing row byte-for-byte, drops the old tables, and renames the *new*
tables into the original names -- never the other way around, so the other
tables that hold `FOREIGN KEY ... REFERENCES mgboost_legacy_stars_plan_
switches(id)` (`mgboost_legacy_stars_plan_switch_events`,
`mgboost_legacy_stars_plan_switch_wl_baselines`) keep resolving correctly
without SQLite silently rewriting their FK clauses to follow a renamed-away
table.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time

from .legacy_stars_plan_switch_schema import MIGRATION_ID as V1_MIGRATION_ID
from .legacy_stars_plan_switch_schema import SCHEMA_CHECKSUM as V1_SCHEMA_CHECKSUM

MIGRATION_ID = "dl063_legacy_stars_plan_switch_v2"

_SWITCH_COLUMNS = (
    "id,public_id,account_id,invoice_id,state,revision,source_plan_version_id,"
    "source_subscription_id,source_subscription_status,original_source_expiry,"
    "aligned_source_expiry,target_plan_version_id,duration_days,target_expiry,"
    "confirmed_at,activation_at,source_post_confirmation_row_version,"
    "device_count_at_confirm,review_reason,applied_at,created_at,updated_at"
)
_EVENT_COLUMNS = "id,switch_id,event_type,actor_ref,reason,revision,created_at"

_FINAL_SWITCHES_TABLE = """
    CREATE TABLE mgboost_legacy_stars_plan_switches (
     id INTEGER PRIMARY KEY AUTOINCREMENT, public_id TEXT NOT NULL UNIQUE,
     account_id INTEGER NOT NULL, invoice_id INTEGER NOT NULL UNIQUE,
     state TEXT NOT NULL CHECK(state IN ('PENDING_PAYMENT','SCHEDULED','APPLYING','APPLIED','MANUAL_REVIEW','CANCELLED','REFUNDED')),
     revision INTEGER NOT NULL DEFAULT 1 CHECK(revision>0),
     source_plan_version_id INTEGER NOT NULL, source_subscription_id INTEGER,
     source_subscription_status TEXT, original_source_expiry INTEGER,
     aligned_source_expiry INTEGER, target_plan_version_id INTEGER NOT NULL,
     duration_days INTEGER NOT NULL CHECK(duration_days IN (30,60)), target_expiry INTEGER,
     confirmed_at INTEGER, activation_at INTEGER,
     source_post_confirmation_row_version INTEGER, device_count_at_confirm INTEGER,
     review_reason TEXT, applied_at INTEGER,
     created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
     FOREIGN KEY(account_id) REFERENCES mgboost_accounts(id) ON DELETE RESTRICT,
     FOREIGN KEY(invoice_id) REFERENCES stars_invoices(id) ON DELETE RESTRICT,
     FOREIGN KEY(source_plan_version_id) REFERENCES mgboost_plan_versions(id) ON DELETE RESTRICT,
     FOREIGN KEY(target_plan_version_id) REFERENCES mgboost_plan_versions(id) ON DELETE RESTRICT,
     CHECK((confirmed_at IS NULL AND activation_at IS NULL AND target_expiry IS NULL) OR (confirmed_at IS NOT NULL AND activation_at IS NOT NULL AND target_expiry=activation_at+duration_days*86400)),
     CHECK(aligned_source_expiry IS NULL OR aligned_source_expiry % 3600 = 0)
    )
"""

_FINAL_SWITCHES_OBJECTS = (
    "CREATE UNIQUE INDEX ux_mgboost_legacy_stars_switch_live_account ON mgboost_legacy_stars_plan_switches(account_id) WHERE state NOT IN ('APPLIED','CANCELLED','REFUNDED')",
    "CREATE INDEX ix_mgboost_legacy_stars_switch_due ON mgboost_legacy_stars_plan_switches(state,activation_at,id)",
    "CREATE TRIGGER trg_mgboost_legacy_stars_switch_no_delete BEFORE DELETE ON mgboost_legacy_stars_plan_switches BEGIN SELECT RAISE(ABORT,'legacy stars plan switches are never deleted'); END",
    "CREATE TRIGGER trg_mgboost_legacy_stars_switch_frozen BEFORE UPDATE ON mgboost_legacy_stars_plan_switches WHEN OLD.confirmed_at IS NOT NULL AND (NEW.source_plan_version_id!=OLD.source_plan_version_id OR NEW.source_subscription_id IS NOT OLD.source_subscription_id OR NEW.source_subscription_status IS NOT OLD.source_subscription_status OR NEW.original_source_expiry IS NOT OLD.original_source_expiry OR NEW.aligned_source_expiry!=OLD.aligned_source_expiry OR NEW.invoice_id!=OLD.invoice_id OR NEW.target_plan_version_id!=OLD.target_plan_version_id OR NEW.duration_days!=OLD.duration_days OR NEW.activation_at!=OLD.activation_at OR NEW.target_expiry!=OLD.target_expiry OR NEW.confirmed_at!=OLD.confirmed_at) BEGIN SELECT RAISE(ABORT,'confirmed switch facts are immutable'); END",
    "CREATE TRIGGER trg_mgboost_legacy_stars_switch_state_machine BEFORE UPDATE OF state ON mgboost_legacy_stars_plan_switches WHEN NEW.state!=OLD.state AND NOT ((OLD.state='PENDING_PAYMENT' AND NEW.state IN ('SCHEDULED','MANUAL_REVIEW','CANCELLED','REFUNDED')) OR (OLD.state='SCHEDULED' AND NEW.state IN ('APPLYING','MANUAL_REVIEW','REFUNDED')) OR (OLD.state='APPLYING' AND NEW.state IN ('APPLIED','MANUAL_REVIEW')) OR (OLD.state='MANUAL_REVIEW' AND NEW.state IN ('SCHEDULED','PENDING_PAYMENT','REFUNDED'))) BEGIN SELECT RAISE(ABORT,'invalid legacy stars switch state change'); END",
)

_FINAL_EVENTS_TABLE = """
    CREATE TABLE mgboost_legacy_stars_plan_switch_events (
     id INTEGER PRIMARY KEY AUTOINCREMENT, switch_id INTEGER NOT NULL,
     event_type TEXT NOT NULL CHECK(event_type IN ('CREATED','CONFIRMED','APPLIED','MANUAL_REVIEW','MANUAL_REVIEW_RETRY','CANCELLED','REFUNDED')),
     actor_ref TEXT NOT NULL CHECK(length(actor_ref) BETWEEN 1 AND 128),
     reason TEXT NOT NULL CHECK(length(reason) BETWEEN 1 AND 300),
     revision INTEGER NOT NULL, created_at INTEGER NOT NULL,
     FOREIGN KEY(switch_id) REFERENCES mgboost_legacy_stars_plan_switches(id) ON DELETE RESTRICT
    )
"""

_FINAL_EVENTS_OBJECTS = (
    "CREATE TRIGGER trg_mgboost_legacy_stars_switch_events_no_update BEFORE UPDATE ON mgboost_legacy_stars_plan_switch_events BEGIN SELECT RAISE(ABORT,'switch events are append-only'); END",
    "CREATE TRIGGER trg_mgboost_legacy_stars_switch_events_no_delete BEFORE DELETE ON mgboost_legacy_stars_plan_switch_events BEGIN SELECT RAISE(ABORT,'switch events are never deleted'); END",
)

SCHEMA_CHECKSUM = hashlib.sha256(
    (MIGRATION_ID + "\n" + _FINAL_SWITCHES_TABLE + "\n" + "\n".join(_FINAL_SWITCHES_OBJECTS)
     + "\n" + _FINAL_EVENTS_TABLE + "\n" + "\n".join(_FINAL_EVENTS_OBJECTS)).encode("utf-8")
).hexdigest()


def _record_stats(connection: sqlite3.Connection, table: str, columns: str) -> tuple[int, set]:
    rows = connection.execute(f"SELECT {columns} FROM {table} ORDER BY id").fetchall()
    return len(rows), {tuple(row) for row in rows}


def _verify_final_schema(connection: sqlite3.Connection) -> None:
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for table in ("mgboost_legacy_stars_plan_switches", "mgboost_legacy_stars_plan_switch_events"):
        if table not in tables:
            raise RuntimeError(f"DL-063 v2 {table} is missing")
    status_check = " ".join((connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='mgboost_legacy_stars_plan_switches'"
    ).fetchone()[0] or "").upper().split())
    if "'PENDING_PAYMENT','SCHEDULED','APPLYING','APPLIED','MANUAL_REVIEW','CANCELLED','REFUNDED'" not in status_check:
        raise RuntimeError("DL-063 v2 switches state contract is incompatible")
    event_check = " ".join((connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='mgboost_legacy_stars_plan_switch_events'"
    ).fetchone()[0] or "").upper().split())
    if "'CREATED','CONFIRMED','APPLIED','MANUAL_REVIEW','MANUAL_REVIEW_RETRY','CANCELLED','REFUNDED'" not in event_check:
        raise RuntimeError("DL-063 v2 events contract is incompatible")
    objects = {row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('index','trigger')"
    )}
    required = {
        "ux_mgboost_legacy_stars_switch_live_account", "ix_mgboost_legacy_stars_switch_due",
        "trg_mgboost_legacy_stars_switch_no_delete", "trg_mgboost_legacy_stars_switch_frozen",
        "trg_mgboost_legacy_stars_switch_state_machine",
        "trg_mgboost_legacy_stars_switch_events_no_update", "trg_mgboost_legacy_stars_switch_events_no_delete",
    }
    if not required.issubset(objects):
        raise RuntimeError("DL-063 v2 indexes/triggers incomplete")
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise RuntimeError("foreign-key corruption blocks DL-063 v2 startup")


def _backfill_missing_alignment_grace(connection: sqlite3.Connection, timestamp: int) -> None:
    """Astra review round 3, finding 1: a SCHEDULED switch created by the
    PRE-grace ``confirm_locked`` (i.e. before LEGACY_COMMERCIAL_ALIGNMENT_
    GRACE was implemented, any time before this v2 migration first runs)
    recorded the correct ``aligned_source_expiry`` boundary on the switch
    row itself, but never actually CAS-moved the live subscription's
    ``current_expiry`` forward to it -- only the fixed ``confirm_locked``
    does that now. ``apply_locked``'s CAS check requires
    ``sub.current_expiry == aligned_source_expiry``, so an untouched
    pre-fix row would otherwise be rejected into MANUAL_REVIEW on its very
    next apply attempt, stranding an already-paid switch instead of
    applying it.

    Fail-closed CAS, not a repair tool: grace is applied ONLY if the live
    subscription still matches EXACTLY the pre-grace snapshot the switch
    itself recorded at confirmation time (``source_subscription_id``,
    ``original_source_expiry``, ``source_subscription_status``, and
    ``source_post_confirmation_row_version`` -- the row_version the old
    pre-fix confirm_locked left in place, since it never bumped it for a
    grace step that didn't exist yet). If a legitimate renewal, admin
    change, or anything else touched the subscription between the old
    confirmation and this migration running, that is real drift -- this
    migration must NEVER overwrite it, mask it, or fabricate a row_version/
    mutation on top of it. Leaving the row untouched here means
    ``apply_locked``'s own CAS re-check correctly finds the same divergence
    and routes it to MANUAL_REVIEW at apply time, exactly as it already
    does for any other post-confirmation drift.

    Idempotent even for a ZERO-LENGTH grace (``original_source_expiry ==
    aligned_source_expiry``, e.g. a source already expiring exactly on a
    UTC hour): the manual legacy-transition engine's own confirm_payment is
    the source of truth here -- it always performs the CAS and always
    records exactly one grace mutation, unconditionally, precisely once per
    transition, because its OWN state machine (PENDING_PAYMENT is consumed
    forever) is what prevents a second call, not a before/after value
    comparison. A zero-length grace leaves ``current_expiry`` numerically
    unchanged, so comparing subscription values alone can never tell "never
    graced yet" apart from "already graced, called again" once row_version
    bookkeeping has already moved in lockstep. This backfill instead reuses
    the codebase's standing idempotency-key mechanism (the same
    ``mgboost_entitlement_mutations.idempotency_key_hash`` UNIQUE guard
    every other PH5 writer already relies on, e.g. ``apply_locked``) as its
    own "have I already backfilled this switch" signal, checked BEFORE any
    value comparison -- so a repeated call is a true no-op regardless of
    whether the grace was zero-length or not.

    A row already graced under the FIXED ``confirm_locked`` (no backfill
    ever needed) is still excluded by the pre-grace-snapshot predicate
    below exactly as before: its own mutation carries no idempotency key
    matching this backfill's derived hash, but ``current_expiry`` no longer
    equals ``original_source_expiry`` for a real (non-zero) gap -- and for
    a zero-length one, ``confirm_locked`` never needed to touch anything
    that would look ambiguous here in the first place.

    Runs once per switch, ever, inside this one-shot v2 migration, since
    every switch confirmed after this point already goes through the fixed
    ``confirm_locked`` and can never re-drift.
    """
    from .subscription_renewal import _idempotency_hash

    pending = connection.execute(
        "SELECT id,account_id,source_subscription_id,original_source_expiry,"
        "source_subscription_status,aligned_source_expiry,source_post_confirmation_row_version "
        "FROM mgboost_legacy_stars_plan_switches "
        "WHERE state='SCHEDULED' AND aligned_source_expiry IS NOT NULL"
    ).fetchall()
    for switch in pending:
        idem_hash = _idempotency_hash(f"dl063-v2-backfill-grace-{switch['id']:020d}")
        if connection.execute(
            "SELECT 1 FROM mgboost_entitlement_mutations WHERE idempotency_key_hash=?", (idem_hash,)
        ).fetchone() is not None:
            continue  # this exact switch was already backfilled -- true no-op regardless of value drift/zero-length
        sub = connection.execute(
            "SELECT id,current_expiry,status,row_version FROM mgboost_subscriptions WHERE id=?",
            (switch["source_subscription_id"],),
        ).fetchone()
        if (not sub
                or sub["current_expiry"] != switch["original_source_expiry"]
                or sub["status"] != switch["source_subscription_status"]
                or sub["row_version"] != switch["source_post_confirmation_row_version"]):
            # Either the source vanished (apply_locked's own CAS handles
            # that at apply time), the row was already graced under the
            # fixed code (current_expiry no longer equals the pre-grace
            # original_source_expiry, for a real non-zero gap), or
            # something legitimately changed the subscription since
            # confirmation -- real drift. None of these are ours to fix or
            # paper over here; leave the row exactly as found.
            continue
        before_expiry, before_status = sub["current_expiry"], sub["status"]
        updated = connection.execute(
            "UPDATE mgboost_subscriptions SET current_expiry=?,status='ACTIVE',updated_at=?,row_version=row_version+1 "
            "WHERE id=? AND row_version=?",
            (switch["aligned_source_expiry"], timestamp, sub["id"], sub["row_version"]),
        )
        if updated.rowcount != 1:
            raise RuntimeError(f"DL-063 v2 backfill grace CAS failed for switch {switch['id']}")
        connection.execute(
            "INSERT INTO mgboost_entitlement_mutations (account_id,subscription_id,operation,payment_channel,"
            "mutation_source,actor_type,actor_ref,reason,idempotency_key_hash,before_json,after_json,created_at) "
            "VALUES (?,?,'LEGACY_COMMERCIAL_ALIGNMENT_GRACE','TELEGRAM_STARS','MIGRATION','SYSTEM',?,?,?,?,?,?)",
            (switch["account_id"], sub["id"], "dl063_v2_migration",
             "retroactive alignment grace for a pre-v2 SCHEDULED switch", idem_hash,
             json.dumps({"status": before_status, "current_expiry": before_expiry}, sort_keys=True),
             json.dumps({"status": "ACTIVE", "current_expiry": switch["aligned_source_expiry"]}, sort_keys=True),
             timestamp),
        )
        # source_post_confirmation_row_version is NOT in the frozen-column
        # list (see trg_mgboost_legacy_stars_switch_frozen) -- updating it
        # here is legal and required so apply_locked's own row_version CAS
        # matches the now-graced live row, exactly as if confirm_locked had
        # done this originally.
        connection.execute(
            "UPDATE mgboost_legacy_stars_plan_switches SET source_post_confirmation_row_version=? WHERE id=?",
            (sub["row_version"] + 1, switch["id"]),
        )


def apply_legacy_stars_plan_switch_schema_v2(connection: sqlite3.Connection, *, now: int | None = None) -> bool:
    """Additive in effect (adds one new state value and its supporting
    terminal semantics), idempotent, no destructive rewrite -- every
    existing row/column value in both rebuilt tables is preserved."""
    timestamp = int(time.time()) if now is None else int(now)
    connection.execute("PRAGMA foreign_keys=OFF")
    try:
        connection.execute("BEGIN IMMEDIATE")
        v1 = connection.execute(
            "SELECT schema_checksum FROM mgboost_schema_migrations WHERE migration_id=?", (V1_MIGRATION_ID,)
        ).fetchone()
        if v1 is None or v1[0] != V1_SCHEMA_CHECKSUM:
            raise RuntimeError("DL-063 v2 requires the exact dl063_legacy_stars_plan_switch_v1 schema")
        existing = connection.execute(
            "SELECT schema_checksum FROM mgboost_schema_migrations WHERE migration_id=?", (MIGRATION_ID,)
        ).fetchone()
        if existing is not None:
            if existing[0] != SCHEMA_CHECKSUM:
                raise RuntimeError("DL-063 v2 schema checksum mismatch")
            _verify_final_schema(connection)
            connection.commit()
            return False

        # Rebuild mgboost_legacy_stars_plan_switches under a temp name, copy
        # every row unchanged, drop the old table, rename the NEW table into
        # the original name (never rename the old one away) so that
        # mgboost_legacy_stars_plan_switch_events/_wl_baselines' own
        # `FOREIGN KEY ... REFERENCES mgboost_legacy_stars_plan_switches(id)`
        # clauses keep resolving without SQLite rewriting them to follow a
        # renamed-away table.
        before_switch_count, before_switch_rows = _record_stats(
            connection, "mgboost_legacy_stars_plan_switches", _SWITCH_COLUMNS)
        connection.execute(_FINAL_SWITCHES_TABLE.replace(
            "CREATE TABLE mgboost_legacy_stars_plan_switches",
            "CREATE TABLE mgboost_legacy_stars_plan_switches_dl063v2_new",
        ))
        connection.execute(
            f"INSERT INTO mgboost_legacy_stars_plan_switches_dl063v2_new ({_SWITCH_COLUMNS}) "
            f"SELECT {_SWITCH_COLUMNS} FROM mgboost_legacy_stars_plan_switches"
        )
        copied_count, copied_rows = _record_stats(
            connection, "mgboost_legacy_stars_plan_switches_dl063v2_new", _SWITCH_COLUMNS)
        if (copied_count, copied_rows) != (before_switch_count, before_switch_rows):
            raise RuntimeError("DL-063 v2 switches migration row-count or content mismatch")
        connection.execute("DROP TABLE mgboost_legacy_stars_plan_switches")
        connection.execute(
            "ALTER TABLE mgboost_legacy_stars_plan_switches_dl063v2_new "
            "RENAME TO mgboost_legacy_stars_plan_switches"
        )
        for statement in _FINAL_SWITCHES_OBJECTS:
            connection.execute(statement)
        after_switch_count, after_switch_rows = _record_stats(
            connection, "mgboost_legacy_stars_plan_switches", _SWITCH_COLUMNS)
        if (after_switch_count, after_switch_rows) != (before_switch_count, before_switch_rows):
            raise RuntimeError("DL-063 v2 switches post-rebuild verification mismatch")

        # Same discipline for mgboost_legacy_stars_plan_switch_events
        # (widened event_type CHECK). Nothing else references this table by
        # FK, so this rebuild is simpler, but the rename-new-into-old-name
        # convention is kept identical for consistency and safety.
        before_event_count, before_event_rows = _record_stats(
            connection, "mgboost_legacy_stars_plan_switch_events", _EVENT_COLUMNS)
        connection.execute(_FINAL_EVENTS_TABLE.replace(
            "CREATE TABLE mgboost_legacy_stars_plan_switch_events",
            "CREATE TABLE mgboost_legacy_stars_plan_switch_events_dl063v2_new",
        ))
        connection.execute(
            f"INSERT INTO mgboost_legacy_stars_plan_switch_events_dl063v2_new ({_EVENT_COLUMNS}) "
            f"SELECT {_EVENT_COLUMNS} FROM mgboost_legacy_stars_plan_switch_events"
        )
        copied_event_count, copied_event_rows = _record_stats(
            connection, "mgboost_legacy_stars_plan_switch_events_dl063v2_new", _EVENT_COLUMNS)
        if (copied_event_count, copied_event_rows) != (before_event_count, before_event_rows):
            raise RuntimeError("DL-063 v2 events migration row-count or content mismatch")
        connection.execute("DROP TABLE mgboost_legacy_stars_plan_switch_events")
        connection.execute(
            "ALTER TABLE mgboost_legacy_stars_plan_switch_events_dl063v2_new "
            "RENAME TO mgboost_legacy_stars_plan_switch_events"
        )
        for statement in _FINAL_EVENTS_OBJECTS:
            connection.execute(statement)
        after_event_count, after_event_rows = _record_stats(
            connection, "mgboost_legacy_stars_plan_switch_events", _EVENT_COLUMNS)
        if (after_event_count, after_event_rows) != (before_event_count, before_event_rows):
            raise RuntimeError("DL-063 v2 events post-rebuild verification mismatch")

        _backfill_missing_alignment_grace(connection, timestamp)
        _verify_final_schema(connection)
        connection.execute(
            "INSERT INTO mgboost_schema_migrations (migration_id,schema_checksum,applied_at) VALUES (?,?,?)",
            (MIGRATION_ID, SCHEMA_CHECKSUM, timestamp),
        )
        connection.commit()
        return True
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.execute("PRAGMA foreign_keys=ON")
