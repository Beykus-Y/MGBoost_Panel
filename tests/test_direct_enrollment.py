import importlib
import os
import tempfile

import pytest


PRIMARY = "owner:primary-admin-stable-id"
PRIMARY_LOGIN = "authenticated-primary-login"


def _capability(db, username=PRIMARY_LOGIN):
    from src.security import AdminSessionStore
    _raw, session = AdminSessionStore().create(username, "test-server-jwt")
    return db.primary_admin_authority.authorize_session(session)


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


def _enroll_kwargs(*, username="direct-user-a", tg=910000001, decision="dl-029-direct-canary",
                    key=None, evidence_note="test"):
    return dict(
        legacy_username=username,
        decision_ref=decision,
        ownership_evidence="PROVEN" if tg else "ABSENT",
        telegram_id=tg,
        alias_provenance="OWNER_APPROVED",
        legacy_status="ACTIVE",
        legacy_expiry=None,
        observed_device_count=1,
        observed_hwid_count=1,
        evidence={"source": evidence_note},
        idempotency_key=key or f"enroll-{username}-op-1",
    )


def _enroll(db, capability=None, **overrides):
    capability = capability or _capability(db)
    kwargs = _enroll_kwargs(**overrides)
    return db.direct_enrollment.enroll_direct_account(capability=capability, now=100, **kwargs)


def _invoice(db, *, username, payer_tg, status="paid", price=500, invoice_id=None,
             charge_id=None, duration_days=30):
    row = db.create_stars_invoice(
        created_by_telegram_id=payer_tg, marzban_username=username, tariff_id=None,
        tariff_name="Monthly", duration_days=duration_days, stars_price=price,
    )
    invoice_id = row["id"]
    charge_id = charge_id or f"charge-{invoice_id}"
    if status in ("paid", "plan_committed", "applied"):
        db.mark_invoice_paid(invoice_id, charge_id, None, payer_tg, price)
    if status in ("plan_committed", "applied"):
        db.commit_apply_plan(invoice_id, 1000, 2000)
    if status == "applied":
        db.mark_invoice_applied(invoice_id, 2000)
    if status == "refunded":
        db.mark_invoice_paid(invoice_id, charge_id, None, payer_tg, price)
        db.commit_apply_plan(invoice_id, 1000, 2000)
        db.mark_invoice_applied(invoice_id, 2000)
        db.begin_invoice_refund(invoice_id)
        db.mark_invoice_refunded(invoice_id)
    if status == "manual_review":
        db.mark_invoice_paid_but_ambiguous(invoice_id, charge_id, payer_tg, price, "ambiguous")
    return db.get_invoice(invoice_id)


# --- schema -----------------------------------------------------------------

def test_schema_is_idempotent_and_starts_empty(db):
    from src.direct_enrollment_schema import apply_direct_enrollment_schema
    assert apply_direct_enrollment_schema(db._conn, now=101) is False
    assert db._conn.execute("SELECT COUNT(*) FROM mgboost_direct_account_reviews").fetchone()[0] == 0
    assert db._conn.execute("SELECT COUNT(*) FROM mgboost_direct_enrollment_intents").fetchone()[0] == 0


# --- happy path ---------------------------------------------------------------

def test_happy_path_creates_direct_account_alias_and_review(db):
    result = _enroll(db)
    assert result["account_source"] == "DIRECT"
    assert result["legacy_username"] == "direct-user-a"
    assert result["ownership_evidence"] == "PROVEN"
    account = db.accounts.get_account(result["account_id"])
    assert account["account_source"] == "DIRECT"
    alias = db._conn.execute(
        "SELECT * FROM mgboost_legacy_account_aliases WHERE legacy_username='direct-user-a'"
    ).fetchone()
    assert alias["account_id"] == result["account_id"]
    assert alias["alias_role"] == "PRIMARY"
    owner = db.accounts.get_account_for_telegram(910000001)
    assert owner["id"] == result["account_id"]


def test_never_touches_internal_account_reviews(db):
    _enroll(db)
    assert db._conn.execute("SELECT COUNT(*) FROM mgboost_internal_account_reviews").fetchone()[0] == 0


# --- retry / idempotency ------------------------------------------------------

