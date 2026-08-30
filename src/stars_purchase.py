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
    SELLABLE_PLAN_CODES, SIGNUP_INVOICE_KIND, assert_plan_sellable,
)
from .entitlement_engine import calculate_effective_entitlement
from .promo import PromoConflict
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
        """The purchasable SKUs: the three STANDARD plans plus the three WL
        plans (WL / EXTENDED / FAMILY), every duration the active catalog
        sells for them. Packages are NOT plan SKUs and stay unpurchasable
        (PH6-08 absent)."""
        sellable = set(SELLABLE_PLAN_CODES)
        return [item for item in self.catalog() if item["plan_code"] in sellable]

    def create_invoice(self, *, telegram_id: int, plan_code: str, duration_days: int,
                       ttl_seconds: int, now: int | None = None,
                       promo_redemption_id: int | None = None) -> dict:
        """`promo_redemption_id` (PH5-13): bind a live RESERVED purchase
        reservation inside this invoice's own transaction and write the
        immutable discount snapshot; the invoice is created with the
        DISCOUNTED `stars_price`. Signup invoices (no account) never carry a
        promo -- reservations require an existing proven account."""
        timestamp = int(time.time()) if now is None else int(now)
        assert_plan_sellable(plan_code)
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                promo_discount = self._resolve_promo_discount_locked(
                    promo_redemption_id, telegram_id, timestamp) if promo_redemption_id is not None else None
                account = self._accounts.get_active_account_by_telegram_id(int(telegram_id))
                if account is None:
                    # A Telegram id already bound to a legacy subscription is
                    # a known customer, not a new commercial signup. Keep this
                    # invariant at the invoice primitive as well as the bot
                    # UI so a stale/forged callback cannot create parallel
                    # paid subscriptions.
                    if self._database is not None and self._database.get_tg_user(int(telegram_id)) is not None:
                        raise StarsPurchaseError(
                            "a legacy-linked Telegram user cannot create a canonical signup invoice"
                        )
                    # A brand-new commercial customer. The invoice row (with
                    # a NULL account_id) is the durable pre-payment intent;
                    # no account/entitlement exists until capture confirms
                    # the payment.
                    if promo_discount is not None:
                        raise StarsPurchaseError(
                            "a purchase discount requires an existing account")
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
                final_price = int(price["amount"])
                if promo_discount is not None:
                    from .promo import _discount_from_effect_params
                    final_price = _discount_from_effect_params(
                        promo_discount["effect_params"], final_price)
                cursor = self._conn.execute(
                    "INSERT INTO stars_invoices (created_by_telegram_id,marzban_username,tariff_id,tariff_name,"
                    "duration_days,stars_price,status,expires_at,created_at,invoice_kind,account_id,"
                    "plan_version_id,duration_id,catalog_version_id,price_id,plan_code_snapshot,"
                    "plan_version_snapshot,catalog_version_snapshot,price_amount_snapshot,"
                    "promo_redemption_id,original_stars_price,discount_minor) "
                    "VALUES (?,?,?,?,?,?,'created',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (int(telegram_id), account["public_id"], None, plan["display_name"], int(duration_days),
                     final_price, timestamp + int(ttl_seconds), timestamp, "CANONICAL_PLAN",
                     account["id"], plan["id"], duration["id"], catalog["id"], price["id"], plan["plan_code"],
                     plan["version"], catalog["catalog_version"], price["amount"],
                     promo_redemption_id,
                     int(price["amount"]) if promo_discount is not None else None,
                     int(price["amount"]) - final_price if promo_discount is not None else None),
                )
                invoice_id = cursor.lastrowid
                if promo_discount is not None:
                    self._promo().bind_purchase_reservation_locked(
                        redemption_id=int(promo_redemption_id), telegram_id=int(telegram_id),
                        bound_kind="STARS", bound_invoice_id=invoice_id, now=timestamp)
                self._conn.commit()
                row = self._conn.execute("SELECT * FROM stars_invoices WHERE id=?", (invoice_id,)).fetchone()
                return dict(row)
            except Exception:
                self._conn.rollback()
                raise


    def _promo(self):
        promo = self._database.promo if self._database is not None else None
        if promo is None:
            raise StarsPurchaseError("promo store unavailable")
        return promo

    def _resolve_promo_discount_locked(self, promo_redemption_id, telegram_id, timestamp):
        """Validate a RESERVED purchase reservation (caller's transaction)
        and return its effect params for the immutable discount snapshot.
        Binding to the concrete invoice id happens right after the INSERT."""
        if promo_redemption_id is None:
            return None
        try:
            promo = self._promo()
            row = promo.purchase_reservation_locked(int(promo_redemption_id))
            if row is None or row["effect_kind"] != "PURCHASE_DISCOUNT":
                raise StarsPurchaseError("reservation is not a purchase discount")
            if int(row["owner_telegram_id"] or -1) != int(telegram_id):
                raise StarsPurchaseError("reservation does not belong to this payer")
            if row["status"] != "RESERVED":
                raise StarsPurchaseError(f"reservation is {row['status']}, not RESERVED")
            import json as _json
            return {"effect_params": _json.loads(row["effect_params_json"])}
        except StarsPurchaseError:
            raise
        except Exception as exc:
            raise StarsPurchaseError(f"reservation invalid: {exc}") from exc

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
            try:
                self._conn.execute("BEGIN IMMEDIATE")
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
                else:
                    account = self._accounts.get_active_account_by_telegram_id(int(telegram_id))
                    if not account or account["id"] != row["account_id"]:
                        raise StarsPurchaseError("payer is not the canonical account owner")
                    self._assert_purchase_plan_locked(row["account_id"], row["plan_code_snapshot"])
                if row["promo_redemption_id"] is not None:
                    # PH5-13 pre_checkout gate: RESERVED -> COMMITTED.  The
                    # transaction owning this CAS always commits or rolls back.
                    try:
                        self._promo().commit_purchase_reservation_locked(
                            redemption_id=int(row["promo_redemption_id"]),
                            invoice_id=int(row["id"]), now=timestamp)
                    except PromoConflict as exc:
                        raise StarsPurchaseError(str(exc)) from exc
                self._conn.commit()
                return dict(row)
            except Exception:
                self._conn.rollback()
                raise

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
        if expected != actual:
            raise ProductSnapshotMismatch("immutable product snapshot disagrees with its referenced catalog")
        if row["promo_redemption_id"] is not None:
            # PH5-13: the payable amount is the immutable discounted price
            # (catalog price minus the reservation's discount, floor 1).
            from .promo import _discount_from_effect_params
            reservation = self._promo().purchase_reservation_locked(int(row["promo_redemption_id"]))
            if reservation is None:
                raise ProductSnapshotMismatch("discount reservation is missing")
            effect_params = json.loads(reservation["effect_params_json"])
            expected_final = _discount_from_effect_params(
                effect_params, int(row["price_amount_snapshot"]))
            if int(row["stars_price"]) != expected_final:
                raise ProductSnapshotMismatch("discounted price disagrees with its reservation snapshot")
        elif row["stars_price"] != product["amount"]:
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
                expected_amount = (
                    int(row["stars_price"]) if row["promo_redemption_id"] is not None
                    else int(row["price_amount_snapshot"]))
                if currency != "XTR" or int(amount) != expected_amount:
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
                if row["promo_redemption_id"] is not None:
                    # PH5-13: RESERVE/COMMITTED -> REDEEMED inside the SAME
                    # transaction as the paid flip -- one payment is exactly
                    # one redemption. Zero rows rolls the whole capture back
                    # (surfaced as manual_review on retry, never guessed).
                    self._promo().redeem_purchase_reservation_locked(
                        redemption_id=int(row["promo_redemption_id"]),
                        invoice_id=int(row["id"]), now=timestamp)
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
