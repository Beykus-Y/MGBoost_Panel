"""Read-only unified account mutation/audit timeline for the admin panel.

Aggregates the already-existing immutable per-domain evidence stores into one
account-scoped read model (the PH7-08 direction implemented as pure
presentation -- no second audit framework, no writers). Raw security
credentials never enter an entry: token/UUID/HWID verifiers and key hashes
are structurally excluded by column selection, and every detail dict passes
through ``_scrub`` before leaving this module.
"""

from __future__ import annotations

import json

_DEFAULT_LIMIT_PER_SOURCE = 20
_MAX_TOTAL_ENTRIES = 80

_DROP_KEY_MARKERS = (
    "token", "secret", "verifier", "hwid", "uuid", "bearer", "credential_hash",
    "idempotency_key", "request_hash", "key_hash", "password",
)


def _scrub(detail: dict) -> dict:
    """Keep only bounded scalar values; drop anything credential-shaped."""
    safe: dict = {}
    for raw_key, value in detail.items():
        key = str(raw_key)
        lowered = key.lower()
        if any(marker in lowered for marker in _DROP_KEY_MARKERS):
            continue
        if isinstance(value, bool) or isinstance(value, int):
            safe[key] = value
        elif isinstance(value, str):
            text = value.strip()
            if 0 < len(text) <= 300:
                safe[key] = text
    return safe


def _json_scalar_fields(raw) -> dict:
    """Flatten a stored JSON payload to its bounded scalar subset."""
    if not isinstance(raw, str):
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return _scrub({key: value for key, value in data.items()
                   if isinstance(value, (bool, int, str))})


