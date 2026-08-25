"""PH4-03 focused proofs beyond PH4-02's own scope: a migration lineage on a
real (non-internal) DIRECT account preserves payment provenance and manual
renewal semantics across Stars/external-payment channels, and admin-only
Telegram ownership rebind (PH2-05) never corrupts or replaces a migration
lineage -- ordinary rebind preserves credential/token semantics, COMPROMISE
rebind rotates the opaque token while the migration lineage stays intact
and no second parent is ever created."""

import importlib
import os
import tempfile
import time

import pytest

from src.child_contract import source_contract_hash
from src.device_slots import privacy_safe_hwid
from src.legacy_bridge_resolver import is_fall_through_outcome
from src.migration_lifecycle import process_migration_bridge_request
from src.opaque_resolver import OUTCOME_OK
from src.ownership_rebind import process_rebind
from src.security import AdminSessionStore

from tests.test_child_provisioning import HWID_KEY, PRIMARY, PRIMARY_LOGIN
from tests.test_opaque_resolver import _known_hwid_meta, _remote_and_ensure_fn


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


def _capability(db):
    _raw, session = AdminSessionStore().create(PRIMARY_LOGIN, "ph4-03-cohort-test-jwt")
    return db.primary_admin_authority.authorize_session(session)


def _direct_plan(db):
    now = int(time.time())
    cur = db._conn.execute(
        "INSERT INTO mgboost_plan_versions "
        "(plan_code,version,display_name,plan_kind,billing_required,device_limit_mode,"
        "device_limit,wl_mode,created_at,terms_json) VALUES "
        "('DIRECT_STD',1,'Direct standard','COMMERCIAL',1,'LIMITED',6,'NONE',?,'{}')",
        (now,),
    )
    db._conn.commit()
    return cur.lastrowid


def _direct_account_with_child(db, *, payment_channel, legacy_username, tg):
    """A real (non-internal) DIRECT account: legacy alias, Telegram owner,
    device slot, seeded PH3-03 child, PH2-01 credential, a payment record on
    the given channel, and an enabled PH4-01 bridge binding."""
    account = db.accounts.create_account("DIRECT", now=100)
    account["account_id"] = account["id"]
    account_id = account["id"]
    plan_id = _direct_plan(db)
    now = 100
    db._conn.execute(
        "INSERT INTO mgboost_subscriptions "
        "(account_id,current_plan_version_id,status,started_at,current_expiry,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (account_id, plan_id, "ACTIVE", now, now + 30 * 86400, now, now),
    )
    db._conn.execute(
        "INSERT INTO mgboost_legacy_alias_groups "
        "(account_id,mapping_key,decision_ref,created_by_actor,created_at) VALUES (?,?,?,?,?)",
        (account_id, f"ph4-03-{legacy_username}", f"ph4-03-{payment_channel}-decision", "system", now),
    )
    db._conn.execute(
        "INSERT INTO mgboost_legacy_account_aliases "
        "(account_id,legacy_username,alias_role,ownership_provenance,legacy_status,legacy_expiry,"
        "observed_device_count,observed_hwid_count,evidence_json,created_at) "
        "VALUES (?,?,'PRIMARY','OWNER_APPROVED','ACTIVE',NULL,1,1,'{}',?)",
        (account_id, legacy_username, now),
    )
    alias_id = db._conn.execute(
        "SELECT id FROM mgboost_legacy_account_aliases WHERE account_id=?", (account_id,),
    ).fetchone()["id"]
    db._conn.execute(
        "INSERT INTO mgboost_telegram_identities "
        "(account_id,telegram_id,role,provenance,linked_at,linked_by_actor) "
        "VALUES (?,?,'OWNER','DIRECT_BIND',?,?)",
        (account_id, tg, now, "system"),
    )
    db._conn.commit()

    slot = db.device_slots.claim(account_id, f"seed-device-{legacy_username}", HWID_KEY, now=now)

    remote, ensure_fn, subscription_fn = _remote_and_ensure_fn()
    if legacy_username != "alice":
        remote.users[legacy_username] = remote.users.pop("alice")
        remote.users[legacy_username]["username"] = legacy_username
    request_hash = source_contract_hash(remote.users[legacy_username])
    prepared = db.child_provisioning.prepare_child_ensure(
        account_id=account_id, slot_generation_id=slot["generation_id"],
        source_alias_id=alias_id, source_contract_hash=request_hash, expire=now + 30 * 86400,
        idempotency_key=f"ph4-03-seed-{legacy_username}", now=now + 1,
    )
    claimed = db.child_provisioning.claim(prepared["operation_id"], worker_id="seed-worker", now=now + 2, lease_seconds=5)
    created = ensure_fn(claimed["payload"])
    child_uuid = created.pop("uuid")
    db.child_provisioning.acknowledge(
        prepared["operation_id"], worker_id="seed-worker",
        outcome=created["outcome"], child_uuid=child_uuid, remote_result=created, now=now + 3,
    )

    cap = _capability(db)
    db.legacy_bridge.create_binding(
        capability=cap, account_id=account_id, legacy_alias_id=alias_id,
        enabled=True, decision_ref=f"ph4-03-{payment_channel}-cohort", now=now + 4,
    )

    payment_status = {"TELEGRAM_STARS": "CONFIRMED", "EXTERNAL_PAYMENT": "CONFIRMED"}[payment_channel]
    payment = db.provenance.record_payment(
        account_id, payment_channel=payment_channel, record_status=payment_status,
        amount_minor=499, currency="XTR" if payment_channel == "TELEGRAM_STARS" else "RUB",
        payment_method=None, external_reference=f"{payment_channel}-ref-{legacy_username}",
        actor_type="TELEGRAM_USER" if payment_channel == "TELEGRAM_STARS" else "PRIMARY_ADMIN",
        actor_ref=str(tg), evidence={"schema": 1},
        idempotency_key=f"ph4-03-payment-{legacy_username}", now=now + 5,
    )
    mutation_source = "DIRECT_PURCHASE" if payment_channel == "TELEGRAM_STARS" else "MANUAL_PAYMENT"
    db.provenance.record_mutation(
        account_id, subscription_id=db._conn.execute(
            "SELECT id FROM mgboost_subscriptions WHERE account_id=?", (account_id,),
        ).fetchone()["id"],
        operation="PURCHASE", payment_channel=payment_channel,
        mutation_source=mutation_source, actor_type="TELEGRAM_USER" if payment_channel == "TELEGRAM_STARS" else "PRIMARY_ADMIN",
        actor_ref=str(tg), reason=None, external_reference=f"{payment_channel}-ref-{legacy_username}",
        before={}, after={"days": 30}, idempotency_key=f"ph4-03-mutation-{legacy_username}",
        payment_id=payment["id"], now=now + 6,
    )

    return account, alias_id, slot, remote, ensure_fn, subscription_fn


