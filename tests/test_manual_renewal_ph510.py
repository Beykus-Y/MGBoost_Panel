"""PH5-10 manual external-payment renewal of the same parent account.

Every scenario reuses the real chains end-to-end: PH5-09's durable record +
apply path, PH5-02's DL-044 renewal engine, PH5-04's proof calculation and
PH3-08's durable parent-sync outbox driven through a typed fake Marzban.
"""

import importlib
import os
import tempfile
import threading

import pytest

from src.broker_operations import BrokerOperations
from src.child_contract import source_contract_hash
from src.entitlement_engine import calculate_effective_entitlement
from src.parent_sync import run_account_sync_cycle
from src.plan_catalog import RUB_PRICES
from src.security import AdminSessionStore

from tests.test_marzban_broker import FakeMarzban

PRIMARY = "owner:mgboost-primary:v1"
PRIMARY_LOGIN = "authenticated-primary-login"
HWID_KEY = "mpay-renewal-hwid-key-that-is-at-least-32-bytes"


@pytest.fixture
def db(monkeypatch):
    tmp = tempfile.mkdtemp(prefix="ph510-test-")
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
    from src.wl_package_catalog import seed_wl_package_catalog
    seed_plan_catalog(instance.plan_catalog, now=1)
    seed_wl_package_catalog(instance.wl_package_catalog, now=1)
    yield instance
    instance._conn.close()


def _capability(db):
    _raw, session = AdminSessionStore().create(PRIMARY_LOGIN, "test-server-jwt")
    return db.primary_admin_authority.authorize_session(session)


def _pay_manual(db, cap, account_id, *, plan, days, tag, now):
    return db.manual_payments.create_record(
        cap, account_id=account_id, plan_code=plan, duration_days=days,
        external_reference=f"renew-{tag}", recorded_amount_minor=RUB_PRICES[(plan, days)],
        payment_method="bank_transfer", idempotency_key=f"renew-key-{tag}-------",
        now=now,
    )


def _apply(db, cap, record_id, now):
    return db.manual_payments.apply_record(cap, record_id, now=now)


def _cycle(db, topo, *, worker="manual-ph5-10", sync_fn=None, now=5000):
    if sync_fn is None:
        def sync_fn(payload):
            return BrokerOperations(topo["remote"]).dispatch(
                "child.user.state.sync", payload
            )
    return run_account_sync_cycle(
        db, topo["account"]["id"], sync_fn=sync_fn, worker_id=worker, now=now,
    )


def _build_topology(db, cap, *, plan, days, n_children, tag, now=1000):
    """One DIRECT account paid via one manual RUB payment plus provisioned
    children through the real PH3-03 chain against a typed fake Marzban."""
    account = db.accounts.create_account("DIRECT", now=1)
    db._conn.execute(
        "INSERT INTO mgboost_legacy_alias_groups "
        "(account_id,mapping_key,decision_ref,created_by_actor,created_at) VALUES (?,?,?,?,?)",
        (account["id"], f"mpay-map-{tag}-{account['id']}", "test-decision-ref", "TEST", 1),
    )
    alias_id = db._conn.execute(
        "INSERT INTO mgboost_legacy_account_aliases "
        "(account_id,legacy_username,alias_role,ownership_provenance,legacy_status,"
        "legacy_expiry,observed_device_count,observed_hwid_count,evidence_json,created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (account["id"], f"mpay_parent_{tag}_{account['id']}", "PRIMARY", "OWNER_APPROVED",
         "ACTIVE", None, n_children, n_children, "{}", 1),
    ).lastrowid
    db._conn.commit()
    remote = FakeMarzban()
    template_username = f"mpay_parent_{tag}_{account['id']}"
    remote.users[template_username] = remote.users.pop("alice")
    remote.users[template_username]["username"] = template_username

    record = _pay_manual(db, cap, account["id"], plan=plan, days=days,
                         tag=f"{tag}-initial", now=now)
    applied = _apply(db, cap, record["id"], now=now + 10)
    children = []
    for index in range(n_children):
        slot = db.device_slots.claim(
            account["id"], f"raw-hwid-{tag}-{index}", HWID_KEY, now=now + index,
        )
        prepared = db.child_provisioning.prepare_child_ensure(
            account_id=account["id"], slot_generation_id=slot["generation_id"],
            source_alias_id=alias_id,
            source_contract_hash=source_contract_hash(
                remote.users[template_username]
            ),
            expire=applied["new_expiry"],
            idempotency_key=f"child-{tag}-{index}---------", now=now + 20 + index,
        )
        claim = db.child_provisioning.claim(
            prepared["operation_id"], worker_id="fixture-worker",
            now=now + 21 + index, lease_seconds=60,
        )
        created = BrokerOperations(remote).dispatch("child.user.ensure", claim["payload"])
        child_uuid = created.pop("uuid")
        db.child_provisioning.acknowledge(
            prepared["operation_id"], worker_id="fixture-worker",
            outcome=created["outcome"], child_uuid=child_uuid,
            remote_result=created, now=now + 22 + index,
        )
        verifier = db._conn.execute(
            "SELECT uuid_verifier FROM mgboost_child_user_intents WHERE id=?",
            (prepared["child_intent_id"],),
        ).fetchone()["uuid_verifier"]
        children.append({
            "generation": slot, "intent_id": prepared["child_intent_id"],
            "username": prepared["child_username"], "uuid_verifier": verifier,
        })
    return {
        "account": account, "remote": remote, "initial": applied,
        "initial_record": record, "children": children,
    }


