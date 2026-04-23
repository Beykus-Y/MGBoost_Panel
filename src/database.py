import json
import os
import sqlite3
import time

from .config import DATA_DIR

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
        self._create_tables()

    def _create_tables(self):
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS sub_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                token TEXT NOT NULL,
                username TEXT,
                user_agent TEXT,
                ip TEXT,
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
        """)
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
        existing = self._conn.execute("SELECT COUNT(*) FROM extra_configs").fetchone()[0]
        if existing > 0:
            return
        try:
            with open(EXTRA_CONFIGS_JSON) as f:
                configs = json.load(f)
            now = int(time.time())
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
        existing = self._conn.execute("SELECT COUNT(*) FROM node_filters").fetchone()[0]
        if existing > 0:
            return
        try:
            with open(NODE_FILTERS_JSON) as f:
                filters = json.load(f)
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
        existing = self._conn.execute("SELECT COUNT(*) FROM per_user_configs").fetchone()[0]
        if existing > 0:
            return
        try:
            with open(PER_USER_CONFIGS_JSON) as f:
                data = json.load(f)
            now = int(time.time())
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
        existing = self._conn.execute("SELECT COUNT(*) FROM hysteria_stats").fetchone()[0]
        if existing > 0:
            return
        try:
            with open(HYSTERIA_STATS_JSON) as f:
                stats = json.load(f)
            for token, entry in stats.items():
                self._conn.execute(
                    "INSERT OR REPLACE INTO hysteria_stats (token, upload, download) VALUES (?,?,?)",
                    (token, entry.get("upload", 0), entry.get("download", 0)),
                )
            self._conn.commit()
            print(f"[DB] Migrated {len(stats)} hysteria_stats entries from JSON")
        except Exception as e:
            print(f"[DB] hysteria_stats migration failed: {e}")

    # --- extra_configs ---

    def get_extra_configs(self):
        rows = self._conn.execute(
            "SELECT id, name, uri, enabled, sort_order FROM extra_configs WHERE scope='global' ORDER BY sort_order, id"
        ).fetchall()
        return [dict(r) for r in rows]

    def add_extra_config(self, name, uri, enabled=True):
        max_order = self._conn.execute("SELECT COALESCE(MAX(sort_order),0) FROM extra_configs").fetchone()[0]
        self._conn.execute(
            "INSERT INTO extra_configs (name, uri, enabled, sort_order, scope, created_at) VALUES (?,?,?,?,?,?)",
            (name, uri, 1 if enabled else 0, max_order + 1, "global", int(time.time())),
        )
        self._conn.commit()

    def delete_extra_config(self, config_id):
        self._conn.execute("DELETE FROM extra_configs WHERE id=?", (config_id,))
        self._conn.commit()

    def reorder_extra_configs(self, ordered_ids):
        for i, cid in enumerate(ordered_ids):
            self._conn.execute("UPDATE extra_configs SET sort_order=? WHERE id=?", (i, cid))
        self._conn.commit()

    def toggle_extra_config(self, config_id, enabled):
        self._conn.execute("UPDATE extra_configs SET enabled=? WHERE id=?", (1 if enabled else 0, config_id))
        self._conn.commit()

    # --- per_user_configs ---

    def get_per_user_configs(self, username=None):
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
        rows = self.get_per_user_configs()
        result = {}
        for r in rows:
            result.setdefault(r["username"], []).append(r)
        return result

    def save_per_user_configs_map(self, data):
        """Replace all per-user configs with data dict {username: [{name, uri, enabled}]}."""
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
        row = self._conn.execute(
            "SELECT filter_all, allowed_configs FROM node_filters WHERE username=?", (username,)
        ).fetchone()
        if not row:
            return None
        return {"all": bool(row["filter_all"]), "allowed_configs": json.loads(row["allowed_configs"] or "[]")}

    # --- hysteria_stats ---

    def get_hysteria_stats(self):
        rows = self._conn.execute("SELECT token, upload, download FROM hysteria_stats").fetchall()
        return {r["token"]: {"upload": r["upload"], "download": r["download"]} for r in rows}

    def get_hysteria_traffic(self, token):
        row = self._conn.execute(
            "SELECT upload, download FROM hysteria_stats WHERE token=?", (token,)
        ).fetchone()
        if not row:
            return 0, 0
        return row["upload"], row["download"]

    def update_hysteria_stats(self, token, upload_delta, download_delta):
        existing = self._conn.execute(
            "SELECT upload, download FROM hysteria_stats WHERE token=?", (token,)
        ).fetchone()
        if existing:
            self._conn.execute(
                "UPDATE hysteria_stats SET upload=upload+?, download=download+? WHERE token=?",
                (upload_delta, download_delta, token),
            )
        else:
            self._conn.execute(
                "INSERT INTO hysteria_stats (token, upload, download) VALUES (?,?,?)",
                (token, upload_delta, download_delta),
            )
        self._conn.commit()

    # --- sub_requests ---

    def log_request(self, token, username, user_agent, ip):
        existing = self._conn.execute(
            "SELECT id FROM sub_requests WHERE token=? AND user_agent=?",
            (token, user_agent),
        ).fetchone()
        if existing:
            self._conn.execute(
                "UPDATE sub_requests SET timestamp=?, ip=?, username=? WHERE id=?",
                (int(time.time()), ip, username, existing["id"]),
            )
        else:
            self._conn.execute(
                "INSERT INTO sub_requests (token, username, user_agent, ip, timestamp) VALUES (?,?,?,?,?)",
                (token, username, user_agent, ip, int(time.time())),
            )
        self._conn.commit()

    def get_device_history(self, token: str, limit: int = 10) -> list:
        rows = self._conn.execute(
            "SELECT user_agent, ip, timestamp FROM sub_requests WHERE token=? ORDER BY timestamp DESC LIMIT ?",
            (token, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    # --- settings ---

    def get_setting(self, key: str, default=None):
        row = self._conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        if not row:
            return default
        return row["value"]

    def set_setting(self, key: str, value: str):
        self._conn.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?,?)",
            (key, value),
        )
        self._conn.commit()
