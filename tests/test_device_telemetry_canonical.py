"""PH8-06 canonical per-generation opaque device telemetry.

Covers the owner's T1..T8 scenarios: telemetry is recorded only after the
opaque resolver has already proven credential->account and HWID->current
slot generation; it is generation-scoped (rebind-safe), bounded/sanitized,
never destructively overwritten by a missing field, and preferred over the
legacy `user_devices` evidence path in the admin real-device projection.
"""

import importlib
import os
import tempfile

import pytest

from src.admin_read_models import account_detail
from src.device_telemetry import DeviceTelemetryError
from src.opaque_resolver import (
    OUTCOME_DENY_MISSING_HWID,
    OUTCOME_OK,
    resolve_opaque_subscription,
)

from tests.test_child_provisioning import HWID_KEY, PRIMARY, PRIMARY_LOGIN, _account
from tests.test_opaque_resolver import (
    SUPPORTED_METADATA,
    _issue_active_credential,
    _known_hwid_meta,
    _seed_account_with_first_child,
)


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


def _resolve(db, token, hwid, ensure_fn, subscription_fn, *, extra=None, now=300):
    meta = _known_hwid_meta(hwid) | (extra or {})
    return resolve_opaque_subscription(
        db, token, meta, hmac_key=HWID_KEY, ensure_fn=ensure_fn,
        subscription_fn=subscription_fn, worker_id="telemetry-test-worker", now=now,
    )


# --- T1/T6: successful request creates canonical telemetry, admin sees CONFIRMED ---

def test_successful_opaque_request_creates_canonical_telemetry_for_current_generation(db):
    account, _alias_id, _slot, _remote, ensure_fn, subscription_fn = _seed_account_with_first_child(
        db, mapping="TEL_T1", tg=910001,
    )
    token = _issue_active_credential(db, account["account_id"], idem_prefix="tel-t1-credential")
    result = _resolve(
        db, token, "tel-t1-hwid", ensure_fn, subscription_fn,
        extra={"device_name": "SM-A536E"},
    )
    assert result.outcome == OUTCOME_OK

    rows = db._conn.execute("SELECT * FROM mgboost_device_telemetry").fetchall()
    assert len(rows) == 1
    assert rows[0]["account_id"] == account["account_id"]
    assert rows[0]["model"] == "SM-A536E"
    assert rows[0]["client_name"] == "Happ"
    assert rows[0]["platform"] == "windows"

    detail = account_detail(db, account["account_id"], now=400, device_slot_hmac_key=HWID_KEY)
    device = next(d for d in detail["devices"] if d["slot_number"] == 2)
    assert device["real_device"]["matched"] is True
    assert device["real_device"]["match_state"] == "CONFIRMED"
    assert device["real_device"]["model"] == "SM-A536E"


# --- T2: repeat request updates last_seen, never duplicates -----------------------

def test_repeated_requests_update_last_seen_without_duplicating_the_row(db):
    account, _alias_id, _slot, _remote, ensure_fn, subscription_fn = _seed_account_with_first_child(
        db, mapping="TEL_T2", tg=910002,
    )
    token = _issue_active_credential(db, account["account_id"], idem_prefix="tel-t2-credential")
    _resolve(db, token, "tel-t2-hwid", ensure_fn, subscription_fn, now=300)
    _resolve(db, token, "tel-t2-hwid", ensure_fn, subscription_fn, now=400)
    _resolve(db, token, "tel-t2-hwid", ensure_fn, subscription_fn, now=500)

    rows = db._conn.execute("SELECT * FROM mgboost_device_telemetry").fetchall()
    assert len(rows) == 1
    assert rows[0]["last_seen_at"] == 500
    assert rows[0]["observation_count"] == 3


# --- T3: a request without model never erases a previously known model -----------

def test_request_without_model_does_not_erase_previously_known_model(db):
    account, _alias_id, _slot, _remote, ensure_fn, subscription_fn = _seed_account_with_first_child(
        db, mapping="TEL_T3", tg=910003,
    )
    token = _issue_active_credential(db, account["account_id"], idem_prefix="tel-t3-credential")
    _resolve(db, token, "tel-t3-hwid", ensure_fn, subscription_fn,
             extra={"device_name": "Pixel 8"}, now=300)
    _resolve(db, token, "tel-t3-hwid", ensure_fn, subscription_fn,
             extra={"device_name": None}, now=400)

    row = db._conn.execute("SELECT * FROM mgboost_device_telemetry").fetchone()
    assert row["model"] == "Pixel 8"
    assert row["last_seen_at"] == 400


