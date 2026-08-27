"""PH5-05 canonical Stars plan purchase/renewal adapter.

This is intentionally an adapter over the established Stars invoice callback
and PH5-02 renewal primitive.  It does not charge, refund or mutate Marzban;
it records exactly what was paid, turns one paid invoice into at most one
canonical entitlement mutation, and leaves child convergence to PH3-08's
durable outbox.

PH5-11 adds the first-rollout commercial signup kind
(``CANONICAL_SIGNUP``): a Telegram user with NO canonical account yet may
buy one of the three sellable STANDARD tariffs. Nothing is created before a
confirmed payment -- the invoice row itself (with a NULL account_id) is the
only durable pre-payment intent. At capture time (money already moved) the
bound signup factory resolves-or-creates exactly one DIRECT account for the
payer (fill-once, trigger-enforced); application then proceeds through the
same PH5-02 renewal engine, where the fresh account's first grant is simply
the CREATE operation.
"""

from __future__ import annotations

import json
import sqlite3
import time

from .commercial_signup import (
    SELLABLE_STANDARD_PLAN_CODES, SIGNUP_INVOICE_KIND, assert_plan_sellable,
)
from .entitlement_engine import calculate_effective_entitlement
from .subscription_renewal import (
    PlanMismatch, RenewalError, UnlimitedSubscriptionConflict, UnknownPlan,
)


class StarsPurchaseError(ValueError):
    pass


class PlanChangeRequired(StarsPurchaseError):
    pass


class ProductSnapshotMismatch(StarsPurchaseError):
    pass


def _canonical(value: dict) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _invoice_idempotency_key(invoice_id: int) -> str:
    return f"ph5-05-stars-invoice-{int(invoice_id):020d}"