# --- formula / same-parent guarantees --------------------------------------------


def test_repeated_30d_and_60d_purchases_stack_on_the_same_parent(db):
    cap = _capability(db)
    topo = _build_topology(db, cap, plan="BASIC_PRO", days=30, n_children=2, tag="stack30")
    account_id = topo["account"]["id"]
    prior_tail = topo["initial"]["new_expiry"]
    assert prior_tail > 20000  # the active subscription tail is the DL-044 anchor
    second = _pay_manual(db, cap, account_id, plan="BASIC_PRO", days=30,
                         tag="stack30-second", now=20000)
    r2 = _apply(db, cap, second["id"], now=20000)
    sub_before = db._conn.execute(
        "SELECT id FROM mgboost_subscriptions WHERE account_id=? ORDER BY id DESC LIMIT 1",
        (account_id,),
    ).fetchone()
    assert r2["subscription_id"] == sub_before["id"]
    assert r2["anchor"] == prior_tail
    assert r2["new_expiry"] == prior_tail + 30 * 86400
    third = _pay_manual(db, cap, account_id, plan="BASIC_PRO", days=60,
                        tag="stack30-third", now=20100)
    r3 = _apply(db, cap, third["id"], now=20100)
    assert r3["new_expiry"] == r2["new_expiry"] + 60 * 86400
    subscriptions_after = db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_subscriptions WHERE account_id=?", (account_id,),
    ).fetchone()[0]
    accounts_after = db._conn.execute("SELECT COUNT(*) FROM mgboost_accounts").fetchone()[0]
    assert (subscriptions_after, accounts_after) == (1, 1)
    operations = [
        row["operation"] for row in db._conn.execute(
            "SELECT operation FROM mgboost_entitlement_mutations WHERE account_id=? "
            "ORDER BY id", (account_id,),
        )
    ]
    assert operations == ["CREATE", "RENEW", "RENEW"]
    terms = db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_subscription_terms WHERE account_id=?", (account_id,),
    ).fetchone()[0]
    assert terms == 3


def test_expired_subscription_renews_from_now_not_history(db):
    cap = _capability(db)
    topo = _build_topology(db, cap, plan="WL", days=30, n_children=1, tag="expired")
    account_id = topo["account"]["id"]
    stale_expiry = 900 + 30 * 86400
    db._conn.execute(
        "UPDATE mgboost_subscriptions SET current_expiry=? WHERE account_id=?",
        (stale_expiry, account_id),
    )
    db._conn.commit()
    record = _pay_manual(db, cap, account_id, plan="WL", days=30, tag="expired-renew",
                         now=stale_expiry - 3600)
    result = _apply(db, cap, record["id"], now=stale_expiry + 7200)
    assert result["already_applied"] is False
    # The subscription had fully lapsed: DL-044 anchors from `now`, not from
    # the historical expiry, and never from the record's own creation time.
    assert result["anchor"] == stale_expiry + 7200
    assert result["new_expiry"] == stale_expiry + 7200 + 30 * 86400
    proof = calculate_effective_entitlement(
        db, account_id=account_id, now=result["new_expiry"] - 1
    )
    assert proof["wl"]["real_plan_mode"] == "LIMITED"
    assert proof["subscription"]["effective_status"] == "ACTIVE"


