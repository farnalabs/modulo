"""Unit tests for modulo.core.pipeline_impact.

The module operates purely on the graph dict shape stored in
``PipelineSnapshot.graph_json``, so every oracle is unit-testable without a DB.
The FAR-402 P6 snapshot tests exercise the high-level diff/rollback flow; this
file pins the low-level port-signature and impact/breaking oracles directly,
covering the port-shape normalisation fallbacks (bare-string ports,
``schema_id``/``ref`` refs, dict-form port maps), the removed-port diff branch,
edge-port repoint normalisation, cycle-safe impact propagation, and the
target-side (input-direction) breaking checks.
"""

from modulo.core.pipeline_impact import (
    check_port_change_breaking,
    compute_port_change_impact,
    diff_edge_ports,
    diff_node_ports,
    node_port_signature,
    normalise_edge_port_delta,
)


class TestNodePortSignature:
    def test_bare_string_ports_have_no_schema_ref(self) -> None:
        sig = node_port_signature({"id": "x", "inputs": ["in1", "in2"], "outputs": ["raw1"]})
        assert sig == {"input": {"in1": None, "in2": None}, "output": {"raw1": None}}

    def test_port_dict_accepts_schema_id_ref_fallbacks(self) -> None:
        node = {
            "id": "x",
            "inputs": [
                {"name": "a", "schema_id": "sid"},
                {"name": "b", "ref": "refv"},
            ],
        }
        sig = node_port_signature(node)
        assert sig["input"] == {"a": "sid", "b": "refv"}

    def test_dict_port_map_drops_non_string_refs(self) -> None:
        node = {"id": "x", "outputs": {"o1": {"ref": "r"}, "o2": "s2"}}
        sig = node_port_signature(node)
        assert sig["output"] == {"o2": "s2"}

    def test_port_without_a_name_is_skipped(self) -> None:
        node = {"id": "x", "inputs": [{"schema_ref": "s"}, "named"]}
        assert node_port_signature(node)["input"] == {"named": None}

    def test_non_container_port_value_is_empty(self) -> None:
        assert node_port_signature({"id": "x", "inputs": "oops"}) == {"input": {}, "output": {}}


class TestDiffNodePorts:
    def test_removed_port_is_reported_with_old_ref(self) -> None:
        na = {"id": "a", "outputs": [{"name": "gone", "schema_ref": "s"}]}
        nb = {"id": "a", "outputs": [{"name": "kept", "schema_ref": "s"}]}
        changes = diff_node_ports(na, nb)
        by_port = {c["port"]: c for c in changes}
        assert by_port["gone"] == {
            "node_id": "a",
            "direction": "output",
            "port": "gone",
            "change": "removed",
            "old": "s",
            "new": None,
        }
        assert by_port["kept"]["change"] == "added"

    def test_unchanged_ports_produce_no_delta(self) -> None:
        node = {"id": "a", "outputs": [{"name": "keep", "schema_ref": "s1"}]}
        assert not diff_node_ports(node, dict(node))


class TestNormaliseEdgePortDelta:
    def test_both_repoints_are_attributed_to_endpoint_nodes(self) -> None:
        edge = {"source": "a", "target": "b"}
        ea = dict(edge, source_port="out1")
        eb = dict(edge, source_port="out2", target_port="in1")
        changes = normalise_edge_port_delta(edge, diff_edge_ports(ea, eb))
        assert changes == [
            {
                "node_id": "a",
                "direction": "output",
                "port": "out2",
                "change": "edge_source_port_repoint",
                "old": "out1",
                "new": "out2",
                "edge": {"source": "a", "target": "b"},
            },
            {
                "node_id": "b",
                "direction": "input",
                "port": "in1",
                "change": "edge_target_port_repoint",
                "old": None,
                "new": "in1",
                "edge": {"source": "a", "target": "b"},
            },
        ]

    def test_repoints_resolve_legacy_source_node_id_spelling(self) -> None:
        edge = {"source_node_id": "a", "target_node_id": "b"}
        changes = normalise_edge_port_delta(edge, {"source_port": {"old": "x", "new": "y"}})
        assert changes[0]["node_id"] == "a"
        assert changes[0]["edge"] == {"source": "a", "target": "b"}

    def test_empty_delta_produces_no_changes(self) -> None:
        assert not normalise_edge_port_delta({"source": "a", "target": "b"}, {})


