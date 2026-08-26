"""PH5-01 versioned six-plan catalog: approved data + idempotent seeding.

Every price below is the owner-approved product catalog recorded in
`ROADMAP.md` ("Approved product catalog" / DL-040) -- nothing here invents a
price, duration, device limit or WL quota. This module only turns that
approved table into durable, versioned rows: one `mgboost_plan_versions` row
per plan code (PH3-01 schema, already immutable), one `mgboost_plan_durations`
row per 30/60-day SKU, and one `mgboost_plan_prices` row per (plan, duration,
channel) under a channel-scoped `mgboost_price_catalog_versions` row
(PH5-01 schema).

Seeding is deliberately NOT run automatically at Database startup (the same
discipline PH3-01/PH3-06 used for their own dormant schemas) -- call
`seed_plan_catalog()` explicitly (see `scripts/seed_ph5_01_plan_catalog.py`).
It is idempotent: re-running it against an already-seeded database changes
nothing and returns the existing rows.
"""

from __future__ import annotations

import sqlite3
import time


GB_DECIMAL = 10**9  # WL quota is stored as decimal-byte bytes (PH3-01 convention).

STARS_CATALOG_VERSION = "STARS-2026-08-26-v1"
RUB_CATALOG_VERSION = "RUB-2026-08-23-v1"  # DL-040

DURATIONS_DAYS = (30, 60)

# plan_code -> (display_name, device_limit, wl_quota_gb or None for Non-WL)
# WL period is always a fixed 30 days per plan (DL-044/PH5-02: a 60-day
# purchase creates two sequential 30-day WL periods, not a doubled quota).
PLAN_SPECS = (
    ("BASIC", "Базовый", 3, None),
    ("BASIC_PLUS", "Базовый Плюс", 6, None),
    ("BASIC_PRO", "Базовый Про", 12, None),
    ("WL", "WL", 3, 100),
    ("EXTENDED", "Расширенный", 6, 150),
    ("FAMILY", "Семейный", 12, 150),
)

# (plan_code, duration_days) -> amount, Telegram Stars. ROADMAP.md "Approved
# product catalog".
STARS_PRICES = {
    ("BASIC", 30): 99, ("BASIC", 60): 169,
    ("BASIC_PLUS", 30): 139, ("BASIC_PLUS", 60): 199,
    ("BASIC_PRO", 30): 169, ("BASIC_PRO", 60): 249,
    ("WL", 30): 199, ("WL", 60): 349,
    ("EXTENDED", 30): 249, ("EXTENDED", 60): 399,
    ("FAMILY", 30): 299, ("FAMILY", 60): 449,
}

# (plan_code, duration_days) -> amount, RUB. DL-040 `RUB-2026-08-23-v1`.
RUB_PRICES = {
    ("BASIC", 30): 169, ("BASIC", 60): 279,
    ("BASIC_PLUS", 30): 239, ("BASIC_PLUS", 60): 339,
    ("BASIC_PRO", 30): 279, ("BASIC_PRO", 60): 399,
    ("WL", 30): 349, ("WL", 60): 579,
    ("EXTENDED", 30): 399, ("EXTENDED", 60): 679,
    ("FAMILY", 30): 499, ("FAMILY", 60): 749,
}

CHANNEL_CATALOGS = {
    "TELEGRAM_STARS": (STARS_CATALOG_VERSION, STARS_PRICES),
    "RUB": (RUB_CATALOG_VERSION, RUB_PRICES),
}


class PlanCatalogError(ValueError):
    pass