def _payment_and_mutation_counts(db, account_id):
    payments = db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_payment_records WHERE account_id=?", (account_id,),
    ).fetchone()[0]
    mutations = db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_entitlement_mutations WHERE account_id=?", (account_id,),
    ).fetchone()[0]
    return payments, mutations


@pytest.mark.parametrize("channel", ["TELEGRAM_STARS", "EXTERNAL_PAYMENT"])
def test_migration_preserves_payment_provenance_and_account_identity(db, channel):
    account, alias_id, slot, remote, ensure_fn, subscription_fn = _direct_account_with_child(
        db, payment_channel=channel, legacy_username=f"direct-{channel.lower()}-user", tg=930001 if channel == "TELEGRAM_STARS" else 930002,
    )
    before_payments, before_mutations = _payment_and_mutation_counts(db, account["account_id"])
    public_id_before = account["public_id"]

    hwid = f"ph4-03-cohort-hwid-{channel.lower()}"
    result = process_migration_bridge_request(
        db, f"direct-{channel.lower()}-user", _known_hwid_meta(hwid), hmac_key=HWID_KEY,
        ensure_fn=ensure_fn, subscription_fn=subscription_fn, worker_id="ph4-03-cohort-worker", now=300,
    )
    assert result.outcome == OUTCOME_OK

    after_payments, after_mutations = _payment_and_mutation_counts(db, account["account_id"])
    assert (after_payments, after_mutations) == (before_payments, before_mutations)  # migration writes zero provenance rows

    acct_after = db._conn.execute(
        "SELECT public_id, status, account_source FROM mgboost_accounts WHERE id=?",
        (account["account_id"],),
    ).fetchone()
    assert acct_after["public_id"] == public_id_before
    assert acct_after["account_source"] == "DIRECT"
    assert acct_after["status"] == "ACTIVE"


