import hashlib
import json
import logging
import os
import secrets
import sqlite3
import time
import threading

from .config import DATA_DIR
from .sensitive import is_subscription_token_ref, subscription_token_ref

logger = logging.getLogger(__name__)

# --- audit log event types ------------------------------------------------
# Free-text column (see `audit_log` table below) so new event types can be
# added later without a schema change. These constants document the
# taxonomy currently in use plus ones reserved for a future payments phase.
AUDIT_EVENT_TG_BOUND = "tg_bound"                # first bind of a telegram_id to a marzban_username
AUDIT_EVENT_TG_REBOUND = "tg_rebound"            # telegram_id moved from one marzban_username to another
AUDIT_EVENT_DEVICE_RENAMED = "device_renamed"
AUDIT_EVENT_DEVICE_DEACTIVATED = "device_deactivated"
AUDIT_EVENT_MGMT_CODE_ISSUED = "mgmt_code_issued"        # one-time device-management code generated
AUDIT_EVENT_MGMT_SESSION_CREATED = "mgmt_session_created"  # one-time code successfully exchanged

# Phase 2 — Telegram Stars payments (see selara-tg-bridge phase2_stars_design.md).
AUDIT_EVENT_INVOICE_CREATED = "invoice_created"
AUDIT_EVENT_PAYMENT_SUCCESSFUL = "payment_successful"
AUDIT_EVENT_SUBSCRIPTION_EXTENDED = "subscription_extended"
AUDIT_EVENT_REFUND = "refund"
AUDIT_EVENT_PAYMENT_FAILED = "payment_failed"
AUDIT_EVENT_PAYMENT_PLAN_COMMITTED = "payment_plan_committed"
AUDIT_EVENT_PAYMENT_MANUAL_REVIEW = "payment_manual_review"
AUDIT_EVENT_PAYMENT_APPLY_RETRY_EXHAUSTED = "payment_apply_retry_exhausted"
AUDIT_EVENT_PAYMENT_REQUEUED = "payment_requeued"
AUDIT_EVENT_ORPHAN_PAYMENT_CAPTURED = "orphan_payment_captured"
AUDIT_EVENT_REFUND_UNKNOWN = "refund_unknown"

# Stars invoice lifecycle — see §3 of the design doc.
STARS_INVOICE_TTL_SECONDS = 3600  # 1 hour

# Device-management one-time code / session lifetimes.
MGMT_CODE_TTL_SECONDS = 900      # 15 minutes
MGMT_SESSION_TTL_SECONDS = 900   # 15 minutes — does not silently renew
MGMT_SCOPE_DEVICES = "devices:manage"


