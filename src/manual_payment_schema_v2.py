"""BUG-001 fix: durable APPLYING freeze state for manual payment records.

Root cause (see `BUGS.md` BUG-001): applying a manual payment spans at least
two independently committing transactions -- the canonical entitlement/
renewal (or package-grant) mutation commits first, and only afterwards does
`ManualPaymentStore` commit its own bookkeeping (`mgboost_manual_payment_
applications` row + `status='APPLIED'`). If the process crashes in between,
the record durably stays `PENDING` even though the entitlement was already,
irreversibly, granted -- and `cancel_record`/`edit_pending_record` only ever
checked for `APPLIED`, so a `PENDING` record in this exact window could be
cancelled or edited as though nothing had happened, producing a payment
record that says CANCELLED while the account's entitlement says otherwise.

Fix: a new `APPLYING` status durably recorded *before* the entitlement
mutation is ever attempted (see `src/manual_payment.py::apply_record`). Once
a record is `APPLYING`, `cancel_record`/`edit_pending_record` refuse it
unconditionally -- not because the entitlement mutation is known to have
happened, but because it is no longer known *not* to have happened. Only a
retried `apply_record` (idempotent via the renewal/grant engines' own
deterministic per-record keys) or an eventual `MANUAL_REVIEW` transition may
move a record out of `APPLYING`.

This requires widening the `status` CHECK constraint, which SQLite cannot
ALTER in place, so the table is rebuilt under one transaction -- following
the exact rename/copy/verify discipline already established by
`wl_usage_ledger_schema_v2.py`/`_v3.py`. Every existing row (all of which,
by construction, only ever used the pre-existing five statuses) is preserved
byte-for-byte; no row is reinterpreted, cancelled, or auto-compensated.

Three other tables (`mgboost_manual_payment_edits`, `_applications`,
`_sync_jobs`) hold a `FOREIGN KEY(payment_record_id) REFERENCES
mgboost_manual_payment_records(id)`. SQLite's `ALTER TABLE ... RENAME`
rewrites *other* tables' FK clauses to follow a renamed table -- exactly the
opposite of what a same-name rebuild needs -- so this migration renames the
*new*, final-shaped table into the original name (after dropping the old
one under it) rather than renaming the old table away, which leaves those
three tables' FK clauses (which already say `mgboost_manual_payment_
records`, unchanged) correctly resolved with zero risk of them silently
starting to reference a stale, about-to-be-dropped table. `PRAGMA
foreign_keys` is toggled off only for this table's own DROP/RENAME step
(itself inside one transaction) and unconditionally restored afterwards,
with a final `PRAGMA foreign_key_check` across the whole schema as a
paranoia check before returning.
"""

from __future__ import annotations

import hashlib
import sqlite3
import time

from .manual_payment_schema import (
    MIGRATION_ID as V1_MIGRATION_ID,
    SCHEMA_CHECKSUM as V1_SCHEMA_CHECKSUM,
)


MIGRATION_ID = "bug001_manual_payment_applying_state_v1"

_RECORD_COLUMNS = (
    "id,public_id,kind,status,account_id,plan_version_id,duration_id,catalog_version_id,"
    "catalog_version_snapshot,plan_price_id,package_price_id,package_product_id,"
    "plan_code_snapshot,plan_version_snapshot,duration_days_snapshot,package_sku_snapshot,"
    "package_product_version_snapshot,package_bytes_snapshot,expected_amount_minor,"
    "recorded_amount_minor,currency,payment_method,external_reference,comment,actor_type,"
    "actor_ref,idempotency_key_hash,request_hash,created_at,updated_at,applied_at,"
    "entitlement_mutation_id,applied_operation,applied_expiry,cancelled_at,cancel_reason,"
    "review_reason,review_at"
)

_V1_RECORD_COLUMNS = frozenset(_RECORD_COLUMNS.split(","))

