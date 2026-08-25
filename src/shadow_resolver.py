"""PH3-03 account-aware subscription resolver — dual-run SHADOW mode.

Hard invariant: the legacy resolver in `src/routes/sub.py` always builds the
actual HTTP response by itself, before this module is ever invoked. Nothing
in this module may block, slow down or replace that response. Every stage
here is wrapped so that any exception — broker outage, Marzban outage,
contract drift, a bug in this file itself — degrades to "no shadow metric
recorded" and never surfaces to the caller.

Raw child credentials are read only through the narrow
`child.user.credentials.get` broker operation, authenticated with a
dedicated resolver-only shared key that the main `mgboost-main` broker
client does not hold (see `broker_main.py`). The raw UUID lives in a local
variable for the duration of one comparison and is never logged, stored, or
included in a metric row. Only PASS/FAIL, a bounded mismatch category,
resolver latency and success/failure flags for the credential reread and the
(always-true, by construction) legacy fallback are persisted.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl

from .child_contract import (
    validate_child_credentials_request,
    validate_child_ensure_request,
)
from .service_marzban import BrokerTransport

logger = logging.getLogger(__name__)

# Metric categories. "MATCH" is the only PASS category; everything else is a
# bounded FAIL classification. These names are intentionally the same ones
# used in ROADMAP.md's PH3-03 failure matrix.
CATEGORY_MATCH = "MATCH"
CATEGORY_BROKER_UNAVAILABLE = "BROKER_UNAVAILABLE"
CATEGORY_MARZBAN_UNAVAILABLE = "MARZBAN_UNAVAILABLE"
CATEGORY_RESOLVER_CAPABILITY_DENIED = "RESOLVER_CAPABILITY_DENIED"
CATEGORY_CREDENTIAL_VERIFIER_MISMATCH = "CREDENTIAL_VERIFIER_MISMATCH"
CATEGORY_REMOTE_CHILD_MISSING = "REMOTE_CHILD_MISSING"
CATEGORY_REMOTE_CONTRACT_MISMATCH = "REMOTE_CONTRACT_MISMATCH"
CATEGORY_STALE_SLOT_GENERATION = "STALE_SLOT_GENERATION"
CATEGORY_INVALID_ACCOUNT_SLOT_MAPPING = "INVALID_ACCOUNT_SLOT_MAPPING"
CATEGORY_RESOLVER_TIMEOUT = "RESOLVER_TIMEOUT"
CATEGORY_SHADOW_COMPARISON_FAILURE = "SHADOW_COMPARISON_FAILURE"
CATEGORY_MALFORMED_REQUEST = "MALFORMED_REQUEST"
CATEGORY_RESOLVER_INTERNAL_ERROR = "RESOLVER_INTERNAL_ERROR"

_DB_TIMEOUT_SECONDS = 0.2
_VLESS_PREFIX = "vless://"


class ShadowBindingError(RuntimeError):
    pass


class ShadowResolverBindingStore:
    """Creates/pauses the one row per legacy device that opts a request into
    PH3-03 SHADOW comparison. Deliberately independent of any HTTP route —
    only an explicit administrative/canary tool imports this."""

    def __init__(self, connection: sqlite3.Connection, lock):
        self._conn = connection
        self._lock = lock

    def get_binding_by_device(self, legacy_device_id: int) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM mgboost_shadow_resolver_bindings WHERE legacy_device_id=?",
            (int(legacy_device_id),),
        ).fetchone()
        return dict(row) if row else None

    def create_binding(
        self, *, account_id: int, legacy_alias_id: int, legacy_device_id: int,
        slot_generation_id: int, child_intent_id: int, operation_id: str,
        decision_ref: str, enabled: bool = True, now: int | None = None,
    ) -> dict:
        timestamp = int(time.time()) if now is None else int(now)
        if not isinstance(decision_ref, str) or not 3 <= len(decision_ref) <= 128:
            raise ShadowBindingError("invalid decision reference")
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                alias = self._conn.execute(
                    "SELECT id FROM mgboost_legacy_account_aliases WHERE id=? AND account_id=?",
                    (int(legacy_alias_id), int(account_id)),
                ).fetchone()
                intent = self._conn.execute(
                    "SELECT id FROM mgboost_child_user_intents "
                    "WHERE id=? AND account_id=? AND slot_generation_id=?",
                    (int(child_intent_id), int(account_id), int(slot_generation_id)),
                ).fetchone()
                outbox = self._conn.execute(
                    "SELECT state FROM mgboost_outbox "
                    "WHERE operation_id=? AND account_id=? AND child_intent_id=?",
                    (operation_id, int(account_id), int(child_intent_id)),
                ).fetchone()
                device = self._conn.execute(
                    "SELECT id FROM user_devices WHERE id=?", (int(legacy_device_id),),
                ).fetchone()
                if not alias or not intent or not outbox or not device:
                    raise ShadowBindingError(
                        "shadow binding references an unknown or cross-account row"
                    )
                if outbox["state"] != "APPLIED":
                    raise ShadowBindingError(
                        "child operation must already be APPLIED before a shadow binding"
                    )
                self._conn.execute(
                    "INSERT INTO mgboost_shadow_resolver_bindings("
                    "account_id,legacy_alias_id,legacy_device_id,slot_generation_id,"
                    "child_intent_id,operation_id,mode,enabled,decision_ref,created_at) "
                    "VALUES (?,?,?,?,?,?,'SHADOW',?,?,?)",
                    (
                        int(account_id), int(legacy_alias_id), int(legacy_device_id),
                        int(slot_generation_id), int(child_intent_id), operation_id,
                        1 if enabled else 0, decision_ref, timestamp,
                    ),
                )
                row = self._conn.execute(
                    "SELECT * FROM mgboost_shadow_resolver_bindings WHERE legacy_device_id=?",
                    (int(legacy_device_id),),
                ).fetchone()
                self._conn.commit()
                return dict(row)
            except sqlite3.IntegrityError as exc:
                self._conn.rollback()
                raise ShadowBindingError(str(exc)) from exc
            except Exception:
                self._conn.rollback()
                raise

    def set_enabled(self, binding_id: int, enabled: bool, *, now: int | None = None) -> None:
        """Administrative pause/resume. Binding identity stays immutable —
        only this flag may change, enforced by
        `trg_shadow_binding_identity_immutable` at the schema level."""
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                updated = self._conn.execute(
                    "UPDATE mgboost_shadow_resolver_bindings SET enabled=? WHERE id=?",
                    (1 if enabled else 0, int(binding_id)),
                ).rowcount
                if not updated:
                    raise ShadowBindingError("unknown shadow binding id")
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise


class ShadowOutcome:
    __slots__ = ("result", "category", "credential_result")

    def __init__(self, result: str, category: str, credential_result: str):
        self.result = result
        self.category = category
        self.credential_result = credential_result

    @staticmethod
    def fail(category: str, credential_result: str = "NOT_ATTEMPTED") -> "ShadowOutcome":
        return ShadowOutcome("FAIL", category, credential_result)

    @staticmethod
    def ok() -> "ShadowOutcome":
        return ShadowOutcome("PASS", CATEGORY_MATCH, "SUCCESS")


def _resolver_config() -> dict | None:
    """Read two separate broker identities directly from the environment,
    mirroring the existing MARZBAN_BROKER_* convention used by
    `service_marzban.BrokerTransport`. Returns None when disabled/unset so
    every caller fails closed on shadow work without touching legacy code.

    `child.user.observe` never returns a raw credential, so it uses the same
    `mgboost-main` identity the rest of MGBoost already holds. Only
    `child.user.credentials.get` uses the separate resolver-only identity
    that `broker_main.py` grants exclusively to it (`mgboost-main` loses that
    one capability). This mirrors the exact split Codex already wired into
    `broker_main.py`/`src/broker_server.py`.
    """
    if os.getenv("SHADOW_RESOLVER_ENABLED", "0").strip().lower() not in {
        "1", "true", "yes", "on",
    }:
        return None
    observe_key = os.getenv("MARZBAN_BROKER_AUTH_KEY", "")
    credentials_key = os.getenv("MARZBAN_BROKER_RESOLVER_AUTH_KEY", "")
    if not observe_key or not credentials_key:
        return None
    return {
        "base_url": os.getenv("MARZBAN_BROKER_URL", "http://127.0.0.1:8002"),
        "observe_shared_key": observe_key,
        "observe_client_id": os.getenv("MARZBAN_BROKER_CLIENT_ID", "mgboost-main"),
        "credentials_shared_key": credentials_key,
        "credentials_client_id": os.getenv(
            "MARZBAN_BROKER_RESOLVER_CLIENT_ID", "mgboost-sub-resolver"
        ),
        "timeout": float(os.getenv("MARZBAN_BROKER_RESOLVER_TIMEOUT_SECONDS", "5")),
    }


def _open_ro(db_path: str) -> sqlite3.Connection:
    connection = sqlite3.connect(db_path, timeout=_DB_TIMEOUT_SECONDS, isolation_level=None)
    connection.execute("PRAGMA busy_timeout=%d" % int(_DB_TIMEOUT_SECONDS * 1000))
    connection.row_factory = sqlite3.Row
    return connection


def _find_binding(connection: sqlite3.Connection, username: str, request_key: str):
    device = connection.execute(
        "SELECT id FROM user_devices WHERE username=? AND request_key=?",
        (username, request_key),
    ).fetchone()
    if device is None:
        return None
    return connection.execute(
        "SELECT id, account_id, legacy_alias_id, legacy_device_id, slot_generation_id, "
        "child_intent_id, operation_id, enabled "
        "FROM mgboost_shadow_resolver_bindings WHERE legacy_device_id=?",
        (device["id"],),
    ).fetchone()


def _load_mapping(connection: sqlite3.Connection, binding) -> dict:
    """Join everything the resolver needs and classify structural drift.

    Raises ValueError(category) on any integrity/staleness problem so the
    caller can record a single bounded FAIL category without a broker call.
    """
    intent = connection.execute(
        "SELECT child_username, source_contract_hash, uuid_verifier "
        "FROM mgboost_child_user_intents WHERE id=? AND account_id=?",
        (binding["child_intent_id"], binding["account_id"]),
    ).fetchone()
    alias = connection.execute(
        "SELECT legacy_username FROM mgboost_legacy_account_aliases WHERE id=? AND account_id=?",
        (binding["legacy_alias_id"], binding["account_id"]),
    ).fetchone()
    outbox = connection.execute(
        "SELECT state, payload_json FROM mgboost_outbox WHERE operation_id=? AND account_id=?",
        (binding["operation_id"], binding["account_id"]),
    ).fetchone()
    if not intent or not alias or not outbox:
        raise ValueError(CATEGORY_INVALID_ACCOUNT_SLOT_MAPPING)

    generation = connection.execute(
        "SELECT id, slot_id, generation, status FROM mgboost_device_slot_generations WHERE id=?",
        (binding["slot_generation_id"],),
    ).fetchone()
    if not generation:
        raise ValueError(CATEGORY_INVALID_ACCOUNT_SLOT_MAPPING)
    slot = connection.execute(
        "SELECT current_generation, desired_state FROM mgboost_device_slots WHERE id=?",
        (generation["slot_id"],),
    ).fetchone()
    if not slot:
        raise ValueError(CATEGORY_INVALID_ACCOUNT_SLOT_MAPPING)
    if (
        generation["status"] != "ACTIVE"
        or slot["desired_state"] != "ACTIVE"
        or slot["current_generation"] != generation["generation"]
    ):
        raise ValueError(CATEGORY_STALE_SLOT_GENERATION)

    if outbox["state"] != "APPLIED":
        raise ValueError(CATEGORY_REMOTE_CHILD_MISSING)

    try:
        payload = json.loads(outbox["payload_json"])
        ensure_request = validate_child_ensure_request(payload)
    except (TypeError, ValueError, json.JSONDecodeError):
        raise ValueError(CATEGORY_MALFORMED_REQUEST)

    if ensure_request["child_username"] != intent["child_username"]:
        raise ValueError(CATEGORY_INVALID_ACCOUNT_SLOT_MAPPING)
    if ensure_request["source_username"] != alias["legacy_username"]:
        raise ValueError(CATEGORY_INVALID_ACCOUNT_SLOT_MAPPING)
    if not intent["uuid_verifier"]:
        raise ValueError(CATEGORY_REMOTE_CHILD_MISSING)

    return {
        "ensure_request": ensure_request,
        "uuid_verifier": intent["uuid_verifier"],
    }


def _parse_vless_line(line: str):
    if not line.startswith(_VLESS_PREFIX):
        return None
    rest = line[len(_VLESS_PREFIX):]
    remark = None
    if "#" in rest:
        rest, remark = rest.split("#", 1)
    query = ""
    if "?" in rest:
        rest, query = rest.split("?", 1)
    if "@" not in rest:
        return None
    raw_uuid, host_port = rest.split("@", 1)
    if not raw_uuid or not host_port:
        return None
    return {"uuid": raw_uuid.lower(), "host_port": host_port, "query": query, "remark": remark}


def _render_child_line(entry: dict, child_uuid: str) -> str:
    line = f"{_VLESS_PREFIX}{child_uuid}@{entry['host_port']}"
    if entry["query"]:
        line += f"?{entry['query']}"
    if entry["remark"] is not None:
        line += f"#{entry['remark']}"
    return line


def _compare_functional_config(raw_lines: list[str], child_uuid: str) -> None:
    """Raise ValueError on any functional (non-expected) difference.

    Only the UUID and the display remark are allowed to differ — matching
    the documented expected-difference contract (child username/UUID label,
    no retired Shadowsocks metadata). Address/port, transport, TLS/Reality,
    flow and every other query parameter must be byte-identical. This also
    performs a parser/format-level round trip equivalent to what an INCY-style
    client would need to do, satisfying the required client-compatibility
    sanity check without switching the real approved device.
    """
    legacy_entries = [_parse_vless_line(line) for line in raw_lines if line.startswith(_VLESS_PREFIX)]
    if not legacy_entries or any(entry is None for entry in legacy_entries):
        raise ValueError("unparsable legacy VLESS line")

    child_uuid_lower = child_uuid.lower()
    for entry in legacy_entries:
        if entry["uuid"] == child_uuid_lower:
            raise ValueError("child UUID collides with an existing legacy line")
        rendered = _render_child_line(entry, child_uuid_lower)
        child_entry = _parse_vless_line(rendered)
        if child_entry is None:
            raise ValueError("rendered child VLESS line failed to parse")
        if child_entry["uuid"] != child_uuid_lower:
            raise ValueError("rendered child UUID mismatch")
        if child_entry["host_port"] != entry["host_port"]:
            raise ValueError("child host/port diverged from legacy")
        if sorted(parse_qsl(child_entry["query"])) != sorted(parse_qsl(entry["query"])):
            raise ValueError("child transport/TLS/flow parameters diverged from legacy")


def _run_shadow(config: dict, token: str, username: str, mapping: dict, raw_lines: list[str], started_at: float):
    ensure_request = mapping["ensure_request"]
    observe_transport = BrokerTransport(
        config["base_url"], config["observe_shared_key"],
        client_id=config["observe_client_id"], timeout=config["timeout"],
    )
    try:
        observed = observe_transport.call("child.user.observe", ensure_request)
    except HTTPError as exc:
        if exc.code in (401, 403):
            return ShadowOutcome.fail(CATEGORY_RESOLVER_CAPABILITY_DENIED)
        if exc.code in (502, 503):
            return ShadowOutcome.fail(CATEGORY_MARZBAN_UNAVAILABLE)
        if exc.code == 400:
            # Our own request shape is already locally validated, so a 400
            # here means the broker's server-side observe check rejected it.
            return ShadowOutcome.fail(CATEGORY_MALFORMED_REQUEST)
        return ShadowOutcome.fail(CATEGORY_BROKER_UNAVAILABLE)
    except TimeoutError:
        return ShadowOutcome.fail(CATEGORY_RESOLVER_TIMEOUT)
    except URLError as exc:
        if isinstance(exc.reason, TimeoutError) or "timed out" in str(exc.reason).lower():
            return ShadowOutcome.fail(CATEGORY_RESOLVER_TIMEOUT)
        return ShadowOutcome.fail(CATEGORY_BROKER_UNAVAILABLE)
    except (ValueError, TypeError, KeyError, json.JSONDecodeError):
        return ShadowOutcome.fail(CATEGORY_MALFORMED_REQUEST)

    presence = (observed or {}).get("presence")
    if presence == "ABSENT":
        return ShadowOutcome.fail(CATEGORY_REMOTE_CHILD_MISSING)
    if presence == "MISMATCH":
        return ShadowOutcome.fail(CATEGORY_REMOTE_CONTRACT_MISMATCH)
    if presence != "MATCH":
        return ShadowOutcome.fail(CATEGORY_REMOTE_CONTRACT_MISMATCH)

    credentials_request = validate_child_credentials_request({
        "operation_id": ensure_request["operation_id"],
        "child_username": ensure_request["child_username"],
        "source_contract_hash": ensure_request["source_contract_hash"],
        "expire": ensure_request["expire"],
        "uuid_verifier": mapping["uuid_verifier"],
    })
    credentials_transport = BrokerTransport(
        config["base_url"], config["credentials_shared_key"],
        client_id=config["credentials_client_id"], timeout=config["timeout"],
    )
    try:
        credentials = credentials_transport.call("child.user.credentials.get", credentials_request)
    except HTTPError as exc:
        if exc.code in (401, 403):
            return ShadowOutcome.fail(CATEGORY_RESOLVER_CAPABILITY_DENIED, "FAIL")
        if exc.code in (502, 503):
            return ShadowOutcome.fail(CATEGORY_MARZBAN_UNAVAILABLE, "FAIL")
        if exc.code == 400:
            # Our own request shape is already locally validated, so a 400
            # here means the server-side identity/contract/expiry/verifier
            # comparison in `reread_child_credentials` rejected it.
            return ShadowOutcome.fail(CATEGORY_CREDENTIAL_VERIFIER_MISMATCH, "FAIL")
        return ShadowOutcome.fail(CATEGORY_BROKER_UNAVAILABLE, "FAIL")
    except TimeoutError:
        return ShadowOutcome.fail(CATEGORY_RESOLVER_TIMEOUT, "FAIL")
    except URLError as exc:
        if isinstance(exc.reason, TimeoutError) or "timed out" in str(exc.reason).lower():
            return ShadowOutcome.fail(CATEGORY_RESOLVER_TIMEOUT, "FAIL")
        return ShadowOutcome.fail(CATEGORY_BROKER_UNAVAILABLE, "FAIL")
    except (ValueError, TypeError, KeyError, json.JSONDecodeError):
        return ShadowOutcome.fail(CATEGORY_CREDENTIAL_VERIFIER_MISMATCH, "FAIL")

    try:
        child_uuid = credentials["credentials"]["vless_uuid"]
        if not isinstance(child_uuid, str) or not child_uuid:
            raise ValueError("empty child credential")
    except (KeyError, TypeError, ValueError):
        return ShadowOutcome.fail(CATEGORY_CREDENTIAL_VERIFIER_MISMATCH, "FAIL")

    try:
        _compare_functional_config(raw_lines, child_uuid)
    except ValueError:
        return ShadowOutcome.fail(CATEGORY_SHADOW_COMPARISON_FAILURE, "SUCCESS")
    finally:
        # child_uuid must not outlive this stack frame in any log/metric path.
        child_uuid = None  # noqa: F841

    return ShadowOutcome.ok()


def _record_metric(db_path: str, binding_id: int, outcome: ShadowOutcome, latency_ms: int, now: int):
    day = now - (now % 86400)
    try:
        connection = _open_ro(db_path)
    except Exception as exc:
        logger.warning("shadow resolver metrics connect skipped error_type=%s", type(exc).__name__)
        return
    try:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute(
            "SELECT request_count, latency_total_ms, latency_max_ms FROM "
            "mgboost_shadow_resolver_metrics WHERE bucket_day=? AND binding_id=? AND result=? "
            "AND category=? AND credential_result=? AND legacy_fallback_success=1",
            (day, binding_id, outcome.result, outcome.category, outcome.credential_result),
        ).fetchone()
        if existing:
            connection.execute(
                "UPDATE mgboost_shadow_resolver_metrics SET request_count=request_count+1, "
                "latency_total_ms=latency_total_ms+?, latency_max_ms=MAX(latency_max_ms,?), "
                "last_seen_at=? WHERE bucket_day=? AND binding_id=? AND result=? AND category=? "
                "AND credential_result=? AND legacy_fallback_success=1",
                (latency_ms, latency_ms, now, day, binding_id, outcome.result, outcome.category,
                 outcome.credential_result),
            )
        else:
            connection.execute(
                "INSERT INTO mgboost_shadow_resolver_metrics(bucket_day,binding_id,result,category,"
                "credential_result,legacy_fallback_success,request_count,latency_total_ms,"
                "latency_max_ms,first_seen_at,last_seen_at) VALUES (?,?,?,?,?,1,1,?,?,?,?)",
                (day, binding_id, outcome.result, outcome.category, outcome.credential_result,
                 latency_ms, latency_ms, now, now),
            )
        connection.commit()
    except Exception as exc:
        try:
            connection.rollback()
        except Exception:
            pass
        logger.warning("shadow resolver metrics write skipped error_type=%s", type(exc).__name__)
    finally:
        connection.close()


def _resolve_and_record(config: dict, db_path: str, token: str, username: str, request_key: str, raw_body: bytes):
    started_at = time.monotonic()
    try:
        ro = _open_ro(db_path)
    except Exception as exc:
        logger.warning("shadow resolver db connect skipped error_type=%s", type(exc).__name__)
        return
    try:
        binding = _find_binding(ro, username, request_key)
        if binding is None or not binding["enabled"]:
            return
        binding_id = binding["id"]
        try:
            mapping = _load_mapping(ro, binding)
        except ValueError as exc:
            known = {
                CATEGORY_INVALID_ACCOUNT_SLOT_MAPPING, CATEGORY_STALE_SLOT_GENERATION,
                CATEGORY_REMOTE_CHILD_MISSING, CATEGORY_MALFORMED_REQUEST,
            }
            category = str(exc) if str(exc) in known else CATEGORY_INVALID_ACCOUNT_SLOT_MAPPING
            outcome = ShadowOutcome.fail(category)
            _record_metric(db_path, binding_id, outcome, int((time.monotonic() - started_at) * 1000), int(time.time()))
            return
    finally:
        ro.close()

    try:
        import base64
        decoded = base64.b64decode(raw_body).decode("utf-8")
        raw_lines = [line for line in decoded.strip().split("\n") if line.strip()]
    except Exception:
        outcome = ShadowOutcome.fail(CATEGORY_MALFORMED_REQUEST)
        _record_metric(db_path, binding_id, outcome, int((time.monotonic() - started_at) * 1000), int(time.time()))
        return

    try:
        outcome = _run_shadow(config, token, username, mapping, raw_lines, started_at)
    except Exception as exc:
        logger.warning("shadow resolver internal error skipped error_type=%s", type(exc).__name__)
        outcome = ShadowOutcome.fail(CATEGORY_RESOLVER_INTERNAL_ERROR)

    _record_metric(db_path, binding_id, outcome, int((time.monotonic() - started_at) * 1000), int(time.time()))


def schedule_shadow_resolution(token: str, username: str, device_metadata: dict, raw_body: bytes, *, db_path: str | None = None):
    """Fail-open entrypoint called from `src/routes/sub.py` after the legacy
    response body has already been fully constructed. Always returns
    immediately; all shadow work happens on a background daemon thread so it
    can never add latency to, or affect the outcome of, the legacy request.
    """
    try:
        if not username:
            return
        request_key = (device_metadata or {}).get("request_key")
        if not request_key or not request_key.startswith("hwid:"):
            return
        config = _resolver_config()
        if config is None:
            return
        if db_path is None:
            from .database import DB_PATH  # local import breaks the database<->shadow_resolver cycle
            db_path = DB_PATH
        path = db_path
        thread = threading.Thread(
            target=_resolve_and_record,
            args=(config, path, token, username, request_key, raw_body),
            daemon=True,
            name="shadow-resolver",
        )
        thread.start()
    except Exception as exc:
        try:
            logger.warning("shadow resolver schedule skipped error_type=%s", type(exc).__name__)
        except Exception:
            pass