def test_retry_with_same_idempotency_key_converges_to_one_account(db):
    first = _enroll(db)
    second = _enroll(db)
    assert first["account_id"] == second["account_id"]
    assert db._conn.execute("SELECT COUNT(*) FROM mgboost_accounts WHERE account_source='DIRECT'").fetchone()[0] == 1
    assert db._conn.execute("SELECT COUNT(*) FROM mgboost_direct_account_reviews").fetchone()[0] == 1
    assert db._conn.execute("SELECT COUNT(*) FROM mgboost_legacy_account_aliases").fetchone()[0] == 1


def test_idempotency_key_reused_with_different_payload_conflicts(db):
    from src.direct_enrollment import IdempotencyConflict
    _enroll(db, key="shared-idempotency-key-1")
    with pytest.raises(IdempotencyConflict):
        _enroll(db, username="direct-user-b", key="shared-idempotency-key-1")


# --- ambiguous ownership fails closed -----------------------------------------

def test_ambiguous_ownership_fails_closed(db):
    from src.direct_enrollment import AmbiguousOwnershipRejected
    capability = _capability(db)
    kwargs = _enroll_kwargs(username="direct-user-ambiguous")
    kwargs["ownership_evidence"] = "AMBIGUOUS"
    with pytest.raises(AmbiguousOwnershipRejected):
        db.direct_enrollment.enroll_direct_account(capability=capability, now=100, **kwargs)
    assert db._conn.execute("SELECT COUNT(*) FROM mgboost_accounts").fetchone()[0] == 0
    assert db._conn.execute("SELECT COUNT(*) FROM mgboost_direct_enrollment_intents").fetchone()[0] == 0


# --- cross-account alias conflict ---------------------------------------------

def test_cross_account_alias_conflict_is_rejected(db):
    from src.direct_enrollment import AliasConflict
    _enroll(db, username="direct-user-shared", key="enroll-shared-first")
    with pytest.raises(AliasConflict):
        _enroll(db, username="direct-user-shared", key="enroll-shared-second")
    assert db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_accounts WHERE account_source='DIRECT'"
    ).fetchone()[0] == 1


# --- unauthorized review -------------------------------------------------------

def test_unauthorized_review_is_rejected(db):
    from src.direct_enrollment import PrimaryAdminRequired
    from src.admin_authority import PrimaryAdminCapability
    fake_capability = PrimaryAdminCapability("someone-else", "wrong-seal")
    kwargs = _enroll_kwargs(username="direct-user-unauth")
    with pytest.raises(PrimaryAdminRequired):
        db.direct_enrollment.enroll_direct_account(capability=fake_capability, now=100, **kwargs)
    assert db._conn.execute("SELECT COUNT(*) FROM mgboost_accounts").fetchone()[0] == 0


# --- TELEGRAM_STARS: paid ------------------------------------------------------

def test_paid_stars_invoice_creates_payment_record(db):
    account = _enroll(db, username="stars-user-a", tg=910000010)
    invoice = _invoice(db, username="stars-user-a", payer_tg=910000010, status="applied")
    payment = db.direct_enrollment.record_stars_payment(
        db, invoice=invoice, account_id=account["account_id"], actor_ref="bot:stars", now=200,
    )
    assert payment["payment_channel"] == "TELEGRAM_STARS"
    assert payment["record_status"] == "CONFIRMED"
    assert payment["external_reference"] == invoice["telegram_payment_charge_id"]


# --- refunded Stars invoice never counts as payment ---------------------------

def test_refunded_stars_invoice_is_rejected(db):
    from src.direct_enrollment import InvoiceNotPayable
    account = _enroll(db, username="stars-user-refund", tg=910000011)
    invoice = _invoice(db, username="stars-user-refund", payer_tg=910000011, status="refunded")
    with pytest.raises(InvoiceNotPayable):
        db.direct_enrollment.record_stars_payment(
            db, invoice=invoice, account_id=account["account_id"], actor_ref="bot:stars", now=200,
        )
    assert db._conn.execute("SELECT COUNT(*) FROM mgboost_payment_records").fetchone()[0] == 0


def test_manual_review_stars_invoice_is_rejected(db):
    from src.direct_enrollment import InvoiceNotPayable
    account = _enroll(db, username="stars-user-review", tg=910000012)
    invoice = _invoice(db, username="stars-user-review", payer_tg=910000012, status="manual_review")
    with pytest.raises(InvoiceNotPayable):
        db.direct_enrollment.record_stars_payment(
            db, invoice=invoice, account_id=account["account_id"], actor_ref="bot:stars", now=200,
        )