_FINAL_RECORDS_TABLE = """
    CREATE TABLE mgboost_manual_payment_records (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        public_id TEXT NOT NULL UNIQUE,
        kind TEXT NOT NULL CHECK(kind IN ('PLAN_PRODUCT','WL_PACKAGE')),
        status TEXT NOT NULL DEFAULT 'PENDING'
            CHECK(status IN ('PENDING','APPLYING','APPLIED','CANCELLED','MANUAL_REVIEW')),
        account_id INTEGER NOT NULL,
        plan_version_id INTEGER,
        duration_id INTEGER,
        catalog_version_id INTEGER NOT NULL,
        catalog_version_snapshot TEXT NOT NULL,
        plan_price_id INTEGER,
        package_price_id INTEGER,
        package_product_id INTEGER,
        plan_code_snapshot TEXT,
        plan_version_snapshot INTEGER,
        duration_days_snapshot INTEGER,
        package_sku_snapshot TEXT,
        package_product_version_snapshot INTEGER,
        package_bytes_snapshot INTEGER,
        expected_amount_minor INTEGER NOT NULL CHECK(expected_amount_minor > 0),
        recorded_amount_minor INTEGER NOT NULL CHECK(recorded_amount_minor > 0),
        currency TEXT NOT NULL CHECK(currency='RUB'),
        payment_method TEXT NOT NULL,
        external_reference TEXT NOT NULL UNIQUE,
        comment TEXT,
        actor_type TEXT NOT NULL DEFAULT 'PRIMARY_ADMIN',
        actor_ref TEXT NOT NULL,
        idempotency_key_hash TEXT NOT NULL UNIQUE,
        request_hash TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        applied_at INTEGER,
        entitlement_mutation_id INTEGER,
        applied_operation TEXT,
        applied_expiry INTEGER,
        cancelled_at INTEGER,
        cancel_reason TEXT,
        review_reason TEXT,
        review_at INTEGER,
        CHECK(expected_amount_minor=recorded_amount_minor),
        CHECK((kind='PLAN_PRODUCT' AND plan_price_id IS NOT NULL AND package_price_id IS NULL
               AND package_product_id IS NULL AND plan_code_snapshot IS NOT NULL
               AND plan_version_snapshot IS NOT NULL AND duration_days_snapshot > 0
               AND package_sku_snapshot IS NULL)
           OR (kind='WL_PACKAGE' AND package_price_id IS NOT NULL AND plan_price_id IS NULL
               AND package_product_id IS NOT NULL AND package_sku_snapshot IS NOT NULL
               AND package_product_version_snapshot IS NOT NULL AND package_bytes_snapshot > 0
               AND plan_code_snapshot IS NULL AND duration_days_snapshot IS NULL
               AND plan_version_id IS NULL AND duration_id IS NULL)),
        FOREIGN KEY(account_id) REFERENCES mgboost_accounts(id) ON DELETE RESTRICT,
        FOREIGN KEY(plan_version_id) REFERENCES mgboost_plan_versions(id) ON DELETE RESTRICT,
        FOREIGN KEY(duration_id, plan_version_id)
            REFERENCES mgboost_plan_durations(id, plan_version_id) ON DELETE RESTRICT,
        FOREIGN KEY(catalog_version_id) REFERENCES mgboost_price_catalog_versions(id) ON DELETE RESTRICT,
        FOREIGN KEY(plan_price_id) REFERENCES mgboost_plan_prices(id) ON DELETE RESTRICT,
        FOREIGN KEY(package_price_id) REFERENCES mgboost_wl_package_prices(id) ON DELETE RESTRICT,
        FOREIGN KEY(package_product_id) REFERENCES mgboost_wl_package_products(id) ON DELETE RESTRICT
    )
"""