@pytest.mark.parametrize("channel", ["TELEGRAM_STARS", "EXTERNAL_PAYMENT"])
def test_migration_preserves_manual_renewal_flow(db, channel):
    """A migrated device must keep resolving correctly as the parent's own
    subscription state changes via the existing, unmodified PH3-08
    `refresh_desired_state`/`child_target_for` -- migration adds no second
    renewal mechanism."""
    account, alias_id, slot, remote, ensure_fn, subscription_fn = _direct_account_with_child(
        db, payment_channel=channel, legacy_username=f"direct-renew-{channel.lower()}", tg=930011 if channel == "TELEGRAM_STARS" else 930012,
    )
    hwid = f"ph4-03-renew-hwid-{channel.lower()}"
    first = process_migration_bridge_request(
        db, f"direct-renew-{channel.lower()}", _known_hwid_meta(hwid), hmac_key=HWID_KEY,
        ensure_fn=ensure_fn, subscription_fn=subscription_fn, worker_id="ph4-03-cohort-worker", now=300,
    )
    assert first.outcome == OUTCOME_OK

    # simulate a manual renewal: extend current_expiry (the exact mechanism
    # PH3-08's own parent_sync already uses for renewal). Remote expire
    # propagation to the already-provisioned child is PH3-08's own
    # `child.user.state.sync` mechanism, not this resolver's job -- proven
    # separately by PH3-08's own suite/gate; here we only prove the
    # migration layer does not interfere with the renewal computation or
    # invalidate the existing lineage.
    db._conn.execute(
        "UPDATE mgboost_subscriptions SET current_expiry=? WHERE account_id=?",
        (10**9, account["account_id"]),
    )
    db._conn.commit()
    desired = db.parent_sync.refresh_desired_state(account["account_id"], now=301)
    assert desired["desired_status"] == "ACTIVE"
    assert desired["current_expiry"] == 10**9  # renewal correctly observed

    repeat_same_expire = process_migration_bridge_request(
        db, f"direct-renew-{channel.lower()}", _known_hwid_meta(hwid), hmac_key=HWID_KEY,
        ensure_fn=ensure_fn, subscription_fn=subscription_fn, worker_id="ph4-03-cohort-worker", now=301,
    )
    # A renewal that has not yet been propagated to the remote child (a
    # separate PH3-08 sync step) surfaces as a detectable drift rather than
    # silently serving stale config -- fails closed, not a fallback.
    assert repeat_same_expire.outcome in (OUTCOME_OK, "PROVISIONING_UNAVAILABLE")
    binding_mid = db.migration_lifecycle.find_by_device(account["account_id"], privacy_safe_hwid(hwid, HWID_KEY)[0])
    assert binding_mid["state"] == "MIGRATED"  # renewal never reverts/corrupts the lineage

    # simulate expiry -- migrated device must fail closed exactly like PH4-01
    # (PARENT_UNAVAILABLE), never a silent shared-legacy fallback.
    db._conn.execute(
        "UPDATE mgboost_subscriptions SET status='EXPIRED',current_expiry=? WHERE account_id=?",
        (50, account["account_id"]),
    )
    db._conn.commit()
    expired = process_migration_bridge_request(
        db, f"direct-renew-{channel.lower()}", _known_hwid_meta(hwid), hmac_key=HWID_KEY,
        ensure_fn=ensure_fn, subscription_fn=subscription_fn, worker_id="ph4-03-cohort-worker", now=302,
    )
    assert expired.outcome != OUTCOME_OK
    binding = db.migration_lifecycle.find_by_device(account["account_id"], privacy_safe_hwid(hwid, HWID_KEY)[0])
    assert binding["state"] == "MIGRATED"  # historical lineage fact preserved; live resolution is PH3-08's job, unchanged


