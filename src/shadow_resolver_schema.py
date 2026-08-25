"""Additive account-aware shadow resolver binding and safe aggregate metrics."""

import hashlib
import sqlite3
import time

MIGRATION_ID = "ph3_03_shadow_resolver_v1"

_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS mgboost_shadow_resolver_bindings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        account_id INTEGER NOT NULL,
        legacy_alias_id INTEGER NOT NULL,
        legacy_device_id INTEGER NOT NULL UNIQUE,
        slot_generation_id INTEGER NOT NULL UNIQUE,
        child_intent_id INTEGER NOT NULL UNIQUE,
        operation_id TEXT NOT NULL UNIQUE,
        mode TEXT NOT NULL CHECK(mode='SHADOW'),
        enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
        decision_ref TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        UNIQUE(id, account_id),
        FOREIGN KEY(account_id) REFERENCES mgboost_accounts(id) ON DELETE RESTRICT,
        FOREIGN KEY(legacy_alias_id, account_id)
          REFERENCES mgboost_legacy_account_aliases(id, account_id) ON DELETE RESTRICT,
        FOREIGN KEY(slot_generation_id) REFERENCES mgboost_device_slot_generations(id)
          ON DELETE RESTRICT,
        FOREIGN KEY(child_intent_id, account_id)
          REFERENCES mgboost_child_user_intents(id, account_id) ON DELETE RESTRICT,
        FOREIGN KEY(operation_id) REFERENCES mgboost_outbox(operation_id)
          ON DELETE RESTRICT,
        FOREIGN KEY(legacy_device_id) REFERENCES user_devices(id) ON DELETE RESTRICT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS mgboost_shadow_resolver_metrics (
        bucket_day INTEGER NOT NULL,
        binding_id INTEGER NOT NULL,
        result TEXT NOT NULL CHECK(result IN ('PASS','FAIL')),
        category TEXT NOT NULL CHECK(length(category) BETWEEN 2 AND 64),
        credential_result TEXT NOT NULL CHECK(credential_result IN ('SUCCESS','FAIL','NOT_ATTEMPTED')),
        legacy_fallback_success INTEGER NOT NULL CHECK(legacy_fallback_success IN (0,1)),
        request_count INTEGER NOT NULL CHECK(request_count > 0),
        latency_total_ms INTEGER NOT NULL CHECK(latency_total_ms >= 0),
        latency_max_ms INTEGER NOT NULL CHECK(latency_max_ms >= 0),
        first_seen_at INTEGER NOT NULL,
        last_seen_at INTEGER NOT NULL,
        PRIMARY KEY(bucket_day,binding_id,result,category,credential_result,legacy_fallback_success),
        FOREIGN KEY(binding_id) REFERENCES mgboost_shadow_resolver_bindings(id)
          ON DELETE RESTRICT
    )
    """,
    """CREATE INDEX IF NOT EXISTS ix_shadow_metrics_retention
       ON mgboost_shadow_resolver_metrics(bucket_day,binding_id)""",
    """
    CREATE TRIGGER IF NOT EXISTS trg_shadow_binding_identity_immutable
    BEFORE UPDATE OF account_id,legacy_alias_id,legacy_device_id,slot_generation_id,
                     child_intent_id,operation_id,mode,decision_ref,created_at
    ON mgboost_shadow_resolver_bindings
    BEGIN SELECT RAISE(ABORT,'shadow binding identity is immutable'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_shadow_binding_no_delete
    BEFORE DELETE ON mgboost_shadow_resolver_bindings
    BEGIN SELECT RAISE(ABORT,'shadow binding history is immutable'); END
    """,
)

SCHEMA_CHECKSUM = hashlib.sha256("\n".join(x.strip() for x in _SCHEMA).encode()).hexdigest()


def apply_shadow_resolver_schema(connection: sqlite3.Connection, *, now=None) -> bool:
    timestamp = int(time.time()) if now is None else int(now)
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT schema_checksum FROM mgboost_schema_migrations WHERE migration_id=?",
            (MIGRATION_ID,),
        ).fetchone()
        if row:
            if row[0] != SCHEMA_CHECKSUM:
                raise RuntimeError("shadow resolver schema checksum mismatch")
            connection.commit()
            return False
        for statement in _SCHEMA:
            connection.execute(statement)
        connection.execute(
            "INSERT INTO mgboost_schema_migrations(migration_id,schema_checksum,applied_at) VALUES(?,?,?)",
            (MIGRATION_ID, SCHEMA_CHECKSUM, timestamp),
        )
        connection.commit()
        return True
    except Exception:
        connection.rollback()
        raise
