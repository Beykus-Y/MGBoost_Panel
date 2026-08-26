from src.wl_topology import (
    WL_INBOUND_TAGS,
    WL_NODE_IDS,
    WL_NODES,
    WL_TOPOLOGY_VERSION,
    diff_topology,
    observed_nodes_from_marzban,
    observed_tags_from_marzban,
)


def _clean_observation():
    tags = set(WL_INBOUND_TAGS)
    nodes = {n["id"]: {"role": n["role"], "address": n["address"], "usage_coefficient": n["usage_coefficient"]}
             for n in WL_NODES}
    return tags, nodes


def test_exact_baseline_is_twelve_tags_and_two_nodes():
    assert len(WL_INBOUND_TAGS) == 12
    assert len(WL_NODES) == 2
    assert WL_NODE_IDS == {4, 7}


def test_clean_live_state_matches_exactly():
    tags, nodes = _clean_observation()
    # extra real (non-WL) nodes also present live -- must not affect the diff.
    nodes[3] = {"role": "Estonia", "address": "150.241.74.147", "usage_coefficient": 1.0}
    diff = diff_topology(tags, nodes)
    assert diff.ok
    assert diff.config_version == WL_TOPOLOGY_VERSION


def test_missing_tag_detected():
    tags, nodes = _clean_observation()
    tags.discard("wl-tcp-smart-yandex-maps")
    diff = diff_topology(tags, nodes)
    assert not diff.ok
    assert diff.missing_tags == {"wl-tcp-smart-yandex-maps"}


def test_extra_wl_like_tag_is_flagged_not_auto_included():
    tags, nodes = _clean_observation()
    tags.add("wl-tcp-direct-newpop")
    diff = diff_topology(tags, nodes)
    assert not diff.ok
    assert diff.extra_wl_like_tags == {"wl-tcp-direct-newpop"}
    # never silently treated as WL
    assert "wl-tcp-direct-newpop" not in WL_INBOUND_TAGS


def test_renamed_tag_shows_as_one_missing_one_extra():
    tags, nodes = _clean_observation()
    tags.discard("wl-tcp-direct")
    tags.add("wl-tcp-direct-v2")
    diff = diff_topology(tags, nodes)
    assert diff.missing_tags == {"wl-tcp-direct"}
    assert diff.extra_wl_like_tags == {"wl-tcp-direct-v2"}


def test_stale_non_wl_like_host_tag_never_flagged():
    tags, nodes = _clean_observation()
    tags.add("some-other-inbound")
    diff = diff_topology(tags, nodes)
    assert diff.ok  # not "wl"-prefixed -> not flagged at all


def test_missing_node_detected():
    tags, nodes = _clean_observation()
    del nodes[7]
    diff = diff_topology(tags, nodes)
    assert not diff.ok
    assert diff.missing_node_ids == {7}


def test_node_field_mismatch_detected_coefficient_drift():
    tags, nodes = _clean_observation()
    nodes[4]["usage_coefficient"] = 2.0
    diff = diff_topology(tags, nodes)
    assert not diff.ok
    assert (4, "usage_coefficient", 1.0, 2.0) in diff.node_field_mismatches


def test_node_field_mismatch_detected_role_rename():
    tags, nodes = _clean_observation()
    nodes[4]["role"] = "Some New Name"
    diff = diff_topology(tags, nodes)
    assert not diff.ok
    assert any(m[0] == 4 and m[1] == "role" for m in diff.node_field_mismatches)


def test_non_wl_node_never_included_by_name_containing_wl():
    # a hypothetical unrelated node named "WL-adjacent" must not affect the
    # exact-id-based diff at all.
    tags, nodes = _clean_observation()
    nodes[99] = {"role": "WL-adjacent-but-not-real", "address": "1.2.3.4", "usage_coefficient": 1.0}
    diff = diff_topology(tags, nodes)
    assert diff.ok


def test_observed_shapers_from_real_marzban_payload_shapes():
    nodes_payload = [
        {"id": 4, "name": "RU ONLY WL", "address": "84.201.130.217", "usage_coefficient": 1.0},
        {"id": 7, "name": "Selectel", "address": "5.178.85.8", "usage_coefficient": 1.0},
        {"id": 3, "name": "Estonia", "address": "150.241.74.147", "usage_coefficient": 1.0},
    ]
    inbounds_payload = {
        "vless": [
            {"tag": "wl-tcp-direct"},
            {"tag": "wl-selec-grpc-smart"},
            {"tag": "not-wl-inbound"},
        ]
    }
    nodes = observed_nodes_from_marzban(nodes_payload)
    tags = observed_tags_from_marzban(inbounds_payload)
    assert nodes[4] == {"role": "RU ONLY WL", "address": "84.201.130.217", "usage_coefficient": 1.0}
    assert tags == {"wl-tcp-direct", "wl-selec-grpc-smart", "not-wl-inbound"}