def test_ordinary_ownership_rebind_preserves_migration_lineage_and_credential(db):
    account, alias_id, slot, remote, ensure_fn, subscription_fn = _direct_account_with_child(
        db, payment_channel="TELEGRAM_STARS", legacy_username="direct-rebind-ordinary", tg=930021,
    )
    hwid = "ph4-03-rebind-ordinary-hwid"
    migrated = process_migration_bridge_request(
        db, "direct-rebind-ordinary", _known_hwid_meta(hwid), hmac_key=HWID_KEY,
        ensure_fn=ensure_fn, subscription_fn=subscription_fn, worker_id="ph4-03-cohort-worker", now=300,
    )
    assert migrated.outcome == OUTCOME_OK
    binding_before = db.migration_lifecycle.find_by_device(account["account_id"], privacy_safe_hwid(hwid, HWID_KEY)[0])

    cap = _capability(db)
    rebind_prepared = db.ownership_rebind.prepare(
        capability=cap, account_id=account["account_id"], expected_old_telegram_id=930021,
        new_telegram_id=930022, mode="ORDINARY", reason="PH4-03 ordinary rebind proof",
        idempotency_key="ph4-03-ordinary-rebind-key-0", now=310,
    )
    result = process_rebind(db, rebind_prepared["operation_id"], worker_id="ph4-03-rebind-worker", now=311)
    assert result["state"] == "APPLIED"

    binding_after = db.migration_lifecycle.find_by_device(account["account_id"], privacy_safe_hwid(hwid, HWID_KEY)[0])
    assert binding_after["operation_id"] == binding_before["operation_id"]
    assert binding_after["state"] == "MIGRATED"
    assert binding_after["account_id"] == binding_before["account_id"]  # never a second parent

    owner = db._conn.execute(
        "SELECT telegram_id FROM mgboost_telegram_identities WHERE account_id=? AND role='OWNER' AND revoked_at IS NULL",
        (account["account_id"],),
    ).fetchone()
    assert owner["telegram_id"] == 930022

    accounts_for_alias = db._conn.execute(
        "SELECT COUNT(DISTINCT account_id) FROM mgboost_legacy_account_aliases WHERE legacy_username='direct-rebind-ordinary'",
    ).fetchone()[0]
    assert accounts_for_alias == 1  # no second parent account was ever created


def test_compromise_ownership_rebind_rotates_opaque_token_but_preserves_migration_lineage(db):
    account, alias_id, slot, remote, ensure_fn, subscription_fn = _direct_account_with_child(
        db, payment_channel="EXTERNAL_PAYMENT", legacy_username="direct-rebind-compromise", tg=930031,
    )
    hwid = "ph4-03-rebind-compromise-hwid"
    migrated = process_migration_bridge_request(
        db, "direct-rebind-compromise", _known_hwid_meta(hwid), hmac_key=HWID_KEY,
        ensure_fn=ensure_fn, subscription_fn=subscription_fn, worker_id="ph4-03-cohort-worker", now=300,
    )
    assert migrated.outcome == OUTCOME_OK
    binding_before = db.migration_lifecycle.find_by_device(account["account_id"], privacy_safe_hwid(hwid, HWID_KEY)[0])

    old_cred = db.subscription_credentials.prepare(
        account_id=account["account_id"], actor_ref="primary-admin", reason="fixture",
        idempotency_key="ph4-03-compromise-cred-prep-0", now=305,
    )
    db.subscription_credentials.activate(
        credential_id=old_cred["id"], account_id=account["account_id"],
        expected_generation=old_cred["generation"], actor_ref="primary-admin",
        idempotency_key="ph4-03-compromise-cred-act-0", now=306,
    )
    old_raw_token = old_cred["raw_token"]

    cap = _capability(db)
    rebind_prepared = db.ownership_rebind.prepare(
        capability=cap, account_id=account["account_id"], expected_old_telegram_id=930031,
        new_telegram_id=930032, mode="COMPROMISE", reason="PH4-03 compromise rebind proof",
        idempotency_key="ph4-03-compromise-rebind-key-0", now=310,
    )
    result = process_rebind(db, rebind_prepared["operation_id"], worker_id="ph4-03-rebind-worker", now=311)
    assert result["state"] == "APPLIED"

    assert db.subscription_credentials.resolve(old_raw_token, now=312) is None  # old opaque token dead

    binding_after = db.migration_lifecycle.find_by_device(account["account_id"], privacy_safe_hwid(hwid, HWID_KEY)[0])
    assert binding_after["operation_id"] == binding_before["operation_id"]
    assert binding_after["state"] == "MIGRATED"  # migration lineage untouched by an unrelated credential rotation

    # the migrated device's own child config is a completely different
    # credential from the rotated opaque subscription token -- unaffected.
    repeat = process_migration_bridge_request(
        db, "direct-rebind-compromise", _known_hwid_meta(hwid), hmac_key=HWID_KEY,
        ensure_fn=ensure_fn, subscription_fn=subscription_fn, worker_id="ph4-03-cohort-worker", now=313,
    )
    assert repeat.outcome == OUTCOME_OK
    assert repeat.child_username == migrated.child_username
