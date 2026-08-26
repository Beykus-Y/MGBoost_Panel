"""PH5-03 versioned WL package products priced in the existing PH5-01 catalogs."""

from __future__ import annotations

import sqlite3
import time

from .plan_catalog import GB_DECIMAL, RUB_CATALOG_VERSION, STARS_CATALOG_VERSION


PACKAGE_SPECS = (
    ("WL_PACKAGE_50_GB", "+50 GB", 50),
    ("WL_PACKAGE_100_GB", "+100 GB", 100),
    ("WL_PACKAGE_250_GB", "+250 GB", 250),
    ("WL_PACKAGE_500_GB", "+500 GB", 500),
)
PACKAGE_PRICES = {
    "TELEGRAM_STARS": {
        "WL_PACKAGE_50_GB": 79, "WL_PACKAGE_100_GB": 149,
        "WL_PACKAGE_250_GB": 349, "WL_PACKAGE_500_GB": 599,
    },
    "RUB": {
        "WL_PACKAGE_50_GB": 139, "WL_PACKAGE_100_GB": 249,
        "WL_PACKAGE_250_GB": 579, "WL_PACKAGE_500_GB": 999,
    },
}
CATALOG_VERSIONS = {"TELEGRAM_STARS": STARS_CATALOG_VERSION, "RUB": RUB_CATALOG_VERSION}


class WLPackageCatalogError(ValueError):
    pass


class WLPackageCatalogStore:
    def __init__(self, connection: sqlite3.Connection, lock):
        self._conn = connection
        self._lock = lock

    def get_product(self, sku: str, version: int = 1) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM mgboost_wl_package_products WHERE sku=? AND version=?", (sku, int(version))).fetchone()
        return dict(row) if row else None

    def active_price(self, sku: str, price_channel: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT p.*, product.sku, product.version AS product_version, product.bytes, "
                "cv.catalog_version, cv.channel FROM mgboost_wl_package_prices p "
                "JOIN mgboost_wl_package_products product ON product.id=p.package_product_id "
                "JOIN mgboost_price_catalog_versions cv ON cv.id=p.catalog_version_id "
                "WHERE product.sku=? AND cv.channel=? AND cv.status='ACTIVE' "
                "ORDER BY product.version DESC LIMIT 1", (sku, price_channel),
            ).fetchone()
        return dict(row) if row else None

    def get_or_create_product(self, sku: str, display_name: str, bytes_count: int, *, now: int) -> dict:
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                row = self._conn.execute("SELECT * FROM mgboost_wl_package_products WHERE sku=? AND version=1", (sku,)).fetchone()
                if row:
                    if row["display_name"] != display_name or row["bytes"] != int(bytes_count):
                        raise WLPackageCatalogError("immutable package product disagrees with approved catalog")
                    self._conn.commit()
                    return dict(row)
                cursor = self._conn.execute("INSERT INTO mgboost_wl_package_products (sku,version,display_name,bytes,created_at) VALUES (?,1,?,?,?)", (sku, display_name, int(bytes_count), int(now)))
                row = self._conn.execute("SELECT * FROM mgboost_wl_package_products WHERE id=?", (cursor.lastrowid,)).fetchone()
                self._conn.commit()
                return dict(row)
            except Exception:
                self._conn.rollback()
                raise

    def get_or_create_price(self, *, catalog_version_id: int, product_id: int, amount: int, now: int) -> dict:
        if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
            raise WLPackageCatalogError("package price must be a positive integer")
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                row = self._conn.execute("SELECT * FROM mgboost_wl_package_prices WHERE catalog_version_id=? AND package_product_id=?", (int(catalog_version_id), int(product_id))).fetchone()
                if row:
                    if row["amount"] != int(amount):
                        raise WLPackageCatalogError("immutable package price disagrees with approved catalog")
                    self._conn.commit()
                    return dict(row)
                cursor = self._conn.execute("INSERT INTO mgboost_wl_package_prices (catalog_version_id,package_product_id,amount,created_at) VALUES (?,?,?,?)", (int(catalog_version_id), int(product_id), int(amount), int(now)))
                row = self._conn.execute("SELECT * FROM mgboost_wl_package_prices WHERE id=?", (cursor.lastrowid,)).fetchone()
                self._conn.commit()
                return dict(row)
            except Exception:
                self._conn.rollback()
                raise


def seed_wl_package_catalog(store: WLPackageCatalogStore, *, now: int | None = None) -> dict:
    timestamp = int(time.time()) if now is None else int(now)
    products = {}
    for sku, display_name, gb in PACKAGE_SPECS:
        products[sku] = store.get_or_create_product(sku, display_name, gb * GB_DECIMAL, now=timestamp)
    prices_newly_created = 0
    prices = {}
    for channel, by_sku in PACKAGE_PRICES.items():
        catalog = store._conn.execute("SELECT * FROM mgboost_price_catalog_versions WHERE channel=? AND catalog_version=? AND status='ACTIVE'", (channel, CATALOG_VERSIONS[channel])).fetchone()
        if catalog is None:
            raise WLPackageCatalogError(f"PH5-01 active {channel} catalog is not seeded")
        for sku, amount in by_sku.items():
            existed = store._conn.execute("SELECT 1 FROM mgboost_wl_package_prices WHERE catalog_version_id=? AND package_product_id=?", (catalog["id"], products[sku]["id"])).fetchone()
            price = store.get_or_create_price(catalog_version_id=catalog["id"], product_id=products[sku]["id"], amount=amount, now=timestamp)
            prices[(channel, sku)] = price
            if existed is None:
                prices_newly_created += 1
    return {"products": products, "prices": prices, "prices_newly_created": prices_newly_created}