_FINAL_RECORDS_OBJECTS = (
    """
    CREATE INDEX ix_mgboost_manual_payment_records_status
        ON mgboost_manual_payment_records(status, created_at, id)
    """,
    """
    CREATE INDEX ix_mgboost_manual_payment_records_account
        ON mgboost_manual_payment_records(account_id, created_at, id)
    """,
    """
    CREATE TRIGGER trg_mgboost_manual_payment_record_applied_immutable
        BEFORE UPDATE ON mgboost_manual_payment_records
        WHEN OLD.status IN ('APPLIED','CANCELLED')
        BEGIN SELECT RAISE(ABORT, 'applied or cancelled manual payment record is immutable'); END
    """,
    """
    CREATE TRIGGER trg_mgboost_manual_payment_record_no_delete
        BEFORE DELETE ON mgboost_manual_payment_records
        BEGIN SELECT RAISE(ABORT, 'manual payment records are never deleted'); END
    """,
)

_V1_RECORD_TRIGGERS = {
    "trg_mgboost_manual_payment_record_applied_immutable",
    "trg_mgboost_manual_payment_record_no_delete",
}

# PH5-13 (`promo_schema.py`) additively adds these five nullable columns via
# `ALTER TABLE ... ADD COLUMN` *after* this migration in the bootstrap
# sequence (`database.py::_create_tables`) -- so a fresh/local database still
# reaches this migration in the plain v1 shape. Production, however, already
# had PH5-13 applied *before* this fix was ever written, so its source table
# already carries these columns. Both are legitimate, real shapes of an
# already-checksum-verified PH5-09 v1 table -- never guessed, only the exact
# nullable/typed columns `promo_schema.py` itself adds.
_PROMO_COLUMN_DEFS = (
    ("promo_id", "INTEGER"),
    ("promo_version", "INTEGER"),
    ("promo_redemption_id", "INTEGER"),
    ("original_amount_minor", "INTEGER"),
    ("discount_snapshot_json", "TEXT"),
)
_PROMO_COLUMN_NAMES = tuple(name for name, _ in _PROMO_COLUMN_DEFS)
_PROMO_COLUMNS_SET = frozenset(_PROMO_COLUMN_NAMES)
_RECORD_COLUMNS_WITH_PROMO = _RECORD_COLUMNS + "," + ",".join(_PROMO_COLUMN_NAMES)

# SQLite requires every column-def before any table-level constraint (a
# bare `CHECK(...)`/`FOREIGN KEY(...)` not attached to a single column) --
# so the anchor must be the *last column*, not the first table-level CHECK
# that follows it.
_PROMO_COLUMNS_ANCHOR = "review_at INTEGER,"


def _final_records_table_ddl(*, with_promo: bool) -> str:
    """`_FINAL_RECORDS_TABLE` itself (and `SCHEMA_CHECKSUM` below) stay the
    exact, already-tested plain-v1 shape -- this only ever derives a second,
    additive variant for the already-promo-migrated production case. Never
    mutates the base constant."""
    if not with_promo:
        return _FINAL_RECORDS_TABLE
    extra = "".join(f"\n        {name} {sqltype}," for name, sqltype in _PROMO_COLUMN_DEFS)
    assert _FINAL_RECORDS_TABLE.count(_PROMO_COLUMNS_ANCHOR) == 1
    return _FINAL_RECORDS_TABLE.replace(
        _PROMO_COLUMNS_ANCHOR, _PROMO_COLUMNS_ANCHOR + extra,
    )


SCHEMA_CHECKSUM = hashlib.sha256(
    (MIGRATION_ID + "\n" + _FINAL_RECORDS_TABLE + "\n" + "\n".join(_FINAL_RECORDS_OBJECTS)).encode("utf-8")
).hexdigest()


