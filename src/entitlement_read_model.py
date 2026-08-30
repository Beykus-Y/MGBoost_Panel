"""Read-only composition layer behind the Telegram bot subscription card
(bot UX redesign, slice C1). Composes existing stores into one bot-facing
snapshot and contains no business logic and no mutations:

- entitlement engine   -> plan, subscription status/expiry, WL usage/period
- DeviceSlotStore      -> canonical device count/limit (read-only capacity)
- subscription creds   -> whether an ACTIVE opaque URL exists
- tg_users + Marzban   -> legacy fallback for pre-panel customers

Marzban I/O must never happen inside this module: the bot event loop fetches
the live legacy Marzban user and passes it in as ``legacy_marzban_user``.
Cohort precedence mirrors the same canonical resolver used by Stars
signup/renewal (``AccountStore.get_active_account_by_telegram_id``): a
canonical account always wins; the legacy ``tg_users`` link is consulted
only when no canonical account exists.
"""
import logging
import time

logger = logging.getLogger(__name__)


def build_subscription_card(
    db, *, telegram_id: int,
    legacy_user: dict | None = None,
    legacy_marzban_user: dict | None = None,
    now: int | None = None,
) -> dict | None:
    """Return the bot-facing subscription snapshot, or None when the id
    belongs to no known customer (neither a canonical account nor a legacy
    link)."""
    now = int(time.time()) if now is None else int(now)
    account = db.accounts.get_active_account_by_telegram_id(telegram_id)
    if account is not None:
        return _canonical_card(db, account_id=int(account["id"]), now=now)
    if legacy_user is not None:
        return _legacy_card(db, legacy_user, legacy_marzban_user, now=now)
    return None


def _canonical_card(db, *, account_id: int, now: int) -> dict:
    ent = db.entitlements.calculate(account_id=account_id, now=now) or {}
    sub = ent.get("subscription") or {}
    plan = ent.get("plan") or {}
    device = ent.get("device") or {}
    wl = ent.get("wl") or {}
    status = sub.get("effective_status") or "NONE"
    return {
        "cohort": "canonical",
        "account_id": account_id,
        "status": status,
        "plan_name": plan.get("display_name") or plan.get("code"),
        "plan_code": plan.get("code"),
        "unlimited": status == "UNLIMITED",
        "expiry": sub.get("effective_expiry"),
        "traffic_used": None,
        "traffic_limit": None,
        "wl": _wl_view(wl),
        "devices": _devices_view(db, account_id, device, now=now),
        "has_active_credential": _has_active_credential(db, account_id),
        "marzban_username": None,
    }


def _wl_view(wl: dict) -> dict | None:
    mode = wl.get("effective_mode")
    if mode == "UNLIMITED":
        return {"mode": "UNLIMITED", "quota_bytes": None, "consumed_bytes": None,
                "remaining_bytes": None, "period_ends_at": None}
    if mode != "LIMITED":
        return None
    period = wl.get("current_period") or {}
    quota = wl.get("configured_quota_bytes")
    if quota is None:
        quota = wl.get("base_quota_bytes")
    return {
        "mode": "LIMITED",
        "quota_bytes": quota,
        "consumed_bytes": wl.get("consumed_bytes"),
        "remaining_bytes": wl.get("effective_remaining_bytes"),
        "period_ends_at": period.get("ends_at"),
    }


def _devices_view(db, account_id: int, device: dict, *, now: int) -> dict:
    try:
        capacity = db.device_slots.get_capacity_state(account_id, now=now)
    except Exception as e:
        # Capacity is presentation data: a transient entitlement failure must
        # degrade the card, never break it.
        logger.warning(f"read model: device capacity unavailable: {type(e).__name__}")
        capacity = None
    if capacity is not None:
        return {
            "mode": capacity.get("limit_mode"),
            "limit": capacity.get("effective_limit"),
            "active": capacity.get("active_count"),
        }
    return {
        "mode": device.get("limit_mode"),
        "limit": device.get("limit"),
        "active": None,
    }


def _has_active_credential(db, account_id: int) -> bool:
    row = db._conn.execute(
        "SELECT id FROM mgboost_subscription_credentials "
        "WHERE account_id=? AND status='ACTIVE'", (int(account_id),),
    ).fetchone()
    return row is not None


def _legacy_card(db, legacy_user: dict, legacy_marzban_user: dict | None, *, now: int) -> dict:
    username = legacy_user["marzban_username"]
    info = legacy_marzban_user or {}
    try:
        counts = db.get_active_device_counts([username])
        active = int(counts.get(username, 0))
    except Exception as e:
        logger.warning(f"read model: legacy device count unavailable: {type(e).__name__}")
        active = None
    try:
        limit = int(db.get_device_limit(username))
    except Exception as e:
        logger.warning(f"read model: legacy device limit unavailable: {type(e).__name__}")
        limit = None
    return {
        "cohort": "legacy",
        "account_id": None,
        "status": info.get("status") or "unknown",
        "plan_name": None,
        "plan_code": None,
        "unlimited": bool(info) and info.get("expire") in (None, 0),
        "expiry": info.get("expire"),
        "traffic_used": info.get("used_traffic"),
        "traffic_limit": info.get("data_limit"),
        "wl": None,
        "devices": {"mode": "LIMITED", "limit": limit, "active": active},
        "has_active_credential": False,
        "marzban_username": username,
    }
