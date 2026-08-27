"""PH5-12 admin delivery-routing surface: live host inventory + STANDARD
membership management.

Reads show the real live inbounds/nodes (through the same broker identity
every admin surface uses) classified by the exact PH0-05 allowlist, the
current STANDARD membership and its CAS row_version, and the recent audit
events.

Mutations require the full established boundary -- admin session + CSRF
(``require_admin_auth``), the server-derived primary-admin capability, a
mandatory reason and an idempotency key -- and re-derive the live topology
FRESH per mutation: a new PH6-01 assertion is recorded and
``require_topology_ok()`` must pass BEFORE the change, and the tag being
added must exist in that fresh observation. The store itself rejects exact
WL tags and unverified wl-shaped tags outright; the UI checkbox is
presentation only, never the authority. A stale concurrent update loses
loudly (409), never silently overwrites.
"""

from __future__ import annotations

import time

from ..delivery_routing import (
    STANDARD_PROFILE_CODE, DeliveryRoutingConflict, DeliveryRoutingError,
    UnknownHostRejected, WLLikeHostRejected, WLHostRejected, classify_inbound_tag,
)
from ..http_utils import error_response, json_response
from ..security import require_admin_auth
from ..wl_topology_guard import fetch_live_topology_observation

from .admin_support import (
    bounded_int, bounded_str, read_json_body, require_primary_capability,
    service_marzban,
)


def _fresh_observation(db):
    """One fresh read-only topology observation + assertion. Raises
    TopologyMismatchError (mapped by the caller) on any mismatch -- an
    unhealthy topology fail-closes every routing mutation."""
    client = service_marzban()
    observed_tags, observed_nodes = fetch_live_topology_observation(client, None)
    assertion = db.wl_topology_guard.run_assertion(observed_tags, observed_nodes)
    db.wl_topology_guard.require_topology_ok()
    return set(observed_tags), assertion


def handle_routing_hosts(handler):
    if not require_admin_auth(handler):
        return
    db = handler.server.db
    profile = db.delivery_routing.profile_by_code(STANDARD_PROFILE_CODE)
    membership = db.delivery_routing.membership(STANDARD_PROFILE_CODE)
    try:
        observed_tags, assertion = _fresh_observation(db)
    except Exception as exc:
        json_response(handler, 200, {
            "topology": {"ok": False, "error_class": type(exc).__name__},
            "hosts": [], "membership": membership,
            "plan_delivery": db.delivery_routing.plan_delivery_map(),
            "profile_row_version": profile["row_version"] if profile else None,
            "events": db.delivery_routing.recent_events(),
        })
        return
    live = sorted(observed_tags)
    hosts = [{
        "inbound_tag": tag,
        "classification": classify_inbound_tag(tag),
        "in_standard": tag in set(membership),
    } for tag in live]
    json_response(handler, 200, {
        "topology": {"ok": True, "config_version": assertion["config_version"],
                     "checked_at": assertion["checked_at"]},
        "hosts": hosts,
        "membership": membership,
        "plan_delivery": db.delivery_routing.plan_delivery_map(),
        "profile_row_version": profile["row_version"] if profile else None,
        "events": db.delivery_routing.recent_events(),
    })


def _error_status(exc: Exception) -> int:
    if isinstance(exc, DeliveryRoutingConflict):
        return 409
    return 400


def _handle_host_mutation(handler, operation: str):
    if not require_admin_auth(handler):
        return
    db = handler.server.db
    capability = require_primary_capability(handler, db)
    if capability is None:
        return
    data = read_json_body(handler)
    if data is None:
        return
    tag, tag_error = bounded_str(data, "inbound_tag", min_len=1, max_len=256)
    if tag_error:
        error_response(handler, 400, tag_error)
        return
    reason, reason_error = bounded_str(data, "reason", min_len=3, max_len=300)
    if reason_error:
        error_response(handler, 400, reason_error)
        return
    idempotency_key, key_error = bounded_str(data, "idempotency_key", min_len=16, max_len=128)
    if key_error:
        error_response(handler, 400, key_error)
        return
    expected_row_version, version_error = bounded_int(
        data, "expected_row_version", minimum=1, maximum=2**31 - 1,
    )
    if version_error:
        error_response(handler, 400, version_error)
        return
    profile = db.delivery_routing.profile_by_code(STANDARD_PROFILE_CODE)
    if profile is None or int(profile["row_version"]) != int(expected_row_version):
        error_response(handler, 409, "routing state changed; reload and retry")
        return
    try:
        observed_tags, assertion = _fresh_observation(db)
    except Exception as exc:
        error_response(handler, 503, f"topology check failed: {type(exc).__name__}")
        return
    try:
        result = db.delivery_routing.apply_host_change(
            capability, profile_code=STANDARD_PROFILE_CODE,
            inbound_tag=tag, operation=operation, reason=reason,
            idempotency_key=idempotency_key, observed_live_tags=observed_tags,
            now=int(time.time()),
        )
    except (WLHostRejected, WLLikeHostRejected, UnknownHostRejected, DeliveryRoutingError) as exc:
        error_response(handler, _error_status(exc), str(exc))
        return
    json_response(handler, 200, {
        **result,
        "topology_checked_at": assertion["checked_at"],
    })


def handle_routing_host_add(handler):
    _handle_host_mutation(handler, "ADD")


def handle_routing_host_remove(handler):
    _handle_host_mutation(handler, "REMOVE")
