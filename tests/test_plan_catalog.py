import os
import tempfile

import pytest


@pytest.fixture
def db():
    tmp = tempfile.mkdtemp()
    os.environ["DATA_DIR"] = tmp
    from importlib import reload
    import src.config as cfg
    reload(cfg)
    cfg.DATA_DIR = tmp
    import src.database as db_mod
    reload(db_mod)
    db_mod.DB_PATH = os.path.join(tmp, "db.sqlite3")
    instance = db_mod.Database()
    yield instance
    instance._conn.close()


EXPECTED_PLANS = {
    "BASIC": (3, None),
    "BASIC_PLUS": (6, None),
    "BASIC_PRO": (12, None),
    "WL": (3, 100),
    "EXTENDED": (6, 150),
    "FAMILY": (12, 150),
}

EXPECTED_STARS = {
    ("BASIC", 30): 99, ("BASIC", 60): 169,
    ("BASIC_PLUS", 30): 139, ("BASIC_PLUS", 60): 199,
    ("BASIC_PRO", 30): 169, ("BASIC_PRO", 60): 249,
    ("WL", 30): 199, ("WL", 60): 349,
    ("EXTENDED", 30): 249, ("EXTENDED", 60): 399,
    ("FAMILY", 30): 299, ("FAMILY", 60): 449,
}

EXPECTED_RUB = {
    ("BASIC", 30): 169, ("BASIC", 60): 279,
    ("BASIC_PLUS", 30): 239, ("BASIC_PLUS", 60): 339,
    ("BASIC_PRO", 30): 279, ("BASIC_PRO", 60): 399,
    ("WL", 30): 349, ("WL", 60): 579,
    ("EXTENDED", 30): 399, ("EXTENDED", 60): 679,
    ("FAMILY", 30): 499, ("FAMILY", 60): 749,
}


def test_seed_creates_six_plans_with_approved_device_and_wl_terms(db):
    from src.plan_catalog import seed_plan_catalog

    result = seed_plan_catalog(db.plan_catalog, now=100)
    assert set(result["plan_versions"]) == set(EXPECTED_PLANS)
    for plan_code, (device_limit, wl_gb) in EXPECTED_PLANS.items():
        plan = result["plan_versions"][plan_code]
        assert plan["plan_kind"] == "COMMERCIAL"
        assert plan["billing_required"] == 1
        assert plan["device_limit_mode"] == "LIMITED"
        assert plan["device_limit"] == device_limit
        if wl_gb is None:
            assert plan["wl_mode"] == "NONE"
            assert plan["wl_quota_bytes"] is None
            assert plan["wl_period_days"] is None
        else:
            assert plan["wl_mode"] == "LIMITED"
            assert plan["wl_quota_bytes"] == wl_gb * 10**9
            assert plan["wl_period_days"] == 30


def test_seed_creates_exactly_12_durations_and_prices_per_channel(db):
    from src.plan_catalog import seed_plan_catalog

    seed_plan_catalog(db.plan_catalog, now=100)
    stars = db.plan_catalog.active_catalog("TELEGRAM_STARS")
    rub = db.plan_catalog.active_catalog("RUB")
    assert len(stars) == 12
    assert len(rub) == 12

    stars_by_key = {(row["plan_code"], row["duration_days"]): row["amount"] for row in stars}
    rub_by_key = {(row["plan_code"], row["duration_days"]): row["amount"] for row in rub}
    assert stars_by_key == EXPECTED_STARS
    assert rub_by_key == EXPECTED_RUB
    assert all(row["catalog_version"] == "STARS-2026-08-26-v1" for row in stars)
    assert all(row["catalog_version"] == "RUB-2026-08-23-v1" for row in rub)


def test_seed_is_idempotent(db):
    from src.plan_catalog import seed_plan_catalog

    first = seed_plan_catalog(db.plan_catalog, now=100)
    assert first["prices_newly_created"] == 24
    second = seed_plan_catalog(db.plan_catalog, now=200)
    assert second["prices_newly_created"] == 0
    assert len(db.plan_catalog.active_catalog("TELEGRAM_STARS")) == 12
    assert len(db.plan_catalog.active_catalog("RUB")) == 12
    # Re-seeding does not create a second plan_version row for any plan code.
    for plan_code in EXPECTED_PLANS:
        count = db._conn.execute(
            "SELECT COUNT(*) FROM mgboost_plan_versions WHERE plan_code=?", (plan_code,)
        ).fetchone()[0]
        assert count == 1


def test_seed_script_module_matches_repository_seeding(db):
    import importlib.util
    import json

    script_path = os.path.join(
        os.path.dirname(__file__), "..", "scripts", "seed_ph5_01_plan_catalog.py"
    )
    spec = importlib.util.spec_from_file_location("ph5_01_seed", script_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    db_path = db._conn.execute("PRAGMA database_list").fetchone()[2]
    db._conn.close()
    import sys
    sys.argv = ["seed_ph5_01_plan_catalog.py", "--db", db_path, "--now", "100"]
    module.main()

    import sqlite3
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    count = conn.execute("SELECT COUNT(*) FROM mgboost_plan_prices").fetchone()[0]
    conn.close()
    assert count == 24
