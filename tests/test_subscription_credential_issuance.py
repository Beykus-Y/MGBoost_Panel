"""PH4-04 crash-safe issuance/rotation orchestration: exactly one ACTIVE
credential ever exists, a failed/lost delivery never leaves two actives or
a locked-out account, and retries converge."""

import importlib
import os
import tempfile

import pytest

from src.subscription_credential_issuance import issue_or_reissue_credential

from tests.test_child_provisioning import PRIMARY, PRIMARY_LOGIN, _account


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


def _active_count(db, account_id):
    return db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_subscription_credentials WHERE account_id=? AND status='ACTIVE'",
        (account_id,),
    ).fetchone()[0]


def test_first_issue_activates_one_credential(db):
    account, _alias_id, _slot = _account(db, mapping="ISSUANCE_A", tg=920200001)
    delivered = []
    result = issue_or_reissue_credential(
        db, account_id=account["account_id"], actor_ref="test-admin", reason="initial issue",
        idempotency_key="issuance-a-op-0001", deliver_fn=delivered.append, now=100,
    )
    assert result["status"] == "ACTIVE"
    assert len(delivered) == 1 and len(delivered[0]) == 43
    assert _active_count(db, account["account_id"]) == 1


def test_reissue_rotates_old_credential_out(db):
    account, _alias_id, _slot = _account(db, mapping="ISSUANCE_B", tg=920200002)
    delivered = []
    first = issue_or_reissue_credential(
        db, account_id=account["account_id"], actor_ref="test-admin", reason="initial issue",
        idempotency_key="issuance-b-op-0001", deliver_fn=delivered.append, now=100,
    )
    second = issue_or_reissue_credential(
        db, account_id=account["account_id"], actor_ref="test-admin", reason="rotate",
        idempotency_key="issuance-b-op-0002", deliver_fn=delivered.append, now=200,
    )
    assert first["id"] != second["id"]
    assert delivered[0] != delivered[1]  # a genuinely new token, never the old one repeated
    assert _active_count(db, account["account_id"]) == 1
    old = db._conn.execute(
        "SELECT status, revoke_reason FROM mgboost_subscription_credentials WHERE id=?", (first["id"],)
    ).fetchone()
    assert old["status"] == "REVOKED" and old["revoke_reason"] == "ROTATED"


def test_failed_delivery_leaves_old_credential_untouched(db):
    account, _alias_id, _slot = _account(db, mapping="ISSUANCE_C", tg=920200003)
    delivered = []
    first = issue_or_reissue_credential(
        db, account_id=account["account_id"], actor_ref="test-admin", reason="initial issue",
        idempotency_key="issuance-c-op-0001", deliver_fn=delivered.append, now=100,
    )

    def _broken_delivery(token):
        raise RuntimeError("delivery channel unavailable")

    with pytest.raises(RuntimeError):
        issue_or_reissue_credential(
            db, account_id=account["account_id"], actor_ref="test-admin", reason="rotate attempt",
            idempotency_key="issuance-c-op-0002", deliver_fn=_broken_delivery, now=200,
        )
    # the original credential must still be the one and only ACTIVE one
    assert _active_count(db, account["account_id"]) == 1
    still_active = db._conn.execute(
        "SELECT id, status FROM mgboost_subscription_credentials WHERE account_id=? AND status='ACTIVE'",
        (account["account_id"],),
    ).fetchone()
    assert still_active["id"] == first["id"]
    pending = db._conn.execute(
        "SELECT status FROM mgboost_subscription_credentials WHERE account_id=? AND status='PENDING_DELIVERY'",
        (account["account_id"],),
    ).fetchone()
    assert pending is not None


def test_retry_after_lost_delivery_abandons_stale_and_converges_to_one_active(db):
    account, _alias_id, _slot = _account(db, mapping="ISSUANCE_D", tg=920200004)
    delivered = []

    def _broken_delivery(token):
        raise RuntimeError("simulated crash before delivery ack")

    with pytest.raises(RuntimeError):
        issue_or_reissue_credential(
            db, account_id=account["account_id"], actor_ref="test-admin", reason="first attempt",
            idempotency_key="issuance-d-op-0001", deliver_fn=_broken_delivery, now=100,
        )
    # retry with a fresh idempotency key and a working delivery channel
    result = issue_or_reissue_credential(
        db, account_id=account["account_id"], actor_ref="test-admin", reason="retry",
        idempotency_key="issuance-d-op-0002", deliver_fn=delivered.append, now=101,
    )
    assert result["status"] == "ACTIVE"
    assert len(delivered) == 1
    assert _active_count(db, account["account_id"]) == 1
    abandoned = db._conn.execute(
        "SELECT status, revoke_reason FROM mgboost_subscription_credentials "
        "WHERE account_id=? AND status='REVOKED' AND revoke_reason='ABANDONED_PENDING'",
        (account["account_id"],),
    ).fetchone()
    assert abandoned is not None


def test_never_produces_two_active_credentials_across_several_retries(db):
    account, _alias_id, _slot = _account(db, mapping="ISSUANCE_E", tg=920200005)
    attempt = 0

    def _flaky_delivery(token):
        nonlocal attempt
        attempt += 1
        if attempt % 2 == 1:
            raise RuntimeError("flaky channel")

    for i in range(6):
        try:
            issue_or_reissue_credential(
                db, account_id=account["account_id"], actor_ref="test-admin", reason=f"attempt {i}",
                idempotency_key=f"issuance-e-op-{i:04d}-xxxx", deliver_fn=_flaky_delivery, now=100 + i,
            )
        except RuntimeError:
            pass
        assert _active_count(db, account["account_id"]) <= 1


def test_delivered_raw_token_resolves_and_the_prior_one_no_longer_does(db):
    account, _alias_id, _slot = _account(db, mapping="ISSUANCE_F", tg=920200006)
    delivered = []
    issue_or_reissue_credential(
        db, account_id=account["account_id"], actor_ref="test-admin", reason="initial issue",
        idempotency_key="issuance-f-op-0001", deliver_fn=delivered.append, now=100,
    )
    old_token = delivered[0]
    issue_or_reissue_credential(
        db, account_id=account["account_id"], actor_ref="test-admin", reason="rotate",
        idempotency_key="issuance-f-op-0002", deliver_fn=delivered.append, now=200,
    )
    new_token = delivered[1]
    assert db.subscription_credentials.resolve(old_token, now=300) is None
    resolved = db.subscription_credentials.resolve(new_token, now=300)
    assert resolved is not None and resolved["account_id"] == account["account_id"]