def _table_names(connection: sqlite3.Connection) -> set[str]:
    return {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _record_stats(connection: sqlite3.Connection, table: str, columns: str = _RECORD_COLUMNS) -> tuple[int, set]:
    # Keyed by the immutable primary key `id` so this catches row loss or
    # duplication precisely; every column is compared, not an aggregate.
    rows = connection.execute(f"SELECT {columns} FROM {table} ORDER BY id").fetchall()
    return len(rows), {tuple(row) for row in rows}


def _verify_v1_source(connection: sqlite3.Connection) -> bool:
    """Returns whether the already-deployed PH5-13 promo columns are present
    on the source table (see `_PROMO_COLUMN_DEFS` above for why both shapes
    are legitimate). Anything else -- including a partial/only-some-columns
    promo overlay -- is fail-closed exactly as before."""
    if "mgboost_manual_payment_records" not in _table_names(connection):
        raise RuntimeError("PH5-09 v1 manual-payment-records table is missing")
    columns = {row[1] for row in connection.execute("PRAGMA table_info(mgboost_manual_payment_records)")}
    if columns == _V1_RECORD_COLUMNS:
        has_promo_columns = False
    elif columns == _V1_RECORD_COLUMNS | _PROMO_COLUMNS_SET:
        has_promo_columns = True
    else:
        raise RuntimeError("PH5-09 v1 manual-payment-records columns are unknown or corrupt")
    # Subset, not equality: `legacy_commercial_transition_schema.py` adds
    # four more triggers on this same table (its own idempotent, separately
    # checksum-tracked migration) that this file has no reason to enumerate
    # by name -- `apply_manual_payment_schema_v2` below captures and
    # verbatim-restores every trigger beyond these two required ones, so an
    # extra trigger here is never silently dropped by the rebuild.
    triggers = {row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='mgboost_manual_payment_records'"
    )}
    if not _V1_RECORD_TRIGGERS.issubset(triggers):
        raise RuntimeError("PH5-09 v1 manual-payment-records triggers are unknown or corrupt")
    return has_promo_columns


def _verify_final_schema(connection: sqlite3.Connection) -> None:
    if "mgboost_manual_payment_records" not in _table_names(connection):
        raise RuntimeError("BUG-001 manual-payment-records table is missing")
    columns = {row[1] for row in connection.execute("PRAGMA table_info(mgboost_manual_payment_records)")}
    # Subset, not equality: `promo_schema.py` (PH5-13) independently runs
    # `ALTER TABLE ... ADD COLUMN` on this same table *after* this migration
    # in the bootstrap sequence, so a later reopen's fast-path check must
    # tolerate those additional, unrelated columns rather than reject them.
    if not _V1_RECORD_COLUMNS.issubset(columns):
        raise RuntimeError("BUG-001 manual-payment-records columns are incompatible")
    objects = {row[0]: row[1] or "" for row in connection.execute(
        "SELECT name,sql FROM sqlite_master WHERE type IN ('index','trigger')"
    )}
    required = {
        "ix_mgboost_manual_payment_records_status", "ix_mgboost_manual_payment_records_account",
        *_V1_RECORD_TRIGGERS,
    }
    if not required.issubset(objects):
        raise RuntimeError("BUG-001 manual-payment-records indexes/triggers incomplete")
    status_check = " ".join(
        (connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='mgboost_manual_payment_records'"
        ).fetchone()[0] or "").upper().split()
    )
    if "'PENDING','APPLYING','APPLIED','CANCELLED','MANUAL_REVIEW'" not in status_check:
        raise RuntimeError("BUG-001 manual-payment-records status contract is incompatible")
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise RuntimeError("foreign-key corruption blocks BUG-001 manual-payment startup")