class PlanCatalogStore:
    """Thin repository for the PH5-01 price-catalog tables.

    Reuses `AccountStore.create_plan_version`/`add_plan_duration` for plan
    identity/terms (PH3-01) -- this store only ever writes the new
    channel-price tables.
    """

    def __init__(self, connection: sqlite3.Connection, lock, accounts):
        self._conn = connection
        self._lock = lock
        self._accounts = accounts

    def get_plan_version(self, plan_code: str, version: int = 1) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM mgboost_plan_versions WHERE plan_code=? AND version=?",
                (plan_code, int(version)),
            ).fetchone()
        return dict(row) if row else None

    def get_plan_duration(self, plan_version_id: int, duration_days: int) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM mgboost_plan_durations "
                "WHERE plan_version_id=? AND duration_days=? "
                "ORDER BY duration_version DESC LIMIT 1",
                (int(plan_version_id), int(duration_days)),
            ).fetchone()
        return dict(row) if row else None

    def get_active_catalog_version(self, channel: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM mgboost_price_catalog_versions "
                "WHERE channel=? AND status='ACTIVE'",
                (channel,),
            ).fetchone()
        return dict(row) if row else None

    def get_or_create_catalog_version(
        self, channel: str, catalog_version: str, *, now: int | None = None
    ) -> dict:
        timestamp = int(time.time()) if now is None else int(now)
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                existing = self._conn.execute(
                    "SELECT * FROM mgboost_price_catalog_versions "
                    "WHERE channel=? AND catalog_version=?",
                    (channel, catalog_version),
                ).fetchone()
                if existing:
                    self._conn.commit()
                    return dict(existing)
                active = self._conn.execute(
                    "SELECT catalog_version FROM mgboost_price_catalog_versions "
                    "WHERE channel=? AND status='ACTIVE'",
                    (channel,),
                ).fetchone()
                if active is not None:
                    raise PlanCatalogError(
                        f"channel {channel!r} already has an active catalog version "
                        f"{active['catalog_version']!r}; retire it explicitly first"
                    )
                cursor = self._conn.execute(
                    "INSERT INTO mgboost_price_catalog_versions "
                    "(channel,catalog_version,status,activated_at) "
                    "VALUES (?,?,'ACTIVE',?)",
                    (channel, catalog_version, timestamp),
                )
                row = self._conn.execute(
                    "SELECT * FROM mgboost_price_catalog_versions WHERE id=?",
                    (cursor.lastrowid,),
                ).fetchone()
                self._conn.commit()
                return dict(row)
            except Exception:
                self._conn.rollback()
                raise

    def get_or_create_price(
        self,
        *,
        catalog_version_id: int,
        plan_version_id: int,
        duration_id: int,
        amount: int,
        now: int | None = None,
    ) -> dict:
        if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
            raise PlanCatalogError("amount must be a positive integer")
        timestamp = int(time.time()) if now is None else int(now)
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                existing = self._conn.execute(
                    "SELECT * FROM mgboost_plan_prices "
                    "WHERE catalog_version_id=? AND plan_version_id=? AND duration_id=?",
                    (int(catalog_version_id), int(plan_version_id), int(duration_id)),
                ).fetchone()
                if existing:
                    if existing["amount"] != int(amount):
                        raise PlanCatalogError(
                            "existing price row disagrees with requested amount; "
                            "prices are immutable, retire the catalog version instead"
                        )
                    self._conn.commit()
                    return dict(existing)
                cursor = self._conn.execute(
                    "INSERT INTO mgboost_plan_prices "
                    "(catalog_version_id,plan_version_id,duration_id,amount,created_at) "
                    "VALUES (?,?,?,?,?)",
                    (int(catalog_version_id), int(plan_version_id), int(duration_id),
                     int(amount), timestamp),
                )
                row = self._conn.execute(
                    "SELECT * FROM mgboost_plan_prices WHERE id=?", (cursor.lastrowid,)
                ).fetchone()
                self._conn.commit()
                return dict(row)
            except Exception:
                self._conn.rollback()
                raise

    def active_catalog(self, channel: str) -> list[dict]:
        """Read-only: every plan-duration SKU currently sold on `channel`."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT pv.plan_code, pv.display_name, pv.device_limit_mode, "
                "pv.device_limit, pv.wl_mode, pv.wl_quota_bytes, pv.wl_period_days, "
                "pd.duration_days, pp.amount, cv.catalog_version, cv.channel "
                "FROM mgboost_plan_prices AS pp "
                "JOIN mgboost_price_catalog_versions AS cv ON cv.id=pp.catalog_version_id "
                "JOIN mgboost_plan_versions AS pv ON pv.id=pp.plan_version_id "
                "JOIN mgboost_plan_durations AS pd ON pd.id=pp.duration_id "
                "WHERE cv.channel=? AND cv.status='ACTIVE' "
                "ORDER BY pv.device_limit, pd.duration_days",
                (channel,),
            ).fetchall()
        return [dict(row) for row in rows]


def seed_plan_catalog(store: PlanCatalogStore, *, now: int | None = None) -> dict:
    """Idempotently create the approved six plans, their 30/60-day durations
    and both channels' active price catalogs. Safe to call repeatedly (each
    step reuses an existing row instead of duplicating or overwriting it).
    """
    timestamp = int(time.time()) if now is None else int(now)
    plan_versions: dict[str, dict] = {}
    durations: dict[tuple[str, int], dict] = {}

    for plan_code, display_name, device_limit, wl_gb in PLAN_SPECS:
        plan = store.get_plan_version(plan_code)
        if plan is None:
            wl_mode = "NONE" if wl_gb is None else "LIMITED"
            plan = store._accounts.create_plan_version(
                {
                    "plan_code": plan_code,
                    "version": 1,
                    "display_name": display_name,
                    "plan_kind": "COMMERCIAL",
                    "billing_required": True,
                    "device_limit_mode": "LIMITED",
                    "device_limit": device_limit,
                    "wl_mode": wl_mode,
                    "wl_quota_bytes": None if wl_gb is None else wl_gb * GB_DECIMAL,
                    "wl_period_days": None if wl_gb is None else 30,
                    "terms": {
                        "catalog": "ph5-01-six-plan-v1",
                        "device_limit": device_limit,
                        "wl_quota_gb": wl_gb,
                    },
                },
                now=timestamp,
            )
        plan_versions[plan_code] = plan
        for duration_days in DURATIONS_DAYS:
            duration = store.get_plan_duration(plan["id"], duration_days)
            if duration is None:
                duration = store._accounts.add_plan_duration(
                    plan["id"], duration_days, now=timestamp
                )
            durations[(plan_code, duration_days)] = duration

    catalog_versions: dict[str, dict] = {}
    prices_created = 0
    for channel, (catalog_version, price_table) in CHANNEL_CATALOGS.items():
        catalog_versions[channel] = store.get_or_create_catalog_version(
            channel, catalog_version, now=timestamp
        )
        for (plan_code, duration_days), amount in price_table.items():
            catalog_version_id = catalog_versions[channel]["id"]
            plan_version_id = plan_versions[plan_code]["id"]
            duration_id = durations[(plan_code, duration_days)]["id"]
            pre_existing = store._conn.execute(
                "SELECT 1 FROM mgboost_plan_prices "
                "WHERE catalog_version_id=? AND plan_version_id=? AND duration_id=?",
                (catalog_version_id, plan_version_id, duration_id),
            ).fetchone()
            store.get_or_create_price(
                catalog_version_id=catalog_version_id,
                plan_version_id=plan_version_id,
                duration_id=duration_id,
                amount=amount,
                now=timestamp,
            )
            if pre_existing is None:
                prices_created += 1

    return {
        "plan_versions": plan_versions,
        "durations": durations,
        "catalog_versions": catalog_versions,
        "prices_newly_created": prices_created,
    }
