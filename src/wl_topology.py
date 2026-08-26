"""PH0-05 -- exact versioned WL topology.

This is pure data plus a pure diff function: the exact live WL inbound tag
set and the exact two physical WL nodes, confirmed 2026-08-26 by directly
querying production Marzban (`GET /api/nodes`, `GET /api/inbounds`) through
the already-existing read-only `MarzbanClient.get_nodes`/`get_inbounds`
methods -- not invented, not derived from any `wl` substring search.

Evidence (2026-08-26, live production Marzban):
  - `GET /api/nodes` returned 5 real nodes total. Only two of them actually
    serve a `wl-*` inbound, per the `hosts` table's `address` column tying
    each live `wl-*` inbound tag to a physical node address: `84.201.130.217`
    (node id 4, name "RU ONLY WL", serves the `wl-tcp-*` tag family) and
    `5.178.85.8` (node id 7, name "Selectel", serves the `wl-selec-grpc-*`
    tag family). The other three nodes (Estonia id 3, Beget id 6,
    germanyp2 id 8) carry no WL inbound and are never WL nodes -- they must
    never be included by a node-name substring match (node id 4's own name
    literally contains "WL", which is exactly the kind of thing fuzzy
    matching would get right by accident and wrong the next time a node is
    renamed).
  - `GET /api/inbounds` returned exactly the same 12 live `wl-*` tags
    `ROADMAP.md` PH0-05 already recorded as the 2026-08-23 baseline. Six
    additional `wl-selec-tcp-*` rows exist only in the Marzban `hosts` table
    (stale references to a since-removed inbound) and correctly do NOT
    appear in `get_inbounds()` -- confirming the "stale hosts excluded"
    requirement is inherent to using live inbound config as the source of
    truth, not something that needs its own stale-list.

No fuzzy `wl` substring matching is used anywhere in this module: node and
tag membership are decided by exact id / exact string membership in the
frozensets below, nothing else.
"""

from __future__ import annotations

from dataclasses import dataclass, field


WL_TOPOLOGY_VERSION = "2026-08-26-v1"

WL_INBOUND_TAGS = frozenset({
    "wl-selec-grpc-direct",
    "wl-selec-grpc-direct-5post",
    "wl-selec-grpc-direct-yandex-maps",
    "wl-selec-grpc-smart",
    "wl-selec-grpc-smart-5post",
    "wl-selec-grpc-smart-yandex-maps",
    "wl-tcp-direct",
    "wl-tcp-direct-5post",
    "wl-tcp-direct-yandex-maps",
    "wl-tcp-smart",
    "wl-tcp-smart-5post",
    "wl-tcp-smart-yandex-maps",
})

# `role` is each node's own live Marzban `name` field -- real data, not an
# invented taxonomy label.
WL_NODES = (
    {"id": 4, "role": "RU ONLY WL", "address": "84.201.130.217", "usage_coefficient": 1.0},
    {"id": 7, "role": "Selectel", "address": "5.178.85.8", "usage_coefficient": 1.0},
)

WL_NODE_IDS = frozenset(node["id"] for node in WL_NODES)
_WL_NODES_BY_ID = {node["id"]: node for node in WL_NODES}


@dataclass(frozen=True)
class TopologyDiff:
    config_version: str
    missing_tags: frozenset = field(default_factory=frozenset)
    extra_wl_like_tags: frozenset = field(default_factory=frozenset)
    missing_node_ids: frozenset = field(default_factory=frozenset)
    node_field_mismatches: tuple = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return not (
            self.missing_tags
            or self.extra_wl_like_tags
            or self.missing_node_ids
            or self.node_field_mismatches
        )


def diff_topology(observed_tags, observed_nodes) -> TopologyDiff:
    """Compare live Marzban state against the exact versioned baseline.

    `observed_tags`: iterable of live inbound tag strings (`get_inbounds()`
    shape, already flattened to tag strings by the caller).
    `observed_nodes`: mapping of node id -> {"role"/"name", "address",
    "usage_coefficient"} (`get_nodes()` shape, already keyed by id by the
    caller).

    Exact membership only: an inbound tag is WL iff it is a literal member
    of `WL_INBOUND_TAGS`; a node is a WL node iff its id is a literal
    member of `WL_NODE_IDS`. `extra_wl_like_tags` is alert-only evidence of
    live drift (a tag that looks WL-shaped but isn't on the allowlist) --
    it is never auto-included as WL.
    """
    observed_tags = set(observed_tags)
    missing_tags = frozenset(WL_INBOUND_TAGS - observed_tags)
    extra_wl_like_tags = frozenset(
        tag for tag in observed_tags
        if tag not in WL_INBOUND_TAGS and tag.lower().startswith("wl")
    )

    missing_node_ids = frozenset(WL_NODE_IDS - set(observed_nodes.keys()))

    mismatches = []
    for node_id, expected in _WL_NODES_BY_ID.items():
        observed = observed_nodes.get(node_id)
        if observed is None:
            continue
        for field_name, expected_key in (
            ("role", "role"), ("address", "address"), ("usage_coefficient", "usage_coefficient"),
        ):
            observed_value = observed.get(expected_key, observed.get("name"))
            if field_name == "role" and observed_value is None:
                observed_value = observed.get("name")
            if observed_value != expected[field_name]:
                mismatches.append((node_id, field_name, expected[field_name], observed_value))

    return TopologyDiff(
        config_version=WL_TOPOLOGY_VERSION,
        missing_tags=missing_tags,
        extra_wl_like_tags=extra_wl_like_tags,
        missing_node_ids=missing_node_ids,
        node_field_mismatches=tuple(mismatches),
    )


def observed_nodes_from_marzban(nodes_payload) -> dict:
    """Shape a live `MarzbanClient.get_nodes()` list into `observed_nodes`."""
    result = {}
    for node in nodes_payload:
        result[int(node["id"])] = {
            "role": node.get("name"),
            "address": node.get("address"),
            "usage_coefficient": node.get("usage_coefficient"),
        }
    return result


def observed_tags_from_marzban(inbounds_payload) -> frozenset:
    """Shape a live `MarzbanClient.get_inbounds()` dict into a flat tag set."""
    tags = set()
    if isinstance(inbounds_payload, dict):
        for entries in inbounds_payload.values():
            for entry in entries:
                tag = entry.get("tag")
                if tag:
                    tags.add(tag)
    return frozenset(tags)
