"""Wave A account-centric admin read models and authenticated routes."""

import importlib
import io
import json
import os
import tempfile

import pytest

from src import security
from src.admin_read_models import (
    account_detail, account_summaries, dashboard_summary, migration_grace_summaries,
)
from src.legacy_grace import GRACE_PERIOD_SECONDS
from src.legacy_grace_migration import _genesis_hwid
from src.routes import admin_accounts
from src.security import AdminSessionStore
from tests.test_child_provisioning import HWID_KEY, PRIMARY, PRIMARY_LOGIN, _account


class FakeHandler:
    def __init__(self, *, headers=None):
        self.command = "GET"
        self.headers = headers or {}
        self.rfile = io.BytesIO()
        self.wfile = io.BytesIO()
        self.status = None
        self.response_headers = []

    def send_response(self, status):
        self.status = status

    def send_header(self, name, value):
        self.response_headers.append((name, value))

    def end_headers(self):
        pass

    def json(self):
        return json.loads(self.wfile.getvalue())


@pytest.fixture
def db(monkeypatch):
    tmp = tempfile.mkdtemp()
    monkeypatch.setenv("DATA_DIR", tmp)
    monkeypatch.setenv("PRIMARY_MGBOOST_ADMIN_ACTOR_ID", PRIMARY)
    monkeypatch.setenv("PRIMARY_MGBOOST_ADMIN_LOGIN", PRIMARY_LOGIN)
    import src.config as config
    import src.database as database
    importlib.reload(config)
    importlib.reload(database)
    database.DB_PATH = os.path.join(tmp, "db.sqlite3")
    instance = database.Database()
    yield instance
    instance._conn.close()


def _handler(db, *, authenticated=True):
    headers = {}
    if authenticated:
        raw_session_id, _session = security.create_admin_session(PRIMARY_LOGIN, "jwt")
        headers["Cookie"] = f"{security.ADMIN_SESSION_COOKIE}={raw_session_id}"
    handler = FakeHandler(headers=headers)
    handler.server = type("Server", (), {"db": db})()
    return handler


def _capability(db):
    _raw, session = AdminSessionStore().create(PRIMARY_LOGIN, "jwt")
    return db.primary_admin_authority.authorize_session(session)


def _reviewed_internal(db, *, suffix, aliases, ownership="PROVEN", evidence=None):
    cap = _capability(db)
    plan = db.internal_entitlements.create_internal_plan(
        capability=cap, plan_code=f"ADMIN_UX_{suffix}", version=1,
        display_name="Admin UX fixture", device_limit_mode="LIMITED",
        device_limit=10, wl_mode="UNLIMITED", now=100,
    )
    alias_rows = [{
        "legacy_username": name, "alias_role": "PRIMARY" if index == 0 else "SECONDARY",
        "ownership_provenance": "OWNER_APPROVED", "legacy_status": "ACTIVE",
        "legacy_expiry": None, "observed_device_count": 1,
        "observed_hwid_count": 1, "evidence": {"schema": 1},
    } for index, name in enumerate(aliases)]
    return db.internal_entitlements.create_reviewed_account(
        capability=cap, plan_version_id=plan["id"], legacy_username=aliases[0],
        mapping_key=f"ADMIN_UX_{suffix}", decision_ref=f"admin-ux-{suffix}",
        legacy_aliases=alias_rows, ownership_evidence=ownership,
        telegram_id=900000 + len(aliases) if ownership == "PROVEN" else None,
        legacy_status="ACTIVE", legacy_expiry=None, device_evidence_count=len(aliases),
        hwid_evidence_count=len(aliases), internal_reason="Admin UX reviewed fixture",
        migration_confidence="HIGH", evidence=evidence or {"schema": 1},
        idempotency_key=f"admin-ux-create-{suffix}", now=100,
    )


def test_summary_keeps_parent_readiness_separate_from_real_device_migration(db):
    account, alias_id, slot = _account(db, mapping="ADMIN_READ_SUMMARY", alias="summary-user")
    db.legacy_bridge.create_binding(
        account_id=account["account_id"], legacy_alias_id=alias_id,
        capability=_capability(db), decision_ref="wave-a-test",
        enabled=True, now=200,
    )

    row = account_summaries(db, now=300)[0]
    assert row["primary_alias"] == "summary-user"
    assert row["parent_ready"] is True
    assert row["active_devices"] == 1
    assert row["migrated_devices"] == 0
    assert row["migration_action"] == "WAITING_FOR_REGISTRATION"

    verifier = db._conn.execute(
        "SELECT hwid_verifier FROM mgboost_device_slot_generations WHERE id=?",
        (slot["generation_id"],),
    ).fetchone()[0]
    db.migration_lifecycle.prepare_migration(
        account_id=account["account_id"], legacy_alias_id=alias_id,
        hwid_verifier=verifier, actor_ref="test",
        reason="real device lineage", idempotency_key="wave-a-migration-lineage", now=301,
    )
    after = account_summaries(db, now=302)[0]
    assert after["parent_ready"] is True
    assert after["migrated_devices"] == 0
    assert after["migration_action"] == "WAITING_FOR_REGISTRATION"


