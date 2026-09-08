"""Regression coverage for a real production incident: `apply_manual_payment_
schema_v2` (`BUGS.md` BUG-001) crashed on startup against the real production
database with `RuntimeError: PH5-09 v1 manual-payment-records columns are
unknown or corrupt`.

Root cause: `_verify_v1_source` required an *exact* column/trigger match
against the plain PH5-09 v1 shape. In `database.py::_create_tables`'s own
bootstrap order, `apply_manual_payment_schema_v2` runs *before*
`apply_promo_schema` (PH5-13), so a fresh/test database always reaches this
migration in the plain v1 shape -- every existing test passed. Production,
however, had PH5-13 (five additive nullable columns) *and*
`legacy_commercial_transition_schema.py` (four more triggers on this same
table) already deployed and applied long before this bugfix was ever
written, so its real source table carried columns and triggers this
migration had never seen. The rebuild's `DROP TABLE` would also have
silently destroyed those four foreign triggers with no error anywhere --
their owning module's own idempotency check only looks at its migration
marker, never at whether the triggers still physically exist.

These tests reproduce that exact shape (not the bootstrap order local tests
always get) directly against a real, fully-provisioned `Database()`
instance, and prove the fix: no crash, promo column data intact, every
foreign trigger restored by name and by behavior.
"""

from __future__ import annotations

import importlib
import os
import tempfile

import pytest

from src.manual_payment_schema_v2 import (
    MIGRATION_ID as V2_MIGRATION_ID,
    apply_manual_payment_schema_v2,
)
from src.plan_catalog import RUB_PRICES
from src.security import AdminSessionStore

PRIMARY = "owner:mgboost-primary:v1"
PRIMARY_LOGIN = "authenticated-primary-login"


@pytest.fixture
def db(monkeypatch):
    tmp = tempfile.mkdtemp(prefix="bug001-v2-shape-test-")
    monkeypatch.setenv("DATA_DIR", tmp)
    monkeypatch.setenv("PRIMARY_MGBOOST_ADMIN_ACTOR_ID", PRIMARY)
    monkeypatch.setenv("PRIMARY_MGBOOST_ADMIN_LOGIN", PRIMARY_LOGIN)
    import src.config as config
    import src.database as database
    importlib.reload(config)
    importlib.reload(database)
    database.DB_PATH = os.path.join(tmp, "db.sqlite3")
    instance = database.Database()
    from src.plan_catalog import seed_plan_catalog
    seed_plan_catalog(instance.plan_catalog, now=1)
    yield instance
    instance._conn.close()


def _rewind_v2_migration(conn):
    """A freshly-bootstrapped test DB already has the v2 migration applied
    (bootstrap order runs it before promo/transition add their columns/
    triggers) -- deleting its marker simulates 'this DB has PH5-13 and
    legacy_commercial_transition already live, but has not yet seen this
    fix', which is exactly production's real state at the moment it
    crashed."""
    conn.execute("DELETE FROM mgboost_schema_migrations WHERE migration_id=?", (V2_MIGRATION_ID,))
    conn.commit()


def test_migration_tolerates_preexisting_promo_columns_without_data_loss(db):
    _raw, session = AdminSessionStore().create(PRIMARY_LOGIN, "test-server-jwt")
    cap = db.primary_admin_authority.authorize_session(session)
    account = db.accounts.create_account("DIRECT", now=1)
    record = db.manual_payments.create_record(
        cap, account_id=account["id"], plan_code="WL", duration_days=30,
        external_reference="v2-shape-ref-1", recorded_amount_minor=RUB_PRICES[("WL", 30)],
        payment_method="bank_transfer", idempotency_key="v2-shape-key-0000001", now=100,
    )
    conn = db._conn
    conn.execute(
        "UPDATE mgboost_manual_payment_records SET promo_id=42, promo_version=3, "
        "promo_redemption_id=99, original_amount_minor=999999, discount_snapshot_json=? "
        "WHERE id=?",
        ('{"pct":33}', record["id"]),
    )
    conn.commit()

    _rewind_v2_migration(conn)

    changed = apply_manual_payment_schema_v2(conn)
    assert changed is True

    row = conn.execute(
        "SELECT promo_id, promo_version, promo_redemption_id, original_amount_minor, "
        "discount_snapshot_json FROM mgboost_manual_payment_records WHERE id=?",
        (record["id"],),
    ).fetchone()
    assert tuple(row) == (42, 3, 99, 999999, '{"pct":33}')

    # Idempotent re-run on the now-migrated table must be a clean no-op.
    assert apply_manual_payment_schema_v2(conn) is False


def test_migration_preserves_foreign_triggers_from_other_schemas(db):
    conn = db._conn
    before_triggers = sorted(
        row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' "
            "AND tbl_name='mgboost_manual_payment_records'"
        )
    )
    # Sanity: the fixture DB really does carry the legacy-transition triggers
    # this test is protecting -- if this ever stops being true the test
    # would otherwise pass vacuously.
    assert "trg_mgboost_transition_payment_cancel_propagates" in before_triggers
    assert len(before_triggers) > 2

    _rewind_v2_migration(conn)
    apply_manual_payment_schema_v2(conn)

    after_triggers = sorted(
        row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' "
            "AND tbl_name='mgboost_manual_payment_records'"
        )
    )
    assert after_triggers == before_triggers

    # Behavioral proof, not just name presence: the restored
    # cancel-propagation trigger still actually fires.
    cap_session = AdminSessionStore().create(PRIMARY_LOGIN, "test-server-jwt-2")[1]
    cap = db.primary_admin_authority.authorize_session(cap_session)
    account = db.accounts.create_account("DIRECT", now=1)
    record = db.manual_payments.create_record(
        cap, account_id=account["id"], plan_code="WL", duration_days=30,
        external_reference="v2-shape-ref-2", recorded_amount_minor=RUB_PRICES[("WL", 30)],
        payment_method="bank_transfer", idempotency_key="v2-shape-key-0000002", now=100,
    )
    conn.execute(
        "UPDATE mgboost_manual_payment_records SET status='CANCELLED', cancelled_at=200, "
        "cancel_reason='test' WHERE id=?",
        (record["id"],),
    )
    conn.commit()
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
