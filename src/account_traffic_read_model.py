"""Read-only account traffic detail for the current canonical billing window."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import time

from .wl_parent_pool import compute_parent_wl_pool
from .wl_topology import WL_NODE_IDS


def _period(connection, account_id: int, now: int) -> dict | None:
    wl = connection.execute(
        "SELECT id,starts_at,ends_at,sequence_no FROM mgboost_wl_periods "
        "WHERE account_id=? AND starts_at<=? AND ends_at>? ORDER BY id DESC LIMIT 2",
        (account_id, now, now),
    ).fetchall()
    if len(wl) != 1 and wl:
        return None
    if wl:
        return {"kind": "WL", **dict(wl[0])}
    terms = connection.execute(
        "SELECT id,starts_at,ends_at,sequence_no FROM mgboost_subscription_terms "
        "WHERE account_id=? AND starts_at<=? AND ends_at>? "
        "ORDER BY id DESC LIMIT 2", (account_id, now, now),
    ).fetchall()
    if len(terms) != 1:
        return None
    return {"kind": "TERM", **dict(terms[0])}


def _usage(marzban, token: str, username: str, start: str, end: str) -> dict:
    try:
        payload = marzban.get_user_usage(username, token, start=start, end=end)
        if not isinstance(payload, dict) or not isinstance(payload.get("usages"), list):
            raise ValueError("invalid Marzban usage response")
        nodes = []
        for item in payload["usages"]:
            if not isinstance(item, dict) or type(item.get("used_traffic")) is not int or item["used_traffic"] < 0:
                raise ValueError("invalid Marzban usage item")
            nodes.append({"node_id": item.get("node_id"), "node_name": item.get("node_name"),
                          "bytes": item["used_traffic"]})
        return {"available": True, "bytes": sum(node["bytes"] for node in nodes), "nodes": nodes}
    except Exception:
        return {"available": False, "bytes": None, "nodes": []}


def account_traffic_detail(db, account_id: int, marzban, token: str, *, now: int | None = None) -> dict | None:
    """Keep unavailable remote totals distinct from genuine zero usage."""
    timestamp = int(time.time()) if now is None else int(now)
    if db.accounts.get_account(account_id) is None:
        return None
    connection = db._conn
    period = _period(connection, account_id, timestamp)
    if period is None:
        return {"period": None, "reason": "NO_CURRENT_PERIOD", "slots": [], "legacy": [],
                "total": None, "wl": None, "measured_at": timestamp}

    slots = [dict(row) for row in connection.execute(
        "SELECT id,slot_number FROM mgboost_device_slots WHERE account_id=? ORDER BY slot_number",
        (account_id,),
    ).fetchall()]
    children = [dict(row) for row in connection.execute(
        "SELECT c.id,c.slot_id,c.child_username,g.status AS generation_status "
        "FROM mgboost_child_user_intents c JOIN mgboost_device_slot_generations g "
        "ON g.id=c.slot_generation_id AND g.account_id=c.account_id "
        "WHERE c.account_id=? AND c.observed_state!='NOT_CREATED' "
        "AND g.claimed_at<? AND (g.ended_at IS NULL OR g.ended_at>=?) "
        "ORDER BY c.id", (account_id, period["ends_at"], period["starts_at"]),
    ).fetchall()]
    aliases = [row[0] for row in connection.execute(
        "SELECT legacy_username FROM mgboost_legacy_account_aliases WHERE account_id=? ORDER BY id",
        (account_id,),
    ).fetchall()]
    wl = None
    wl_slots = {}
    if period["kind"] == "WL":
        wl = compute_parent_wl_pool(connection, account_id=account_id, wl_period_id=period["id"])
        nodes = sorted(WL_NODE_IDS)
        placeholders = ",".join("?" for _ in nodes)
        rows = connection.execute(
            "SELECT c.slot_id,s.node_id,SUM(s.bytes_delta) AS bytes "
            "FROM mgboost_wl_usage_samples s JOIN mgboost_child_user_intents c "
            "ON c.id=s.child_intent_id AND c.account_id=s.account_id "
            f"WHERE s.account_id=? AND s.wl_period_id=? AND s.node_id IN ({placeholders}) "
            "GROUP BY c.slot_id,s.node_id",
            (account_id, period["id"], *nodes),
        ).fetchall()
        for row in rows:
            wl_slots.setdefault(row["slot_id"], []).append({"node_id": row["node_id"], "bytes": row["bytes"]})
        wl["last_collected_at"] = connection.execute(
            "SELECT MAX(last_collected_at) FROM mgboost_wl_usage_samples "
            "WHERE account_id=? AND wl_period_id=?", (account_id, period["id"]),
        ).fetchone()[0]

    start = datetime.fromtimestamp(period["starts_at"], timezone.utc).isoformat()
    end = datetime.fromtimestamp(timestamp, timezone.utc).isoformat()
    usernames = list(dict.fromkeys([row["child_username"] for row in children] + aliases))
    with ThreadPoolExecutor(max_workers=4) as executor:
        values = list(executor.map(lambda username: _usage(marzban, token, username, start, end), usernames))
    usage = dict(zip(usernames, values))

    slot_rows = []
    for slot in slots:
        members = [row for row in children if row["slot_id"] == slot["id"]]
        results = [usage[row["child_username"]] for row in members]
        available = all(row["available"] for row in results)
        by_node = {}
        for result in results:
            for node in result["nodes"]:
                key = (node["node_id"], node["node_name"])
                by_node[key] = by_node.get(key, 0) + node["bytes"]
        slot_rows.append({"slot_number": slot["slot_number"], "children": len(members),
                          "historical_children": sum(row["generation_status"] != "ACTIVE" for row in members),
                          "traffic_bytes": sum(row["bytes"] or 0 for row in results) if available else None,
                          "traffic_partial_bytes": sum(row["bytes"] or 0 for row in results),
                          "nodes": [{"node_id": key[0], "node_name": key[1], "bytes": value}
                                    for key, value in sorted(by_node.items(), key=lambda item: str(item[0]))],
                          "wl_bytes": sum(row["bytes"] for row in wl_slots.get(slot["id"], [])) if wl else None,
                          "wl_nodes": wl_slots.get(slot["id"], [])})
    legacy = [{"username": username, "traffic_bytes": usage[username]["bytes"],
               "nodes": usage[username]["nodes"]} for username in aliases]
    all_results = [usage[username] for username in usernames]
    complete = all(row["available"] for row in all_results)
    return {"period": period, "reason": None, "slots": slot_rows, "legacy": legacy,
            "total": sum(row["bytes"] or 0 for row in all_results) if complete else None,
            "partial_total": sum(row["bytes"] or 0 for row in all_results),
            "failed_sources": sum(not row["available"] for row in all_results),
            "wl": wl, "measured_at": timestamp}