# --- payer / ownership mismatch ------------------------------------------------

def test_payer_mismatch_is_rejected(db):
    from src.direct_enrollment import PayerMismatch
    account = _enroll(db, username="stars-user-mismatch", tg=910000013)
    invoice = _invoice(db, username="stars-user-mismatch", payer_tg=999999999, status="applied")
    with pytest.raises(PayerMismatch):
        db.direct_enrollment.record_stars_payment(
            db, invoice=invoice, account_id=account["account_id"], actor_ref="bot:stars", now=200,
        )
    assert db._conn.execute("SELECT COUNT(*) FROM mgboost_payment_records").fetchone()[0] == 0


# --- duplicate Stars invoice is idempotent -------------------------------------

def test_duplicate_stars_invoice_recording_is_idempotent(db):
    account = _enroll(db, username="stars-user-dup", tg=910000014)
    invoice = _invoice(db, username="stars-user-dup", payer_tg=910000014, status="applied")
    first = db.direct_enrollment.record_stars_payment(
        db, invoice=invoice, account_id=account["account_id"], actor_ref="bot:stars", now=200,
    )
    second = db.direct_enrollment.record_stars_payment(
        db, invoice=invoice, account_id=account["account_id"], actor_ref="bot:stars", now=200,
    )
    assert first["id"] == second["id"]
    assert db._conn.execute("SELECT COUNT(*) FROM mgboost_payment_records").fetchone()[0] == 1


# --- EXTERNAL_PAYMENT admin-only primitive -------------------------------------

def test_external_payment_records_manual_payment(db):
    capability = _capability(db)
    account = _enroll(db, capability=capability, username="ext-user-a", tg=910000020)
    payment = db.direct_enrollment.record_external_payment(
        db, capability=capability, account_id=account["account_id"],
        external_reference="bank-ref-0001", amount_minor=150000, currency="RUB",
        reason="Bank transfer confirmed by owner, order #4471", evidence={"proof": "screenshot-hash"},
        idempotency_key="external-payment-op-1", now=300,
    )
    assert payment["payment_channel"] == "EXTERNAL_PAYMENT"
    mutation = db._conn.execute(
        "SELECT * FROM mgboost_entitlement_mutations WHERE account_id=? AND operation='EXTERNAL_PAYMENT_MANUAL_APPLY'",
        (account["account_id"],),
    ).fetchone()
    assert mutation is not None
    assert mutation["mutation_source"] == "MANUAL_PAYMENT"
    assert mutation["payment_channel"] == "EXTERNAL_PAYMENT"


def test_duplicate_external_reference_is_rejected(db):
    from src.provenance import ProvenanceConflict
    capability = _capability(db)
    account = _enroll(db, capability=capability, username="ext-user-b", tg=910000021)
    db.direct_enrollment.record_external_payment(
        db, capability=capability, account_id=account["account_id"],
        external_reference="bank-ref-dup", amount_minor=150000, currency="RUB",
        reason="Bank transfer confirmed by owner, order #4472", evidence={"proof": "hash-1"},
        idempotency_key="external-payment-op-dup-1", now=300,
    )
    with pytest.raises(ProvenanceConflict):
        db.direct_enrollment.record_external_payment(
            db, capability=capability, account_id=account["account_id"],
            external_reference="bank-ref-dup", amount_minor=150000, currency="RUB",
            reason="Second attempt with a different idempotency key", evidence={"proof": "hash-2"},
            idempotency_key="external-payment-op-dup-2", now=301,
        )
    assert db._conn.execute("SELECT COUNT(*) FROM mgboost_payment_records").fetchone()[0] == 1


# --- crash/retry between the orchestration's main steps -----------------------