class StarsPurchaseStore:
    def __init__(self, connection: sqlite3.Connection, lock, accounts, plan_catalog, subscription_renewal):
        self._conn = connection
        self._lock = lock
        self._accounts = accounts
        self._plan_catalog = plan_catalog
        self._subscription_renewal = subscription_renewal
        self._signup_factory = None
        self._database = None

    def bind_database(self, db) -> None:
        self._database = db

    def bind_signup_factory(self, factory) -> None:
        """Wired to ``CommercialSignupStore.ensure_signup_account`` at
        Database construction. Called only from ``capture_paid`` for
        CANONICAL_SIGNUP invoices -- i.e. strictly after a confirmed
        payment."""
        self._signup_factory = factory

    def catalog(self) -> list[dict]:
        return self._plan_catalog.active_catalog("TELEGRAM_STARS")

    def sellable_catalog(self) -> list[dict]:
        """The first-rollout purchasable SKUs: only the three STANDARD
        plans, every duration the active catalog sells for them."""
        sellable = set(SELLABLE_STANDARD_PLAN_CODES)
        return [item for item in self.catalog() if item["plan_code"] in sellable]

    def create_invoice(self, *, telegram_id: int, plan_code: str, duration_days: int,
                       ttl_seconds: int, now: int | None = None) -> dict:
        timestamp = int(time.time()) if now is None else int(now)
        assert_plan_sellable(plan_code)
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                account = self._accounts.get_active_account_by_telegram_id(int(telegram_id))
                if account is None:
                    # A brand-new commercial customer. The invoice row (with
                    # a NULL account_id) is the durable pre-payment intent;
                    # no account/entitlement exists until capture confirms
                    # the payment.
                    plan, duration, catalog, price = self._lookup_active_product_locked(plan_code, duration_days)
                    cursor = self._conn.execute(
                        "INSERT INTO stars_invoices (created_by_telegram_id,marzban_username,tariff_id,tariff_name,"
                        "duration_days,stars_price,status,expires_at,created_at,invoice_kind,account_id,"
                        "plan_version_id,duration_id,catalog_version_id,price_id,plan_code_snapshot,"
                        "plan_version_snapshot,catalog_version_snapshot,price_amount_snapshot) "
                        "VALUES (?,?,?,?,?,?,'created',?,?,?,?,?,?,?,?,?,?,?,?)",
                        (int(telegram_id), f"signup-{int(telegram_id)}", None, plan["display_name"],
                         int(duration_days), int(price["amount"]), timestamp + int(ttl_seconds),
                         timestamp, SIGNUP_INVOICE_KIND, None, plan["id"], duration["id"],
                         catalog["id"], price["id"], plan["plan_code"], plan["version"],
                         catalog["catalog_version"], price["amount"]),
                    )
                    invoice_id = cursor.lastrowid
                    self._conn.commit()
                    row = self._conn.execute("SELECT * FROM stars_invoices WHERE id=?", (invoice_id,)).fetchone()
                    return dict(row)
                plan, duration, catalog, price = self._lookup_active_product_locked(plan_code, duration_days)
                self._assert_purchase_plan_locked(account["id"], plan_code)
                cursor = self._conn.execute(
                    "INSERT INTO stars_invoices (created_by_telegram_id,marzban_username,tariff_id,tariff_name,"
                    "duration_days,stars_price,status,expires_at,created_at,invoice_kind,account_id,"
                    "plan_version_id,duration_id,catalog_version_id,price_id,plan_code_snapshot,"
                    "plan_version_snapshot,catalog_version_snapshot,price_amount_snapshot) "
                    "VALUES (?,?,?,?,?,?,'created',?,?,?,?,?,?,?,?,?,?,?,?)",
                    (int(telegram_id), account["public_id"], None, plan["display_name"], int(duration_days),
                     int(price["amount"]), timestamp + int(ttl_seconds), timestamp, "CANONICAL_PLAN",
                     account["id"], plan["id"], duration["id"], catalog["id"], price["id"], plan["plan_code"],
                     plan["version"], catalog["catalog_version"], price["amount"]),
                )
                invoice_id = cursor.lastrowid
                self._conn.commit()
                row = self._conn.execute("SELECT * FROM stars_invoices WHERE id=?", (invoice_id,)).fetchone()
                return dict(row)
            except Exception:
                self._conn.rollback()
                raise

    def _lookup_active_product_locked(self, plan_code: str, duration_days: int):
        plan = self._conn.execute(
            "SELECT * FROM mgboost_plan_versions WHERE plan_code=? AND version=1", (plan_code,)
        ).fetchone()
        if not plan or plan["plan_kind"] != "COMMERCIAL" or not plan["billing_required"]:
            raise StarsPurchaseError("unknown or non-billable plan")
        duration = self._conn.execute(
            "SELECT * FROM mgboost_plan_durations WHERE plan_version_id=? AND duration_days=? "
            "ORDER BY duration_version DESC LIMIT 1", (plan["id"], int(duration_days)),
        ).fetchone()
        catalog = self._conn.execute(
            "SELECT * FROM mgboost_price_catalog_versions WHERE channel='TELEGRAM_STARS' AND status='ACTIVE'",
        ).fetchone()
        if not duration or not catalog:
            raise StarsPurchaseError("requested plan product is unavailable")
        price = self._conn.execute(
            "SELECT * FROM mgboost_plan_prices WHERE catalog_version_id=? AND plan_version_id=? AND duration_id=?",
            (catalog["id"], plan["id"], duration["id"]),
        ).fetchone()
        if not price:
            raise StarsPurchaseError("requested plan price is unavailable")
        return dict(plan), dict(duration), dict(catalog), dict(price)

    def _assert_purchase_plan_locked(self, account_id: int, plan_code: str) -> None:
        # First-rollout purchase gate: only the three non-WL STANDARD plans
        # are sellable, regardless of any subscription state below.
        assert_plan_sellable(plan_code)
        subscription = self._conn.execute(
            "SELECT s.status,p.plan_code FROM mgboost_subscriptions s "
            "JOIN mgboost_plan_versions p ON p.id=s.current_plan_version_id "
            "WHERE s.account_id=? ORDER BY s.id DESC LIMIT 1", (int(account_id),),
        ).fetchone()
        if not subscription:
            return
        if subscription["status"] == "UNLIMITED":
            raise StarsPurchaseError("unlimited subscription cannot be overwritten by a commercial purchase")
        if subscription["plan_code"] != plan_code:
            raise PlanChangeRequired("a different real plan requires PH5-06")

    def validate_invoice_for_checkout(self, invoice_id: int, telegram_id: int, *, now: int | None = None) -> dict:
        timestamp = int(time.time()) if now is None else int(now)
        with self._lock:
            row = self._conn.execute("SELECT * FROM stars_invoices WHERE id=?", (int(invoice_id),)).fetchone()
            if not row or row["invoice_kind"] not in {"CANONICAL_PLAN", SIGNUP_INVOICE_KIND}:
                raise StarsPurchaseError("not a canonical Stars invoice")
            if row["status"] != "created" or timestamp >= row["expires_at"]:
                raise StarsPurchaseError("invoice is not payable")
            self._validate_snapshot_locked(dict(row))
            if row["invoice_kind"] == SIGNUP_INVOICE_KIND:
                # A signup invoice is personal: only its creator may check
                # out. No account exists (or is created) at checkout time.
                if int(row["created_by_telegram_id"]) != int(telegram_id):
                    raise StarsPurchaseError("payer is not the signup invoice creator")
                assert_plan_sellable(row["plan_code_snapshot"])
                return dict(row)
            account = self._accounts.get_active_account_by_telegram_id(int(telegram_id))
            if not account or account["id"] != row["account_id"]:
                raise StarsPurchaseError("payer is not the canonical account owner")
            self._assert_purchase_plan_locked(row["account_id"], row["plan_code_snapshot"])
            return dict(row)

    def _validate_snapshot_locked(self, row: dict) -> None:
        if row["invoice_kind"] not in {"CANONICAL_PLAN", SIGNUP_INVOICE_KIND}:
            raise ProductSnapshotMismatch("not canonical")
        product = self._conn.execute(
            "SELECT pv.plan_code,pv.version,pd.duration_days,cv.catalog_version,cv.channel,pp.amount "
            "FROM mgboost_plan_prices pp JOIN mgboost_plan_versions pv ON pv.id=pp.plan_version_id "
            "JOIN mgboost_plan_durations pd ON pd.id=pp.duration_id "
            "JOIN mgboost_price_catalog_versions cv ON cv.id=pp.catalog_version_id "
            "WHERE pp.id=? AND pp.plan_version_id=? AND pp.duration_id=? AND pp.catalog_version_id=?",
            (row["price_id"], row["plan_version_id"], row["duration_id"], row["catalog_version_id"]),
        ).fetchone()
        if not product or product["channel"] != "TELEGRAM_STARS":
            raise ProductSnapshotMismatch("product references are missing or have the wrong channel")
        expected = (row["plan_code_snapshot"], row["plan_version_snapshot"], row["duration_days"],
                    row["catalog_version_snapshot"], row["price_amount_snapshot"])
        actual = (product["plan_code"], product["version"], product["duration_days"],
                  product["catalog_version"], product["amount"])
        if expected != actual or row["stars_price"] != product["amount"]:
            raise ProductSnapshotMismatch("immutable product snapshot disagrees with its referenced catalog")

    def capture_paid(self, invoice_id: int, *, charge_id: str, provider_charge_id: str | None,
                     payer_telegram_id: int, currency: str, amount: int,
                     now: int | None = None) -> str:
        """Capture a valid canonical callback exactly once.

        Returns ``paid``, ``manual_review`` or ``duplicate``.  Any mismatch
        after money moved is recorded as manual review rather than guessed.
        """
        timestamp = int(time.time()) if now is None else int(now)
        # Pre-read + signup account resolution happen BEFORE capture's write
        # transaction: the signup factory is itself transactional and must
        # never run nested inside this store's BEGIN IMMEDIATE. It is only
        # ever called for a well-formed, unpaid signup invoice with a
        # matching charge amount -- i.e. strictly after money moved.
        signup_account_id: int | None = None
        with self._lock:
            pre_row = self._conn.execute(
                "SELECT * FROM stars_invoices WHERE id=?", (int(invoice_id),)
            ).fetchone()
        if (
            pre_row is not None and pre_row["invoice_kind"] == SIGNUP_INVOICE_KIND
            and pre_row["status"] == "created"
            and currency == "XTR" and int(amount) == int(pre_row["price_amount_snapshot"])
        ):
            try:
                self._validate_snapshot_locked(dict(pre_row))
                assert_plan_sellable(pre_row["plan_code_snapshot"])
                if self._signup_factory is None:
                    raise StarsPurchaseError("signup factory unavailable")
                signup_account_id = int(self._signup_factory(
                    telegram_id=int(payer_telegram_id), invoice_id=int(pre_row["id"]),
                    plan_code=pre_row["plan_code_snapshot"],
                ))
            except Exception:
                signup_account_id = None
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                row = self._conn.execute("SELECT * FROM stars_invoices WHERE id=?", (int(invoice_id),)).fetchone()
                if not row or row["invoice_kind"] not in {"CANONICAL_PLAN", SIGNUP_INVOICE_KIND}:
                    self._conn.rollback()
                    return "manual_review"
                if row["status"] != "created":
                    self._conn.rollback()
                    return "duplicate" if row["telegram_payment_charge_id"] == charge_id else "manual_review"
                reason = None
                account_id = row["account_id"]
                if currency != "XTR" or int(amount) != int(row["price_amount_snapshot"]):
                    reason = "amount_or_currency_mismatch"
                elif row["invoice_kind"] == SIGNUP_INVOICE_KIND:
                    # Confirmed payment for a brand-new customer. The
                    # account was resolved-or-created above (idempotent,
                    # fill-once -- retries and duplicate deliveries converge
                    # to exactly one account, never a second one).
                    if signup_account_id is None:
                        reason = "signup_account_error"
                    else:
                        account_id = signup_account_id
                        try:
                            self._validate_snapshot_locked(dict(row))
                            assert_plan_sellable(row["plan_code_snapshot"])
                        except Exception:
                            reason = "product_or_account_state_mismatch"
                else:
                    account = self._accounts.get_active_account_by_telegram_id(int(payer_telegram_id))
                    if not account or account["id"] != row["account_id"]:
                        reason = "payer_account_mismatch"
                    else:
                        try:
                            self._validate_snapshot_locked(dict(row))
                            self._assert_purchase_plan_locked(row["account_id"], row["plan_code_snapshot"])
                        except PlanChangeRequired:
                            reason = "plan_change_requires_ph5_06"
                        except Exception:
                            reason = "product_or_account_state_mismatch"
                if reason:
                    self._conn.execute(
                        "UPDATE stars_invoices SET status='manual_review',telegram_payment_charge_id=?,"
                        "provider_payment_charge_id=?,payer_telegram_id=?,total_amount=?,payment_currency=?,paid_at=?,"
                        "manual_review_reason=?,manual_review_at=? WHERE id=? AND status='created'",
                        (charge_id, provider_charge_id, int(payer_telegram_id), int(amount), currency, timestamp,
                         reason, timestamp, int(invoice_id)),
                    )
                    self._conn.commit()
                    return "manual_review"
                self._conn.execute(
                    "UPDATE stars_invoices SET status='paid',telegram_payment_charge_id=?,provider_payment_charge_id=?,"
                    "payer_telegram_id=?,total_amount=?,payment_currency=?,paid_at=? WHERE id=? AND status='created'",
                    (charge_id, provider_charge_id, int(payer_telegram_id), int(amount), currency, timestamp, int(invoice_id)),
                )
                snapshot = {
                    "plan_code": row["plan_code_snapshot"], "plan_version": row["plan_version_snapshot"],
                    "duration_days": row["duration_days"], "catalog_version": row["catalog_version_snapshot"],
                    "price_amount": row["price_amount_snapshot"], "invoice_id": int(invoice_id),
                }
                self._conn.execute(
                    "INSERT INTO mgboost_stars_payment_evidence (invoice_id,account_id,telegram_payment_charge_id,"
                    "provider_payment_charge_id,payer_telegram_id,currency,amount,invoice_snapshot_json,captured_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (int(invoice_id), account_id, charge_id, provider_charge_id, int(payer_telegram_id),
                     currency, int(amount), _canonical(snapshot), timestamp),
                )
                self._conn.commit()
                return "paid"
            except sqlite3.IntegrityError:
                self._conn.rollback()
                return "duplicate"
            except Exception:
                self._conn.rollback()
                raise

    def pending_invoices(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM stars_invoices WHERE invoice_kind IN "
                "('CANONICAL_PLAN','CANONICAL_SIGNUP') AND status='paid' ORDER BY id"
            ).fetchall()
        return [dict(row) for row in rows]

    def apply_paid_invoice(self, invoice_id: int, *, now: int | None = None) -> dict:
        """Apply one paid invoice through PH5-02 with invoice-scoped idempotency."""
        timestamp = int(time.time()) if now is None else int(now)
        with self._lock:
            row = self._conn.execute("SELECT * FROM stars_invoices WHERE id=?", (int(invoice_id),)).fetchone()
            if not row or row["invoice_kind"] not in {"CANONICAL_PLAN", SIGNUP_INVOICE_KIND}:
                raise StarsPurchaseError("canonical invoice not found")
            row = dict(row)
            if row["status"] not in {"paid", "canonical_applied"}:
                raise StarsPurchaseError(f"invoice is not applicable from {row['status']}")
            if row["account_id"] is None:
                # A signup invoice always gains its account at capture time;
                # a paid row without one is a structural anomaly -> review.
                self._mark_manual_locked(row["id"], "missing_signup_account", timestamp)
                raise StarsPurchaseError("paid signup invoice has no bound account")
            try:
                self._validate_snapshot_locked(row)
            except ProductSnapshotMismatch as exc:
                if row["status"] == "paid":
                    self._mark_manual_locked(row["id"], "stale_product_catalog_snapshot", timestamp)
                raise exc
        # Keep the PH5-02 commit and its immediate PH5-04 proof in the same
        # process-local critical section.  SQLite already serializes writers
        # cross-process; this additionally prevents a second local callback
        # from moving expiry between this payment's commit and its proof.
        with self._lock:
            try:
                renewal = self._subscription_renewal.apply_same_plan_purchase(
                    account_id=row["account_id"], plan_code=row["plan_code_snapshot"], duration_days=row["duration_days"],
                    payment_channel="TELEGRAM_STARS", mutation_source="DIRECT_PURCHASE", actor_type="TELEGRAM",
                    actor_ref=str(row["payer_telegram_id"]), external_reference=row["telegram_payment_charge_id"],
                    reason=f"canonical Stars invoice {row['id']}", idempotency_key=_invoice_idempotency_key(row["id"]), now=timestamp,
                )
            except (PlanMismatch, UnlimitedSubscriptionConflict, UnknownPlan, RenewalError) as exc:
                self._mark_manual_locked(row["id"], f"apply_state_mismatch:{type(exc).__name__}", timestamp)
                raise StarsPurchaseError("canonical payment requires manual review") from exc
            entitlement = calculate_effective_entitlement(self._database, account_id=row["account_id"], now=timestamp)
            # A replay may occur after another independently paid invoice has
            # legitimately extended the same subscription.  The replayed
            # mutation's immutable expiry is then a lower historical point,
            # while PH5-04 correctly reports the later current expiry.  A
            # fresh mutation must match exactly; a replay must still retain
            # the paid plan and must never observe an expiry below its own
            # committed entitlement.
            expiry_matches = (
                entitlement["subscription"]["effective_expiry"] == renewal["new_expiry"]
                if not renewal["already_applied"]
                else entitlement["subscription"]["effective_expiry"] >= renewal["new_expiry"]
            )
            if entitlement["plan"]["code"] != row["plan_code_snapshot"] or not expiry_matches:
                raise RuntimeError("PH5-04 entitlement result disagrees with applied Stars renewal")
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                existing = self._conn.execute(
                    "SELECT * FROM mgboost_stars_purchase_applications WHERE invoice_id=?", (row["id"],)
                ).fetchone()
                if existing is None:
                    mutation = self._conn.execute(
                        "SELECT operation FROM mgboost_entitlement_mutations WHERE id=? AND account_id=?",
                        (renewal["mutation_id"], row["account_id"]),
                    ).fetchone()
                    self._conn.execute(
                        "INSERT INTO mgboost_stars_purchase_applications (invoice_id,account_id,entitlement_mutation_id,"
                        "applied_operation,applied_expiry,entitlement_snapshot_json,created_at) VALUES (?,?,?,?,?,?,?)",
                        (row["id"], row["account_id"], renewal["mutation_id"], mutation["operation"], renewal["new_expiry"],
                         _canonical(entitlement), timestamp),
                    )
                self._conn.execute(
                    "INSERT OR IGNORE INTO mgboost_stars_purchase_sync_jobs "
                    "(invoice_id,account_id,entitlement_mutation_id,created_at,updated_at) VALUES (?,?,?,?,?)",
                    (row["id"], row["account_id"], renewal["mutation_id"], timestamp, timestamp),
                )
                self._conn.execute(
                    "UPDATE stars_invoices SET status='canonical_applied',applied_expire=?,canonical_applied_at=?,"
                    "entitlement_mutation_id=? WHERE id=? AND status IN ('paid','canonical_applied')",
                    (renewal["new_expiry"], timestamp, renewal["mutation_id"], row["id"]),
                )
                self._conn.commit()
                return {**renewal, "entitlement": entitlement}
            except Exception:
                self._conn.rollback()
                raise

    def _mark_manual_locked(self, invoice_id: int, reason: str, now: int) -> None:
        self._conn.execute(
            "UPDATE stars_invoices SET status='manual_review',manual_review_reason=?,manual_review_at=? "
            "WHERE id=? AND status='paid'", (reason, now, int(invoice_id)),
        )
        self._conn.commit()

    def pending_sync_jobs(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM mgboost_stars_purchase_sync_jobs WHERE state='PENDING' ORDER BY invoice_id"
            ).fetchall()
        return [dict(row) for row in rows]

    def record_sync_result(self, invoice_id: int, *, state: str, error_class: str | None = None,
                           now: int | None = None) -> None:
        if state not in {"PENDING", "SYNCED", "MANUAL_REVIEW"}:
            raise ValueError("invalid Stars child sync state")
        timestamp = int(time.time()) if now is None else int(now)
        with self._lock:
            self._conn.execute(
                "UPDATE mgboost_stars_purchase_sync_jobs SET state=?,attempts=attempts+1,last_error_class=?,"
                "last_attempt_at=?,synced_at=?,updated_at=? WHERE invoice_id=? AND state='PENDING'",
                (state, error_class, timestamp, timestamp if state == "SYNCED" else None, timestamp, int(invoice_id)),
            )
            self._conn.commit()