def account_timeline(
    db, account_id: int, *, limit_per_source: int = _DEFAULT_LIMIT_PER_SOURCE,
) -> dict:
    account_id = int(account_id)
    entries: list[dict] = []

    def emit(ts, source, kind, label, detail):
        try:
            timestamp = int(ts)
        except (TypeError, ValueError):
            return
        entries.append({
            "ts": timestamp,
            "source": source,
            "kind": kind,
            "label": label,
            "detail": _scrub(detail),
        })

    conn = db._conn

    # PH3-09 canonical entitlement mutations (incl. renewals/packages/admin).
    for row in conn.execute(
        "SELECT created_at,operation,payment_channel,mutation_source,actor_type,"
        "actor_ref,reason,external_reference,before_json,after_json "
        "FROM mgboost_entitlement_mutations WHERE account_id=? "
        "ORDER BY created_at DESC,id DESC LIMIT ?", (account_id, limit_per_source),
    ).fetchall():
        detail = {
            "operation": row["operation"], "payment_channel": row["payment_channel"],
            "mutation_source": row["mutation_source"], "actor_type": row["actor_type"],
            **_json_scalar_fields(row["before_json"]),
            **_json_scalar_fields(row["after_json"]),
        }
        if row["actor_ref"]:
            detail["actor_ref"] = row["actor_ref"]
        if row["reason"]:
            detail["reason"] = row["reason"]
        if row["external_reference"]:
            detail["external_reference"] = row["external_reference"]
        emit(row["created_at"], "ENTITLEMENT_MUTATION", row["operation"],
             f"Изменение entitlement · {row['mutation_source']}", detail)

    # Canonical payment records (provenance; covers Stars+external+grants).
    for row in conn.execute(
        "SELECT created_at,payment_channel,record_status,amount_minor,currency,"
        "payment_method,external_reference,actor_type FROM mgboost_payment_records "
        "WHERE account_id=? ORDER BY created_at DESC,id DESC LIMIT ?",
        (account_id, limit_per_source),
    ).fetchall():
        amount = row["amount_minor"]
        label = row["currency"] and isinstance(amount, int) and f"{amount} {row['currency']}" or None
        emit(row["created_at"], "PAYMENT_RECORD", row["record_status"],
             f"Платёж ({row['payment_channel']}){': ' + label if label else ''}", {
                 "payment_channel": row["payment_channel"], "record_status": row["record_status"],
                 "amount_minor": amount if isinstance(amount, int) else None,
                 "payment_method": row["payment_method"],
                 "external_reference": row["external_reference"],
                 "actor_type": row["actor_type"],
             })

    # Manual payments lifecycle + audited edits + application + sync state.
    manual = db.manual_payments
    try:
        records = manual.list_records(account_id=account_id, limit=limit_per_source)
    except Exception:
        records = []
    for record in records:
        rid = record.get("id")
        base = {"public_id": record.get("public_id"), "kind": record.get("kind"),
                "status": record.get("status")}
        label = f"Ручной платёж {record.get('public_id')}"
        emit(record.get("created_at"), "MANUAL_PAYMENT", "CREATED",
             f"{label} · создан (PENDING)", {
                 **base, "plan_code_snapshot": record.get("plan_code_snapshot"),
                 "package_sku_snapshot": record.get("package_sku_snapshot"),
                 "duration_days_snapshot": record.get("duration_days_snapshot"),
                 "expected_amount_minor": record.get("expected_amount_minor"),
                 "payment_method": record.get("payment_method"),
                 "external_reference": record.get("external_reference"),
             })
        if record.get("status") == "CANCELLED":
            emit(record.get("updated_at"), "MANUAL_PAYMENT", "CANCELLED",
                 f"{label} · отменён", base)
        if record.get("status") == "MANUAL_REVIEW":
            emit(record.get("updated_at"), "MANUAL_PAYMENT", "MANUAL_REVIEW",
                 f"{label} · ручная проверка", base)
        if record.get("status") == "APPLIED":
            application = None
            try:
                application = manual.get_application(rid)
            except Exception:
                pass
            detail = dict(base)
            if application:
                detail.update({
                    "applied_operation": application.get("applied_operation"),
                    "applied_expiry": application.get("applied_expiry"),
                    "entitlement_mutation_id": application.get("entitlement_mutation_id"),
                })
            emit(application.get("applied_at") if application else record.get("updated_at"),
                 "MANUAL_PAYMENT", "APPLIED", f"{label} · применён", detail)

    for row in conn.execute(
        "SELECT e.created_at,e.edit_kind,e.reason,e.actor_ref,e.before_json,e.after_json "
        "FROM mgboost_manual_payment_edits e WHERE e.account_id=? "
        "ORDER BY e.created_at DESC,e.id DESC LIMIT ?", (account_id, limit_per_source),
    ).fetchall():
        before = _json_scalar_fields(row["before_json"])
        after = _json_scalar_fields(row["after_json"])
        changed = [
            f"{field}: «{before.get(field, '')}» → «{after.get(field, '')}»"
            for field in sorted(set(before) | set(after))
            if before.get(field) != after.get(field)
        ]
        emit(row["created_at"], "MANUAL_PAYMENT_EDIT", (row["edit_kind"] or "EDIT").upper(),
             f"Правка ручного платежа ({row['edit_kind']}): {', '.join(changed[:4])}", {
                 "edit_kind": row["edit_kind"],
                 "changed_fields": ", ".join(changed)[:400],
                 "actor_ref": row["actor_ref"], "reason": row["reason"],
             })

    # Device revoke/free/rebind lifecycle operations.
    for row in conn.execute(
        "SELECT o.updated_at,o.operation_kind,o.state,o.reason,o.attempts,"
        "o.last_error_class,g.slot_number FROM mgboost_child_lifecycle_operations o "
        "JOIN mgboost_child_user_intents c ON c.id=o.old_child_intent_id "
        "JOIN mgboost_device_slot_generations g ON g.id=c.slot_generation_id "
        "WHERE o.account_id=? ORDER BY o.updated_at DESC,o.id DESC LIMIT ?",
        (account_id, limit_per_source),
    ).fetchall():
        kind_ru = {"REVOKE": "Отзыв устройства", "FREE": "Освобождение слота",
                   "REBIND": "Перепривязка слота"}.get(row["operation_kind"], row["operation_kind"])
        emit(row["updated_at"], "DEVICE_LIFECYCLE", f"{row['operation_kind']}_{row['state']}",
             f"{kind_ru} (слот {row['slot_number']}) · {row['state']}", {
                 "slot_number": row["slot_number"], "operation_kind": row["operation_kind"],
                 "state": row["state"], "attempts": row["attempts"],
                 "last_error_class": row["last_error_class"], "reason": row["reason"],
             })

    # Migration binding events.
    for row in conn.execute(
        "SELECT ev.created_at,ev.event_type,ev.from_state,ev.to_state,ev.safe_error_class,"
        "ev.reason FROM mgboost_migration_binding_events ev WHERE ev.account_id=? "
        "ORDER BY ev.created_at DESC,ev.id DESC LIMIT ?", (account_id, limit_per_source),
    ).fetchall():
        emit(row["created_at"], "MIGRATION_BINDING", row["event_type"],
             f"Миграция · {row['event_type']}", {
                 "from_state": row["from_state"], "to_state": row["to_state"],
                 "safe_error_class": row["safe_error_class"], "reason": row["reason"],
             })

    # Legacy grace period events.
    for row in conn.execute(
        "SELECT created_at,event_type,from_end_at,to_end_at,actor_ref,reason "
        "FROM mgboost_legacy_grace_events WHERE account_id=? "
        "ORDER BY created_at DESC,id DESC LIMIT ?", (account_id, limit_per_source),
    ).fetchall():
        emit(row["created_at"], "LEGACY_GRACE", row["event_type"],
             f"Grace-период · {row['event_type']}", {
                 "from_end_at": row["from_end_at"], "to_end_at": row["to_end_at"],
                 "actor_ref": row["actor_ref"], "reason": row["reason"],
             })

    # Opaque subscription credential events (no verifier columns selected).
    for row in conn.execute(
        "SELECT created_at,event_type,actor_ref,reason FROM "
        "mgboost_subscription_credential_events WHERE account_id=? "
        "ORDER BY created_at DESC,id DESC LIMIT ?", (account_id, limit_per_source),
    ).fetchall():
        emit(row["created_at"], "SUBSCRIPTION_CREDENTIAL", row["event_type"],
             f"Credential · {row['event_type']}", {
                 "actor_ref": row["actor_ref"], "reason": row["reason"],
             })

    # Telegram ownership rebind events.
    for row in conn.execute(
        "SELECT ev.created_at,ev.event_type,ev.safe_error_class,o.mode,"
        "o.expected_old_telegram_id AS old_tg,o.new_telegram_id AS new_tg,o.reason "
        "FROM mgboost_ownership_rebind_events ev JOIN mgboost_ownership_rebind_operations o "
        "ON o.id=ev.rebind_operation_id WHERE ev.account_id=? "
        "ORDER BY ev.created_at DESC,ev.id DESC LIMIT ?", (account_id, limit_per_source),
    ).fetchall():
        emit(row["created_at"], "OWNERSHIP_REBIND", row["event_type"],
             f"Rebind владельца Telegram ({row['mode']}) · {row['event_type']}", {
                 "mode": row["mode"], "old_telegram_id": row["old_tg"],
                 "new_telegram_id": row["new_tg"], "safe_error_class": row["safe_error_class"],
                 "reason": row["reason"],
             })

    entries.sort(key=lambda entry: (-entry["ts"], entry["source"], entry["kind"]))
    truncated = len(entries) > _MAX_TOTAL_ENTRIES
    return {
        "entries": entries[:_MAX_TOTAL_ENTRIES],
        "truncated": truncated,
        "sources_covered": [
            "mgboost_entitlement_mutations", "mgboost_payment_records",
            "manual_payments(+edits/applications)", "child_lifecycle_operations",
            "migration_binding_events", "legacy_grace_events",
            "subscription_credential_events", "ownership_rebind_events",
        ],
    }