def test_orchestration_flow_converges_across_a_simulated_crash(db):
    capability = _capability(db)
    invoice = _invoice(db, username="orch-user-a", payer_tg=910000030, status="applied")

    # Step 1 only -- simulate a crash right after enrollment, before payment.
    account = db.direct_enrollment.enroll_direct_account(
        capability=capability, now=100,
        **_enroll_kwargs(username="orch-user-a", tg=910000030, key="orch-op-1-crash-retry"),
    )
    assert db._conn.execute("SELECT COUNT(*) FROM mgboost_payment_records").fetchone()[0] == 0

    # Retry the WHOLE orchestration flow (as a caller would after a lost response).
    result = db.direct_enrollment.process_direct_stars_enrollment(
        db, capability=capability, invoice=invoice, decision_ref="dl-029-direct-canary",
        ownership_evidence="PROVEN", telegram_id=910000030, alias_provenance="OWNER_APPROVED",
        legacy_status="ACTIVE", legacy_expiry=None, observed_device_count=1,
        observed_hwid_count=1, evidence={"source": "test"}, idempotency_key="orch-op-1-crash-retry",
        actor_ref="bot:stars", now=200,
    )
    assert result["account"]["account_id"] == account["account_id"]

    # Retry AGAIN in full -- must not duplicate anything.
    result2 = db.direct_enrollment.process_direct_stars_enrollment(
        db, capability=capability, invoice=invoice, decision_ref="dl-029-direct-canary",
        ownership_evidence="PROVEN", telegram_id=910000030, alias_provenance="OWNER_APPROVED",
        legacy_status="ACTIVE", legacy_expiry=None, observed_device_count=1,
        observed_hwid_count=1, evidence={"source": "test"}, idempotency_key="orch-op-1-crash-retry",
        actor_ref="bot:stars", now=300,
    )
    assert result2["payment"]["id"] == result["payment"]["id"]
    assert db._conn.execute("SELECT COUNT(*) FROM mgboost_accounts WHERE account_source='DIRECT'").fetchone()[0] == 1
    assert db._conn.execute("SELECT COUNT(*) FROM mgboost_direct_account_reviews").fetchone()[0] == 1
    assert db._conn.execute("SELECT COUNT(*) FROM mgboost_legacy_account_aliases").fetchone()[0] == 1
    assert db._conn.execute("SELECT COUNT(*) FROM mgboost_payment_records").fetchone()[0] == 1
    assert db._conn.execute("SELECT COUNT(*) FROM mgboost_direct_enrollment_intents").fetchone()[0] == 1


# --- OWNER_ATTESTED_LEGACY_EXTERNAL_PAYMENT: historical fact, no invented details ---

def test_owner_attested_legacy_payment_records_no_fabricated_monetary_data(db):
    capability = _capability(db)
    account = _enroll(db, capability=capability, username="legacy-attest-a", tg=910000040)
    result = db.direct_enrollment.record_owner_attested_legacy_payment(
        db, capability=capability, account_id=account["account_id"],
        decision_ref="dl-owner-decision-2026-08-26", attestation_note="Owner confirms this "
        "legacy subscription was historically paid directly, exact amount/date unknown",
        evidence={"source": "owner-attestation", "channel_known": True}, now=400,
    )
    assert result["payment_channel"] == "EXTERNAL_PAYMENT"
    assert "amount" not in result and "amount_minor" not in result
    assert "date" not in result and "external_reference" not in result
    mutation = db._conn.execute(
        "SELECT * FROM mgboost_entitlement_mutations WHERE account_id=? "
        "AND operation='OWNER_ATTESTED_LEGACY_EXTERNAL_PAYMENT'",
        (account["account_id"],),
    ).fetchone()
    assert mutation is not None
    assert mutation["mutation_source"] == "MANUAL_PAYMENT"
    assert mutation["payment_channel"] == "EXTERNAL_PAYMENT"
    # Never lands in the canonical mgboost_payment_records table with invented details.
    assert db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_payment_records WHERE account_id=?", (account["account_id"],)
    ).fetchone()[0] == 0


def test_owner_attestation_retry_with_same_details_is_idempotent(db):
    capability = _capability(db)
    account = _enroll(db, capability=capability, username="legacy-attest-b", tg=910000041)
    kwargs = dict(
        capability=capability, account_id=account["account_id"],
        decision_ref="dl-owner-decision-2026-08-26",
        attestation_note="Owner confirms historical direct payment, details unknown",
        evidence={"source": "owner-attestation"},
    )
    first = db.direct_enrollment.record_owner_attested_legacy_payment(db, now=400, **kwargs)
    second = db.direct_enrollment.record_owner_attested_legacy_payment(db, now=401, **kwargs)
    assert first["id"] == second["id"]
    assert db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_owner_attested_legacy_payments"
    ).fetchone()[0] == 1