def test_existing_far_future_expiry_keeps_extending_from_itself(db):
    cap = _capability(db)
    account = db.accounts.create_account("DIRECT", now=1)
    far_future = 90 * 86400
    db._conn.execute(
        "INSERT INTO mgboost_subscriptions (account_id,current_plan_version_id,status,"
        "started_at,current_expiry,created_at,updated_at,row_version) "
        "SELECT ?,p.id,'ACTIVE',0,?,0,0,1 FROM mgboost_plan_versions p "
        "WHERE p.plan_code='BASIC'",
        (account["id"], far_future),
    )
    db._conn.commit()
    record = _pay_manual(db, cap, account["id"], plan="BASIC", days=30, tag="far-future",
                         now=far_future - 86400)
    result = _apply(db, cap, record["id"], now=far_future - 86400)
    assert result["anchor"] == far_future
    assert result["new_expiry"] == far_future + 30 * 86400
    current = db._conn.execute(
        "SELECT current_expiry FROM mgboost_subscriptions WHERE account_id=?", (account["id"],),
    ).fetchone()[0]
    assert current == result["new_expiry"]


def test_duplicate_replay_of_a_successful_payment_adds_zero_extra_days(db):
    cap = _capability(db)
    topo = _build_topology(db, cap, plan="BASIC", days=30, n_children=1, tag="replay")
    replayed = _apply(db, cap, topo["initial_record"]["id"], now=9950)
    assert replayed["already_applied"] is True
    assert replayed["new_expiry"] == topo["initial"]["new_expiry"]
    terms = db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_subscription_terms WHERE account_id=?",
        (topo["account"]["id"],),
    ).fetchone()[0]
    assert terms == 1


def test_retry_after_a_later_independent_renewal_never_rolls_back_or_doubles(db):
    cap = _capability(db)
    topo = _build_topology(db, cap, plan="BASIC", days=30, n_children=1, tag="later")
    account_id = topo["account"]["id"]
    # The fixture already applied the first payment; this call replays it.
    older = _apply(db, cap, topo["initial_record"]["id"], now=12000)
    assert older["already_applied"] is True
    # A fresh record extends from the still-future tail of the first payment.
    newer = _pay_manual(db, cap, account_id, plan="BASIC", days=30, tag="later-newer",
                        now=12100)
    newest = _apply(db, cap, newer["id"], now=12100)
    stale_retry = _apply(db, cap, topo["initial_record"]["id"], now=12200)
    assert stale_retry["already_applied"] is True
    assert stale_retry["new_expiry"] == older["new_expiry"]
    current = db._conn.execute(
        "SELECT current_expiry FROM mgboost_subscriptions WHERE account_id=?", (account_id,),
    ).fetchone()[0]
    assert newest["anchor"] == older["new_expiry"]
    assert newest["new_expiry"] == older["new_expiry"] + 30 * 86400
    assert current == newest["new_expiry"]


# --- concurrency ------------------------------------------------------------------