def test_detail_exposes_masked_operational_device_and_keeps_internal_ids_technical(db):
    account, _alias_id, _slot = _account(db, mapping="ADMIN_READ_DETAIL", alias="detail-user")
    detail = account_detail(db, account["account_id"], now=400)

    device = detail["devices"][0]
    assert device["hwid_masked"].startswith("hwid_")
    assert "hwid_verifier" not in device
    assert "slot_generation_id" not in device
    assert "child_username" not in device
    assert detail["technical"]["device_lineage"][0]["hwid_verifier"].startswith("hmac-sha256:")
    assert detail["telegram"]["status"] == "BOUND"
    assert detail["subscription"]["effective"]["device_limit"] == 10


def test_dashboard_grace_block_is_conditional_and_ticket_counter_is_compact(db):
    account = db.accounts.create_account("DIRECT")
    assert dashboard_summary(db, now=1_000)["grace_campaign"] is None

    db.legacy_grace.start(
        account_id=account["id"], cohort_ref="wave-a-cohort",
        capability=_capability(db), reason="wave a dashboard fixture",
        idempotency_key="wave-a-grace-fixture", now=2_000,
    )
    summary = dashboard_summary(db, now=2_100)
    assert summary["grace_campaign"]["accounts_total"] == 1
    assert summary["grace_campaign"]["accounts_with_real_lineage"] == 0
    assert summary["grace_campaign"]["total_real_lineages"] == 0
    assert summary["grace_campaign"]["elapsed_percent"] == 0
    assert summary["tickets"] == {"open": 0, "unanswered": 0}

    ended = dashboard_summary(db, now=2_000 + GRACE_PERIOD_SECONDS)
    assert ended["grace_campaign"]["active"] is False
    assert ended["grace_campaign"]["elapsed_percent"] == 100
    assert ended["grace_campaign"]["remaining_percent"] == 0


def test_read_routes_require_auth_and_return_account_models(db, monkeypatch):
    account, _alias_id, _slot = _account(db, mapping="ADMIN_READ_ROUTE", alias="route-user")

    denied = _handler(db, authenticated=False)
    admin_accounts.handle_admin_accounts_list(denied)
    assert denied.status == 401

    monkeypatch.setattr(admin_accounts, "_marzban_notes", lambda _handler: ({"route-user": "Алёна"}, True))
    listed = _handler(db)
    admin_accounts.handle_admin_accounts_list(listed)
    assert listed.status == 200
    assert listed.json()["accounts"][0]["primary_alias"] == "route-user"
    assert listed.json()["accounts"][0]["display_note"] == "Алёна"

    detailed = _handler(db)
    admin_accounts.handle_admin_account_detail(detailed, str(account["account_id"]))
    assert detailed.status == 200
    assert detailed.json()["account"]["id"] == account["account_id"]

    missing = _handler(db)
    admin_accounts.handle_admin_account_detail(missing, "999999")
    assert missing.status == 404


def test_note_display_fallback_and_multiple_aliases_are_deterministic(db):
    created = _reviewed_internal(
        db, suffix="MULTI_NOTE", aliases=["primary_alias", "secondary_alias"],
    )
    notes = {"primary_alias": "  Алиса  ", "secondary_alias": "Другой label"}
    detail = account_detail(db, created["account_id"], notes_by_alias=notes, now=200)
    assert detail["display_identity"] == {
        "display_note": "Алиса", "display_note_source_alias": "primary_alias",
        "primary_alias": "primary_alias", "public_id": detail["account"]["public_id"],
    }
    assert [(row["legacy_username"], row["note"]) for row in detail["aliases"]] == [
        ("primary_alias", "Алиса"), ("secondary_alias", "Другой label"),
    ]

    fallback = account_detail(
        db, created["account_id"], notes_by_alias={"secondary_alias": "Вторичный note"}, now=200,
    )
    assert fallback["display_identity"]["display_note"] == "Вторичный note"
    assert fallback["display_identity"]["display_note_source_alias"] == "secondary_alias"

    without = account_detail(db, created["account_id"], notes_by_alias={}, now=200)
    assert without["display_identity"]["display_note"] is None
    assert without["display_identity"]["primary_alias"] == "primary_alias"