# --- T4: denied/malformed request never creates telemetry ------------------------

def test_denied_request_missing_hwid_never_creates_telemetry(db):
    account, _alias_id, _slot, _remote, ensure_fn, subscription_fn = _seed_account_with_first_child(
        db, mapping="TEL_T4", tg=910004,
    )
    token = _issue_active_credential(db, account["account_id"], idem_prefix="tel-t4-credential")
    result = resolve_opaque_subscription(
        db, token, SUPPORTED_METADATA, hmac_key=HWID_KEY, ensure_fn=ensure_fn,
        subscription_fn=subscription_fn, worker_id="telemetry-test-worker", now=300,
    )
    assert result.outcome == OUTCOME_DENY_MISSING_HWID
    count = db._conn.execute("SELECT COUNT(*) FROM mgboost_device_telemetry").fetchone()[0]
    assert count == 0


# --- T5: rebind/new generation never shows the old generation's telemetry --------

def test_rebind_starts_the_new_generation_with_no_inherited_telemetry(db):
    account, _alias_id, slot, _remote, ensure_fn, subscription_fn = _seed_account_with_first_child(
        db, mapping="TEL_T5", tg=910005,
    )
    token = _issue_active_credential(db, account["account_id"], idem_prefix="tel-t5-credential")
    _resolve(db, token, "tel-t5-old-hwid", ensure_fn, subscription_fn,
             extra={"device_name": "Old Phone"}, now=300)

    detail = account_detail(db, account["account_id"], now=350, device_slot_hmac_key=HWID_KEY)
    device = next(d for d in detail["devices"] if d["slot_number"] == 2)
    assert device["real_device"]["matched"] is True

    # Rebind THIS newly-claimed slot (slot_number 2) to a brand new HWID.
    new_slot_row = db._conn.execute(
        "SELECT id AS slot_id, current_generation FROM mgboost_device_slots "
        "WHERE account_id=? AND slot_number=2", (account["account_id"],),
    ).fetchone()
    db.device_slots.rebind(
        account["account_id"], new_slot_row["slot_id"], new_slot_row["current_generation"],
        "tel-t5-new-hwid", HWID_KEY, reason="test rebind", now=400,
    )
    detail_after = account_detail(db, account["account_id"], now=450, device_slot_hmac_key=HWID_KEY)
    device_after = next(d for d in detail_after["devices"] if d["slot_number"] == 2)
    assert device_after["real_device"]["match_state"] == "UNKNOWN"
    assert device_after["real_device"]["model"] is None

    # Historical row for the old generation is untouched (audit evidence).
    old_rows = db._conn.execute(
        "SELECT model FROM mgboost_device_telemetry WHERE model='Old Phone'"
    ).fetchall()
    assert len(old_rows) == 1


# --- T7: legacy exact-verifier fallback still works when canonical is absent -----

def test_legacy_exact_verifier_fallback_still_confirms_without_canonical_telemetry(db):
    account, _alias_id, _slot = _account(db, mapping="TEL_T7", alias="tel-t7-user")
    account_id = account["account_id"]
    db.check_device_access(
        "tel-t7-user", "legacy-token-1",
        {
            "request_key": "hwid:" + "9" * 32,
            "device_id": "privacy-safe-test-hwid-TEL_T7",
            "device_name": "iPad", "platform": "ios",
            "client_name": "incy", "client_version": "2.5.1",
        },
        hwid_hmac_key=HWID_KEY,
    )
    detail = account_detail(db, account_id, now=500, device_slot_hmac_key=HWID_KEY)
    slot_one = next(r for r in detail["devices"] if r["slot_number"] == 1)
    assert slot_one["real_device"]["match_state"] == "CONFIRMED"
    assert slot_one["real_device"]["model"] == "iPad"


# --- T8: no proof at all -> UNKNOWN, no fuzzy matching ----------------------------