def test_concurrent_stars_and_manual_renewals_stack_exactly_once_each(db):
    cap = _capability(db)
    account = db.accounts.create_account("DIRECT", now=1)
    db.accounts.link_telegram_owner(account["id"], 777001, provenance="MIGRATION",
                                    actor="test", now=1)
    # PH5-11: must stay on the real Stars path, so the plan must be one of
    # the sellable first-rollout SKUs (FAMILY is gated now).
    invoice = db.stars_purchases.create_invoice(
        telegram_id=777001, plan_code="BASIC_PLUS", duration_days=60, ttl_seconds=3600, now=100,
    )
    assert db.stars_purchases.capture_paid(
        invoice["id"], charge_id="charge-stars-concurrent", provider_charge_id=None,
        payer_telegram_id=777001, currency="XTR", amount=invoice["stars_price"], now=101,
    ) == "paid"
    manual = _pay_manual(db, cap, account["id"], plan="BASIC_PLUS", days=60,
                         tag="concurrent-manual", now=102)
    results = {}
    barrier = threading.Barrier(2)

    def stars_side():
        barrier.wait()
        results["stars"] = db.stars_purchases.apply_paid_invoice(invoice["id"], now=300)

    def manual_side():
        barrier.wait()
        results["manual"] = _apply(db, cap, manual["id"], now=300)

    t1, t2 = threading.Thread(target=stars_side), threading.Thread(target=manual_side)
    t1.start(); t2.start(); t1.join(); t2.join()
    star_result, manual_result = results["stars"], results["manual"]
    total = 300 + 60 * 86400 + 60 * 86400
    first, last = sorted([star_result["new_expiry"], manual_result["new_expiry"]])
    assert last == total
    assert first >= 300 + 60 * 86400
    rows = db._conn.execute(
        "SELECT COALESCE(SUM(payment_channel='TELEGRAM_STARS'),0),"
        "COALESCE(SUM(payment_channel='EXTERNAL_PAYMENT'),0),COUNT(*) "
        "FROM mgboost_entitlement_mutations WHERE account_id=?", (account["id"],),
    ).fetchone()
    # One CREATE from whichever side won the empty-account race plus both renewals.
    stars_count, external_count, total_rows = tuple(rows)
    assert (stars_count, external_count) == (1, 1)
    assert total_rows in (2, 3)


def test_concurrent_distinct_manual_payments_never_double_or_lose_days(db):
    cap = _capability(db)
    account = db.accounts.create_account("DIRECT", now=1)
    records = [
        _pay_manual(db, cap, account["id"], plan="EXTENDED", days=30, tag=f"cc-{i}", now=400 + i)
        for i in range(4)
    ]
    outcomes = []
    barrier = threading.Barrier(4)

    def worker(record):
        barrier.wait()
        outcomes.append(_apply(db, cap, record["id"], now=4060))

    threads = [threading.Thread(target=worker, args=(r,)) for r in records]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(outcomes) == 4
    expiries = sorted(o["new_expiry"] for o in outcomes)
    assert expiries[-1] == 4060 + 4 * 30 * 86400
    renew_rows = db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_subscription_terms WHERE account_id=?", (account["id"],),
    ).fetchone()[0]
    assert renew_rows == 4
    final = db._conn.execute(
        "SELECT current_expiry FROM mgboost_subscriptions WHERE account_id=?", (account["id"],),
    ).fetchone()[0]
    assert final == expiries[-1]


# --- child topology / parent-sync machinery ----------------------------------------


@pytest.mark.parametrize("n_children", [0, 1, 3])
def test_child_expiry_synchronization_matrix(db, n_children):
    cap = _capability(db)
    topo = _build_topology(db, cap, plan="BASIC_PRO", days=30, n_children=n_children,
                           tag=f"sync{n_children}")
    expected_expiry = topo["initial"]["new_expiry"]
    result = _cycle(db, topo, worker="matrix-worker")
    if n_children == 0:
        assert result["prepared"] == 0
        assert len(db.manual_payments.pending_sync_jobs()) == 1
        return
    assert result["aggregate_state"] == "IN_SYNC"
    verifiers = {
        row["id"]: row["uuid_verifier"] for row in db._conn.execute(
            "SELECT id,uuid_verifier FROM mgboost_child_user_intents WHERE account_id=?",
            (topo["account"]["id"],),
        )
    }
    for child in topo["children"]:
        intent = db._conn.execute(
            "SELECT observed_state,desired_state FROM mgboost_child_user_intents WHERE id=?",
            (child["intent_id"],),
        ).fetchone()
        assert (intent["observed_state"], intent["desired_state"]) == ("ACTIVE", "ACTIVE")
        # UUID identity is never rotated by renewal or its child sync.
        assert child["uuid_verifier"]
        assert verifiers[child["intent_id"]] == child["uuid_verifier"]
        remote_user = topo["remote"].users[child["username"]]
        assert remote_user["expire"] == expected_expiry
        assert remote_user["status"] == "active"
    db.manual_payments.record_sync_result(topo["initial_record"]["id"], state="SYNCED",
                                          now=5100)
    assert len(db.manual_payments.pending_sync_jobs()) == 0


