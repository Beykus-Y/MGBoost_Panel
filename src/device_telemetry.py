"""PH8-06 canonical per-generation opaque device telemetry store.

Callers must only ever invoke `record_observation()` AFTER the caller has
already proven, through some other authority, that:

  credential -> account_id (an ACTIVE opaque credential resolved to this
      account, PH2-01 `subscription_credentials.resolve`)
  HWID -> current slot generation (`hwid_gate.evaluate()` returned an
      ALLOW decision and `device_slots.claim()` returned the exact
      `slot_generation_id`/`hwid_verifier` for THIS account)

This module has no notion of credentials, HWID candidates or gate
decisions at all -- it accepts no raw HWID, no opaque token, no
denied/foreign/malformed request. A caller that has not already produced
that proof must never call `record_observation()`.

Client-reported fields (model/platform/client_name/client_version) are
presentation data, never authority -- see `_sanitize()`. An update never
erases a previously known non-empty value with an absent one: only a
freshly reported non-empty value replaces the stored one.
"""

from __future__ import annotations

import re
import sqlite3
import time


_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]+")
_WS_RE = re.compile(r"\s+")

_FIELD_MAX_LEN = {
    "model": 64,
    "platform": 32,
    "client_name": 64,
    "client_version": 32,
}


class DeviceTelemetryError(RuntimeError):
    pass


def _sanitize(value, *, maximum: int) -> str | None:
    if value is None:
        return None
    text = str(value)
    text = _CONTROL_RE.sub(" ", text)
    text = _WS_RE.sub(" ", text).strip()
    if not text:
        return None
    return text[:maximum]


class DeviceTelemetryStore:
    def __init__(self, connection: sqlite3.Connection, lock):
        self._conn = connection
        self._lock = lock

    def record_observation(
        self, *, account_id: int, slot_generation_id: int, hwid_verifier: str,
        model=None, platform=None, client_name=None, client_version=None,
        now: int | None = None,
    ) -> dict:
        if not isinstance(hwid_verifier, str) or len(hwid_verifier) != 76 \
                or not hwid_verifier.startswith("hmac-sha256:"):
            raise DeviceTelemetryError("invalid hwid verifier")
        timestamp = int(time.time()) if now is None else int(now)
        account_id = int(account_id)
        slot_generation_id = int(slot_generation_id)
        model = _sanitize(model, maximum=_FIELD_MAX_LEN["model"])
        platform = _sanitize(platform, maximum=_FIELD_MAX_LEN["platform"])
        client_name = _sanitize(client_name, maximum=_FIELD_MAX_LEN["client_name"])
        client_version = _sanitize(client_version, maximum=_FIELD_MAX_LEN["client_version"])
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                owner = self._conn.execute(
                    "SELECT account_id FROM mgboost_device_slot_generations WHERE id=?",
                    (slot_generation_id,),
                ).fetchone()
                if owner is None or int(owner["account_id"]) != account_id:
                    raise DeviceTelemetryError(
                        "slot_generation_id does not belong to account_id"
                    )
                existing = self._conn.execute(
                    "SELECT * FROM mgboost_device_telemetry WHERE slot_generation_id=?",
                    (slot_generation_id,),
                ).fetchone()
                if existing is None:
                    cursor = self._conn.execute(
                        "INSERT INTO mgboost_device_telemetry "
                        "(account_id,slot_generation_id,hwid_verifier,model,platform,"
                        "client_name,client_version,observation_count,first_seen_at,last_seen_at) "
                        "VALUES (?,?,?,?,?,?,?,1,?,?)",
                        (account_id, slot_generation_id, hwid_verifier, model, platform,
                         client_name, client_version, timestamp, timestamp),
                    )
                    row = self._conn.execute(
                        "SELECT * FROM mgboost_device_telemetry WHERE id=?",
                        (cursor.lastrowid,),
                    ).fetchone()
                    self._conn.commit()
                    return dict(row)
                if existing["hwid_verifier"] != hwid_verifier:
                    # A slot_generation_id is 1:1 with exactly one hwid_verifier
                    # for its whole lifetime (device_slots enforces this); a
                    # mismatch here means the caller passed an inconsistent
                    # pair and must never silently overwrite a different
                    # device's evidence.
                    raise DeviceTelemetryError(
                        "hwid_verifier does not match this generation's existing telemetry"
                    )
                self._conn.execute(
                    "UPDATE mgboost_device_telemetry SET "
                    "model=COALESCE(?,model), platform=COALESCE(?,platform), "
                    "client_name=COALESCE(?,client_name), client_version=COALESCE(?,client_version), "
                    "observation_count=observation_count+1, last_seen_at=? "
                    "WHERE id=?",
                    (model, platform, client_name, client_version, timestamp, existing["id"]),
                )
                row = self._conn.execute(
                    "SELECT * FROM mgboost_device_telemetry WHERE id=?", (existing["id"],),
                ).fetchone()
                self._conn.commit()
                return dict(row)
            except Exception:
                self._conn.rollback()
                raise

    def list_for_account(self, account_id: int) -> list[dict]:
        """Internal-only evidence feed for `device_real_projection.project_real_device`.
        Never returned directly to any HTTP caller."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id,account_id,slot_generation_id,hwid_verifier,model,platform,"
                "client_name,client_version,last_seen_at "
                "FROM mgboost_device_telemetry WHERE account_id=?",
                (int(account_id),),
            ).fetchall()
        return [
            {
                "account_id": row["account_id"],
                "hwid_verifier": row["hwid_verifier"],
                "observed_id": row["id"],
                "model": row["model"],
                "platform": row["platform"],
                "client_name": row["client_name"],
                "client_version": row["client_version"],
                "last_seen_at": row["last_seen_at"],
            }
            for row in rows
        ]
