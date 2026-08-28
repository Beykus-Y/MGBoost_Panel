"""PH2-01 opaque subscription resolver engine.

Dormant: this module is only ever reached through `src/routes/opaque_sub.py`,
which itself is gated by `OPAQUE_SUBSCRIPTION_ENABLED` (default off) and is
not externally reachable in production regardless (see that module's
docstring). No new account/slot/child logic is invented here -- this is pure
orchestration over the existing PH3-02 `DeviceSlotStore`, PH3-03
`ChildProvisioningStore`, PH3-04 `hwid_gate`, and PH3-08 `parent_sync`
primitives, exactly the reuse the PH2-01/PH4-01 contracts require.

    opaque token -> ACTIVE credential (PH2-01) -> parent account
        -> fresh parent desired state (PH3-08)
        -> HWID compatibility + slot resolution (PH3-04 gate -> PH3-02 claim)
        -> lazy child ensure (PH3-03, expire from the *current* parent state)
        -> child subscription body (new typed child.user.subscription.get)

`resolve_account_device()` below is the shared tail of this pipeline --
everything from "we already know the account_id" onward. PH4-01's legacy
bridge (`src/legacy_bridge_resolver.py`) calls it too, after its own
independent, evidence-based legacy-alias-to-account resolution, so the two
entry points can never drift into two different security postures for the
exact same downstream decision.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from . import hwid_gate
from .child_contract import derive_operation_id
from .entitlement_engine import exact_wl_allowed_for_delivery
from .parent_sync import child_target_for


@dataclass(frozen=True)
class OpaqueResolveResult:
    outcome: str
    child_username: str | None = None
    slot_number: int | None = None
    generation: int | None = None
    body_b64: str | None = None
    headers: dict | None = None


# Outcomes that must all produce the exact same uniform external response --
# the caller (route handler) is responsible for that uniformity; this engine
# only needs to expose a precise reason for logging/testing.
OUTCOME_INVALID_TOKEN = "INVALID_TOKEN"
OUTCOME_PARENT_UNAVAILABLE = "PARENT_UNAVAILABLE"
OUTCOME_DENY_UNSUPPORTED_CLIENT = "DENY_UNSUPPORTED_CLIENT"
OUTCOME_DENY_MISSING_HWID = "DENY_MISSING_HWID"
OUTCOME_DENY_MALFORMED_HWID = "DENY_MALFORMED_HWID"
OUTCOME_DENY_SLOT_LIMIT = "DENY_SLOT_LIMIT"
OUTCOME_DENY_CROSS_ACCOUNT_HWID = "DENY_CROSS_ACCOUNT_HWID"
OUTCOME_PROVISIONING_PENDING = "PROVISIONING_PENDING"
OUTCOME_PROVISIONING_UNAVAILABLE = "PROVISIONING_UNAVAILABLE"
# Terminal provisioning failure (durable outbox ERROR, e.g. the PH5-11
# render-boundary backstop's WL_INBOUND_IN_STANDARD_CHILD poison): never
# reported as PENDING, never retried by callers -- recovery goes through the
# explicit audited repair primitive, not through re-resolution.
OUTCOME_PROVISIONING_FAILED_PERMANENT = "PROVISIONING_FAILED_PERMANENT"
OUTCOME_INTERNAL_ERROR = "INTERNAL_ERROR"
OUTCOME_OK = "OK"


def resolve_account_device(
    db, account_id: int, device_metadata: dict, *, hmac_key, ensure_fn, subscription_fn,
    worker_id: str, now: int,
) -> OpaqueResolveResult:
    """Shared tail: an account_id is already known and trusted (resolved by
    the caller through its own independent authority -- an opaque credential
    for PH2-01, a reviewed legacy alias for PH4-01). Never accepts a
    caller-supplied slot/generation/child -- only `account_id` and raw
    device metadata."""
    try:
        desired = db.parent_sync.refresh_desired_state(account_id, now=now)
    except Exception:
        return OpaqueResolveResult(OUTCOME_INTERNAL_ERROR)
    if desired["desired_status"] in ("EXPIRED", "DISABLED"):
        return OpaqueResolveResult(OUTCOME_PARENT_UNAVAILABLE)

    decision = hwid_gate.evaluate(
        slots=db.device_slots, account_id=account_id,
        client_name=device_metadata.get("client_name"),
        client_version=device_metadata.get("client_version"),
        platform=device_metadata.get("platform"),
        hwid_candidate_present=bool(device_metadata.get("hwid_candidate_present")),
        hwid_candidate_supported=bool(device_metadata.get("hwid_candidate_supported")),
        raw_hwid=device_metadata.get("device_id"),
        hmac_key=hmac_key, now=now,
    )
    if not decision.allowed:
        return OpaqueResolveResult({
            hwid_gate.DECISION_DENY_UNSUPPORTED_CLIENT: OUTCOME_DENY_UNSUPPORTED_CLIENT,
            hwid_gate.DECISION_DENY_MISSING_HWID: OUTCOME_DENY_MISSING_HWID,
            hwid_gate.DECISION_DENY_MALFORMED_HWID: OUTCOME_DENY_MALFORMED_HWID,
            hwid_gate.DECISION_DENY_SLOT_LIMIT: OUTCOME_DENY_SLOT_LIMIT,
            hwid_gate.DECISION_DENY_CROSS_ACCOUNT_HWID: OUTCOME_DENY_CROSS_ACCOUNT_HWID,
            hwid_gate.DECISION_INTERNAL_ERROR: OUTCOME_INTERNAL_ERROR,
        }.get(decision.decision, OUTCOME_INTERNAL_ERROR))

    slot_row = db._conn.execute(
        "SELECT g.id AS slot_generation_id FROM mgboost_device_slot_generations AS g "
        "JOIN mgboost_device_slots AS s ON s.id=g.slot_id "
        "WHERE g.account_id=? AND s.slot_number=? AND g.generation=? AND g.status='ACTIVE'",
        (account_id, decision.slot_number, decision.generation),
    ).fetchone()
    if slot_row is None:
        return OpaqueResolveResult(OUTCOME_INTERNAL_ERROR)
    slot_generation_id = slot_row["slot_generation_id"]

    alias = db._conn.execute(
        "SELECT id, legacy_username FROM mgboost_legacy_account_aliases "
        "WHERE account_id=? AND alias_role='PRIMARY'", (account_id,),
    ).fetchone()
    if alias is None:
        return OpaqueResolveResult(OUTCOME_INTERNAL_ERROR)

    existing_intent = db._conn.execute(
        "SELECT id, child_username, source_contract_hash, uuid_verifier, "
        "desired_state, observed_state FROM mgboost_child_user_intents "
        "WHERE slot_generation_id=?", (slot_generation_id,),
    ).fetchone()

    target_status, target_expire = child_target_for(desired["desired_status"], desired.get("current_expiry"))
    if target_status != "active":
        # Should not happen given the desired-status check above, but never
        # provision a child that would start out disabled.
        return OpaqueResolveResult(OUTCOME_PARENT_UNAVAILABLE)

    if existing_intent is not None and existing_intent["observed_state"] == "ACTIVE":
        child_username = existing_intent["child_username"]
        source_contract_hash = existing_intent["source_contract_hash"]
        uuid_verifier = existing_intent["uuid_verifier"]
    else:
        source = db._conn.execute(
            "SELECT source_contract_hash FROM mgboost_child_user_intents "
            "WHERE account_id=? LIMIT 1", (account_id,),
        ).fetchone()
        # Determine the account's already-approved source contract hash from
        # any prior child of this account. If this is the account's first
        # child ever, the only remaining authority is the account's pinned
        # SYSTEM-OWNED provisioning template (PH5-11 commercial signup): an
        # infrastructure-owned source user whose exact contract was verified
        # and pinned by the durable signup worker -- never a customer legacy
        # user, never a caller-supplied value. Legacy accounts (no template
        # row) keep the exact prior behavior: their genesis child must be
        # provisioned through the existing PH3-03 pipeline first.
        if source is None:
            source = db._conn.execute(
                "SELECT source_contract_hash FROM mgboost_provisioning_templates "
                "WHERE account_id=? AND state='ACTIVE'", (account_id,),
            ).fetchone()
        if source is None:
            return OpaqueResolveResult(OUTCOME_PROVISIONING_UNAVAILABLE)
        # Resolve the account's canonical WL delivery policy BEFORE any
        # remote mutation (P0 hotfix, production incident account #8): the
        # PH5-11 backstop below must only fire for entitlements whose
        # current delivery does NOT grant WL. Computation failure is
        # transient-unavailable here -- nothing durable was mutated and no
        # poison state is created for a local read error.
        try:
            wl_allowed = exact_wl_allowed_for_delivery(db, account_id=account_id, now=now)
        except Exception:
            return OpaqueResolveResult(OUTCOME_PROVISIONING_UNAVAILABLE)
        idem_key = f"account-device-resolver-child-v1:{slot_generation_id}"
        try:
            prepared = db.child_provisioning.prepare_child_ensure(
                account_id=account_id, slot_generation_id=slot_generation_id,
                source_alias_id=alias["id"], source_contract_hash=source["source_contract_hash"],
                expire=target_expire, idempotency_key=idem_key, now=now,
            )
        except Exception:
            return OpaqueResolveResult(OUTCOME_INTERNAL_ERROR)

        if prepared["state"] != "APPLIED":
            claimed = db.child_provisioning.claim(
                prepared["operation_id"], worker_id=worker_id, now=now, lease_seconds=20,
            )
            if claimed is None:
                refreshed = db._conn.execute(
                    "SELECT state FROM mgboost_outbox WHERE operation_id=?",
                    (prepared["operation_id"],),
                ).fetchone()
                if not refreshed or refreshed["state"] != "APPLIED":
                    if refreshed is not None and refreshed["state"] == "ERROR":
                        # Terminal provisioning failure: structurally distinct
                        # from pending/busy -- callers must never report this
                        # as PROVISIONING_PENDING or re-queue it.
                        return OpaqueResolveResult(OUTCOME_PROVISIONING_FAILED_PERMANENT)
                    return OpaqueResolveResult(OUTCOME_PROVISIONING_PENDING)
            else:
                try:
                    created = ensure_fn(claimed["payload"])
                except Exception:
                    db.child_provisioning.retry(
                        prepared["operation_id"], worker_id=worker_id,
                        error_class="PROVISIONING_UNAVAILABLE", now=now,
                    )
                    return OpaqueResolveResult(OUTCOME_PROVISIONING_UNAVAILABLE)
                # PH5-11 fail-safe, scoped by the canonical delivery policy
                # (P0 hotfix): a freshly created child must never carry an
                # exact PH0-05 WL inbound UNLESS this account's current
                # entitlement grants WL access (LEGACY_PAID_COMPAT
                # 'UNLIMITED' and other WL-capable semantics legitimately
                # clone WL inbounds from their source). The pinned
                # template/source contract should already make a WL leak
                # impossible; this is the render-boundary backstop. WL
                # classification is exact allowlist membership only.
                from .wl_topology import WL_INBOUND_TAGS
                child_tags = set((created.get("inbounds") or {}).get("vless") or [])
                if child_tags & set(WL_INBOUND_TAGS) and not wl_allowed:
                    db.child_provisioning.fail_permanent(
                        prepared["operation_id"], worker_id=worker_id,
                        error_class="WL_INBOUND_IN_STANDARD_CHILD", now=now,
                    )
                    return OpaqueResolveResult(OUTCOME_PROVISIONING_FAILED_PERMANENT)
                child_uuid = created.pop("uuid")
                db.child_provisioning.acknowledge(
                    prepared["operation_id"], worker_id=worker_id,
                    outcome=created["outcome"], child_uuid=child_uuid, remote_result=created,
                    now=now,
                )

        final_intent = db._conn.execute(
            "SELECT child_username, source_contract_hash, uuid_verifier, observed_state "
            "FROM mgboost_child_user_intents WHERE id=?", (prepared["child_intent_id"],),
        ).fetchone()
        if final_intent is not None and final_intent["observed_state"] == "ERROR":
            return OpaqueResolveResult(OUTCOME_PROVISIONING_FAILED_PERMANENT)
        if final_intent is None or final_intent["observed_state"] != "ACTIVE":
            return OpaqueResolveResult(OUTCOME_PROVISIONING_PENDING)
        child_username = final_intent["child_username"]
        source_contract_hash = final_intent["source_contract_hash"]
        uuid_verifier = final_intent["uuid_verifier"]

    subscription_request = {
        "operation_id": derive_operation_id(child_username),
        "child_username": child_username,
        "source_contract_hash": source_contract_hash,
        "expire": target_expire,
        "uuid_verifier": uuid_verifier,
    }
    try:
        sub_result = subscription_fn(subscription_request)
    except Exception:
        return OpaqueResolveResult(OUTCOME_PROVISIONING_UNAVAILABLE)

    return OpaqueResolveResult(
        OUTCOME_OK, child_username=child_username, slot_number=decision.slot_number,
        generation=decision.generation, body_b64=sub_result["body_b64"],
        headers=sub_result["headers"],
    )


def resolve_opaque_subscription(
    db, raw_token: str, device_metadata: dict, *, hmac_key, ensure_fn, subscription_fn,
    worker_id: str, now: int | None = None,
) -> OpaqueResolveResult:
    timestamp = int(time.time()) if now is None else int(now)

    credential = db.subscription_credentials.resolve(raw_token, now=timestamp)
    if credential is None:
        return OpaqueResolveResult(OUTCOME_INVALID_TOKEN)

    return resolve_account_device(
        db, credential["account_id"], device_metadata, hmac_key=hmac_key,
        ensure_fn=ensure_fn, subscription_fn=subscription_fn, worker_id=worker_id, now=timestamp,
    )
