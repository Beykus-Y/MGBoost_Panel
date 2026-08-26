"""PH6-01 -- runtime topology allowlist/assertions.

Consumes the PH0-05 exact versioned baseline (`src/wl_topology.py`) against
a live observation and durably records the result. This is the allowlist
gate a future PH6-06 destructive enforcement decision must consult before
touching any real inbound state -- `require_topology_ok()` is that gate,
exposed now but not yet called by any live enforcement path (PH6-06 does
not exist yet).

Fetching the live observation itself (`fetch_live_topology_observation`)
is a thin, separately-testable wrapper around the already-existing
read-only `MarzbanClient.get_nodes`/`get_inbounds` calls -- no new Marzban
API surface, no mutation.
"""

from __future__ import annotations

import json
import sqlite3
import time

from .wl_topology import WL_TOPOLOGY_VERSION, diff_topology, observed_nodes_from_marzban, observed_tags_from_marzban


class TopologyMismatchError(RuntimeError):
    def __init__(self, diff):
        self.diff = diff
        super().__init__(
            f"WL topology mismatch (config_version={diff.config_version}): "
            f"missing_tags={sorted(diff.missing_tags)} "
            f"extra_wl_like_tags={sorted(diff.extra_wl_like_tags)} "
            f"missing_node_ids={sorted(diff.missing_node_ids)} "
            f"node_field_mismatches={diff.node_field_mismatches}"
        )


def fetch_live_topology_observation(marzban_client, admin_token):
    """Read-only: shape a live Marzban node/inbound query into
    (observed_tags, observed_nodes) for `diff_topology`."""
    nodes_payload = marzban_client.get_nodes(admin_token)
    inbounds_payload = marzban_client.get_inbounds(admin_token)
    return observed_tags_from_marzban(inbounds_payload), observed_nodes_from_marzban(nodes_payload)


class WLTopologyGuardStore:
    def __init__(self, connection: sqlite3.Connection, lock):
        self._conn = connection
        self._lock = lock

    def run_assertion(self, observed_tags, observed_nodes, *, now: int | None = None) -> dict:
        timestamp = int(time.time()) if now is None else int(now)
        diff = diff_topology(observed_tags, observed_nodes)
        with self._lock:
            self._conn.execute(
                "INSERT INTO mgboost_wl_topology_assertions "
                "(config_version, ok, missing_tags_json, extra_wl_like_tags_json, "
                "missing_node_ids_json, node_field_mismatches_json, checked_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    diff.config_version,
                    1 if diff.ok else 0,
                    json.dumps(sorted(diff.missing_tags)),
                    json.dumps(sorted(diff.extra_wl_like_tags)),
                    json.dumps(sorted(diff.missing_node_ids)),
                    json.dumps(list(diff.node_field_mismatches)),
                    timestamp,
                ),
            )
            self._conn.commit()
        return {
            "ok": diff.ok,
            "config_version": diff.config_version,
            "missing_tags": sorted(diff.missing_tags),
            "extra_wl_like_tags": sorted(diff.extra_wl_like_tags),
            "missing_node_ids": sorted(diff.missing_node_ids),
            "node_field_mismatches": list(diff.node_field_mismatches),
            "checked_at": timestamp,
        }

    def latest_assertion(self) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM mgboost_wl_topology_assertions ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        return {
            "ok": bool(row["ok"]),
            "config_version": row["config_version"],
            "missing_tags": json.loads(row["missing_tags_json"]),
            "extra_wl_like_tags": json.loads(row["extra_wl_like_tags_json"]),
            "missing_node_ids": json.loads(row["missing_node_ids_json"]),
            "node_field_mismatches": json.loads(row["node_field_mismatches_json"]),
            "checked_at": row["checked_at"],
        }

    def require_topology_ok(self) -> None:
        """Gate for any future destructive WL enforcement action (PH6-06).

        Fails closed: no assertion on record yet is treated the same as a
        mismatch -- a caller must never assume the topology is fine just
        because nobody has checked.
        """
        latest = self.latest_assertion()
        if latest is None or not latest["ok"] or latest["config_version"] != WL_TOPOLOGY_VERSION:
            from .wl_topology import TopologyDiff
            diff = TopologyDiff(
                config_version=latest["config_version"] if latest else "NONE",
                missing_tags=frozenset(latest["missing_tags"]) if latest else frozenset(),
                extra_wl_like_tags=frozenset(latest["extra_wl_like_tags"]) if latest else frozenset(),
                missing_node_ids=frozenset(latest["missing_node_ids"]) if latest else frozenset(),
                node_field_mismatches=tuple(tuple(m) for m in latest["node_field_mismatches"]) if latest else (),
            )
            raise TopologyMismatchError(diff)
