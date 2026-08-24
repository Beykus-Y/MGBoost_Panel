import importlib
import os
import sqlite3
import tempfile
import threading

import pytest


@pytest.fixture
def db(monkeypatch):
    tmp = tempfile.mkdtemp()
    monkeypatch.setenv("DATA_DIR", tmp)
    import src.config as config
    import src.database as database
    importlib.reload(config)
    importlib.reload(database)
    database.DB_PATH = os.path.join(tmp, "db.sqlite3")
    instance = database.Database()
    yield instance
    instance._conn.close()


def _account(db, source="DIRECT"):
    return db.accounts.create_account(source, now=1)


def _payment(db, account_id, channel, ref, key):
    status = {
        "TELEGRAM_STARS": "CONFIRMED",
        "EXTERNAL_PAYMENT": "CONFIRMED",
        "ADMIN_GRANT": "ADMIN_GRANTED",
        "UNKNOWN_LEGACY": "UNKNOWN_LEGACY",
    }[channel]
    return db.provenance.record_payment(
        account_id, payment_channel=channel, record_status=status,
        amount_minor=None if channel == "UNKNOWN_LEGACY" else 100,
        currency=None if channel == "UNKNOWN_LEGACY" else "XTR",
        payment_method=None, external_reference=ref,
        actor_type="MIGRATION" if channel == "UNKNOWN_LEGACY" else "SYSTEM",
        actor_ref=None, evidence={"schema": 1}, idempotency_key=key, now=10,
    )


@pytest.mark.parametrize("channel", [
    "TELEGRAM_STARS", "EXTERNAL_PAYMENT", "ADMIN_GRANT", "UNKNOWN_LEGACY",
])
def test_explicit_payment_channels_are_immutable_and_idempotent(db, channel):
    account = _account(db)
    ref = None if channel == "UNKNOWN_LEGACY" else channel + "-reference"
    first = _payment(db, account["id"], channel, ref, "payment-key-" + channel)
    second = _payment(db, account["id"], channel, ref, "payment-key-" + channel)
    assert first["id"] == second["id"]
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        db._conn.execute(
            "UPDATE mgboost_payment_records SET actor_type='OTHER' WHERE id=?",
            (first["id"],),
        )


def test_duplicate_reference_or_changed_idempotent_payload_conflicts(db):
    from src.provenance import ProvenanceConflict
    account = _account(db)
    _payment(db, account["id"], "EXTERNAL_PAYMENT", "bank-ref-1", "payment-key-number-one")
    with pytest.raises(ProvenanceConflict):
        _payment(db, account["id"], "EXTERNAL_PAYMENT", "bank-ref-1", "payment-key-number-two")
    with pytest.raises(ProvenanceConflict):
        db.provenance.record_payment(
            account["id"], payment_channel="EXTERNAL_PAYMENT", record_status="CONFIRMED",
            amount_minor=200, currency="RUB", payment_method="transfer",
            external_reference="bank-ref-2", actor_type="PRIMARY_ADMIN", actor_ref="owner",
            evidence={}, idempotency_key="payment-key-number-one", now=10,
        )


def test_account_scoped_payment_mutation_link_rejects_idor(db):
    from src.provenance import ProvenanceError
    first = _account(db)
    second = _account(db)
    payment = _payment(
        db, first["id"], "TELEGRAM_STARS", "stars-charge-1", "stars-payment-key-one"
    )
    with pytest.raises(ProvenanceError, match="belong"):
        db.provenance.record_mutation(
            second["id"], subscription_id=None, operation="RENEW",
            payment_channel="TELEGRAM_STARS", mutation_source="DIRECT_PURCHASE",
            actor_type="TELEGRAM_USER", actor_ref="masked", reason=None,
            external_reference="stars-charge-1", before={}, after={"days": 30},
            idempotency_key="cross-account-mutation-key", payment_id=payment["id"], now=11,
        )


