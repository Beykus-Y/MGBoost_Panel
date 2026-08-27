"""PH5-11 first commercial STANDARD signup: self-service DIRECT account +
system-owned provisioning template.

A brand-new Telegram customer has no legacy account, no legacy
source_username, no legacy subscription URL and no reviewed alias. This
module is the ONLY self-service account-creation path in the system, and it
exists exclusively downstream of a confirmed Stars payment:

  * ``ensure_signup_account()`` is called from ``capture_paid`` -- i.e. only
    after Telegram reports money actually moved. It is idempotent and
    fill-once: retrying the same charge converges to exactly ONE account,
    one alias, one Telegram owner link and one template job; a payer that
    already owns a canonical account is bound to THAT account, never given a
    second one.
  * The account's PRIMARY alias is a SYSTEM-OWNED provisioning template
    username (``tpl-<account_public_id>``) -- infrastructure, not a customer
    identity. The template's exact VLESS contract (flow + the STANDARD
    delivery-profile inbound membership) is pinned as a hash by the durable
    worker job (``ensure_template_for_account``); the anti-tamper
    source-contract verification in the broker is preserved verbatim, and
    the customer never receives the template's UUID or subscription URL.
  * Every occupied device slot still gets its own child Marzban user with
    its own Marzban-minted UUID, exactly as for migrated accounts.

The local write here is deliberately free of any Marzban call; the only
remote step is the worker-driven template provisioning, and its failure
never loses the paid entitlement (account + subscription + credential are
already durable; the job retries; the first device fail-closes uniformly
until the template exists).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time

from .delivery_routing import STANDARD_PROFILE_CODE, DeliveryRoutingError
from .wl_topology import WL_INBOUND_TAGS


# First-rollout sellable plans: exactly the three non-WL STANDARD tariffs.
# WL / EXTENDED / FAMILY are not purchasable in this rollout and are gated
# here as well as in the catalog/UI layers.
SELLABLE_STANDARD_PLAN_CODES = ("BASIC", "BASIC_PLUS", "BASIC_PRO")

SIGNUP_INVOICE_KIND = "CANONICAL_SIGNUP"

# Evidenced against live production children (2026-08-27 read-only probe):
# every real child user carries proxies.vless.flow="xtls-rprx-vision".
TEMPLATE_VLESS_FLOW = "xtls-rprx-vision"

_ACTOR = "system:commercial-signup"


class CommercialSignupError(RuntimeError):
    pass


class PlanNotSellable(CommercialSignupError):
    pass


def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def derive_template_username(account_public_id: str) -> str:
    """Infrastructure-owned source username for one account's provisioning
    template. Deterministic, unique per account (the aliases table is
    globally UNIQUE), valid per ``legacy_contract.USERNAME_RE``."""
    if not isinstance(account_public_id, str) or not account_public_id.startswith("acct_"):
        raise CommercialSignupError("invalid account public id")
    username = f"tpl-{account_public_id}"
    if len(username) > 128:
        raise CommercialSignupError("derived template username is invalid")
    return username


def assert_plan_sellable(plan_code: str) -> None:
    if plan_code not in SELLABLE_STANDARD_PLAN_CODES:
        raise PlanNotSellable(
            "only the BASIC / BASIC_PLUS / BASIC_PRO STANDARD tariffs are "
            "purchasable in this rollout"
        )


class CommercialSignupStore:
    def __init__(self, connection: sqlite3.Connection, lock, accounts, delivery_routing):
        self._conn = connection
        self._lock = lock
        self._accounts = accounts
        self._delivery_routing = delivery_routing

    # --- self-service DIRECT account (confirmed-payment downstream only) -----

    def ensure_signup_account(
        self, *, telegram_id: int, invoice_id: int, plan_code: str, now: int | None = None,
    ) -> int:
        """Resolve-or-create the payer's canonical DIRECT account for a paid
        signup invoice, and bind it to the invoice exactly once.

        Never creates a second account for one Telegram owner: an existing
        active canonical owner account is reused (the purchase then follows
        the ordinary same-plan renewal semantics through PH5-02). The
        invoice's fill-once ``account_id`` is the authoritative anchor, so a
        concurrent caller that arrives after the binding commit but before
        the owner link resolves to the SAME account and simply re-runs the
        (idempotent) owner link."""
        timestamp = int(time.time()) if now is None else int(now)
        assert_plan_sellable(plan_code)
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                invoice = self._conn.execute(
                    "SELECT id,account_id,payer_telegram_id,created_by_telegram_id "
                    "FROM stars_invoices WHERE id=?", (int(invoice_id),),
                ).fetchone()
                if invoice is None:
                    raise CommercialSignupError("signup invoice not found")
                payer = int(invoice["payer_telegram_id"] or invoice["created_by_telegram_id"])
                if payer != int(telegram_id):
                    raise CommercialSignupError("payer does not match the signup invoice")
                account_id = invoice["account_id"]
                if account_id is not None:
                    account = self._accounts.get_account(int(account_id))
                    if account is None or account["status"] == "CLOSED":
                        raise CommercialSignupError("bound signup account is not active")
                else:
                    account = self._accounts.get_active_account_by_telegram_id(int(telegram_id))
                    if account is None:
                        account = self._accounts.create_account("DIRECT", now=timestamp)
                    account_id = int(account["id"])
                    public_id = account["public_id"]

                    alias = self._conn.execute(
                        "SELECT id FROM mgboost_legacy_account_aliases WHERE account_id=?",
                        (account_id,),
                    ).fetchone()
                    if alias is None:
                        self._conn.execute(
                            "INSERT INTO mgboost_legacy_alias_groups "
                            "(account_id,mapping_key,decision_ref,created_by_actor,created_at) "
                            "VALUES (?,?,?,?,?)",
                            (account_id, f"commercial-signup-v1:{public_id}",
                             f"stars-signup-invoice-{int(invoice_id)}", _ACTOR, timestamp),
                        )
                        evidence = {
                            "telegram_id": int(telegram_id), "invoice_id": int(invoice_id),
                            "plan_code": plan_code, "kind": SIGNUP_INVOICE_KIND,
                        }
                        self._conn.execute(
                            "INSERT INTO mgboost_legacy_account_aliases "
                            "(account_id,legacy_username,alias_role,ownership_provenance,legacy_status,"
                            "legacy_expiry,observed_device_count,observed_hwid_count,evidence_json,created_at) "
                            "VALUES (?,?,'PRIMARY','EVIDENCE_PROVEN','ACTIVE',NULL,0,0,?,?)",
                            (account_id, derive_template_username(public_id),
                             _canonical(evidence), timestamp),
                        )
                        self._conn.execute(
                            "INSERT INTO mgboost_direct_account_reviews "
                            "(account_id,legacy_username,ownership_evidence,decision_ref,reviewed_by_actor,"
                            "evidence_json,created_at) VALUES (?,?,?,?,?,?,?)",
                            (account_id, derive_template_username(public_id), "PROVEN",
                             f"stars-signup-invoice-{int(invoice_id)}", _ACTOR,
                             _canonical(evidence), timestamp),
                        )
                        self._conn.execute(
                            "INSERT OR IGNORE INTO mgboost_signup_template_jobs "
                            "(account_id,invoice_id,state,created_at,updated_at) "
                            "VALUES (?,?, 'PENDING',?,?)",
                            (account_id, int(invoice_id), timestamp, timestamp),
                        )
                    # Fill-once (schema trigger refuses any later re-binding).
                    self._conn.execute(
                        "UPDATE stars_invoices SET account_id=? WHERE id=? AND account_id IS NULL",
                        (account_id, int(invoice_id)),
                    )
                self._conn.commit()
            except sqlite3.IntegrityError as exc:
                self._conn.rollback()
                raise CommercialSignupError("signup account binding conflict") from exc
            except Exception:
                self._conn.rollback()
                raise

        # Outside the transaction above (the identity store opens its own):
        # idempotent, reached by EVERY caller including the one that found
        # the binding already filled, so the owner link always converges.
        # Uniqueness is enforced by partial unique indexes.
        self._accounts.link_telegram_owner(
            account_id, int(telegram_id), provenance="DIRECT_BIND", actor=_ACTOR,
            now=timestamp,
        )
        return account_id

    # --- system-owned provisioning template ----------------------------------

    def template_for_account(self, account_id: int) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM mgboost_provisioning_templates WHERE account_id=? AND state='ACTIVE'",
                (int(account_id),),
            ).fetchone()
        return dict(row) if row else None

    def pending_template_jobs(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM mgboost_signup_template_jobs WHERE state='PENDING' ORDER BY account_id"
            ).fetchall()
        return [dict(row) for row in rows]

    def record_template_result(self, account_id: int, *, state: str, error_class: str | None = None,
                               now: int | None = None) -> None:
        if state not in {"PENDING", "READY", "MANUAL_REVIEW"}:
            raise ValueError("invalid signup template job state")
        timestamp = int(time.time()) if now is None else int(now)
        with self._lock:
            self._conn.execute(
                "UPDATE mgboost_signup_template_jobs SET state=?,attempts=attempts+1,"
                "last_error_class=?,last_attempt_at=?,ready_at=?,updated_at=? "
                "WHERE account_id=? AND state='PENDING'",
                (state, error_class, timestamp, timestamp if state == "READY" else None,
                 timestamp, int(account_id)),
            )
            self._conn.commit()

    def ensure_template_for_account(self, account_id: int, *, marzban, now: int | None = None) -> dict:
        """Converge the account's remote template user with the pinned
        STANDARD membership, then pin its exact contract hash locally.

        Idempotent and convergent by re-reading remote state first; a
        template that already exists with a DIFFERENT contract than the
        pinned hash is a STOP-class manual review, never silently re-pinned
        (existing children verify against the pinned hash)."""
        timestamp = int(time.time()) if now is None else int(now)
        account_id = int(account_id)
        account = self._accounts.get_account(account_id)
        if account is None or account["status"] != "ACTIVE":
            raise CommercialSignupError("active account required for template provisioning")
        template_username = derive_template_username(account["public_id"])

        alias = self._conn.execute(
            "SELECT id FROM mgboost_legacy_account_aliases WHERE account_id=? AND alias_role='PRIMARY'",
            (account_id,),
        ).fetchone()
        if alias is None:
            raise CommercialSignupError("account has no provisioning alias")

        membership = self._delivery_routing.membership(STANDARD_PROFILE_CODE)
        if not membership:
            return {"state": "MANUAL_REVIEW", "error_class": "empty_standard_delivery_profile"}
        if any(tag in WL_INBOUND_TAGS for tag in membership):
            # Corrupted routing storage must never reach the remote template.
            return {"state": "MANUAL_REVIEW", "error_class": "wl_tag_in_standard_profile"}

        payload = {
            "username": template_username,
            "proxies": {"vless": {"flow": TEMPLATE_VLESS_FLOW}},
            "inbounds": {"vless": sorted(membership)},
            "expire": 0,
            "data_limit": None,
            "data_limit_reset_strategy": "no_reset",
            "status": "active",
            "note": "MGBoost infrastructure provisioning template (STANDARD)",
        }

        existing = None
        try:
            existing = marzban.get_user(template_username)
        except Exception as exc:
            if not _is_not_found(exc):
                raise
        if existing is None:
            try:
                marzban.create_user(payload)
            except Exception as exc:
                if _is_conflict(exc):
                    # A concurrent creator won the race; fall through to the
                    # authoritative reread below.
                    existing = None
                else:
                    raise
        template = None
        try:
            template = marzban.get_user(template_username)
        except Exception:
            template = None
        if template is None:
            raise CommercialSignupError("template user is not readable after provisioning")

        from .child_contract import source_contract_hash
        computed_hash = source_contract_hash(template)
        pinned = self.template_for_account(account_id)
        if pinned is not None:
            if pinned["template_username"] != template_username:
                return {"state": "MANUAL_REVIEW", "error_class": "template_username_mismatch"}
            if pinned["source_contract_hash"] != computed_hash:
                return {"state": "MANUAL_REVIEW", "error_class": "template_contract_drift"}
            return {"state": "READY", "source_contract_hash": computed_hash,
                    "already_pinned": True}

        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                self._conn.execute(
                    "INSERT INTO mgboost_provisioning_templates "
                    "(account_id,template_username,source_contract_hash,state,pinned_at,updated_at) "
                    "VALUES (?,?,?,'ACTIVE',?,?)",
                    (account_id, template_username, computed_hash, timestamp, timestamp),
                )
                self._conn.commit()
            except sqlite3.IntegrityError:
                self._conn.rollback()
                pinned = self.template_for_account(account_id)
                if pinned is None or pinned["source_contract_hash"] != computed_hash:
                    return {"state": "MANUAL_REVIEW", "error_class": "template_contract_drift"}
                return {"state": "READY", "source_contract_hash": computed_hash,
                        "already_pinned": True}
            except Exception:
                self._conn.rollback()
                raise
        return {"state": "READY", "source_contract_hash": computed_hash, "already_pinned": False}


def _is_not_found(exc: Exception) -> bool:
    return getattr(exc, "code", None) == 404


def _is_conflict(exc: Exception) -> bool:
    return getattr(exc, "code", None) in (400, 409)
