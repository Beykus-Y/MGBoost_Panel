"""PH4-04 minimal admin surface for PH2-01 opaque subscription credentials.

Admin-only: requires an authenticated admin session (`require_admin_auth`,
CSRF-checked for the mutating route) AND the server-derived primary-admin
capability (`PrimaryAdminAuthority` -- the same boundary PH3-06/PH4-01
already use for every other privileged write). The raw token is returned
exactly once, in this response, and is never logged, stored, or included in
any list/detail/audit payload afterward -- matching PH2-01's own contract.
"""

from __future__ import annotations

import re
import secrets
import time

from ..admin_authority import PrimaryAdminAuthorizationError
from ..http_utils import error_response, json_response, read_body
from ..security import require_admin_auth
from ..subscription_credential_issuance import issue_or_reissue_credential

_ACCOUNT_ID_RE = re.compile(r"^\d{1,18}$")
_MAX_REASON_LENGTH = 300


def _require_primary_capability(handler, db):
    try:
        capability = db.primary_admin_authority.authorize_session(handler._admin_session)
    except PrimaryAdminAuthorizationError:
        error_response(handler, 403, "Primary admin capability required")
        return None
    return capability.actor_id


def _account_or_404(handler, db, account_id_raw: str):
    if not _ACCOUNT_ID_RE.fullmatch(account_id_raw or ""):
        error_response(handler, 404, "Account not found")
        return None
    account = db.accounts.get_account(int(account_id_raw))
    if account is None:
        error_response(handler, 404, "Account not found")
        return None
    return account


def _credential_status(db, account_id: int) -> dict:
    row = db._conn.execute(
        "SELECT id,generation,status,created_at,activated_at,revoked_at,revoke_reason,"
        "last_used_at FROM mgboost_subscription_credentials "
        "WHERE account_id=? ORDER BY generation DESC LIMIT 1", (account_id,),
    ).fetchone()
    if row is None:
        return {"account_id": account_id, "credential": None}
    return {
        "account_id": account_id,
        "credential": {
            "id": row["id"], "generation": row["generation"], "status": row["status"],
            "created_at": row["created_at"], "activated_at": row["activated_at"],
            "revoked_at": row["revoked_at"], "revoke_reason": row["revoke_reason"],
            "last_used_at": row["last_used_at"],
        },
    }


def handle_subscription_credential_status(handler, account_id):
    if not require_admin_auth(handler):
        return
    db = handler.server.db
    account = _account_or_404(handler, db, account_id)
    if account is None:
        return
    json_response(handler, 200, _credential_status(db, account["id"]))


def handle_subscription_credential_issue(handler, account_id):
    if not require_admin_auth(handler):
        return
    db = handler.server.db
    actor = _require_primary_capability(handler, db)
    if actor is None:
        return
    account = _account_or_404(handler, db, account_id)
    if account is None:
        return

    try:
        payload = read_body(handler)
        import json as _json
        data = _json.loads(payload) if payload else {}
    except Exception:
        error_response(handler, 400, "Invalid request body")
        return
    if not isinstance(data, dict):
        error_response(handler, 400, "Invalid request body")
        return
    reason = str(data.get("reason") or "").strip()
    if not 3 <= len(reason) <= _MAX_REASON_LENGTH:
        error_response(handler, 400, "A bounded reason is required")
        return

    # PH4-04 corrective fix: issuing while a credential is already ACTIVE is
    # a destructive rotation (the old URL stops working immediately) and
    # must never happen from a single accidental click -- same rule the
    # Telegram surface enforces via its two-step confirm/cancel flow. A
    # fresh account with no ACTIVE credential yet is normal initial issuance
    # and stays a single call.
    existing = db._conn.execute(
        "SELECT id FROM mgboost_subscription_credentials WHERE account_id=? AND status='ACTIVE'",
        (account["id"],),
    ).fetchone()
    if existing is not None and data.get("confirm") is not True:
        json_response(handler, 409, {
            "requires_confirmation": True,
            "account_id": account["id"],
            "message": "An ACTIVE credential already exists. Resubmit with confirm: true to rotate it.",
        })
        return

    delivered = {}

    def _deliver(raw_token: str) -> None:
        delivered["raw_token"] = raw_token

    idempotency_key = f"admin-issue-v1:{account['id']}:{secrets.token_urlsafe(24)}"
    try:
        credential = issue_or_reissue_credential(
            db, account_id=account["id"], actor_ref=actor, reason=reason,
            idempotency_key=idempotency_key, deliver_fn=_deliver, now=int(time.time()),
        )
    except Exception:
        error_response(handler, 500, "Issuance failed")
        return

    json_response(handler, 200, {
        "account_id": account["id"],
        "credential": {
            "id": credential["id"], "generation": credential["generation"],
            "status": credential["status"],
        },
        # The ONLY place this value is ever returned. Never logged, never
        # persisted, never included in the status endpoint above.
        "raw_token": delivered["raw_token"],
        "canonical_url": f"https://sub.beykus.fun/{delivered['raw_token']}",
    })