def test_no_proof_at_all_is_unknown_never_fuzzy_matched(db):
    account, _alias_id, _slot = _account(db, mapping="TEL_T8", alias="tel-t8-user")
    detail = account_detail(db, account["account_id"], now=500, device_slot_hmac_key=HWID_KEY)
    slot_one = next(r for r in detail["devices"] if r["slot_number"] == 1)
    assert slot_one["real_device"]["matched"] is False
    assert slot_one["real_device"]["match_state"] == "UNKNOWN"


# --- store-level guard: cannot stamp a foreign generation -------------------------

def test_store_refuses_a_slot_generation_that_belongs_to_another_account(db):
    account_a, _alias_a, slot_a = _account(db, mapping="TEL_GUARD_A", alias="tel-guard-a", tg=920001)
    account_b, _alias_b, _slot_b = _account(db, mapping="TEL_GUARD_B", alias="tel-guard-b", tg=920002)
    with pytest.raises(DeviceTelemetryError):
        db.device_telemetry.record_observation(
            account_id=account_b["account_id"], slot_generation_id=slot_a["generation_id"],
            hwid_verifier="hmac-sha256:" + "0" * 64, model="x", platform=None,
            client_name=None, client_version=None, now=100,
        )


# --- corrective hardening pass: store proves the verifier itself --------------

def _authoritative_hwid_verifier(db, generation_id):
    return db._conn.execute(
        "SELECT hwid_verifier FROM mgboost_device_slot_generations WHERE id=?",
        (generation_id,),
    ).fetchone()["hwid_verifier"]


def test_store_allows_the_exact_authoritative_account_and_verifier_pair(db):
    account, _alias_id, slot = _account(db, mapping="TEL_VERIFY_OK", alias="tel-verify-ok", tg=920010)
    verifier = _authoritative_hwid_verifier(db, slot["generation_id"])

    row = db.device_telemetry.record_observation(
        account_id=account["account_id"], slot_generation_id=slot["generation_id"],
        hwid_verifier=verifier, model="SM-A536E", platform="android",
        client_name="Happ", client_version="4.3.0", now=100,
    )
    assert row["hwid_verifier"] == verifier
    assert row["model"] == "SM-A536E"


def test_store_rejects_correct_account_with_wrong_verifier(db):
    account, _alias_id, slot = _account(db, mapping="TEL_VERIFY_WRONG", alias="tel-verify-wrong", tg=920011)
    wrong_verifier = "hmac-sha256:" + "f" * 64
    assert wrong_verifier != _authoritative_hwid_verifier(db, slot["generation_id"])

    with pytest.raises(DeviceTelemetryError):
        db.device_telemetry.record_observation(
            account_id=account["account_id"], slot_generation_id=slot["generation_id"],
            hwid_verifier=wrong_verifier, model="spoofed-model", platform=None,
            client_name=None, client_version=None, now=100,
        )
    # No row created and no legitimate future row polluted by the rejected call.
    row = db._conn.execute(
        "SELECT * FROM mgboost_device_telemetry WHERE slot_generation_id=?",
        (slot["generation_id"],),
    ).fetchone()
    assert row is None


def test_store_rejects_wrong_account_even_with_the_correct_verifier(db):
    """The account/verifier pair must BOTH be authoritative-exact; a correct
    verifier borrowed for the wrong account_id must still fail closed."""
    account_a, _alias_a, slot_a = _account(db, mapping="TEL_VERIFY_XACC_A", alias="tel-vx-a", tg=920012)
    account_b, _alias_b, _slot_b = _account(db, mapping="TEL_VERIFY_XACC_B", alias="tel-vx-b", tg=920013)
    real_verifier = _authoritative_hwid_verifier(db, slot_a["generation_id"])

    with pytest.raises(DeviceTelemetryError):
        db.device_telemetry.record_observation(
            account_id=account_b["account_id"], slot_generation_id=slot_a["generation_id"],
            hwid_verifier=real_verifier, model=None, platform=None,
            client_name=None, client_version=None, now=100,
        )
    row = db._conn.execute(
        "SELECT * FROM mgboost_device_telemetry WHERE slot_generation_id=?",
        (slot_a["generation_id"],),
    ).fetchone()
    assert row is None
