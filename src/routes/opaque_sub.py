"""PH2-01 public opaque subscription resolver route.

Dormant in production for two independent reasons, either of which alone is
sufficient: `OPAQUE_SUBSCRIPTION_ENABLED` defaults to off (this handler
returns the exact same uniform invalid response regardless of DB state when
it is), and neither `sub.beykus.fun` nor `panel.beykus.fun`'s nginx vhost
proxies a root path to this application at all (only `/sub/`, `/lk/`,
`/assets/`, `/internal/`, `/sub-admin*` are proxied) -- so this code path is
currently unreachable by any real external request no matter what.

Response uniformity mirrors `src/routes/sub.py`'s existing invalid-token
contract exactly: same status, same body, same bounded-timing floor. Unlike
the legacy resolver, an unknown/malformed/revoked/expired opaque token, a
denied HWID, an unavailable parent and a transient provisioning failure all
collapse to the exact same external response -- this route never leaks which
of those applies to an unauthenticated caller.

PH4-04 corrective fix: a normal human browser opening a valid, ACTIVE
opaque URL must see the existing legacy browser landing page instead of the
uniform invalid response -- a browser hit is never treated as a device and
must never claim a slot, provision a child, or otherwise mutate anything.
The browser check happens ONLY after the token itself resolves to an
ACTIVE credential with a non-expired/non-disabled parent -- an
invalid/unknown/revoked/expired token still gets the exact same uniform
invalid response regardless of User-Agent (no token-validity oracle is
introduced for browsers).
"""

import base64
import time

from ..config import DEVICE_SLOT_HMAC_KEY, OPAQUE_SUBSCRIPTION_ENABLED
from ..device_headers import extract_device_metadata
from ..opaque_resolver import OUTCOME_OK, resolve_opaque_subscription
from ..service_marzban import ServiceMarzbanClient
from ..subscription import process_subscription
from .sub import (
    _invalid_subscription_response,
    _observe_grace_activity_fail_open,
    check_subscription_rate_limit,
    is_browser_request,
    send_browser_landing,
)

_client = ServiceMarzbanClient()


def _ensure_fn(payload):
    return _client.ensure_child_user(payload)


def _subscription_fn(payload):
    return _client.get_child_subscription(payload)


def _try_browser_landing(handler, db, token) -> bool:
    """Returns True if this request was fully handled as a browser landing
    (either the page, or a fall-through to the uniform invalid response for
    an invalid/expired token/parent) -- caller must return immediately in
    that case. Returns False only for a non-browser request, which must
    proceed to the real resolver unchanged. Never claims a slot, creates a
    child, or mutates any device/migration state -- read-only credential
    and parent-state lookups only (the same reads the real resolver would
    do anyway before it ever claims anything)."""
    if not is_browser_request(handler):
        return False
    started_at = time.monotonic()
    credential = db.subscription_credentials.resolve(token)
    if credential is None:
        _invalid_subscription_response(handler, started_at)
        return True
    try:
        desired = db.parent_sync.refresh_desired_state(credential["account_id"])
    except Exception:
        _invalid_subscription_response(handler, started_at)
        return True
    if desired["desired_status"] in ("EXPIRED", "DISABLED"):
        _invalid_subscription_response(handler, started_at)
        return True
    proto = handler.headers.get("X-Forwarded-Proto", "https")
    host = handler.headers.get("Host", "")
    sub_url = f"{proto}://{host}/{token}"
    send_browser_landing(handler, sub_url)
    return True


def handle_opaque_sub(handler, token):
    started_at = time.monotonic()
    if check_subscription_rate_limit(handler):
        return
    if not OPAQUE_SUBSCRIPTION_ENABLED:
        _invalid_subscription_response(handler, started_at)
        return

    db = handler.server.db

    if _try_browser_landing(handler, db, token):
        return

    device_metadata = extract_device_metadata(handler.headers)

    result = resolve_opaque_subscription(
        db, token, device_metadata, hmac_key=DEVICE_SLOT_HMAC_KEY,
        ensure_fn=_ensure_fn, subscription_fn=_subscription_fn,
        worker_id="opaque-resolver-inline-worker",
    )

    if result.outcome != OUTCOME_OK:
        _invalid_subscription_response(handler, started_at)
        return

    try:
        credential = db.subscription_credentials.resolve(token)
    except Exception:
        credential = None
    if credential is not None:
        _observe_grace_activity_fail_open(db, credential.get("account_id"), "OPAQUE")

    body = base64.b64decode(result.body_b64)
    new_body, out_headers = process_subscription(body, result.headers, token, result.child_username, db)

    handler.send_response(200)
    handler.send_header("Content-Type", "text/plain; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Referrer-Policy", "no-referrer")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.send_header("X-Frame-Options", "DENY")
    for key, val in out_headers.items():
        handler.send_header(key, val)
    handler.send_header("Content-Length", str(len(new_body)))
    handler.end_headers()
    handler.wfile.write(new_body)