class TestComputePortChangeImpact:
    def test_cyclic_graph_does_not_recurse_forever(self) -> None:
        graph = {
            "nodes": [{"id": "a"}, {"id": "b"}],
            "edges": [{"source": "a", "target": "b"}, {"source": "b", "target": "a"}],
        }
        assert compute_port_change_impact(graph, [{"node_id": "b"}]) == {"a", "b"}

    def test_impact_uses_source_node_id_edge_spelling(self) -> None:
        graph = {
            "nodes": [{"id": "a"}, {"id": "b"}],
            "edges": [{"source_node_id": "a", "target_node_id": "b"}],
        }
        assert compute_port_change_impact(graph, [{"node_id": "a"}]) == {"a", "b"}

    def test_bare_unknown_node_is_ignored(self) -> None:
        assert not compute_port_change_impact({"nodes": [], "edges": []}, [("ghost",)])


class TestCheckPortChangeBreaking:
    def test_removed_input_port_blocks_consuming_target_edge(self) -> None:
        graph_new = {
            "nodes": [{"id": "b", "inputs": []}],
            "edges": [{"source": "a", "target": "b", "target_port": "in"}],
        }
        findings = check_port_change_breaking(
            graph_new, [{"node_id": "b", "direction": "input", "port": "in", "change": "removed"}]
        )
        assert findings
        assert findings[0]["severity"] == "block"
        assert findings[0]["direction"] == "input"
        assert findings[0]["node_id"] == "b"

    def test_modified_input_port_is_warning_on_target_edge(self) -> None:
        graph_new = {
            "nodes": [{"id": "b", "inputs": [{"name": "in", "schema_ref": "s2"}]}],
            "edges": [{"source": "a", "target": "b", "target_port": "in"}],
        }
        findings = check_port_change_breaking(
            graph_new, [{"node_id": "b", "direction": "input", "port": "in", "change": "modified"}]
        )
        assert findings
        assert findings[0]["severity"] == "warning"

    def test_repoint_to_undeclared_port_blocks(self) -> None:
        graph_new = {"nodes": [{"id": "b", "inputs": [{"name": "in1", "schema_ref": "s"}]}]}
        findings = check_port_change_breaking(
            graph_new,
            [
                {
                    "node_id": "b",
                    "direction": "input",
                    "port": "newin",
                    "change": "edge_target_port_repoint",
                    "old": "in1",
                    "new": "newin",
                    "edge": {"source": "a", "target": "b"},
                }
            ],
        )
        assert findings
        assert findings[0]["severity"] == "block"

    def test_repoint_with_differing_schema_ref_is_warning(self) -> None:
        graph_new = {
            "nodes": [
                {
                    "id": "a",
                    "outputs": [
                        {"name": "out1", "schema_ref": "s1"},
                        {"name": "out2", "schema_ref": "s2"},
                    ],
                }
            ]
        }
        findings = check_port_change_breaking(
            graph_new,
            [
                {
                    "node_id": "a",
                    "direction": "output",
                    "port": "out2",
                    "change": "edge_source_port_repoint",
                    "old": "out1",
                    "new": "out2",
                    "edge": {"source": "a", "target": "b"},
                }
            ],
        )
        assert findings
        assert findings[0]["severity"] == "warning"

    def test_repoint_with_unknown_direction_is_safe(self) -> None:
        entry = {
            "node_id": "a",
            "direction": "sideways",
            "port": "p",
            "change": "edge_source_port_repoint",
            "edge": {},
        }
        assert not check_port_change_breaking({"nodes": []}, [entry])

    def test_no_consuming_edge_is_safe(self) -> None:
        graph_new = {"nodes": [{"id": "a", "outputs": []}], "edges": []}
        findings = check_port_change_breaking(
            graph_new, [{"node_id": "a", "direction": "output", "port": "res", "change": "removed"}]
        )
        assert not findings
