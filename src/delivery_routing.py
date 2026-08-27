"""PH5-12 operational delivery routing: plan -> delivery profile -> host membership.

This store is the ONLY writer of STANDARD host membership. Its safety policy
is backend-authoritative and fail-closed:

  * WL classification is EXACT ``PH0-05`` allowlist membership
    (``wl_topology.WL_INBOUND_TAGS``) -- never a substring/fuzzy ``wl`` test;
  * adding an exact-WL inbound tag to the STANDARD profile is structurally
    impossible (rejected before any write);
  * a tag that is not on the exact allowlist but merely ``wl``-shaped is
    treated as unverified/suspicious drift and is likewise rejected -- a
    stale or unknown WL-like row is never auto-classified as usable STANDARD
    (or WL) just by its name;
  * a membership addition requires the tag to exist in a FRESH live topology
    observation whose assertion passed ``require_topology_ok()`` -- the
    caller (admin route) supplies that observed set; the store never trusts
    a caller-named tag it cannot see in live state;
  * every accepted mutation is one optimistic-CAS bump of the profile's
    ``row_version`` plus one append-only audit event in the same
    transaction; a stale concurrent update is rejected (the caller maps that
    to 409), never silently overwritten;
  * the same idempotency key replays its original event (``already_applied``).

Membership changes take effect for provisioning (templates/children created
afterwards) and require no repurchase of any plan version.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time

from .admin_authority import PrimaryAdminAuthorizationError
from .wl_topology import WL_INBOUND_TAGS


STANDARD_PROFILE_CODE = "STANDARD"

# The first-rollout sellable plans and their single delivery profile. Future
# WL plans may map to additional profiles; purchasing those plans is out of
# scope and independently gated by the purchase layer.
PLAN_DELIVERY_DEFAULTS = (
    ("BASIC", STANDARD_PROFILE_CODE),
    ("BASIC_PLUS", STANDARD_PROFILE_CODE),
    ("BASIC_PRO", STANDARD_PROFILE_CODE),
)

ACTOR_SYSTEM = "SYSTEM"


class DeliveryRoutingError(ValueError):
    pass


class DeliveryRoutingConflict(DeliveryRoutingError):
    pass


class WLHostRejected(DeliveryRoutingError):
    """Exact PH0-05 WL inbound -- must never enter the STANDARD profile."""


class WLLikeHostRejected(DeliveryRoutingError):
    """wl-shaped but not on the exact allowlist -- unverified drift, fail closed."""


class UnknownHostRejected(DeliveryRoutingError):
    """Not present in the fresh live topology observation."""


def _idempotency_hash(idempotency_key: str) -> str:
    key = idempotency_key if isinstance(idempotency_key, str) else ""
    if not 16 <= len(key) <= 512:
        raise DeliveryRoutingError("idempotency_key must be a string of 16..512 characters")
    return hashlib.sha256(("ph5-12-delivery-routing-v1\0" + key).encode("utf-8")).hexdigest()


def _clean_reason(reason) -> str:
    text = (reason or "").strip()
    if not 3 <= len(text) <= 300:
        raise DeliveryRoutingError("a bounded human-readable reason (3..300) is required")
    return text


def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def classify_inbound_tag(tag: str) -> str:
    """Exact PH0-05 classification of one live inbound tag.

    ``WL_EXACT``   -- literal allowlist member (must never reach STANDARD);
    ``WL_SUSPECT`` -- wl-shaped but not allowlisted (unverified drift);
    ``STANDARD``   -- everything else (still only addable from live state).
    """
    if tag in WL_INBOUND_TAGS:
        return "WL_EXACT"
    if isinstance(tag, str) and tag.lower().startswith("wl"):
        return "WL_SUSPECT"
    return "STANDARD"


class DeliveryRoutingStore:
    def __init__(self, connection: sqlite3.Connection, lock, primary_admin_authority):
        self._conn = connection
        self._lock = lock
        self._authority = primary_admin_authority

    # --- reads ---------------------------------------------------------------

    def profile_by_code(self, profile_code: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM mgboost_delivery_profiles WHERE profile_code=?",
                (profile_code,),
            ).fetchone()
        return dict(row) if row else None

    def membership(self, profile_code: str) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT h.inbound_tag FROM mgboost_delivery_profile_hosts h "
                "JOIN mgboost_delivery_profiles p ON p.id=h.profile_id "
                "WHERE p.profile_code=? ORDER BY h.inbound_tag",
                (profile_code,),
            ).fetchall()
        return [row["inbound_tag"] for row in rows]

    def profile_for_plan(self, plan_code: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT p.* FROM mgboost_delivery_profiles p "
                "JOIN mgboost_plan_delivery_profiles m ON m.profile_id=p.id "
                "WHERE m.plan_code=?",
                (plan_code,),
            ).fetchone()
        return dict(row) if row else None

    def plan_delivery_map(self) -> dict[str, str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT m.plan_code, p.profile_code FROM mgboost_plan_delivery_profiles m "
                "JOIN mgboost_delivery_profiles p ON p.id=m.profile_id ORDER BY m.plan_code"
            ).fetchall()
        return {row["plan_code"]: row["profile_code"] for row in rows}

    def recent_events(self, limit: int = 20) -> list[dict]:
        limit = max(1, min(int(limit), 100))
        with self._lock:
            rows = self._conn.execute(
                "SELECT event_type,profile_code,inbound_tag,actor_type,actor_ref,reason,"
                "before_json,after_json,created_at FROM mgboost_delivery_profile_events "
                "ORDER BY id DESC LIMIT ?", (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    # --- mutations -----------------------------------------------------------

    def _profile_locked(self, profile_code: str) -> dict:
        row = self._conn.execute(
            "SELECT * FROM mgboost_delivery_profiles WHERE profile_code=?", (profile_code,)
        ).fetchone()
        if row is None:
            raise DeliveryRoutingError(f"unknown delivery profile {profile_code!r}")
        return dict(row)

    def _replay(self, idem_hash: str) -> dict | None:
        row = self._conn.execute(
            "SELECT event_type,profile_code,inbound_tag,after_json FROM mgboost_delivery_profile_events "
            "WHERE idempotency_key_hash=?", (idem_hash,),
        ).fetchone()
        if row is None:
            return None
        try:
            after = json.loads(row["after_json"])
        except (TypeError, ValueError):
            after = {}
        return {
            "event_type": row["event_type"], "profile_code": row["profile_code"],
            "inbound_tag": row["inbound_tag"], **(after if isinstance(after, dict) else {}),
            "already_applied": True,
        }

    def apply_host_change(
        self, capability, *, profile_code: str, inbound_tag: str, operation: str,
        reason: str, idempotency_key: str, observed_live_tags, now: int | None = None,
        system_actor: bool = False,
    ) -> dict:
        """Add or remove one host from a delivery profile.

        ``observed_live_tags`` must be the tag set of a FRESH live topology
        observation that already passed ``require_topology_ok()`` (the admin
        route's job). Additions are only accepted for tags present there.

        ``system_actor=True`` is the audited SYSTEM path used exclusively by
        the offline seed script (no HTTP caller can set it); every other
        caller must present the sealed primary-admin capability.
        """
        timestamp = int(time.time()) if now is None else int(now)
        if operation not in ("ADD", "REMOVE"):
            raise DeliveryRoutingError("operation must be ADD or REMOVE")
        tag = (inbound_tag or "").strip()
        if not tag or len(tag) > 256:
            raise DeliveryRoutingError("inbound_tag is invalid")
        reason = _clean_reason(reason)
        if system_actor:
            actor_ref, actor_type = "system:delivery-routing-seed", ACTOR_SYSTEM
        else:
            try:
                actor_ref = self._authority.require(capability)
            except PrimaryAdminAuthorizationError:
                raise DeliveryRoutingError("primary MGBoost admin capability required")
            actor_type = "PRIMARY_ADMIN"
        idem_hash = _idempotency_hash(idempotency_key)

        replay = self._replay(idem_hash)
        if replay is not None:
            return replay

        # Exact-classification guard, applied BEFORE any write. This is the
        # backend authority the UI checkbox only mirrors.
        if operation == "ADD":
            classification = classify_inbound_tag(tag)
            if classification == "WL_EXACT":
                raise WLHostRejected(
                    "this inbound is an exact PH0-05 WL host and can never be "
                    "added to the STANDARD delivery profile"
                )
            if classification == "WL_SUSPECT":
                raise WLLikeHostRejected(
                    "this tag is wl-shaped but absent from the exact PH0-05 "
                    "allowlist -- unverified topology drift is fail-closed"
                )
            if tag not in set(observed_live_tags or ()):
                raise UnknownHostRejected(
                    "host is absent from the fresh live topology observation"
                )

        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                profile = self._profile_locked(profile_code)
                existing = self._conn.execute(
                    "SELECT id FROM mgboost_delivery_profile_hosts "
                    "WHERE profile_id=? AND inbound_tag=?",
                    (profile["id"], tag),
                ).fetchone()
                if operation == "ADD" and existing is not None:
                    raise DeliveryRoutingConflict("host is already a member of this profile")
                if operation == "REMOVE" and existing is None:
                    raise DeliveryRoutingConflict("host is not a member of this profile")

                before = {"members": self._members_locked(profile["id"])}
                if operation == "ADD":
                    self._conn.execute(
                        "INSERT INTO mgboost_delivery_profile_hosts (profile_id,inbound_tag,created_at) "
                        "VALUES (?,?,?)", (profile["id"], tag, timestamp),
                    )
                else:
                    # The schema trigger refuses any delete that has no prior
                    # HOST_REMOVED audit event; the event is written first,
                    # inside this same transaction.
                    self._conn.execute(
                        "INSERT INTO mgboost_delivery_profile_events "
                        "(event_type,profile_code,inbound_tag,actor_type,actor_ref,reason,"
                        "before_json,after_json,idempotency_key_hash,created_at) "
                        "VALUES ('HOST_REMOVED',?,?,?,?,?,?,?,?,?)",
                        (profile_code, tag, actor_type, actor_ref, reason,
                         _canonical(before), _canonical({"removed": tag}),
                         idem_hash, timestamp),
                    )
                    self._conn.execute(
                        "DELETE FROM mgboost_delivery_profile_hosts "
                        "WHERE profile_id=? AND inbound_tag=?", (profile["id"], tag),
                    )
                after_members = self._members_locked(profile["id"])
                updated = self._conn.execute(
                    "UPDATE mgboost_delivery_profiles SET row_version=row_version+1,updated_at=? "
                    "WHERE id=? AND row_version=?",
                    (timestamp, profile["id"], profile["row_version"]),
                )
                if updated.rowcount != 1:
                    raise DeliveryRoutingConflict(
                        "concurrent routing modification detected; reload and retry"
                    )
                if operation == "ADD":
                    self._conn.execute(
                        "INSERT INTO mgboost_delivery_profile_events "
                        "(event_type,profile_code,inbound_tag,actor_type,actor_ref,reason,"
                        "before_json,after_json,idempotency_key_hash,created_at) "
                        "VALUES ('HOST_ADDED',?,?,?,?,?,?,?,?,?)",
                        (profile_code, tag, actor_type, actor_ref, reason,
                         _canonical(before), _canonical({"members": after_members}),
                         idem_hash, timestamp),
                    )
                self._conn.commit()
                return {
                    "event_type": f"HOST_{'ADDED' if operation == 'ADD' else 'REMOVED'}",
                    "profile_code": profile_code, "inbound_tag": tag,
                    "row_version": profile["row_version"] + 1,
                    "members": after_members, "already_applied": False,
                }
            except sqlite3.IntegrityError as exc:
                self._conn.rollback()
                raise DeliveryRoutingConflict(
                    "an identical routing change is already being applied"
                ) from exc
            except Exception:
                self._conn.rollback()
                raise

    def _members_locked(self, profile_id: int) -> list[str]:
        rows = self._conn.execute(
            "SELECT inbound_tag FROM mgboost_delivery_profile_hosts WHERE profile_id=? "
            "ORDER BY inbound_tag", (profile_id,),
        ).fetchall()
        return [row["inbound_tag"] for row in rows]

    # --- idempotent bootstrap (seed script only; audited like any mutation) --

    def ensure_defaults(self, *, now: int | None = None) -> dict:
        """Create the STANDARD profile shell and the first-rollout plan
        mapping if absent. Never touches membership. Safe to call
        repeatedly; no admin capability required because it can never change
        delivery behavior on its own (an empty profile provisions nothing)."""
        timestamp = int(time.time()) if now is None else int(now)
        created_profiles, created_mappings = [], []
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                profile = self._conn.execute(
                    "SELECT * FROM mgboost_delivery_profiles WHERE profile_code=?",
                    (STANDARD_PROFILE_CODE,),
                ).fetchone()
                if profile is None:
                    cursor = self._conn.execute(
                        "INSERT INTO mgboost_delivery_profiles (profile_code,created_at,updated_at) "
                        "VALUES (?,?,?)", (STANDARD_PROFILE_CODE, timestamp, timestamp),
                    )
                    profile_id = cursor.lastrowid
                    self._conn.execute(
                        "INSERT INTO mgboost_delivery_profile_events "
                        "(event_type,profile_code,inbound_tag,actor_type,actor_ref,reason,"
                        "before_json,after_json,created_at) "
                        "VALUES ('PROFILE_SEEDED',?,NULL,?,?,?,?,?,?)",
                        (STANDARD_PROFILE_CODE, ACTOR_SYSTEM, None,
                         "delivery-routing bootstrap",
                         _canonical({}), _canonical({"profile_code": STANDARD_PROFILE_CODE}),
                         timestamp),
                    )
                    created_profiles.append(STANDARD_PROFILE_CODE)
                else:
                    profile_id = profile["id"]
                for plan_code, mapped_profile in PLAN_DELIVERY_DEFAULTS:
                    row = self._conn.execute(
                        "SELECT 1 FROM mgboost_plan_delivery_profiles WHERE plan_code=?",
                        (plan_code,),
                    ).fetchone()
                    if row is None:
                        target = self._conn.execute(
                            "SELECT id FROM mgboost_delivery_profiles WHERE profile_code=?",
                            (mapped_profile,),
                        ).fetchone()
                        if target is None:
                            continue
                        self._conn.execute(
                            "INSERT INTO mgboost_plan_delivery_profiles (plan_code,profile_id,created_at) "
                            "VALUES (?,?,?)", (plan_code, target["id"], timestamp),
                        )
                        created_mappings.append(plan_code)
                self._conn.commit()
                return {
                    "profiles_created": created_profiles,
                    "plan_mappings_created": created_mappings,
                }
            except Exception:
                self._conn.rollback()
                raise