def test_direct_renewal_records_same_account_without_replacement(db):
    from src.provenance import ProvenanceConflict
    account = _account(db)
    payment = _payment(
        db, account["id"], "TELEGRAM_STARS", "stars-charge-2", "stars-payment-key-two"
    )
    mutation = db.provenance.record_mutation(
        account["id"], subscription_id=None, operation="RENEW",
        payment_channel="TELEGRAM_STARS", mutation_source="DIRECT_PURCHASE",
        actor_type="TELEGRAM_USER", actor_ref="masked", reason=None,
        external_reference="stars-charge-2", before={"expiry": 100},
        after={"expiry": 200}, idempotency_key="same-account-renewal-key",
        payment_id=payment["id"], now=11,
    )
    repeated = db.provenance.record_mutation(
        account["id"], subscription_id=None, operation="RENEW",
        payment_channel="TELEGRAM_STARS", mutation_source="DIRECT_PURCHASE",
        actor_type="TELEGRAM_USER", actor_ref="masked", reason=None,
        external_reference="stars-charge-2", before={"expiry": 100},
        after={"expiry": 200}, idempotency_key="same-account-renewal-key",
        payment_id=payment["id"], now=12,
    )
    assert mutation["id"] == repeated["id"]
    assert db._conn.execute("SELECT COUNT(*) FROM mgboost_accounts").fetchone()[0] == 1
    with pytest.raises(ProvenanceConflict):
        db.provenance.record_mutation(
            account["id"], subscription_id=None, operation="RENEW",
            payment_channel="TELEGRAM_STARS", mutation_source="DIRECT_PURCHASE",
            actor_type="TELEGRAM_USER", actor_ref="masked", reason=None,
            external_reference="stars-charge-2", before={"expiry": 100},
            after={"expiry": 201}, idempotency_key="same-account-renewal-key",
            payment_id=payment["id"], now=12,
        )


def test_concurrent_payment_reference_has_one_durable_winner(db):
    from src.provenance import ProvenanceConflict, ProvenanceStore
    account = _account(db)
    path = db._conn.execute("PRAGMA database_list").fetchone()[2]
    second_conn = sqlite3.connect(path, check_same_thread=False)
    second_conn.row_factory = sqlite3.Row
    second_conn.execute("PRAGMA foreign_keys=ON")
    second = ProvenanceStore(second_conn, threading.RLock())
    barrier = threading.Barrier(2)
    outcomes = []

    def write(store, suffix):
        barrier.wait()
        try:
            store.record_payment(
                account["id"], payment_channel="EXTERNAL_PAYMENT",
                record_status="CONFIRMED", amount_minor=100, currency="RUB",
                payment_method="transfer", external_reference="same-bank-ref",
                actor_type="PRIMARY_ADMIN", actor_ref="owner", evidence={},
                idempotency_key="concurrent-payment-" + suffix, now=10,
            )
            outcomes.append("written")
        except ProvenanceConflict:
            outcomes.append("conflict")

    threads = [threading.Thread(target=write, args=(store, suffix))
               for store, suffix in ((db.provenance, "first"), (second, "second"))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    second_conn.close()
    assert sorted(outcomes) == ["conflict", "written"]


def test_no_username_or_note_inference_api_or_source(db):
    from src.provenance import ProvenanceError
    account = _account(db, "UNKNOWN_LEGACY")
    with pytest.raises(ProvenanceError, match="explicit"):
        db.provenance.record_payment(
            account["id"], payment_channel="INFER_FROM_USERNAME",
            record_status="UNKNOWN_LEGACY", amount_minor=None, currency=None,
            payment_method=None, external_reference=None, actor_type="MIGRATION",
            actor_ref=None, evidence={"note": "must not infer"},
            idempotency_key="no-inference-payment-key", now=10,
        )
    source = open(os.path.join(os.path.dirname(__file__), "..", "src", "provenance.py"),
                  encoding="utf-8").read().lower()
    assert "infer_from_username" not in source
    assert "username" not in source