def apply_manual_payment_schema_v2(connection: sqlite3.Connection, *, now: int | None = None) -> bool:
    """Apply the BUG-001 APPLYING-state fix. Additive in effect (adds one new
    status value and its supporting freeze semantics), idempotent, no
    destructive rewrite -- every existing row/column value is preserved."""
    timestamp = int(time.time()) if now is None else int(now)
    connection.execute("PRAGMA foreign_keys=OFF")
    try:
        connection.execute("BEGIN IMMEDIATE")
        v1 = connection.execute(
            "SELECT schema_checksum FROM mgboost_schema_migrations WHERE migration_id=?",
            (V1_MIGRATION_ID,),
        ).fetchone()
        if v1 is None or v1[0] != V1_SCHEMA_CHECKSUM:
            raise RuntimeError("BUG-001 manual-payment fix requires the exact PH5-09 v1 schema")
        existing = connection.execute(
            "SELECT schema_checksum FROM mgboost_schema_migrations WHERE migration_id=?",
            (MIGRATION_ID,),
        ).fetchone()
        if existing is not None:
            if existing[0] != SCHEMA_CHECKSUM:
                raise RuntimeError("BUG-001 manual-payment fix schema checksum mismatch")
            _verify_final_schema(connection)
            connection.commit()
            return False

        has_promo_columns = _verify_v1_source(connection)
        copy_columns = _RECORD_COLUMNS_WITH_PROMO if has_promo_columns else _RECORD_COLUMNS
        before_count, before_rows = _record_stats(connection, "mgboost_manual_payment_records", copy_columns)

        # Capture every trigger beyond the two this migration itself owns
        # (recreated below via `_FINAL_RECORDS_OBJECTS`, unchanged text) --
        # e.g. `legacy_commercial_transition_schema.py`'s four orchestration
        # triggers on this same table. `DROP TABLE` below would otherwise
        # silently destroy them with no error anywhere: the owning module's
        # own idempotency check only looks at its migration marker, never at
        # whether its triggers still physically exist.
        foreign_triggers = {
            row[0]: row[1] for row in connection.execute(
                "SELECT name, sql FROM sqlite_master WHERE type='trigger' "
                "AND tbl_name='mgboost_manual_payment_records' AND name NOT IN (%s)"
                % ",".join("?" for _ in _V1_RECORD_TRIGGERS),
                tuple(_V1_RECORD_TRIGGERS),
            )
        }

        # Build the new-shaped table under a temporary name and copy every
        # row unchanged, then drop the old table and rename the *new* one
        # into the original name -- never the other way around, so the
        # three dependent tables' `REFERENCES mgboost_manual_payment_records`
        # clauses (which SQLite would otherwise silently rewrite to follow a
        # renamed-away old table) are never touched at all. When the source
        # already carries PH5-13's promo columns (production, where PH5-13
        # shipped before this fix existed), the rebuilt table carries them
        # too -- `promo_schema.py`'s own idempotency guard (its migration_id
        # already recorded) means it will never try to re-`ADD COLUMN` them.
        connection.execute(_final_records_table_ddl(with_promo=has_promo_columns).replace(
            "CREATE TABLE mgboost_manual_payment_records",
            "CREATE TABLE mgboost_manual_payment_records_bug001_new",
        ))
        connection.execute(
            f"INSERT INTO mgboost_manual_payment_records_bug001_new ({copy_columns}) "
            f"SELECT {copy_columns} FROM mgboost_manual_payment_records"
        )
        copied_count, copied_rows = _record_stats(
            connection, "mgboost_manual_payment_records_bug001_new", copy_columns,
        )
        if (copied_count, copied_rows) != (before_count, before_rows):
            raise RuntimeError("BUG-001 manual-payment migration row-count or content mismatch")
        connection.execute("DROP TABLE mgboost_manual_payment_records")
        connection.execute(
            "ALTER TABLE mgboost_manual_payment_records_bug001_new "
            "RENAME TO mgboost_manual_payment_records"
        )
        for statement in _FINAL_RECORDS_OBJECTS:
            connection.execute(statement)
        for name, sql in foreign_triggers.items():
            connection.execute(sql)
        after_count, after_rows = _record_stats(connection, "mgboost_manual_payment_records", copy_columns)
        if (after_count, after_rows) != (before_count, before_rows):
            raise RuntimeError("BUG-001 manual-payment migration post-rebuild verification mismatch")
        restored_triggers = {
            row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='mgboost_manual_payment_records'"
            )
        }
        if restored_triggers != _V1_RECORD_TRIGGERS | set(foreign_triggers):
            raise RuntimeError("BUG-001 manual-payment migration lost or gained a foreign trigger")

        _verify_final_schema(connection)
        connection.execute(
            "INSERT INTO mgboost_schema_migrations "
            "(migration_id,schema_checksum,applied_at) VALUES (?,?,?)",
            (MIGRATION_ID, SCHEMA_CHECKSUM, timestamp),
        )
        connection.commit()
        return True
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.execute("PRAGMA foreign_keys=ON")