def test_maximum_twelve_child_topologies_converge_without_identity_changes(db):
    cap = _capability(db)
    topo = _build_topology(db, cap, plan="FAMILY", days=30, n_children=12, tag="family12")
    generations_before = {
        row["id"]: (row["hwid_verifier"], row["hwid_masked"], row["status"])
        for row in db._conn.execute(
            "SELECT id,hwid_verifier,hwid_masked,status FROM mgboost_device_slot_generations "
            "WHERE account_id=? ORDER BY id", (topo["account"]["id"],),
        )
    }
    usernames_before = sorted(topo["remote"].users)
    verifiers_before = {
        row["id"]: row["uuid_verifier"] for row in db._conn.execute(
            "SELECT id,uuid_verifier FROM mgboost_child_user_intents WHERE account_id=?",
            (topo["account"]["id"],),
        )
    }
    renewal = _pay_manual(db, cap, topo["account"]["id"], plan="FAMILY", days=30,
                          tag="family12-renew", now=6000)
    renewed = _apply(db, cap, renewal["id"], now=6000)
    assert renewed["new_expiry"] == topo["initial"]["new_expiry"] + 30 * 86400
    result = _cycle(db, topo, worker="family12-worker", now=6500)
    assert result["aggregate_state"] == "IN_SYNC"
    assert result["applied"] == 12
    generations_after = {
        row["id"]: (row["hwid_verifier"], row["hwid_masked"], row["status"])
        for row in db._conn.execute(
            "SELECT id,hwid_verifier,hwid_masked,status FROM mgboost_device_slot_generations "
            "WHERE account_id=? ORDER BY id", (topo["account"]["id"],),
        )
    }
    assert generations_after == generations_before
    verifiers_after = {
        row["id"]: row["uuid_verifier"] for row in db._conn.execute(
            "SELECT id,uuid_verifier FROM mgboost_child_user_intents WHERE account_id=?",
            (topo["account"]["id"],),
        )
    }
    assert verifiers_after == verifiers_before
    assert sorted(topo["remote"].users) == usernames_before
    for child in topo["children"]:
        remote_user = topo["remote"].users[child["username"]]
        assert remote_user["expire"] == renewed["new_expiry"]
        assert remote_user["status"] == "active"
    proof = calculate_effective_entitlement(
        db, account_id=topo["account"]["id"], now=renewed["new_expiry"] - 1
    )
    assert proof["device"]["limit"] == 12
    assert proof["subscription"]["effective_expiry"] == renewed["new_expiry"]
    slots = db._conn.execute(
        "SELECT COUNT(*) FROM mgboost_device_slots WHERE account_id=?", (topo["account"]["id"],),
    ).fetchone()[0]
    assert slots == 12


def test_partial_remote_failure_stays_recoverable_and_never_doubles_days(db):
    cap = _capability(db)
    topo = _build_topology(db, cap, plan="FAMILY", days=30, n_children=3, tag="partial")
    failing_child = topo["children"][1]["username"]

    def flaky_sync(payload):
        if payload["child_username"] == failing_child and not flaky_sync.failed_once:
            flaky_sync.failed_once = True
            raise ConnectionError("transient broker outage")
        return BrokerOperations(topo["remote"]).dispatch("child.user.state.sync", payload)

    flaky_sync.failed_once = False
    # A broker outage escapes the cycle exactly like the real drivers see it:
    # the child op stays IN_FLIGHT under an expiring lease, fully recoverable.
    with pytest.raises(ConnectionError):
        _cycle(db, topo, worker="flaky-worker", sync_fn=flaky_sync, now=7000)
    job = db._conn.execute(
        "SELECT state,attempts FROM mgboost_manual_payment_sync_jobs "
        "WHERE payment_record_id=?", (topo["initial_record"]["id"],),
    ).fetchone()
    # No external driver has marked convergence yet: the durable hand-off row
    # stays PENDING/recoverable until a later cycle proves IN_SYNC.
    assert job["state"] == "PENDING"
    healed = run_account_sync_cycle(
        db, topo["account"]["id"],
        sync_fn=lambda p: BrokerOperations(topo["remote"]).dispatch(
            "child.user.state.sync", p
        ),
        worker_id="heal-worker", now=7600,
    )
    assert healed["aggregate_state"] == "IN_SYNC"
    for child in topo["children"]:
        remote_user = topo["remote"].users[child["username"]]
        assert remote_user["expire"] == topo["initial"]["new_expiry"]
    subscription = db._conn.execute(
        "SELECT current_expiry FROM mgboost_subscriptions WHERE account_id=?",
        (topo["account"]["id"],),
    ).fetchone()[0]
    assert subscription == topo["initial"]["new_expiry"]