def test_owner_attestation_conflicting_details_are_rejected(db):
    from src.direct_enrollment import OwnerAttestationConflict
    capability = _capability(db)
    account = _enroll(db, capability=capability, username="legacy-attest-c", tg=910000042)
    db.direct_enrollment.record_owner_attested_legacy_payment(
        db, capability=capability, account_id=account["account_id"],
        decision_ref="dl-owner-decision-2026-08-26", attestation_note="First attestation text",
        evidence={"source": "owner-attestation"}, now=400,
    )
    with pytest.raises(OwnerAttestationConflict):
        db.direct_enrollment.record_owner_attested_legacy_payment(
            db, capability=capability, account_id=account["account_id"],
            decision_ref="dl-owner-decision-2026-08-26", attestation_note="Different attestation text",
            evidence={"source": "owner-attestation"}, now=401,
        )
    assert db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_owner_attested_legacy_payments"
    ).fetchone()[0] == 1


def test_owner_attestation_requires_reviewed_direct_account(db):
    from src.direct_enrollment import DirectEnrollmentError
    capability = _capability(db)
    with pytest.raises(DirectEnrollmentError):
        db.direct_enrollment.record_owner_attested_legacy_payment(
            db, capability=capability, account_id=999999, decision_ref="dl-owner-decision",
            attestation_note="No such account should ever be attestable",
            evidence={"source": "owner-attestation"}, now=400,
        )


# --- bot-linked Telegram evidence integration (reuse tg_users, no new mechanism) ---

def test_existing_bot_linked_telegram_mapping_is_reused_not_duplicated(db):
    db.save_tg_user(910000050, "bot-linked-user-a")
    account = _enroll(db, username="bot-linked-user-a", tg=910000050, key="enroll-bot-linked-a-op")
    assert account["ownership_evidence"] == "PROVEN"
    owner = db.accounts.get_account_for_telegram(910000050)
    assert owner["id"] == account["account_id"]
    assert db._conn.execute(
        "SELECT COUNT(*) FROM tg_users WHERE telegram_id=?", (910000050,)
    ).fetchone()[0] == 1


def test_conflicting_bot_mapping_fails_closed(db):
    from src.direct_enrollment import TelegramMappingConflict
    db.save_tg_user(910000060, "bot-linked-user-b")
    with pytest.raises(TelegramMappingConflict):
        _enroll(db, username="bot-linked-user-b", tg=910000099, key="enroll-bot-linked-b-op")
    assert db._conn.execute("SELECT COUNT(*) FROM mgboost_accounts WHERE account_source='DIRECT'").fetchone()[0] == 0


def test_ambiguous_bot_mapping_with_two_telegram_ids_fails_closed(db):
    from src.direct_enrollment import AmbiguousOwnershipRejected
    db.save_tg_user(910000070, "bot-linked-user-c")
    db.save_tg_user(910000071, "bot-linked-user-c")
    capability = _capability(db)
    with pytest.raises(AmbiguousOwnershipRejected):
        _enroll(db, capability=capability, username="bot-linked-user-c", tg=910000070,
                key="enroll-bot-linked-c-op")
    assert db._conn.execute("SELECT COUNT(*) FROM mgboost_accounts WHERE account_source='DIRECT'").fetchone()[0] == 0


# --- Stars N/A decision does not weaken Stars paid/refunded validation ---

def test_stars_validation_unchanged_after_owner_attestation_addition(db):
    from src.direct_enrollment import InvoiceNotPayable, PAYABLE_STARS_STATUSES
    assert PAYABLE_STARS_STATUSES == {"paid", "plan_committed", "applied"}
    account = _enroll(db, username="stars-user-regression", tg=910000080)
    invoice = _invoice(db, username="stars-user-regression", payer_tg=910000080, status="refunded")
    with pytest.raises(InvoiceNotPayable):
        db.direct_enrollment.record_stars_payment(
            db, invoice=invoice, account_id=account["account_id"], actor_ref="bot:stars", now=200,
        )


# --- schema -------------------------------------------------------------------

def test_legacy_payment_attestation_schema_is_idempotent_and_starts_empty(db):
    from src.legacy_payment_attestation_schema import apply_legacy_payment_attestation_schema
    assert apply_legacy_payment_attestation_schema(db._conn, now=101) is False
    assert db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_owner_attested_legacy_payments"
    ).fetchone()[0] == 0