def _hash_secret(raw: str) -> str:
    """SHA-256 hex digest, used to store one-time codes / session ids the
    same way a password reset token would be stored — never the raw value."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()

DB_PATH = os.path.join(DATA_DIR, "db.sqlite3")
EXTRA_CONFIGS_JSON = os.path.join(DATA_DIR, "extra_configs.json")
PER_USER_CONFIGS_JSON = os.path.join(DATA_DIR, "per_user_configs.json")
NODE_FILTERS_JSON = os.path.join(DATA_DIR, "node_filters.json")
HYSTERIA_STATS_JSON = os.path.join(DATA_DIR, "hysteria_stats.json")


class Database:
    def __init__(self):
        os.makedirs(DATA_DIR, exist_ok=True)
        self._conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._create_tables()

    def _create_tables(self):
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS sub_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                token TEXT NOT NULL,
                username TEXT,
                user_agent TEXT,
                ip TEXT,
                request_key TEXT,
                device_id TEXT,
                device_name TEXT,
                client_name TEXT,
                client_version TEXT,
                platform TEXT,
                os TEXT,
                fingerprint TEXT,
                metadata_json TEXT,
                timestamp INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS extra_configs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                uri TEXT NOT NULL,
                enabled INTEGER DEFAULT 1,
                sort_order INTEGER DEFAULT 0,
                scope TEXT DEFAULT 'global',
                created_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS node_filters (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                filter_all INTEGER DEFAULT 1,
                allowed_configs TEXT DEFAULT '[]'
            );

            CREATE TABLE IF NOT EXISTS hysteria_stats (
                token TEXT PRIMARY KEY,
                upload INTEGER DEFAULT 0,
                download INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS per_user_configs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL,
                name TEXT NOT NULL,
                uri TEXT NOT NULL,
                enabled INTEGER DEFAULT 1,
                sort_order INTEGER DEFAULT 0,
                created_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS user_devices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL,
                token TEXT NOT NULL,
                request_key TEXT NOT NULL,
                device_name TEXT,
                display_name TEXT,
                platform TEXT,
                client_name TEXT,
                client_version TEXT,
                is_active INTEGER DEFAULT 1,
                first_seen INTEGER NOT NULL,
                last_seen INTEGER NOT NULL,
                UNIQUE(username, request_key)
            );

            CREATE TABLE IF NOT EXISTS hwid_lock (
                request_key TEXT PRIMARY KEY,
                username TEXT NOT NULL,
                locked_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS node_settings (
                node_key TEXT PRIMARY KEY,
                node_id INTEGER,
                node_name TEXT,
                node_address TEXT,
                billing_group TEXT DEFAULT '',
                provider TEXT DEFAULT '',
                location TEXT DEFAULT '',
                monthly_cost REAL,
                currency TEXT DEFAULT 'USD',
                traffic_included_gb REAL,
                traffic_price_per_tb REAL,
                importance TEXT DEFAULT 'normal',
                can_remove INTEGER DEFAULT 1,
                note TEXT DEFAULT '',
                updated_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS tg_users (
                telegram_id INTEGER PRIMARY KEY,
                marzban_username TEXT NOT NULL,
                registered_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS tickets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                marzban_username TEXT,
                status TEXT NOT NULL DEFAULT 'open',
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS ticket_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_id INTEGER NOT NULL,
                role TEXT NOT NULL,
                text TEXT NOT NULL,
                ts INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                telegram_id INTEGER,
                marzban_username TEXT,
                target TEXT,
                metadata_json TEXT
            );

            CREATE TABLE IF NOT EXISTS mgmt_codes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code_hash TEXT NOT NULL UNIQUE,
                telegram_id INTEGER NOT NULL,
                marzban_username TEXT NOT NULL,
                scope TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL,
                used_at INTEGER
            );

            CREATE TABLE IF NOT EXISTS mgmt_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_hash TEXT NOT NULL UNIQUE,
                telegram_id INTEGER NOT NULL,
                marzban_username TEXT NOT NULL,
                scope TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS stars_tariffs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                duration_days INTEGER NOT NULL,
                stars_price INTEGER NOT NULL,
                active INTEGER DEFAULT 1,
                sort_order INTEGER DEFAULT 0,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );
            -- IMPORTANT: this table is created EMPTY. No default row is ever
            -- inserted here or anywhere else — see Phase 2 design doc §9.

            CREATE TABLE IF NOT EXISTS stars_invoices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,

                created_by_telegram_id INTEGER NOT NULL,
                payer_telegram_id INTEGER,

                marzban_username TEXT NOT NULL,
                tariff_id INTEGER,
                tariff_name TEXT NOT NULL,
                duration_days INTEGER NOT NULL,
                stars_price INTEGER NOT NULL,

                status TEXT NOT NULL DEFAULT 'created',

                telegram_payment_charge_id TEXT UNIQUE,
                provider_payment_charge_id TEXT,
                total_amount INTEGER,
                payment_currency TEXT,

                base_expire_observed INTEGER,
                target_expire INTEGER,

                apply_attempts INTEGER DEFAULT 0,
                last_apply_error TEXT,
                last_apply_attempt_at INTEGER,
                applied_expire INTEGER,

                manual_review_reason TEXT,
                manual_review_at INTEGER,
                resolved_by_admin_at INTEGER,

                expires_at INTEGER NOT NULL,

                created_at INTEGER NOT NULL,
                paid_at INTEGER,
                plan_committed_at INTEGER,
                applied_at INTEGER,
                refunded_at INTEGER,

                refund_previous_status TEXT,
                refund_requested_at INTEGER,
                refund_unknown_at INTEGER,
                refund_last_error TEXT,
                refund_reconciled_at INTEGER
            );

            CREATE TABLE IF NOT EXISTS stars_orphan_payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_payment_charge_id TEXT NOT NULL UNIQUE,
                provider_payment_charge_id TEXT,
                payer_telegram_id INTEGER NOT NULL,
                currency TEXT NOT NULL,
                total_amount INTEGER NOT NULL,
                invoice_payload TEXT NOT NULL,
                reason TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'manual_review',
                received_at INTEGER NOT NULL,
                resolved_at INTEGER,
                refund_previous_status TEXT,
                refund_requested_at INTEGER,
                refund_unknown_at INTEGER,
                refund_last_error TEXT,
                refunded_at INTEGER,
                refund_reconciled_at INTEGER
            );

            CREATE TABLE IF NOT EXISTS internal_hmac_nonces (
                nonce_hash TEXT PRIMARY KEY,
                request_hash TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS internal_idempotency (
                key_hash TEXT PRIMARY KEY,
                request_hash TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('pending', 'completed')),
                response_status INTEGER,
                response_hash TEXT,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                expires_at INTEGER
            );

        """)
        self._conn.commit()
        self._ensure_sub_request_columns()
        self._ensure_node_settings_columns()
        self._ensure_stars_invoice_columns()
        self._conn.executescript("""
            CREATE INDEX IF NOT EXISTS idx_sub_requests_token_key
                ON sub_requests(token, request_key);
            CREATE INDEX IF NOT EXISTS idx_sub_requests_username_ts
                ON sub_requests(username, timestamp);
            CREATE INDEX IF NOT EXISTS idx_user_devices_username
                ON user_devices(username, is_active);
            CREATE INDEX IF NOT EXISTS idx_audit_log_telegram_id
                ON audit_log(telegram_id);
            CREATE INDEX IF NOT EXISTS idx_audit_log_username
                ON audit_log(marzban_username);
            CREATE INDEX IF NOT EXISTS idx_audit_log_event_type
                ON audit_log(event_type, timestamp);
            CREATE INDEX IF NOT EXISTS idx_mgmt_codes_expires
                ON mgmt_codes(expires_at);
            CREATE INDEX IF NOT EXISTS idx_mgmt_sessions_expires
                ON mgmt_sessions(expires_at);
            CREATE INDEX IF NOT EXISTS idx_stars_invoices_status
                ON stars_invoices(status);
            CREATE INDEX IF NOT EXISTS idx_stars_invoices_created_by
                ON stars_invoices(created_by_telegram_id);
            CREATE INDEX IF NOT EXISTS idx_stars_invoices_payer
                ON stars_invoices(payer_telegram_id);
            CREATE INDEX IF NOT EXISTS idx_stars_invoices_username
                ON stars_invoices(marzban_username);
            CREATE INDEX IF NOT EXISTS idx_stars_invoices_charge_id
                ON stars_invoices(telegram_payment_charge_id);
            CREATE INDEX IF NOT EXISTS idx_stars_orphan_payments_status
                ON stars_orphan_payments(status);
            CREATE INDEX IF NOT EXISTS idx_stars_orphan_payments_payer
                ON stars_orphan_payments(payer_telegram_id);
            CREATE INDEX IF NOT EXISTS idx_internal_hmac_nonces_expires
                ON internal_hmac_nonces(expires_at);
            CREATE INDEX IF NOT EXISTS idx_internal_idempotency_expires
                ON internal_idempotency(state, expires_at);
        """)
        self._conn.commit()

    @staticmethod
    def _internal_key_hash(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def consume_internal_nonce(
        self, nonce: str, request_hash: str, *, now: int, ttl_seconds: int,
        max_rows: int = 50000,
    ) -> bool:
        """Atomically consume a signed-request nonce across processes.

        Only a SHA-256 reference is stored.  SQLite's write transaction and
        the primary key are the cross-process compare-and-set boundary.
        """
        nonce_hash = self._internal_key_hash(nonce)
        expires_at = now + max(1, int(ttl_seconds))
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                self._conn.execute(
                    "DELETE FROM internal_hmac_nonces WHERE expires_at<=?", (now,)
                )
                existing = self._conn.execute(
                    "SELECT 1 FROM internal_hmac_nonces WHERE nonce_hash=?",
                    (nonce_hash,),
                ).fetchone()
                if existing:
                    self._conn.rollback()
                    return False
                count = self._conn.execute(
                    "SELECT COUNT(*) FROM internal_hmac_nonces"
                ).fetchone()[0]
                if count >= max(1, int(max_rows)):
                    raise RuntimeError("internal nonce store capacity reached")
                self._conn.execute(
                    "INSERT INTO internal_hmac_nonces "
                    "(nonce_hash, request_hash, created_at, expires_at) VALUES (?,?,?,?)",
                    (nonce_hash, request_hash, now, expires_at),
                )
                self._conn.commit()
                return True
            except Exception:
                self._conn.rollback()
                raise

    def begin_internal_idempotency(
        self, key: str, request_hash: str, *, now: int, ttl_seconds: int,
        max_rows: int = 50000,
    ) -> dict:
        """CAS a signed v2 mutation into a durable pending state.

        Pending rows deliberately have no automatic expiry: a process can die
        after the remote effect and before the local acknowledgement.  Such an
        operation must be reconciled, never silently re-executed after a TTL.
        """
        key_hash = self._internal_key_hash(key)
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                self._conn.execute(
                    "DELETE FROM internal_idempotency "
                    "WHERE state='completed' AND expires_at IS NOT NULL AND expires_at<=?",
                    (now,),
                )
                row = self._conn.execute(
                    "SELECT request_hash, state, response_status "
                    "FROM internal_idempotency WHERE key_hash=?",
                    (key_hash,),
                ).fetchone()
                if row:
                    self._conn.rollback()
                    if not secrets.compare_digest(row["request_hash"], request_hash):
                        return {"state": "conflict"}
                    return {
                        "state": row["state"],
                        "response_status": row["response_status"],
                    }
                count = self._conn.execute(
                    "SELECT COUNT(*) FROM internal_idempotency"
                ).fetchone()[0]
                if count >= max(1, int(max_rows)):
                    raise RuntimeError("internal idempotency store capacity reached")
                self._conn.execute(
                    "INSERT INTO internal_idempotency "
                    "(key_hash, request_hash, state, created_at, updated_at, expires_at) "
                    "VALUES (?,?,'pending',?,?,NULL)",
                    (key_hash, request_hash, now, now),
                )
                self._conn.commit()
                return {"state": "new"}
            except Exception:
                self._conn.rollback()
                raise

    def complete_internal_idempotency(
        self, key: str, request_hash: str, *, response_status: int,
        response_hash: str, now: int, ttl_seconds: int,
    ) -> None:
        key_hash = self._internal_key_hash(key)
        expires_at = now + max(1, int(ttl_seconds))
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                cursor = self._conn.execute(
                    "UPDATE internal_idempotency SET state='completed', "
                    "response_status=?, response_hash=?, updated_at=?, expires_at=? "
                    "WHERE key_hash=? AND request_hash=? AND state='pending'",
                    (
                        int(response_status), response_hash, now, expires_at,
                        key_hash, request_hash,
                    ),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError("internal idempotency CAS failed")
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def _ensure_sub_request_columns(self):
        columns = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(sub_requests)").fetchall()
        }
        expected = {
            "request_key": "TEXT",
            "device_id": "TEXT",
            "device_name": "TEXT",
            "client_name": "TEXT",
            "client_version": "TEXT",
            "platform": "TEXT",
            "os": "TEXT",
            "fingerprint": "TEXT",
            "metadata_json": "TEXT",
        }
        with self._lock:
            for name, column_type in expected.items():
                if name not in columns:
                    self._conn.execute(f"ALTER TABLE sub_requests ADD COLUMN {name} {column_type}")
            self._conn.commit()

    def _ensure_node_settings_columns(self):
        columns = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(node_settings)").fetchall()
        }
        expected = {
            "billing_group": "TEXT DEFAULT ''",
            "monitor_quiet_hours": "TEXT DEFAULT '[]'",
        }
        with self._lock:
            for name, column_type in expected.items():
                if name not in columns:
                    self._conn.execute(f"ALTER TABLE node_settings ADD COLUMN {name} {column_type}")
            self._conn.commit()

    def _ensure_stars_invoice_columns(self):
        """Additive migration for installations that already started Phase 2.

        Phase 1 ignores these columns and the orphan table, so this remains
        compatible with an application-code rollback without a DB rollback.
        """
        columns = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(stars_invoices)").fetchall()
        }
        expected = {
            "payment_currency": "TEXT",
            "refund_previous_status": "TEXT",
            "refund_requested_at": "INTEGER",
            "refund_unknown_at": "INTEGER",
            "refund_last_error": "TEXT",
            "refund_reconciled_at": "INTEGER",
        }
        with self._lock:
            for name, column_type in expected.items():
                if name not in columns:
                    self._conn.execute(
                        f"ALTER TABLE stars_invoices ADD COLUMN {name} {column_type}"
                    )
            self._conn.commit()

    def migrate_from_json(self):
        """One-time migration from legacy JSON files into SQLite."""
        self._migrate_extra_configs()
        self._migrate_node_filters()
        self._migrate_per_user_configs()
        self._migrate_hysteria_stats()

    def _migrate_extra_configs(self):
        if not os.path.exists(EXTRA_CONFIGS_JSON):
            return
        with self._lock:
            existing = self._conn.execute("SELECT COUNT(*) FROM extra_configs").fetchone()[0]
            if existing > 0:
                return
        try:
            with open(EXTRA_CONFIGS_JSON) as f:
                configs = json.load(f)
            now = int(time.time())
            with self._lock:
                for i, c in enumerate(configs):
                    self._conn.execute(
                        "INSERT INTO extra_configs (name, uri, enabled, sort_order, scope, created_at) VALUES (?,?,?,?,?,?)",
                        (c.get("name", c["uri"][:30]), c["uri"], 1 if c.get("enabled", True) else 0, i, "global", now),
                    )
                self._conn.commit()
            print(f"[DB] Migrated {len(configs)} extra_configs from JSON")
        except Exception as e:
            print(f"[DB] extra_configs migration failed: {e}")

    def _migrate_node_filters(self):
        if not os.path.exists(NODE_FILTERS_JSON):
            return
        with self._lock:
            existing = self._conn.execute("SELECT COUNT(*) FROM node_filters").fetchone()[0]
            if existing > 0:
                return
        try:
            with open(NODE_FILTERS_JSON) as f:
                filters = json.load(f)
            with self._lock:
                for username, filt in filters.items():
                    if "hosts" in filt or "allowed_ips" in filt:
                        filter_all, allowed = 1, "[]"
                    else:
                        filter_all = 1 if filt.get("all", True) else 0
                        allowed = json.dumps(filt.get("allowed_configs") or [])
                    self._conn.execute(
                        "INSERT OR REPLACE INTO node_filters (username, filter_all, allowed_configs) VALUES (?,?,?)",
                        (username, filter_all, allowed),
                    )
                self._conn.commit()
            print(f"[DB] Migrated {len(filters)} node_filters from JSON")
        except Exception as e:
            print(f"[DB] node_filters migration failed: {e}")

    def _migrate_per_user_configs(self):
        if not os.path.exists(PER_USER_CONFIGS_JSON):
            return
        with self._lock:
            existing = self._conn.execute("SELECT COUNT(*) FROM per_user_configs").fetchone()[0]
            if existing > 0:
                return
        try:
            with open(PER_USER_CONFIGS_JSON) as f:
                data = json.load(f)
            now = int(time.time())
            with self._lock:
                for username, configs in data.items():
                    for i, c in enumerate(configs):
                        self._conn.execute(
                            "INSERT INTO per_user_configs (username, name, uri, enabled, sort_order, created_at) VALUES (?,?,?,?,?,?)",
                            (username, c.get("name", c["uri"][:30]), c["uri"], 1 if c.get("enabled", True) else 0, i, now),
                        )
                self._conn.commit()
            print("[DB] Migrated per_user_configs from JSON")
        except Exception as e:
            print(f"[DB] per_user_configs migration failed: {e}")

    def _migrate_hysteria_stats(self):
        if not os.path.exists(HYSTERIA_STATS_JSON):
            return
        with self._lock:
            existing = self._conn.execute("SELECT COUNT(*) FROM hysteria_stats").fetchone()[0]
            if existing > 0:
                return
        try:
            with open(HYSTERIA_STATS_JSON) as f:
                stats = json.load(f)
            with self._lock:
                for token, entry in stats.items():
                    self._conn.execute(
                        "INSERT OR REPLACE INTO hysteria_stats (token, upload, download) VALUES (?,?,?)",
                        (subscription_token_ref(token), entry.get("upload", 0), entry.get("download", 0)),
                    )
                self._conn.commit()
            print(f"[DB] Migrated {len(stats)} hysteria_stats entries from JSON")
        except Exception as e:
            print(f"[DB] hysteria_stats migration failed: {e}")

    # --- extra_configs ---

    def get_extra_configs(self):
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, name, uri, enabled, sort_order FROM extra_configs WHERE scope='global' ORDER BY sort_order, id"
            ).fetchall()
        return [dict(r) for r in rows]

    def add_extra_config(self, name, uri, enabled=True):
        with self._lock:
            max_order = self._conn.execute("SELECT COALESCE(MAX(sort_order),0) FROM extra_configs").fetchone()[0]
            self._conn.execute(
                "INSERT INTO extra_configs (name, uri, enabled, sort_order, scope, created_at) VALUES (?,?,?,?,?,?)",
                (name, uri, 1 if enabled else 0, max_order + 1, "global", int(time.time())),
            )
            self._conn.commit()

    def delete_extra_config(self, config_id):
        with self._lock:
            self._conn.execute("DELETE FROM extra_configs WHERE id=?", (config_id,))
            self._conn.commit()

    def reorder_extra_configs(self, ordered_ids):
        with self._lock:
            for i, cid in enumerate(ordered_ids):
                self._conn.execute("UPDATE extra_configs SET sort_order=? WHERE id=?", (i, cid))
            self._conn.commit()

    def toggle_extra_config(self, config_id, enabled):
        with self._lock:
            self._conn.execute("UPDATE extra_configs SET enabled=? WHERE id=?", (1 if enabled else 0, config_id))
            self._conn.commit()

    # --- per_user_configs ---

    def get_per_user_configs(self, username=None):
        with self._lock:
            if username:
                rows = self._conn.execute(
                    "SELECT id, username, name, uri, enabled FROM per_user_configs WHERE username=? ORDER BY sort_order, id",
                    (username,),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT id, username, name, uri, enabled FROM per_user_configs ORDER BY username, sort_order, id"
                ).fetchall()
        return [dict(r) for r in rows]

    def get_per_user_configs_map(self):
        with self._lock:
            rows = self.get_per_user_configs()
            result = {}
            for r in rows:
                result.setdefault(r["username"], []).append(r)
        return result

    def save_per_user_configs_map(self, data):
        """Replace all per-user configs with data dict {username: [{name, uri, enabled}]}."""
        with self._lock:
            self._conn.execute("DELETE FROM per_user_configs")
            now = int(time.time())
            for username, configs in data.items():
                for i, c in enumerate(configs):
                    self._conn.execute(
                        "INSERT INTO per_user_configs (username, name, uri, enabled, sort_order, created_at) VALUES (?,?,?,?,?,?)",
                        (username, c.get("name", c["uri"][:30]), c["uri"], 1 if c.get("enabled", True) else 0, i, now),
                    )
            self._conn.commit()

    # --- node_filters ---

    def get_node_filters(self):
        with self._lock:
            rows = self._conn.execute("SELECT username, filter_all, allowed_configs FROM node_filters").fetchall()
        result = {}
        for r in rows:
            result[r["username"]] = {
                "all": bool(r["filter_all"]),
                "allowed_configs": json.loads(r["allowed_configs"] or "[]"),
            }
        return result

    def save_node_filters(self, filters_dict):
        """Replace all node filters with filters_dict {username: {all, allowed_configs}}."""
        with self._lock:
            self._conn.execute("DELETE FROM node_filters")
            for username, filt in filters_dict.items():
                if "hosts" in filt or "allowed_ips" in filt:
                    filter_all, allowed = 1, "[]"
                else:
                    filter_all = 1 if filt.get("all", True) else 0
                    allowed = json.dumps(filt.get("allowed_configs") or [])
                self._conn.execute(
                    "INSERT INTO node_filters (username, filter_all, allowed_configs) VALUES (?,?,?)",
                    (username, filter_all, allowed),
                )
            self._conn.commit()

    def get_node_filter(self, username):
        with self._lock:
            row = self._conn.execute(
                "SELECT filter_all, allowed_configs FROM node_filters WHERE username=?", (username,)
            ).fetchone()
        if not row:
            return None
        return {"all": bool(row["filter_all"]), "allowed_configs": json.loads(row["allowed_configs"] or "[]")}

    # --- hysteria_stats ---

    def get_hysteria_stats(self):
        with self._lock:
            rows = self._conn.execute("SELECT token, upload, download FROM hysteria_stats").fetchall()
        # Never expose a raw legacy bearer through the admin API.  Merge is
        # defensive for the short cutover window where an old raw row and a
        # new verifier row could coexist before the controlled migration.
        result = {}
        for row in rows:
            ref = subscription_token_ref(row["token"])
            item = result.setdefault(ref, {"upload": 0, "download": 0})
            item["upload"] += row["upload"]
            item["download"] += row["download"]
        return result

    def get_hysteria_traffic(self, token):
        token_ref = subscription_token_ref(token)
        with self._lock:
            row = self._conn.execute(
                "SELECT upload, download FROM hysteria_stats WHERE token=?", (token_ref,)
            ).fetchone()
            # Compatibility only until the controlled PH1-06 DB migration.
            if not row and token_ref != token:
                row = self._conn.execute(
                    "SELECT upload, download FROM hysteria_stats WHERE token=?", (token,)
                ).fetchone()
        if not row:
            return 0, 0
        return row["upload"], row["download"]

    def update_hysteria_stats(self, token, upload_delta, download_delta):
        token_ref = subscription_token_ref(token)
        with self._lock:
            existing = self._conn.execute(
                "SELECT upload, download FROM hysteria_stats WHERE token=?", (token_ref,)
            ).fetchone()
            storage_key = token_ref
            if not existing and token_ref != token:
                legacy = self._conn.execute(
                    "SELECT upload, download FROM hysteria_stats WHERE token=?", (token,)
                ).fetchone()
                if legacy:
                    # Updating an existing legacy row does not create new raw
                    # evidence and preserves counters until offline migration.
                    existing = legacy
                    storage_key = token
            if existing:
                self._conn.execute(
                    "UPDATE hysteria_stats SET upload=upload+?, download=download+? WHERE token=?",
                    (upload_delta, download_delta, storage_key),
                )
            else:
                self._conn.execute(
                    "INSERT INTO hysteria_stats (token, upload, download) VALUES (?,?,?)",
                    (token_ref, upload_delta, download_delta),
                )
            self._conn.commit()

    def migrate_legacy_subscription_token_storage(self) -> dict:
        """Replace raw local token keys with SHA-256 references atomically.

        This does not rotate or revoke a Marzban token.  It is intentionally
        an explicit operational step, run only after the PH1-06 encrypted
        quarantine/restore gate; startup never performs the cleanup silently.
        """
        counts = {"sub_requests": 0, "user_devices": 0, "hysteria_stats": 0}
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                for table in ("sub_requests", "user_devices"):
                    rows = self._conn.execute(f"SELECT id, token FROM {table}").fetchall()
                    for row in rows:
                        if is_subscription_token_ref(row["token"]):
                            continue
                        self._conn.execute(
                            f"UPDATE {table} SET token=? WHERE id=?",
                            (subscription_token_ref(row["token"]), row["id"]),
                        )
                        counts[table] += 1

                rows = self._conn.execute(
                    "SELECT token, upload, download FROM hysteria_stats"
                ).fetchall()
                for row in rows:
                    raw = row["token"]
                    if is_subscription_token_ref(raw):
                        continue
                    ref = subscription_token_ref(raw)
                    existing = self._conn.execute(
                        "SELECT upload, download FROM hysteria_stats WHERE token=?", (ref,)
                    ).fetchone()
                    if existing:
                        self._conn.execute(
                            "UPDATE hysteria_stats SET upload=?, download=? WHERE token=?",
                            (
                                existing["upload"] + row["upload"],
                                existing["download"] + row["download"],
                                ref,
                            ),
                        )
                        self._conn.execute("DELETE FROM hysteria_stats WHERE token=?", (raw,))
                    else:
                        self._conn.execute(
                            "UPDATE hysteria_stats SET token=? WHERE token=?", (ref, raw)
                        )
                    counts["hysteria_stats"] += 1
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return counts

    # --- sub_requests ---

    @staticmethod
    def _device_select_columns():
        return (
            "user_agent, ip, timestamp, device_id, device_name, client_name, "
            "client_version, platform, os, fingerprint, metadata_json"
        )

    @staticmethod
    def _device_row_to_dict(row):
        result = dict(row)
        metadata_json = result.pop("metadata_json", None)
        result["metadata"] = {}
        if metadata_json:
            try:
                result["metadata"] = json.loads(metadata_json)
            except (TypeError, ValueError):
                result["metadata"] = {}
        return result

    def log_request(self, token, username, user_agent, ip, device_metadata=None):
        token_ref = subscription_token_ref(token)
        device_metadata = device_metadata or {}
        request_key = device_metadata.get("request_key")
        metadata = device_metadata.get("metadata") or {}
        metadata_json = json.dumps(metadata, ensure_ascii=False, sort_keys=True) if metadata else None

        with self._lock:
            existing = None
            if request_key:
                existing = self._conn.execute(
                    "SELECT id FROM sub_requests WHERE token=? AND request_key=? ORDER BY timestamp DESC LIMIT 1",
                    (token_ref, request_key),
                ).fetchone()
                if not existing and user_agent:
                    existing = self._conn.execute(
                        "SELECT id FROM sub_requests WHERE token=? AND user_agent=? AND request_key IS NULL ORDER BY timestamp DESC LIMIT 1",
                        (token_ref, user_agent),
                    ).fetchone()
            elif user_agent:
                existing = self._conn.execute(
                    "SELECT id FROM sub_requests WHERE token=? AND user_agent=? ORDER BY timestamp DESC LIMIT 1",
                    (token_ref, user_agent),
                ).fetchone()

            payload = (
                int(time.time()),
                ip,
                username,
                user_agent,
                request_key,
                device_metadata.get("device_id"),
                device_metadata.get("device_name"),
                device_metadata.get("client_name"),
                device_metadata.get("client_version"),
                device_metadata.get("platform"),
                device_metadata.get("os"),
                device_metadata.get("fingerprint"),
                metadata_json,
            )

            if existing:
                self._conn.execute(
                    """
                    UPDATE sub_requests
                    SET timestamp=?, ip=?, username=?, user_agent=?, request_key=?,
                        device_id=?, device_name=?, client_name=?, client_version=?,
                        platform=?, os=?, fingerprint=?, metadata_json=?
                    WHERE id=?
                    """,
                    payload + (existing["id"],),
                )
            else:
                self._conn.execute(
                    """
                    INSERT INTO sub_requests (
                        timestamp, ip, username, user_agent, request_key,
                        device_id, device_name, client_name, client_version,
                        platform, os, fingerprint, metadata_json, token
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    payload + (token_ref,),
                )
            self._conn.commit()

    def get_device_history(self, token: str, limit: int = 10) -> list:
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {self._device_select_columns()} FROM sub_requests WHERE token=? ORDER BY timestamp DESC LIMIT ?",
                (subscription_token_ref(token), limit),
            ).fetchall()
        return [self._device_row_to_dict(r) for r in rows]

    def get_device_history_by_username(self, username: str, limit: int = 10) -> list:
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {self._device_select_columns()} FROM sub_requests WHERE username=? ORDER BY timestamp DESC LIMIT ?",
                (username, limit),
            ).fetchall()
        return [self._device_row_to_dict(r) for r in rows]

    def get_last_devices_by_usernames(self, usernames):
        cleaned = [username for username in usernames if username]
        if not cleaned:
            return {}

        placeholders = ",".join("?" for _ in cleaned)
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT username, {self._device_select_columns()}
                FROM sub_requests
                WHERE username IN ({placeholders})
                ORDER BY timestamp DESC
                """,
                cleaned,
            ).fetchall()

        result = {}
        for row in rows:
            username = row["username"]
            if username not in result:
                entry = self._device_row_to_dict(row)
                entry.pop("username", None)
                result[username] = entry
        return result

    # --- settings ---

    def get_setting(self, key: str, default=None):
        with self._lock:
            row = self._conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        if not row:
            return default
        return row["value"]

    def set_setting(self, key: str, value: str):
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?,?)",
                (key, value),
            )
            self._conn.commit()

    # --- device limits ---

    def get_device_limit(self, username: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM settings WHERE key=?", (f"device_limit:{username}",)
            ).fetchone()
            if not row:
                row = self._conn.execute(
                    "SELECT value FROM settings WHERE key='device_limit_default'"
                ).fetchone()
        return int(row["value"]) if row else 3

    def set_device_limit(self, username: str, limit: int):
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?,?)",
                (f"device_limit:{username}", str(limit)),
            )
            self._conn.commit()

    # --- user_devices / hwid_lock ---

    def check_device_access(self, username: str, token: str, device_metadata: dict) -> tuple:
        """
        Returns (blocked: bool, reason: str | None).
        Side effect: registers or refreshes device entry if access is granted.
        Only enforces limits for hwid: keys; fp: keys pass through.
        """
        request_key = device_metadata.get("request_key")
        if not request_key or not request_key.startswith("hwid:"):
            return False, None

        now = int(time.time())
        token_ref = subscription_token_ref(token)

        with self._lock:
            # 1. Global anti-sharing check
            lock_row = self._conn.execute(
                "SELECT username FROM hwid_lock WHERE request_key=?", (request_key,)
            ).fetchone()
            if lock_row and lock_row["username"] != username:
                return True, "device_locked"

            # 2. Is device already registered for this user?
            existing = self._conn.execute(
                "SELECT id, is_active FROM user_devices WHERE username=? AND request_key=?",
                (username, request_key),
            ).fetchone()

            if existing and existing["is_active"]:
                self._conn.execute(
                    "UPDATE user_devices SET last_seen=?, token=?, client_version=? WHERE id=?",
                    (now, token_ref, device_metadata.get("client_version"), existing["id"]),
                )
                self._conn.commit()
                return False, None

            # 3. Count active slots (limit=0 means unlimited)
            limit = self.get_device_limit(username)
            if limit > 0:
                active_count = self._conn.execute(
                    "SELECT COUNT(*) FROM user_devices WHERE username=? AND is_active=1",
                    (username,),
                ).fetchone()[0]
                if active_count >= limit:
                    return True, "device_limit_reached"

            # 4. Register or re-activate
            device_name = device_metadata.get("device_name") or device_metadata.get("platform")
            if existing:
                self._conn.execute(
                    """UPDATE user_devices
                       SET is_active=1, last_seen=?, token=?, client_name=?, client_version=?, device_name=?
                       WHERE id=?""",
                    (now, token_ref, device_metadata.get("client_name"),
                     device_metadata.get("client_version"), device_name, existing["id"]),
                )
            else:
                self._conn.execute(
                    """INSERT INTO user_devices
                       (username, token, request_key, device_name, platform, client_name,
                        client_version, is_active, first_seen, last_seen)
                       VALUES (?,?,?,?,?,?,?,1,?,?)""",
                    (username, token_ref, request_key, device_name,
                     device_metadata.get("platform"), device_metadata.get("client_name"),
                     device_metadata.get("client_version"), now, now),
                )

            # 5. Acquire hwid_lock
            if lock_row:
                self._conn.execute(
                    "UPDATE hwid_lock SET username=?, locked_at=? WHERE request_key=?",
                    (username, now, request_key),
                )
            else:
                self._conn.execute(
                    "INSERT INTO hwid_lock (request_key, username, locked_at) VALUES (?,?,?)",
                    (request_key, username, now),
                )

            self._conn.commit()
            return False, None

    def get_user_devices(self, username: str) -> list:
        with self._lock:
            rows = self._conn.execute(
                """SELECT id, request_key, device_name, display_name, platform, client_name,
                          client_version, is_active, first_seen, last_seen
                   FROM user_devices WHERE username=? ORDER BY last_seen DESC""",
                (username,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_active_device_counts(self, usernames: list) -> dict:
        cleaned = [u for u in usernames if isinstance(u, str) and u]
        if not cleaned:
            return {}
        placeholders = ",".join("?" for _ in cleaned)
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT username, COUNT(*) AS active_count
                FROM user_devices
                WHERE is_active=1 AND username IN ({placeholders})
                GROUP BY username
                """,
                cleaned,
            ).fetchall()
        counts = {u: 0 for u in cleaned}
        counts.update({r["username"]: r["active_count"] for r in rows})
        return counts

    def deactivate_device(self, device_id: int, username: str) -> bool:
        """Deactivate a user device and release its hwid_lock."""
        with self._lock:
            row = self._conn.execute(
                "SELECT request_key FROM user_devices WHERE id=? AND username=?",
                (device_id, username),
            ).fetchone()
            if not row:
                return False
            self._conn.execute(
                "UPDATE user_devices SET is_active=0 WHERE id=? AND username=?",
                (device_id, username),
            )
            self._conn.execute(
                "DELETE FROM hwid_lock WHERE request_key=? AND username=?",
                (row["request_key"], username),
            )
            self._conn.commit()
        self.log_audit_event(
            AUDIT_EVENT_DEVICE_DEACTIVATED, marzban_username=username, target=str(device_id),
        )
        return True

    def rename_device(self, device_id: int, username: str, display_name: str) -> bool:
        with self._lock:
            result = self._conn.execute(
                "UPDATE user_devices SET display_name=? WHERE id=? AND username=?",
                (display_name[:50], device_id, username),
            )
            self._conn.commit()
        ok = result.rowcount > 0
        if ok:
            self.log_audit_event(
                AUDIT_EVENT_DEVICE_RENAMED, marzban_username=username, target=str(device_id),
                metadata={"new_name": display_name[:50]},
            )
        return ok

    def admin_remove_device(self, device_id: int) -> bool:
        """Admin: remove device completely and release its hwid_lock."""
        with self._lock:
            row = self._conn.execute(
                "SELECT request_key, username FROM user_devices WHERE id=?", (device_id,)
            ).fetchone()
            if not row:
                return False
            self._conn.execute("DELETE FROM user_devices WHERE id=?", (device_id,))
            self._conn.execute(
                "DELETE FROM hwid_lock WHERE request_key=? AND username=?",
                (row["request_key"], row["username"]),
            )
            self._conn.commit()
        return True

    # --- monitor quiet hours ---

    def get_node_quiet_hours(self, node_id) -> list:
        node_key = self._node_key(node_id)
        with self._lock:
            row = self._conn.execute(
                "SELECT monitor_quiet_hours FROM node_settings WHERE node_key=?", (node_key,)
            ).fetchone()
        if not row:
            return []
        return json.loads(row["monitor_quiet_hours"] or "[]")

    def set_node_quiet_hours(self, node_id, quiet_hours: list):
        node_key = self._node_key(node_id)
        with self._lock:
            existing = self._conn.execute(
                "SELECT node_key FROM node_settings WHERE node_key=?", (node_key,)
            ).fetchone()
            if existing:
                self._conn.execute(
                    "UPDATE node_settings SET monitor_quiet_hours=? WHERE node_key=?",
                    (json.dumps(quiet_hours), node_key),
                )
            else:
                self._conn.execute(
                    "INSERT INTO node_settings (node_key, monitor_quiet_hours, updated_at) VALUES (?,?,?)",
                    (node_key, json.dumps(quiet_hours), int(time.time())),
                )
            self._conn.commit()

    # --- node settings / economics ---

    @staticmethod
    def _node_key(node_id) -> str:
        if node_id is None or node_id == "":
            return "null"
        return str(node_id)

    @staticmethod
    def _node_settings_row_to_dict(row) -> dict:
        result = dict(row)
        result["can_remove"] = bool(result.get("can_remove"))
        return result

    def get_node_settings(self) -> dict:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT node_key, node_id, node_name, node_address, billing_group, provider, location,
                       monthly_cost, currency, traffic_included_gb, traffic_price_per_tb,
                       importance, can_remove, note, updated_at
                FROM node_settings
                ORDER BY node_name, node_key
                """
            ).fetchall()
        return {row["node_key"]: self._node_settings_row_to_dict(row) for row in rows}

    def get_node_setting(self, node_id):
        node_key = self._node_key(node_id)
        with self._lock:
            row = self._conn.execute(
                """
                SELECT node_key, node_id, node_name, node_address, billing_group, provider, location,
                       monthly_cost, currency, traffic_included_gb, traffic_price_per_tb,
                       importance, can_remove, note, updated_at
                FROM node_settings
                WHERE node_key=?
                """,
                (node_key,),
            ).fetchone()
        return self._node_settings_row_to_dict(row) if row else None

    def save_node_setting(self, data: dict) -> dict:
        node_id = data.get("node_id")
        node_key = self._node_key(node_id)
        now = int(time.time())

        payload = {
            "node_key": node_key,
            "node_id": int(node_id) if node_id not in (None, "") else None,
            "node_name": data.get("node_name") or "",
            "node_address": data.get("node_address") or "",
            "billing_group": data.get("billing_group") or "",
            "provider": data.get("provider") or "",
            "location": data.get("location") or "",
            "monthly_cost": data.get("monthly_cost"),
            "currency": data.get("currency") or "USD",
            "traffic_included_gb": data.get("traffic_included_gb"),
            "traffic_price_per_tb": data.get("traffic_price_per_tb"),
            "importance": data.get("importance") or "normal",
            "can_remove": 1 if data.get("can_remove", True) else 0,
            "note": data.get("note") or "",
            "updated_at": now,
        }

        with self._lock:
            self._conn.execute(
                """
                INSERT INTO node_settings (
                    node_key, node_id, node_name, node_address, billing_group, provider, location,
                    monthly_cost, currency, traffic_included_gb, traffic_price_per_tb,
                    importance, can_remove, note, updated_at
                ) VALUES (
                    :node_key, :node_id, :node_name, :node_address, :billing_group, :provider, :location,
                    :monthly_cost, :currency, :traffic_included_gb, :traffic_price_per_tb,
                    :importance, :can_remove, :note, :updated_at
                )
                ON CONFLICT(node_key) DO UPDATE SET
                    node_id=excluded.node_id,
                    node_name=excluded.node_name,
                    node_address=excluded.node_address,
                    billing_group=excluded.billing_group,
                    provider=excluded.provider,
                    location=excluded.location,
                    monthly_cost=excluded.monthly_cost,
                    currency=excluded.currency,
                    traffic_included_gb=excluded.traffic_included_gb,
                    traffic_price_per_tb=excluded.traffic_price_per_tb,
                    importance=excluded.importance,
                    can_remove=excluded.can_remove,
                    note=excluded.note,
                    updated_at=excluded.updated_at
                """,
                payload,
            )
            self._conn.commit()

        return self.get_node_setting(node_id)

    # ------------------------------------------------------------------ audit_log

    def log_audit_event(
        self,
        event_type: str,
        telegram_id: int | None = None,
        marzban_username: str | None = None,
        target: str | None = None,
        metadata: dict | None = None,
    ):
        """Append an audit log entry. Never raises — a logging failure must
        never break the primary action that triggered it; on failure the
        error is written to the application logger instead.

        Never pass secret values (tokens, passwords, API keys) in `metadata`.
        """
        try:
            metadata_json = json.dumps(metadata, ensure_ascii=False, sort_keys=True) if metadata else None
            with self._lock:
                self._conn.execute(
                    "INSERT INTO audit_log (timestamp, event_type, telegram_id, marzban_username, target, metadata_json)"
                    " VALUES (?,?,?,?,?,?)",
                    (int(time.time()), event_type, telegram_id, marzban_username, target, metadata_json),
                )
                self._conn.commit()
        except Exception as e:
            logger.error(f"audit_log write failed (event_type={event_type}): {e}")

    def get_audit_log(
        self,
        event_type: str | None = None,
        telegram_id: int | None = None,
        marzban_username: str | None = None,
        limit: int = 100,
    ) -> list:
        query = "SELECT * FROM audit_log WHERE 1=1"
        params: list = []
        if event_type:
            query += " AND event_type=?"
            params.append(event_type)
        if telegram_id is not None:
            query += " AND telegram_id=?"
            params.append(telegram_id)
        if marzban_username:
            query += " AND marzban_username=?"
            params.append(marzban_username)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        result = []
        for r in rows:
            entry = dict(r)
            metadata_json = entry.pop("metadata_json", None)
            entry["metadata"] = {}
            if metadata_json:
                try:
                    entry["metadata"] = json.loads(metadata_json)
                except (TypeError, ValueError):
                    entry["metadata"] = {}
            result.append(entry)
        return result

    # ------------------------------------------------------------------ tg_users

    def get_tg_user(self, telegram_id: int) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM tg_users WHERE telegram_id = ?", (telegram_id,)
            ).fetchone()
        return dict(row) if row else None

    def save_tg_user(self, telegram_id: int, marzban_username: str):
        """Upsert the (single, current) binding for this telegram_id.

        Note: this is intentionally M:1 — multiple telegram_ids may bind to
        the same marzban_username at the same time (shared subscription).
        Each telegram_id stores exactly one current binding.
        """
        now = int(time.time())
        with self._lock:
            existing = self._conn.execute(
                "SELECT marzban_username FROM tg_users WHERE telegram_id = ?", (telegram_id,)
            ).fetchone()
            old_username = existing["marzban_username"] if existing else None
            self._conn.execute(
                "INSERT INTO tg_users (telegram_id, marzban_username, registered_at) VALUES (?,?,?)"
                " ON CONFLICT(telegram_id) DO UPDATE SET marzban_username=excluded.marzban_username",
                (telegram_id, marzban_username, now),
            )
            self._conn.commit()

        if old_username is None:
            self.log_audit_event(
                AUDIT_EVENT_TG_BOUND, telegram_id=telegram_id, marzban_username=marzban_username,
            )
        elif old_username != marzban_username:
            self.log_audit_event(
                AUDIT_EVENT_TG_REBOUND, telegram_id=telegram_id, marzban_username=marzban_username,
                metadata={"old_username": old_username, "new_username": marzban_username},
            )
        return {"rebound": old_username is not None and old_username != marzban_username, "old_username": old_username}

    # ------------------------------------------------------------------ tickets

    def create_ticket(self, telegram_id: int, marzban_username: str | None = None, status: str = "open") -> int:
        now = int(time.time())
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO tickets (telegram_id, marzban_username, status, created_at, updated_at) VALUES (?,?,?,?,?)",
                (telegram_id, marzban_username, status, now, now),
            )
            self._conn.commit()
            return cur.lastrowid

    def get_open_ticket(self, telegram_id: int) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM tickets WHERE telegram_id = ? AND status != 'closed' ORDER BY id DESC LIMIT 1",
                (telegram_id,),
            ).fetchone()
        return dict(row) if row else None

    def get_ticket(self, ticket_id: int) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM tickets WHERE id = ?", (ticket_id,)
            ).fetchone()
        return dict(row) if row else None

    def update_ticket_status(self, ticket_id: int, status: str):
        now = int(time.time())
        with self._lock:
            self._conn.execute(
                "UPDATE tickets SET status=?, updated_at=? WHERE id=?",
                (status, now, ticket_id),
            )
            self._conn.commit()

    def list_tickets(self, status: str | None = None, limit: int = 50) -> list:
        with self._lock:
            if status:
                rows = self._conn.execute(
                    "SELECT * FROM tickets WHERE status=? ORDER BY id DESC LIMIT ?",
                    (status, limit),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM tickets ORDER BY id DESC LIMIT ?", (limit,)
                ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ ticket_messages

    def add_ticket_message(self, ticket_id: int, role: str, text: str) -> int:
        now = int(time.time())
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO ticket_messages (ticket_id, role, text, ts) VALUES (?,?,?,?)",
                (ticket_id, role, text, now),
            )
            self._conn.execute(
                "UPDATE tickets SET updated_at=? WHERE id=?", (now, ticket_id)
            )
            self._conn.commit()
            return cur.lastrowid

    def get_ticket_messages(self, ticket_id: int, limit: int = 20) -> list:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM ticket_messages WHERE ticket_id=? ORDER BY id ASC LIMIT ?",
                (ticket_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ device management sessions
    #
    # Two-step flow used by the Telegram bot to grant a browser session the
    # right to mutate (rename/deactivate) devices on a marzban_username the
    # requesting telegram_id is currently bound to:
    #   1. create_mgmt_code()  — bot issues a one-time code, TTL ~15 min,
    #      bound to (telegram_id, marzban_username) at issuance. Only the
    #      SHA-256 hash is stored, mirroring how a password-reset token
    #      would be stored.
    #   2. exchange_mgmt_code() — web frontend exchanges the code exactly
    #      once for an opaque session id (also stored only as a hash). The
    #      code is atomically marked used so a second exchange attempt
    #      always fails, even under a race.
    #   3. get_mgmt_session() — backend route handlers look up the session
    #      by its (hashed) cookie value on every mutating request; nothing
    #      about identity/scope/username is ever trusted from the request
    #      body/query itself.

    def create_mgmt_code(
        self,
        telegram_id: int,
        marzban_username: str,
        scope: str = MGMT_SCOPE_DEVICES,
        ttl_seconds: int = MGMT_CODE_TTL_SECONDS,
    ) -> str:
        """Issue a one-time device-management code. Returns the raw code
        (only returned once — callers must not persist it themselves)."""
        raw_code = secrets.token_urlsafe(24)
        code_hash = _hash_secret(raw_code)
        now = int(time.time())
        with self._lock:
            self._conn.execute(
                "INSERT INTO mgmt_codes (code_hash, telegram_id, marzban_username, scope, created_at, expires_at)"
                " VALUES (?,?,?,?,?,?)",
                (code_hash, telegram_id, marzban_username, scope, now, now + ttl_seconds),
            )
            self._conn.commit()
        # Never log the raw code — only that one was issued.
        self.log_audit_event(
            AUDIT_EVENT_MGMT_CODE_ISSUED, telegram_id=telegram_id, marzban_username=marzban_username,
            metadata={"scope": scope, "ttl_seconds": ttl_seconds},
        )
        return raw_code

    def exchange_mgmt_code(
        self, raw_code: str, session_ttl_seconds: int = MGMT_SESSION_TTL_SECONDS
    ) -> dict | None:
        """Validate + single-use-consume a one-time code and issue a
        management session. Returns None if the code is unknown, expired,
        or already used. Returns {session_id (raw), telegram_id,
        marzban_username, scope, expires_at} on success."""
        if not raw_code:
            return None
        code_hash = _hash_secret(raw_code)
        now = int(time.time())
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM mgmt_codes WHERE code_hash=?", (code_hash,)
            ).fetchone()
            if not row:
                return None
            if row["used_at"] is not None or row["expires_at"] < now:
                return None
            # Atomically mark used — guards against a race where two
            # concurrent exchange attempts both pass the checks above.
            cur = self._conn.execute(
                "UPDATE mgmt_codes SET used_at=? WHERE id=? AND used_at IS NULL",
                (now, row["id"]),
            )
            if cur.rowcount == 0:
                self._conn.commit()
                return None

            raw_session = secrets.token_urlsafe(32)
            session_hash = _hash_secret(raw_session)
            expires_at = now + session_ttl_seconds
            self._conn.execute(
                "INSERT INTO mgmt_sessions (session_hash, telegram_id, marzban_username, scope, created_at, expires_at)"
                " VALUES (?,?,?,?,?,?)",
                (session_hash, row["telegram_id"], row["marzban_username"], row["scope"], now, expires_at),
            )
            self._conn.commit()

        self.log_audit_event(
            AUDIT_EVENT_MGMT_SESSION_CREATED, telegram_id=row["telegram_id"],
            marzban_username=row["marzban_username"], metadata={"scope": row["scope"]},
        )
        return {
            "session_id": raw_session,
            "telegram_id": row["telegram_id"],
            "marzban_username": row["marzban_username"],
            "scope": row["scope"],
            "expires_at": expires_at,
        }

    def get_mgmt_session(self, raw_session_id: str) -> dict | None:
        """Look up a management session by its raw (unhashed) cookie value.
        Returns None if unknown or expired. Never returns/accepts the
        session id itself as authoritative for anything but the lookup key
        — callers must use the returned telegram_id/marzban_username/scope,
        never values passed separately by the client."""
        if not raw_session_id:
            return None
        session_hash = _hash_secret(raw_session_id)
        now = int(time.time())
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM mgmt_sessions WHERE session_hash=?", (session_hash,)
            ).fetchone()
        if not row or row["expires_at"] < now:
            return None
        return dict(row)

    # ------------------------------------------------------------------ stars_tariffs
    #
    # Admin-managed tariff catalog for Telegram Stars payments. The table is
    # created empty (see _create_tables) and no code path ever seeds a
    # default row — the owner creates tariffs manually via the admin panel
    # (Phase 2 design doc §9).

    def get_stars_tariffs(self) -> list:
        """All tariffs (admin view), including inactive ones."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM stars_tariffs ORDER BY sort_order, id"
            ).fetchall()
        return [dict(r) for r in rows]

    def get_active_stars_tariffs(self) -> list:
        """Active tariffs only (bot purchase-flow view). Empty by default."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM stars_tariffs WHERE active=1 ORDER BY sort_order, id"
            ).fetchall()
        return [dict(r) for r in rows]

    def get_stars_tariff(self, tariff_id: int) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM stars_tariffs WHERE id=?", (tariff_id,)
            ).fetchone()
        return dict(row) if row else None

    def save_stars_tariff(self, data: dict) -> dict:
        """Create (no 'id') or update (with 'id') a tariff. Never touches
        already-created stars_invoices rows — those snapshot their own
        tariff fields by value at invoice-creation time (§9)."""
        now = int(time.time())
        tariff_id = data.get("id")
        with self._lock:
            if tariff_id:
                self._conn.execute(
                    "UPDATE stars_tariffs SET name=?, duration_days=?, stars_price=?, "
                    "active=?, sort_order=?, updated_at=? WHERE id=?",
                    (
                        data["name"], data["duration_days"], data["stars_price"],
                        1 if data.get("active", True) else 0, data.get("sort_order", 0),
                        now, tariff_id,
                    ),
                )
            else:
                max_order = self._conn.execute(
                    "SELECT COALESCE(MAX(sort_order),0) FROM stars_tariffs"
                ).fetchone()[0]
                cur = self._conn.execute(
                    "INSERT INTO stars_tariffs (name, duration_days, stars_price, active, "
                    "sort_order, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                    (
                        data["name"], data["duration_days"], data["stars_price"],
                        1 if data.get("active", True) else 0,
                        data.get("sort_order", max_order + 1), now, now,
                    ),
                )
                tariff_id = cur.lastrowid
            self._conn.commit()
        return self.get_stars_tariff(tariff_id)

    def delete_stars_tariff(self, tariff_id: int):
        with self._lock:
            self._conn.execute("DELETE FROM stars_tariffs WHERE id=?", (tariff_id,))
            self._conn.commit()

    def toggle_stars_tariff(self, tariff_id: int, active: bool):
        with self._lock:
            self._conn.execute(
                "UPDATE stars_tariffs SET active=? WHERE id=?", (1 if active else 0, tariff_id)
            )
            self._conn.commit()

    def reorder_stars_tariffs(self, ordered_ids: list):
        with self._lock:
            for i, tid in enumerate(ordered_ids):
                self._conn.execute("UPDATE stars_tariffs SET sort_order=? WHERE id=?", (i, tid))
            self._conn.commit()

    # ------------------------------------------------------------------ stars_invoices
    #
    # See Phase 2 design doc §3/§4 for the full state machine and the
    # crash-safe durability rationale. Every state transition below is a
    # single `UPDATE ... WHERE status IN (...)` compare-and-set, mirroring
    # exchange_mgmt_code above — cur.rowcount == 0 always means "someone/
    # something else already moved this row", never an error to retry.

    @staticmethod
    def _stars_invoice_row_to_dict(row) -> dict | None:
        return dict(row) if row else None

    def create_stars_invoice(
        self,
        created_by_telegram_id: int,
        marzban_username: str,
        tariff_id: int | None,
        tariff_name: str,
        duration_days: int,
        stars_price: int,
        ttl_seconds: int = STARS_INVOICE_TTL_SECONDS,
    ) -> dict:
        now = int(time.time())
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT INTO stars_invoices (
                    created_by_telegram_id, marzban_username, tariff_id, tariff_name,
                    duration_days, stars_price, status, expires_at, created_at
                ) VALUES (?,?,?,?,?,?,'created',?,?)
                """,
                (
                    created_by_telegram_id, marzban_username, tariff_id, tariff_name,
                    duration_days, stars_price, now + ttl_seconds, now,
                ),
            )
            self._conn.commit()
            invoice_id = cur.lastrowid
        self.log_audit_event(
            AUDIT_EVENT_INVOICE_CREATED, telegram_id=created_by_telegram_id,
            marzban_username=marzban_username,
            metadata={"invoice_id": invoice_id, "tariff_name": tariff_name,
                      "duration_days": duration_days, "stars_price": stars_price},
        )
        return self.get_invoice(invoice_id)

    def get_invoice(self, invoice_id: int) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM stars_invoices WHERE id=?", (invoice_id,)
            ).fetchone()
        return self._stars_invoice_row_to_dict(row)

    def get_invoice_by_charge_id(self, charge_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM stars_invoices WHERE telegram_payment_charge_id=?", (charge_id,)
            ).fetchone()
        return self._stars_invoice_row_to_dict(row)

    def mark_invoice_paid(
        self,
        invoice_id: int,
        telegram_payment_charge_id: str,
        provider_payment_charge_id: str | None,
        payer_telegram_id: int,
        total_amount: int,
        currency: str = "XTR",
    ) -> bool:
        """created -> paid. False means this row/charge was already recorded
        (Telegram's at-least-once delivery, or a genuine duplicate) — a safe
        no-op, never re-notify or re-apply from the caller."""
        now = int(time.time())
        with self._lock:
            if self._conn.execute(
                "SELECT 1 FROM stars_orphan_payments WHERE telegram_payment_charge_id=?",
                (telegram_payment_charge_id,),
            ).fetchone():
                return False
            try:
                cur = self._conn.execute(
                    "UPDATE stars_invoices SET status='paid', "
                    "telegram_payment_charge_id=?, provider_payment_charge_id=?, "
                    "payer_telegram_id=?, total_amount=?, payment_currency=?, paid_at=? "
                    "WHERE id=? AND status='created' AND telegram_payment_charge_id IS NULL",
                    (telegram_payment_charge_id, provider_payment_charge_id,
                     payer_telegram_id, total_amount, currency, now, invoice_id),
                )
                self._conn.commit()
            except sqlite3.IntegrityError:
                self._conn.rollback()
                return False
            ok = cur.rowcount == 1
        if ok:
            self.log_audit_event(
                AUDIT_EVENT_PAYMENT_SUCCESSFUL, telegram_id=payer_telegram_id,
                metadata={"invoice_id": invoice_id, "total_amount": total_amount,
                          "charge_id": telegram_payment_charge_id},
            )
        return ok

    def mark_invoice_paid_but_ambiguous(
        self,
        invoice_id: int,
        charge_id: str,
        payer_telegram_id: int,
        total_amount: int,
        reason: str,
        currency: str = "XTR",
        provider_payment_charge_id: str | None = None,
    ) -> bool:
        """created -> manual_review directly, recording the payment fields
        that were captured even though the normal `paid` state was skipped —
        used when successful_payment's own re-validation (currency/amount)
        disagrees with the invoice row (§4.1/§8.2). Money already moved, so
        this must never be silently dropped."""
        now = int(time.time())
        with self._lock:
            if self._conn.execute(
                "SELECT 1 FROM stars_orphan_payments WHERE telegram_payment_charge_id=?",
                (charge_id,),
            ).fetchone():
                return False
            try:
                cur = self._conn.execute(
                    "UPDATE stars_invoices SET status='manual_review', "
                    "telegram_payment_charge_id=?, provider_payment_charge_id=?, "
                    "payer_telegram_id=?, total_amount=?, payment_currency=?, "
                    "paid_at=?, manual_review_reason=?, manual_review_at=? "
                    "WHERE id=? AND status='created'",
                    (charge_id, provider_payment_charge_id, payer_telegram_id, total_amount,
                     currency, now, reason, now, invoice_id),
                )
                self._conn.commit()
            except sqlite3.IntegrityError:
                self._conn.rollback()
                return False
            ok = cur.rowcount == 1
        if ok:
            self.log_audit_event(
                AUDIT_EVENT_PAYMENT_MANUAL_REVIEW, telegram_id=payer_telegram_id,
                metadata={"invoice_id": invoice_id, "reason": reason, "total_amount": total_amount},
            )
        return ok

    def get_pending_apply_invoices(self) -> list:
        """Rows the apply-worker still needs to act on, ordered so that for
        any given marzban_username the earliest (head-of-line) row comes
        first — later rows for the same username are naturally left parked
        (never picked up) until the head resolves to a terminal state or
        genuinely-safe-to-retry-next-tick state (§5.1)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM stars_invoices WHERE status IN ('paid','plan_committed') "
                "ORDER BY marzban_username, id"
            ).fetchall()
        return [dict(r) for r in rows]

    def commit_apply_plan(self, invoice_id: int, base_expire_observed: int, target_expire: int) -> bool:
        """paid -> plan_committed. The single most important write in this
        design — durably records the ABSOLUTE target before any Marzban
        mutation is attempted (§4.2)."""
        now = int(time.time())
        with self._lock:
            cur = self._conn.execute(
                "UPDATE stars_invoices SET status='plan_committed', "
                "base_expire_observed=?, target_expire=?, plan_committed_at=? "
                "WHERE id=? AND status='paid'",
                (base_expire_observed, target_expire, now, invoice_id),
            )
            self._conn.commit()
            ok = cur.rowcount == 1
        if ok:
            row = self.get_invoice(invoice_id)
            self.log_audit_event(
                AUDIT_EVENT_PAYMENT_PLAN_COMMITTED,
                telegram_id=row["payer_telegram_id"] if row else None,
                marzban_username=row["marzban_username"] if row else None,
                metadata={"invoice_id": invoice_id, "base_expire_observed": base_expire_observed,
                          "target_expire": target_expire},
            )
        return ok

    def mark_invoice_applied(self, invoice_id: int, applied_expire: int) -> bool:
        """plan_committed -> applied. Terminal, success."""
        now = int(time.time())
        with self._lock:
            cur = self._conn.execute(
                "UPDATE stars_invoices SET status='applied', applied_expire=?, applied_at=? "
                "WHERE id=? AND status='plan_committed'",
                (applied_expire, now, invoice_id),
            )
            self._conn.commit()
            return cur.rowcount == 1

    def mark_invoice_manual_review(self, invoice_id: int, reason: str) -> bool:
        """paid or plan_committed -> manual_review. Used both by the
        eligibility re-check at plan-commit time (from 'paid') and by the
        3-case recovery comparison's ambiguous case 3 (from
        'plan_committed') — §4.2."""
        now = int(time.time())
        with self._lock:
            cur = self._conn.execute(
                "UPDATE stars_invoices SET status='manual_review', "
                "manual_review_reason=?, manual_review_at=? "
                "WHERE id=? AND status IN ('paid','plan_committed')",
                (reason, now, invoice_id),
            )
            self._conn.commit()
            ok = cur.rowcount == 1
        if ok:
            row = self.get_invoice(invoice_id)
            self.log_audit_event(
                AUDIT_EVENT_PAYMENT_MANUAL_REVIEW,
                telegram_id=row["payer_telegram_id"] if row else None,
                marzban_username=row["marzban_username"] if row else None,
                metadata={"invoice_id": invoice_id, "reason": reason},
            )
        return ok

    def mark_invoice_apply_failed(self, invoice_id: int, reason: str) -> bool:
        """paid or plan_committed -> apply_failed_user_missing /
        apply_retry_exhausted, depending on `reason` ('user_missing' or
        'retry_exhausted'). Terminal-ish, needs operator action."""
        status = "apply_failed_user_missing" if reason == "user_missing" else "apply_retry_exhausted"
        with self._lock:
            cur = self._conn.execute(
                f"UPDATE stars_invoices SET status='{status}' "
                "WHERE id=? AND status IN ('paid','plan_committed')",
                (invoice_id,),
            )
            self._conn.commit()
            ok = cur.rowcount == 1
        if ok and status == "apply_retry_exhausted":
            row = self.get_invoice(invoice_id)
            self.log_audit_event(
                AUDIT_EVENT_PAYMENT_APPLY_RETRY_EXHAUSTED,
                telegram_id=row["payer_telegram_id"] if row else None,
                marzban_username=row["marzban_username"] if row else None,
                metadata={
                    "invoice_id": invoice_id,
                    "stage": ("plan_committed" if row and row["target_expire"] is not None
                              else "before_plan_commit"),
                    "apply_attempts": row["apply_attempts"] if row else None,
                    "last_apply_error": row["last_apply_error"] if row else None,
                },
            )
        return ok

    def record_apply_attempt_failure(
        self, invoice_id: int, error: str, max_attempts: int | None = None
    ) -> bool:
        """Durably record one transient apply failure.

        When max_attempts is supplied, the increment and transition to
        apply_retry_exhausted happen in the same SQLite statement.
        """
        now = int(time.time())
        with self._lock:
            if max_attempts is None:
                cur = self._conn.execute(
                    "UPDATE stars_invoices SET apply_attempts=apply_attempts+1, "
                    "last_apply_error=?, last_apply_attempt_at=? "
                    "WHERE id=? AND status IN ('paid','plan_committed')",
                    (error[:500] if error else None, now, invoice_id),
                )
            else:
                # The failure counter and exhaustion transition are one
                # SQLite statement, closing the crash window between them.
                cur = self._conn.execute(
                    "UPDATE stars_invoices SET apply_attempts=apply_attempts+1, "
                    "last_apply_error=?, last_apply_attempt_at=?, "
                    "status=CASE WHEN apply_attempts+1>=? "
                    "THEN 'apply_retry_exhausted' ELSE status END "
                    "WHERE id=? AND status IN ('paid','plan_committed')",
                    (error[:500] if error else None, now, max_attempts, invoice_id),
                )
            self._conn.commit()
            ok = cur.rowcount == 1
            row = self._conn.execute(
                "SELECT * FROM stars_invoices WHERE id=?", (invoice_id,)
            ).fetchone() if ok else None
        if ok and row and row["status"] == "apply_retry_exhausted":
            row = dict(row)
            self.log_audit_event(
                AUDIT_EVENT_PAYMENT_APPLY_RETRY_EXHAUSTED,
                telegram_id=row["payer_telegram_id"],
                marzban_username=row["marzban_username"],
                metadata={
                    "invoice_id": invoice_id,
                    "stage": ("plan_committed" if row["target_expire"] is not None
                              else "before_plan_commit"),
                    "apply_attempts": row["apply_attempts"],
                    "last_apply_error": row["last_apply_error"],
                },
            )
        return ok

    def list_stars_invoices(self, status: str | None = None, limit: int = 200) -> list:
        with self._lock:
            if status:
                rows = self._conn.execute(
                    "SELECT * FROM stars_invoices WHERE status=? ORDER BY id DESC LIMIT ?",
                    (status, limit),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM stars_invoices ORDER BY id DESC LIMIT ?", (limit,)
                ).fetchall()
        return [dict(r) for r in rows]

    def resolve_manual_review_confirm_applied(self, invoice_id: int, applied_expire: int) -> bool:
        """Operator action: 'Confirm applied'. WHERE status IN
        ('manual_review','apply_retry_exhausted') -> 'applied'. The caller
        (admin route) is responsible for performing the fresh get_user
        itself and passing that live value in as `applied_expire` — this
        method does no Marzban I/O (§4.4)."""
        now = int(time.time())
        with self._lock:
            cur = self._conn.execute(
                "UPDATE stars_invoices SET status='applied', applied_expire=?, "
                "applied_at=?, resolved_by_admin_at=? "
                "WHERE id=? AND status IN ('manual_review','apply_retry_exhausted')",
                (applied_expire, now, now, invoice_id),
            )
            self._conn.commit()
            return cur.rowcount == 1

    def resolve_manual_review_requeue(self, invoice_id: int) -> bool:
        """Operator action: 'Requeue'. WHERE status IN
        ('manual_review','apply_retry_exhausted') -> 'plan_committed' — the
        worker retries with the SAME previously-committed target_expire,
        never a recomputed one."""
        now = int(time.time())
        with self._lock:
            cur = self._conn.execute(
                "UPDATE stars_invoices SET status='plan_committed', resolved_by_admin_at=? "
                "WHERE id=? AND status IN ('manual_review','apply_retry_exhausted') "
                "AND base_expire_observed IS NOT NULL AND target_expire IS NOT NULL",
                (now, invoice_id),
            )
            self._conn.commit()
            ok = cur.rowcount == 1
        if ok:
            row = self.get_invoice(invoice_id)
            self.log_audit_event(
                AUDIT_EVENT_PAYMENT_REQUEUED,
                telegram_id=row["payer_telegram_id"] if row else None,
                marzban_username=row["marzban_username"] if row else None,
                metadata={"invoice_id": invoice_id,
                          "base_expire_observed": row["base_expire_observed"],
                          "target_expire": row["target_expire"]},
            )
        return ok

    def begin_invoice_refund(self, invoice_id: int) -> bool:
        """Durably claim the one allowed remote refund attempt."""
        now = int(time.time())
        with self._lock:
            cur = self._conn.execute(
                "UPDATE stars_invoices SET refund_previous_status=status, "
                "status='refund_pending', refund_requested_at=?, refund_last_error=NULL "
                "WHERE id=? AND status IN "
                "('applied','manual_review','apply_retry_exhausted','apply_failed_user_missing')",
                (now, invoice_id),
            )
            self._conn.commit()
            return cur.rowcount == 1

    def mark_invoice_refund_known_failed(self, invoice_id: int, error: str) -> bool:
        """Restore the prior actionable state only for an explicit False.

        Exceptions and timeouts are not proof of failure and must use
        mark_invoice_refund_unknown instead.
        """
        with self._lock:
            cur = self._conn.execute(
                "UPDATE stars_invoices SET status=refund_previous_status, "
                "refund_last_error=? WHERE id=? AND status='refund_pending' "
                "AND refund_previous_status IS NOT NULL",
                ((error or "explicit_false")[:500], invoice_id),
            )
            self._conn.commit()
            return cur.rowcount == 1

    def mark_invoice_refund_unknown(self, invoice_id: int, error: str) -> bool:
        now = int(time.time())
        with self._lock:
            cur = self._conn.execute(
                "UPDATE stars_invoices SET status='refund_unknown', refund_unknown_at=?, "
                "refund_last_error=? WHERE id=? AND status='refund_pending'",
                (now, (error or "unknown remote outcome")[:500], invoice_id),
            )
            self._conn.commit()
            ok = cur.rowcount == 1
        if ok:
            row = self.get_invoice(invoice_id)
            self.log_audit_event(
                AUDIT_EVENT_REFUND_UNKNOWN,
                telegram_id=row["payer_telegram_id"] if row else None,
                marzban_username=row["marzban_username"] if row else None,
                metadata={"invoice_id": invoice_id, "error": (error or "")[:500]},
            )
        return ok

    def mark_invoice_refunded(self, invoice_id: int, reconciled: bool = False) -> bool:
        """Record a confirmed Telegram refund; never changes VPN expiry."""
        now = int(time.time())
        with self._lock:
            cur = self._conn.execute(
                "UPDATE stars_invoices SET status='refunded', refunded_at=?, "
                "resolved_by_admin_at=?, refund_reconciled_at=? "
                "WHERE id=? AND status IN ('refund_pending','refund_unknown')",
                (now, now, now if reconciled else None, invoice_id),
            )
            self._conn.commit()
            ok = cur.rowcount == 1
        if ok:
            row = self.get_invoice(invoice_id)
            self.log_audit_event(
                AUDIT_EVENT_REFUND,
                telegram_id=row["payer_telegram_id"] if row else None,
                marzban_username=row["marzban_username"] if row else None,
                metadata={"invoice_id": invoice_id, "reconciled": reconciled},
            )
        return ok

    # ---------------------------------------------------------- orphan payments

    def record_stars_orphan_payment(
        self,
        telegram_payment_charge_id: str,
        provider_payment_charge_id: str | None,
        payer_telegram_id: int,
        currency: str,
        total_amount: int,
        invoice_payload: str,
        reason: str,
    ) -> tuple[dict | None, bool]:
        """Capture an otherwise-unmatched successful_payment exactly once."""
        now = int(time.time())
        # Keep Telegram's trusted financial identifier byte-for-byte intact.
        # Only payload/display/audit previews are length-bounded.
        charge_id = str(telegram_payment_charge_id)
        payload = str(invoice_payload or "")[:256]
        with self._lock:
            # Charge identity is global across both ledgers at the service
            # layer. A duplicate update with a malformed/different payload
            # must not create a second refundable financial record.
            if self._conn.execute(
                "SELECT 1 FROM stars_invoices WHERE telegram_payment_charge_id=?",
                (charge_id,),
            ).fetchone():
                return None, False
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO stars_orphan_payments "
                "(telegram_payment_charge_id, provider_payment_charge_id, "
                "payer_telegram_id, currency, total_amount, invoice_payload, reason, "
                "status, received_at) VALUES (?,?,?,?,?,?,?,'manual_review',?)",
                (charge_id, provider_payment_charge_id, payer_telegram_id,
                 str(currency or "")[:16], int(total_amount), payload,
                 str(reason or "unknown")[:500], now),
            )
            self._conn.commit()
            created = cur.rowcount == 1
            row = self._conn.execute(
                "SELECT * FROM stars_orphan_payments WHERE telegram_payment_charge_id=?",
                (charge_id,),
            ).fetchone()
        result = dict(row)
        if created:
            self.log_audit_event(
                AUDIT_EVENT_ORPHAN_PAYMENT_CAPTURED,
                telegram_id=payer_telegram_id,
                metadata={"orphan_payment_id": result["id"], "charge_id": charge_id[:256],
                          "reason": result["reason"]},
            )
            self.log_audit_event(
                AUDIT_EVENT_PAYMENT_FAILED,
                telegram_id=payer_telegram_id,
                metadata={"orphan_payment_id": result["id"], "charge_id": charge_id[:256],
                          "reason": result["reason"]},
            )
        return result, created

    def get_stars_orphan_payment(self, orphan_id: int) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM stars_orphan_payments WHERE id=?", (orphan_id,)
            ).fetchone()
        return dict(row) if row else None

    def get_stars_orphan_by_charge_id(self, charge_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM stars_orphan_payments WHERE telegram_payment_charge_id=?",
                (charge_id,),
            ).fetchone()
        return dict(row) if row else None

    def list_stars_orphan_payments(self, status: str | None = None, limit: int = 200) -> list:
        with self._lock:
            if status:
                rows = self._conn.execute(
                    "SELECT * FROM stars_orphan_payments WHERE status=? "
                    "ORDER BY id DESC LIMIT ?", (status, limit),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM stars_orphan_payments ORDER BY id DESC LIMIT ?", (limit,)
                ).fetchall()
        return [dict(row) for row in rows]

    def begin_orphan_refund(self, orphan_id: int) -> bool:
        now = int(time.time())
        with self._lock:
            cur = self._conn.execute(
                "UPDATE stars_orphan_payments SET refund_previous_status=status, "
                "status='refund_pending', refund_requested_at=?, refund_last_error=NULL "
                "WHERE id=? AND status='manual_review'",
                (now, orphan_id),
            )
            self._conn.commit()
            return cur.rowcount == 1

    def mark_orphan_refund_known_failed(self, orphan_id: int, error: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE stars_orphan_payments SET status=refund_previous_status, "
                "refund_last_error=? WHERE id=? AND status='refund_pending' "
                "AND refund_previous_status IS NOT NULL",
                ((error or "explicit_false")[:500], orphan_id),
            )
            self._conn.commit()
            return cur.rowcount == 1

    def mark_orphan_refund_unknown(self, orphan_id: int, error: str) -> bool:
        now = int(time.time())
        with self._lock:
            cur = self._conn.execute(
                "UPDATE stars_orphan_payments SET status='refund_unknown', "
                "refund_unknown_at=?, refund_last_error=? "
                "WHERE id=? AND status='refund_pending'",
                (now, (error or "unknown remote outcome")[:500], orphan_id),
            )
            self._conn.commit()
            ok = cur.rowcount == 1
        if ok:
            row = self.get_stars_orphan_payment(orphan_id)
            self.log_audit_event(
                AUDIT_EVENT_REFUND_UNKNOWN,
                telegram_id=row["payer_telegram_id"] if row else None,
                metadata={"orphan_payment_id": orphan_id, "error": (error or "")[:500]},
            )
        return ok

    def mark_orphan_refunded(self, orphan_id: int, reconciled: bool = False) -> bool:
        now = int(time.time())
        with self._lock:
            cur = self._conn.execute(
                "UPDATE stars_orphan_payments SET status='refunded', refunded_at=?, "
                "resolved_at=?, refund_reconciled_at=? "
                "WHERE id=? AND status IN ('refund_pending','refund_unknown')",
                (now, now, now if reconciled else None, orphan_id),
            )
            self._conn.commit()
            ok = cur.rowcount == 1
        if ok:
            row = self.get_stars_orphan_payment(orphan_id)
            self.log_audit_event(
                AUDIT_EVENT_REFUND,
                telegram_id=row["payer_telegram_id"] if row else None,
                metadata={"orphan_payment_id": orphan_id, "reconciled": reconciled},
            )
        return ok
