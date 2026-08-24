"""Additive dormant PH3-03 alias, child-intent and durable outbox schema."""

from __future__ import annotations

import hashlib
import sqlite3
import time

from .device_slot_schema import MIGRATION_ID as SLOT_MIGRATION_ID
from .device_slot_schema import SCHEMA_CHECKSUM as SLOT_SCHEMA_CHECKSUM
from .internal_entitlement_schema import MIGRATION_ID as INTERNAL_MIGRATION_ID
from .internal_entitlement_schema import SCHEMA_CHECKSUM as INTERNAL_SCHEMA_CHECKSUM
from .provenance_schema import MIGRATION_ID as PROVENANCE_MIGRATION_ID
from .provenance_schema import SCHEMA_CHECKSUM as PROVENANCE_SCHEMA_CHECKSUM


MIGRATION_ID = "ph3_03_child_prerequisites_v1"

_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS mgboost_legacy_alias_groups (
        account_id INTEGER PRIMARY KEY,
        mapping_key TEXT NOT NULL UNIQUE
            CHECK(length(mapping_key) BETWEEN 3 AND 128),
        decision_ref TEXT NOT NULL CHECK(length(decision_ref) BETWEEN 3 AND 128),
        created_by_actor TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        FOREIGN KEY(account_id) REFERENCES mgboost_accounts(id) ON DELETE RESTRICT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS mgboost_legacy_account_aliases (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        account_id INTEGER NOT NULL,
        legacy_username TEXT NOT NULL UNIQUE,
        alias_role TEXT NOT NULL CHECK(alias_role IN ('PRIMARY','SECONDARY')),
        ownership_provenance TEXT NOT NULL
            CHECK(ownership_provenance IN ('OWNER_APPROVED','EVIDENCE_PROVEN')),
        legacy_status TEXT NOT NULL
            CHECK(legacy_status IN ('ACTIVE','DISABLED','EXPIRED','UNLIMITED')),
        legacy_expiry INTEGER,
        observed_device_count INTEGER NOT NULL CHECK(observed_device_count >= 0),
        observed_hwid_count INTEGER NOT NULL CHECK(observed_hwid_count >= 0),
        evidence_json TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        UNIQUE(id, account_id),
        FOREIGN KEY(account_id) REFERENCES mgboost_legacy_alias_groups(account_id)
            ON DELETE RESTRICT
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS ux_mgboost_alias_one_primary
        ON mgboost_legacy_account_aliases(account_id)
        WHERE alias_role='PRIMARY'
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_mgboost_alias_account
        ON mgboost_legacy_account_aliases(account_id, id)
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS ux_mgboost_generation_full_identity
        ON mgboost_device_slot_generations(
            id, account_id, slot_id, slot_number, generation
        )
    """,
    """
    CREATE TABLE IF NOT EXISTS mgboost_child_user_intents (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        public_id TEXT NOT NULL UNIQUE,
        account_id INTEGER NOT NULL,
        slot_id INTEGER NOT NULL,
        slot_generation_id INTEGER NOT NULL UNIQUE,
        slot_number INTEGER NOT NULL CHECK(slot_number BETWEEN 1 AND 99),
        generation INTEGER NOT NULL CHECK(generation > 0),
        source_alias_id INTEGER NOT NULL,
        child_username TEXT NOT NULL UNIQUE
            CHECK(substr(child_username,1,4)='mgc_'
                  AND length(child_username)=30
                  AND substr(child_username,5) NOT GLOB '*[^a-z2-7]*'),
        source_contract_hash TEXT NOT NULL
            CHECK(length(source_contract_hash)=64
                  AND source_contract_hash NOT GLOB '*[^0-9a-f]*'),
        desired_state TEXT NOT NULL DEFAULT 'ACTIVE'
            CHECK(desired_state IN ('ACTIVE','DISABLED','REVOKED')),
        observed_state TEXT NOT NULL DEFAULT 'NOT_CREATED'
            CHECK(observed_state IN (
                'NOT_CREATED','ACTIVE','DISABLED','REVOKED','UNKNOWN','ERROR'
            )),
        uuid_verifier TEXT
            CHECK(uuid_verifier IS NULL OR
                  (length(uuid_verifier)=71 AND substr(uuid_verifier,1,7)='sha256:'
                   AND substr(uuid_verifier,8) NOT GLOB '*[^0-9a-f]*')),
        uuid_masked TEXT
            CHECK(uuid_masked IS NULL OR
                  (length(uuid_masked)=13 AND substr(uuid_masked,1,5)='uuid_')),
        shadowsocks_verifier TEXT
            CHECK(shadowsocks_verifier IS NULL OR
                  (length(shadowsocks_verifier)=71
                   AND substr(shadowsocks_verifier,1,7)='sha256:'
                   AND substr(shadowsocks_verifier,8) NOT GLOB '*[^0-9a-f]*')),
        shadowsocks_masked TEXT
            CHECK(shadowsocks_masked IS NULL OR
                  (length(shadowsocks_masked)=13
                   AND substr(shadowsocks_masked,1,3)='ss_')),
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        row_version INTEGER NOT NULL DEFAULT 1 CHECK(row_version > 0),
        UNIQUE(id, account_id),
        CHECK((uuid_verifier IS NULL AND uuid_masked IS NULL)
              OR (uuid_verifier IS NOT NULL AND uuid_masked IS NOT NULL)),
        CHECK((shadowsocks_verifier IS NULL AND shadowsocks_masked IS NULL)
              OR (shadowsocks_verifier IS NOT NULL
                  AND shadowsocks_masked IS NOT NULL)),
        FOREIGN KEY(account_id) REFERENCES mgboost_accounts(id) ON DELETE RESTRICT,
        FOREIGN KEY(source_alias_id, account_id)
            REFERENCES mgboost_legacy_account_aliases(id, account_id)
            ON DELETE RESTRICT,
        FOREIGN KEY(slot_generation_id, account_id, slot_id, slot_number, generation)
            REFERENCES mgboost_device_slot_generations(
                id, account_id, slot_id, slot_number, generation
            ) ON DELETE RESTRICT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS mgboost_outbox (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        operation_id TEXT NOT NULL UNIQUE
            CHECK(substr(operation_id,1,3)='op_'
                  AND length(operation_id)=29
                  AND substr(operation_id,4) NOT GLOB '*[^a-z2-7]*'),
        account_id INTEGER NOT NULL,
        child_intent_id INTEGER NOT NULL,
        operation_kind TEXT NOT NULL CHECK(operation_kind='CHILD_USER_ENSURE'),
        state TEXT NOT NULL DEFAULT 'PENDING'
            CHECK(state IN ('PENDING','IN_FLIGHT','RETRY','APPLIED','ERROR')),
        idempotency_key_hash TEXT NOT NULL UNIQUE
            CHECK(length(idempotency_key_hash)=64
                  AND idempotency_key_hash NOT GLOB '*[^0-9a-f]*'),
        request_hash TEXT NOT NULL
            CHECK(length(request_hash)=64 AND request_hash NOT GLOB '*[^0-9a-f]*'),
        payload_json TEXT NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
        next_attempt_at INTEGER NOT NULL,
        lease_owner TEXT,
        lease_expires_at INTEGER,
        last_error_class TEXT,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        row_version INTEGER NOT NULL DEFAULT 1 CHECK(row_version > 0),
        UNIQUE(child_intent_id, operation_kind),
        UNIQUE(id, account_id),
        CHECK((state='IN_FLIGHT' AND lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)
              OR (state!='IN_FLIGHT' AND lease_owner IS NULL AND lease_expires_at IS NULL)),
        FOREIGN KEY(child_intent_id, account_id)
            REFERENCES mgboost_child_user_intents(id, account_id) ON DELETE RESTRICT
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_mgboost_outbox_ready
        ON mgboost_outbox(state, next_attempt_at, id)
    """,
    """
    CREATE TABLE IF NOT EXISTS mgboost_outbox_attempt_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        outbox_id INTEGER NOT NULL,
        account_id INTEGER NOT NULL,
        attempt_no INTEGER NOT NULL CHECK(attempt_no > 0),
        event_type TEXT NOT NULL
            CHECK(event_type IN ('STARTED','SUCCEEDED','FAILED','RECONCILED')),
        outcome TEXT,
        remote_effect_verifier TEXT,
        safe_error_class TEXT,
        created_at INTEGER NOT NULL,
        UNIQUE(outbox_id, attempt_no, event_type),
        FOREIGN KEY(outbox_id, account_id)
            REFERENCES mgboost_outbox(id, account_id) ON DELETE RESTRICT
    )
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mgboost_alias_groups_no_update
        BEFORE UPDATE ON mgboost_legacy_alias_groups
        BEGIN SELECT RAISE(ABORT, 'legacy alias groups are immutable'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mgboost_alias_groups_no_delete
        BEFORE DELETE ON mgboost_legacy_alias_groups
        BEGIN SELECT RAISE(ABORT, 'legacy alias groups are immutable'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mgboost_aliases_no_update
        BEFORE UPDATE ON mgboost_legacy_account_aliases
        BEGIN SELECT RAISE(ABORT, 'legacy aliases are immutable'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mgboost_aliases_no_delete
        BEFORE DELETE ON mgboost_legacy_account_aliases
        BEGIN SELECT RAISE(ABORT, 'legacy aliases are immutable'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mgboost_child_identity_immutable
        BEFORE UPDATE OF public_id,account_id,slot_id,slot_generation_id,
                         slot_number,generation,source_alias_id,child_username,
                         source_contract_hash,created_at
        ON mgboost_child_user_intents
        BEGIN SELECT RAISE(ABORT, 'child intent identity is immutable'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mgboost_child_no_delete
        BEFORE DELETE ON mgboost_child_user_intents
        BEGIN SELECT RAISE(ABORT, 'child intent history is immutable'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mgboost_outbox_identity_immutable
        BEFORE UPDATE OF operation_id,account_id,child_intent_id,operation_kind,
                         idempotency_key_hash,request_hash,payload_json,created_at
        ON mgboost_outbox
        BEGIN SELECT RAISE(ABORT, 'outbox operation identity is immutable'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mgboost_outbox_no_delete
        BEFORE DELETE ON mgboost_outbox
        BEGIN SELECT RAISE(ABORT, 'outbox history is immutable'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mgboost_outbox_events_no_update
        BEFORE UPDATE ON mgboost_outbox_attempt_events
        BEGIN SELECT RAISE(ABORT, 'outbox attempt events are immutable'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mgboost_outbox_events_no_delete
        BEFORE DELETE ON mgboost_outbox_attempt_events
        BEGIN SELECT RAISE(ABORT, 'outbox attempt events are immutable'); END
    """,
)

SCHEMA_CHECKSUM = hashlib.sha256(
    "\n".join(statement.strip() for statement in _SCHEMA_STATEMENTS).encode("utf-8")
).hexdigest()

NEW_RUNTIME_TABLES = (
    "mgboost_legacy_alias_groups",
    "mgboost_legacy_account_aliases",
    "mgboost_child_user_intents",
    "mgboost_outbox",
    "mgboost_outbox_attempt_events",
)


def _verify(connection: sqlite3.Connection) -> None:
    required = {
        "mgboost_legacy_alias_groups": {"account_id", "mapping_key", "decision_ref"},
        "mgboost_legacy_account_aliases": {
            "id", "account_id", "legacy_username", "alias_role",
            "ownership_provenance", "legacy_status", "legacy_expiry",
            "observed_device_count", "observed_hwid_count", "evidence_json",
        },
        "mgboost_child_user_intents": {
            "id", "public_id", "account_id", "slot_id", "slot_generation_id",
            "slot_number", "generation", "source_alias_id", "child_username",
            "source_contract_hash", "desired_state", "observed_state",
            "uuid_verifier", "uuid_masked", "row_version",
            "shadowsocks_verifier", "shadowsocks_masked",
        },
        "mgboost_outbox": {
            "id", "operation_id", "account_id", "child_intent_id", "operation_kind",
            "state", "idempotency_key_hash", "request_hash", "payload_json",
            "attempts", "next_attempt_at", "lease_owner", "lease_expires_at",
        },
        "mgboost_outbox_attempt_events": {
            "id", "outbox_id", "account_id", "attempt_no", "event_type",
            "outcome", "remote_effect_verifier", "safe_error_class", "created_at",
        },
    }
    for table, columns in required.items():
        actual = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        if columns - actual:
            raise RuntimeError(f"PH3-03 prerequisite table {table} is incompatible")
    objects = {row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE name LIKE 'trg_mgboost_alias_%' "
        "OR name LIKE 'trg_mgboost_child_%' OR name LIKE 'trg_mgboost_outbox_%' "
        "OR name='ux_mgboost_alias_one_primary' "
        "OR name='ux_mgboost_generation_full_identity'"
    )}
    expected = {
        "ux_mgboost_alias_one_primary", "ux_mgboost_generation_full_identity",
        "trg_mgboost_alias_groups_no_update", "trg_mgboost_alias_groups_no_delete",
        "trg_mgboost_aliases_no_update", "trg_mgboost_aliases_no_delete",
        "trg_mgboost_child_identity_immutable", "trg_mgboost_child_no_delete",
        "trg_mgboost_outbox_identity_immutable", "trg_mgboost_outbox_no_delete",
        "trg_mgboost_outbox_events_no_update", "trg_mgboost_outbox_events_no_delete",
    }
    if not expected.issubset(objects):
        raise RuntimeError("PH3-03 prerequisite schema objects incomplete")


def apply_child_provisioning_schema(
    connection: sqlite3.Connection, *, now: int | None = None
) -> bool:
    connection.execute("PRAGMA foreign_keys=ON")
    timestamp = int(time.time()) if now is None else int(now)
    required_parents = (
        (SLOT_MIGRATION_ID, SLOT_SCHEMA_CHECKSUM),
        (INTERNAL_MIGRATION_ID, INTERNAL_SCHEMA_CHECKSUM),
        (PROVENANCE_MIGRATION_ID, PROVENANCE_SCHEMA_CHECKSUM),
    )
    try:
        connection.execute("BEGIN IMMEDIATE")
        for migration_id, checksum in required_parents:
            row = connection.execute(
                "SELECT schema_checksum FROM mgboost_schema_migrations WHERE migration_id=?",
                (migration_id,),
            ).fetchone()
            if not row or row[0] != checksum:
                raise RuntimeError("PH3-03 prerequisites require exact parent schemas")
        existing = connection.execute(
            "SELECT schema_checksum FROM mgboost_schema_migrations WHERE migration_id=?",
            (MIGRATION_ID,),
        ).fetchone()
        if existing:
            if existing[0] != SCHEMA_CHECKSUM:
                raise RuntimeError("PH3-03 prerequisite schema checksum mismatch")
            _verify(connection)
            connection.commit()
            return False
        for statement in _SCHEMA_STATEMENTS:
            connection.execute(statement)
        _verify(connection)
        connection.execute(
            "INSERT INTO mgboost_schema_migrations "
            "(migration_id,schema_checksum,applied_at) VALUES (?,?,?)",
            (MIGRATION_ID, SCHEMA_CHECKSUM, timestamp),
        )
        connection.commit()
        return True
    except Exception:
        connection.rollback()
        raise