def test_restart_backoff_and_late_second_renewal_keep_durable_handoff(db):
    cap = _capability(db)
    topo = _build_topology(db, cap, plan="BASIC", days=30, n_children=1, tag="restart")
    data_dir = os.environ["DATA_DIR"]
    db._conn.close()
    import src.config as config2
    import src.database as database2
    importlib.reload(config2)
    importlib.reload(database2)
    database2.DB_PATH = os.path.join(data_dir, "db.sqlite3")
    reopened = database2.Database()
    try:
        jobs = reopened.manual_payments.pending_sync_jobs()
        assert len(jobs) == 1 and jobs[0]["payment_record_id"] == topo["initial_record"]["id"]
        _raw, session = AdminSessionStore().create(PRIMARY_LOGIN, "test-server-jwt")
        cap2 = reopened.primary_admin_authority.authorize_session(session)
        late = _pay_manual(reopened, cap2, topo["account"]["id"], plan="BASIC", days=60,
                           tag="restart-second", now=8000)
        result = _apply(reopened, cap2, late["id"], now=8000)
        assert result["new_expiry"] == topo["initial"]["new_expiry"] + 60 * 86400
        # A leased/backoff-style cycle still acknowledges against fresh state.
        cyc = run_account_sync_cycle(
            reopened, topo["account"]["id"],
            sync_fn=lambda p: {"outcome": "ALREADY_IN_SYNC"},
            worker_id="restart-worker-a", now=8100,
        )
        assert cyc["aggregate_state"] in {"IN_SYNC", "PENDING"}
        expiries = {
            row["desired_expire"] for row in reopened._conn.execute(
                "SELECT desired_expire FROM mgboost_parent_sync_operations "
                "WHERE account_id=? AND state='APPLIED'", (topo["account"]["id"],),
            )
        }
        applied_fact = reopened._conn.execute(
            "SELECT MAX(applied_expiry) AS m FROM mgboost_manual_payment_records "
            "WHERE account_id=?", (topo["account"]["id"],),
        ).fetchone()["m"]
        assert max(expiries) <= max(result["new_expiry"], applied_fact or 0)
        assert applied_fact == result["new_expiry"]
    finally:
        reopened._conn.close()


def test_wl_period_history_is_preserved_and_new_periods_append_contiguously(db):
    cap = _capability(db)
    topo = _build_topology(db, cap, plan="WL", days=60, n_children=2, tag="wlbuckets")
    first_periods = [
        (row["sequence_no"], row["starts_at"], row["ends_at"])
        for row in db._conn.execute(
            "SELECT sequence_no,starts_at,ends_at FROM mgboost_wl_periods "
            "WHERE account_id=? ORDER BY sequence_no", (topo["account"]["id"],),
        )
    ]
    assert len(first_periods) == 2
    assert first_periods[0][2] == first_periods[1][1]
    # WL windows are UTC-hour aligned while the subscription expiry stays
    # exact-second (DL-020); the prior tail is therefore its floored hour.
    prior_tail = first_periods[-1][2]
    initial_expiry = topo["initial"]["new_expiry"]
    assert prior_tail == initial_expiry - (initial_expiry % 3600)
    renewal = _pay_manual(db, cap, topo["account"]["id"], plan="WL", days=30,
                          tag="wlbuckets-renew", now=prior_tail - 600)
    renewed = _apply(db, cap, renewal["id"], now=prior_tail - 600)
    all_periods = [
        (row["sequence_no"], row["starts_at"], row["ends_at"])
        for row in db._conn.execute(
            "SELECT sequence_no,starts_at,ends_at FROM mgboost_wl_periods "
            "WHERE account_id=? ORDER BY sequence_no", (topo["account"]["id"],),
        )
    ]
    assert len(all_periods) == 3
    assert all_periods[:2] == first_periods  # history byte-identical, never rewritten
    new_period = all_periods[-1]
    expected_anchor = renewed["anchor"] - (renewed["anchor"] % 3600)
    assert new_period[0] == first_periods[-1][0] + 1
    assert new_period[1] == expected_anchor
    assert new_period[2] == new_period[1] + 30 * 86400