def test_note_is_presentation_text_and_public_id_and_all_aliases_are_searchable(db):
    created = _reviewed_internal(
        db, suffix="SEARCH_NOTE", aliases=["search_primary", "search_secondary"],
    )
    malicious = '<img src=x onerror="globalThis.pwned=1">'
    row = account_summaries(
        db, notes_by_alias={"search_primary": malicious}, now=200,
    )[0]
    assert row["display_note"] == malicious
    assert row["aliases"] == ["search_primary", "search_secondary"]
    assert row["public_id"].startswith("acct_")
    # No identity/ownership field is changed or inferred from note.
    assert row["primary_alias"] == "search_primary"
    assert row["telegram_status"] == "BOUND"


def test_technical_account_filter_uses_structured_evidence_not_username(db):
    visible, _alias, _slot = _account(
        db, mapping="ADMIN_FUZZY_TEST_VISIBLE", alias="contains-test-canary-words",
    )
    technical = _reviewed_internal(
        db, suffix="SERVICE", aliases=["ordinary-looking-name"], ownership="ABSENT",
        evidence={"schema": 1, "purpose": "production-verification-canary"},
    )
    default_ids = {row["id"] for row in account_summaries(db, now=200)}
    assert visible["account_id"] in default_ids
    assert technical["account_id"] not in default_ids
    all_rows = account_summaries(db, now=200, include_technical=True)
    by_id = {row["id"]: row for row in all_rows}
    assert by_id[technical["account_id"]]["technical_account"] is True
    assert by_id[visible["account_id"]]["technical_account"] is False


def test_telegram_aggregate_explains_cohort_and_does_not_use_action(db):
    bound, _alias, _slot = _account(db, mapping="ADMIN_TG_BOUND", alias="tg-bound")
    unbound = _reviewed_internal(
        db, suffix="TG_UNBOUND", aliases=["tg-unbound"], ownership="ABSENT",
    )
    ambiguous = _reviewed_internal(
        db, suffix="TG_AMBIG", aliases=["tg-ambiguous"], ownership="ABSENT",
    )
    db.save_tg_user(771001, "tg-ambiguous")
    db.save_tg_user(771002, "tg-ambiguous")
    for index, account_id in enumerate((bound["account_id"], unbound["account_id"], ambiguous["account_id"])):
        db.legacy_grace.start(
            account_id=account_id, cohort_ref="telegram-summary",
            capability=_capability(db), reason="telegram aggregate fixture",
            idempotency_key=f"telegram-summary-{index:04d}", now=2_000,
        )
    model = migration_grace_summaries(db, now=2_100)
    counts = model["summary"]["telegram"]
    assert counts == {"BOUND": 1, "UNREGISTERED": 1, "PENDING_LINK": 0, "AMBIGUOUS": 1}
    assert sum(counts.values()) == model["summary"]["cohort_accounts"] == 3
    actions = {row["account_id"]: row["action"] for row in model["accounts"]}
    assert actions[unbound["account_id"]] == "WAITING_FOR_REGISTRATION"
    assert actions[ambiguous["account_id"]] == "MANUAL_REVIEW"


def test_real_lineage_denominator_and_genesis_proof_are_separate(db):
    account, alias_id, _slot = _account(db, mapping="ADMIN_GENESIS", alias="genesis-user")
    account_id = account["account_id"]
    # Claiming the exact canonical synthetic input is the only bootstrap proof.
    genesis = db.device_slots.claim(account_id, _genesis_hwid(account_id), HWID_KEY, now=101)
    detail = account_detail(db, account_id, now=200, device_slot_hmac_key=HWID_KEY)
    bootstrap = next(row for row in detail["devices"] if row["proven_genesis_bootstrap"])
    assert bootstrap["real_migration_lineage"] is False

    db.legacy_grace.start(
        account_id=account_id, cohort_ref="lineage-summary", capability=_capability(db),
        reason="lineage denominator fixture", idempotency_key="lineage-summary-0001", now=2_000,
    )
    before = migration_grace_summaries(db, now=2_100)["summary"]
    assert before["accounts_with_real_lineage"] == 0
    assert before["total_real_lineages"] == 0
    verifier = db._conn.execute(
        "SELECT hwid_verifier FROM mgboost_device_slot_generations "
        "WHERE account_id=? AND id!=? ORDER BY id LIMIT 1",
        (account_id, genesis["generation_id"]),
    ).fetchone()[0]
    db.migration_lifecycle.prepare_migration(
        account_id=account_id, legacy_alias_id=alias_id, hwid_verifier=verifier,
        actor_ref="test", reason="real lineage fixture",
        idempotency_key="lineage-real-device-0001", now=2_101,
    )
    after = migration_grace_summaries(db, now=2_102)["summary"]
    assert after["accounts_with_real_lineage"] == 1
    assert after["accounts_without_real_lineage"] == 0
    assert after["total_real_lineages"] == 1
