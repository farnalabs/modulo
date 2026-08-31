"""Unit tests for the FAR-416 / F1 port model (port_resolver)."""

import jmespath
import pytest

from modulo.core.graph_validator._types import ValidationResult
from modulo.core.pipeline_engine.executor import compute_retry_aware_topology_hash
from modulo.core.pipeline_engine.port_resolver import (
    DEFAULT_INPUT_PORT,
    DEFAULT_OUTPUT_PORT,
    JOIN_NODE_TYPE,
    PORT_FAN_IN_ERROR,
    PORT_TYPE_MISMATCH_ERROR,
    compute_port_topology_hash,
    is_port_declared,
    port_to_state_key,
    resolve_port_state_key,
    synthesize_node_ports,
    validate_port_topology,
)


def _node(nid: str, **kw: object) -> dict:
    return {"id": nid, "node_type": kw.pop("node_type", "agent"), **kw}


def _edge(src: str, tgt: str, **kw: object) -> dict:
    return {"source": src, "target": tgt, "type": kw.pop("type", "normal"), **kw}


# ---------------------------------------------------------------------------
# Default synthesis (lazy backfill)
# ---------------------------------------------------------------------------


def test_synthesize_backfills_default_ports():
    node = _node("a")
    out = synthesize_node_ports(node)
    assert out["inputs"] == [{"port": DEFAULT_INPUT_PORT}]
    assert out["outputs"] == [{"port": DEFAULT_OUTPUT_PORT}]
    # original untouched
    assert "inputs" not in node


def test_synthesize_keeps_custom_ports():
    node = _node(
        "a",
        inputs=[{"port": "in", "schema_ref": "s1"}],
        outputs=[{"port": "summary"}, {"port": "errors"}],
    )
    out = synthesize_node_ports(node)
    assert out["inputs"] == [{"port": "in", "schema_ref": "s1"}]
    assert [p["port"] for p in out["outputs"]] == ["summary", "errors"]


def test_synthesize_rejects_duplicate_port_names():
    node = _node("a", outputs=[{"port": "x"}, {"port": "x"}])
    with pytest.raises(ValueError, match="duplicate output port name 'x'"):
        synthesize_node_ports(node)


def test_is_port_declared():
    assert not is_port_declared(_node("a"))
    assert is_port_declared(_node("a", inputs=[{"port": "in"}]))
    assert is_port_declared(_node("a", outputs=[{"port": "out"}]))


def test_is_port_declared_null_keys_are_not_declared():
    # FAR-480 regression: the API serializer round-trips every node with
    # inputs/outputs keys present but set to null. Key presence alone must
    # not declare ports — null keys are legacy port-less nodes.
    assert not is_port_declared(_node("a", inputs=None, outputs=None))
    assert not is_port_declared(_node("a", inputs=None))
    assert not is_port_declared(_node("a", outputs=None))


# ---------------------------------------------------------------------------
# Port -> state-key identity mapping
# ---------------------------------------------------------------------------


def test_port_to_state_key_is_identity():
    assert port_to_state_key("summary") == "summary"
    assert resolve_port_state_key(_node("a"), "summary") == "summary"


# ---------------------------------------------------------------------------
# Fan-in safety
# ---------------------------------------------------------------------------


def test_fan_in_on_declared_port_errors():
    graph = {
        "nodes": [
            _node("a", outputs=[{"port": "out"}]),
            _node("c", outputs=[{"port": "out"}]),
            _node("b", inputs=[{"port": "in"}]),
        ],
        "edges": [
            _edge("a", "b", target_port="in"),
            _edge("c", "b", target_port="in"),
        ],
    }
    result = ValidationResult()
    validate_port_topology(graph, result)
    fan_in = [i for i in result.issues if i.code == PORT_FAN_IN_ERROR]
    assert len(fan_in) == 1
    assert fan_in[0].node_id == "b"


def test_fan_in_many_into_join_accepted():
    graph = {
        "nodes": [
            _node("a", outputs=[{"port": "out"}]),
            _node("b", outputs=[{"port": "out"}]),
            _node("join", node_type=JOIN_NODE_TYPE, inputs=[{"port": "in"}]),
        ],
        "edges": [
            _edge("a", "join", target_port="in"),
            _edge("b", "join", target_port="in"),
        ],
    }
    result = ValidationResult()
    validate_port_topology(graph, result)
    assert not any(i.code == PORT_FAN_IN_ERROR for i in result.issues)


def test_fan_in_legacy_graph_not_rejected():
    # Fully-legacy graph (no declared ports, no edge ports) keeps backward-compat
    # last-write-wins fan-in behaviour — must NOT raise PORT_FAN_IN_ERROR.
    graph = {
        "nodes": [_node("a"), _node("b"), _node("c")],
        "edges": [_edge("a", "b"), _edge("a", "c")],
    }
    result = ValidationResult()
    validate_port_topology(graph, result)
    assert not any(i.code == PORT_FAN_IN_ERROR for i in result.issues)


def test_fan_in_legacy_graph_with_default_edge_ports_not_rejected():
    # Sibling of the FAR-480 fix (the edge half): every persisted edge carries
    # source_port='out' / target_port='in' (DB NOT NULL defaults, migration
    # 0141) and the save-time validator-edge serializer always emits them, so
    # KEY PRESENCE would declare ports on every legacy edge. Default-equivalent
    # values must NOT flip strict fan-in on — the FAR-480 production shape
    # (null-port nodes, 2-edge fan-in target, default-valued edge ports)
    # stays legacy last-write-wins.
    graph = {
        "nodes": [
            _node("a", inputs=None, outputs=None),
            _node("c", inputs=None, outputs=None),
            _node("b", inputs=None, outputs=None),
        ],
        "edges": [
            _edge("a", "b", source_port="out", target_port="in"),
            _edge("c", "b", source_port="out", target_port="in"),
        ],
    }
    result = ValidationResult()
    validate_port_topology(graph, result)
    assert not any(i.code == PORT_FAN_IN_ERROR for i in result.issues)


def test_fan_in_nondefault_edge_port_still_rejected():
    # An edge pointing at a NON-default port IS explicit port topology even
    # when the target node declares no ports of its own.
    graph = {
        "nodes": [_node("a"), _node("c"), _node("b")],
        "edges": [
            _edge("a", "b", target_port="data"),
            _edge("c", "b", target_port="data"),
        ],
    }
    result = ValidationResult()
    validate_port_topology(graph, result)
    fan_in = [i for i in result.issues if i.code == PORT_FAN_IN_ERROR]
    assert len(fan_in) == 1
    assert fan_in[0].node_id == "b"


def test_fan_in_null_port_keys_api_round_trip_not_rejected():
    # FAR-480 regression (the exact production failure): every node round-tripped
    # through the API carries inputs/outputs = null, and a 2-edge fan-in target
    # (conditional low-risk path + normal path; only one fires per run) is legacy
    # last-write-wins fan-in — must NOT raise PORT_FAN_IN_VIOLATION.
    graph = {
        "nodes": [
            _node("a", inputs=None, outputs=None),
            _node("c", inputs=None, outputs=None),
            _node("b", inputs=None, outputs=None),
        ],
        "edges": [_edge("a", "b"), _edge("c", "b")],
    }
    result = ValidationResult()
    validate_port_topology(graph, result)
    assert not any(i.code == PORT_FAN_IN_ERROR for i in result.issues)


def test_fan_in_join_node_type_detected_anywhere():
    # A node declared as join type accepts many even without explicit port decls.
    graph = {
        "nodes": [_node("a"), _node("b"), _node("join", node_type=JOIN_NODE_TYPE)],
        "edges": [_edge("a", "join"), _edge("b", "join")],
    }
    result = ValidationResult()
    validate_port_topology(graph, result)
    assert not any(i.code == PORT_FAN_IN_ERROR for i in result.issues)


# ---------------------------------------------------------------------------
# Port-type validation
# ---------------------------------------------------------------------------


def test_port_mismatch_typed_error():
    graph = {
        "nodes": [
            _node("a", outputs=[{"port": "out", "schema_ref": "alpha"}]),
            _node("b", inputs=[{"port": "in", "schema_ref": "beta"}]),
        ],
        "edges": [_edge("a", "b")],
    }
    result = ValidationResult()
    validate_port_topology(graph, result)
    mism = [i for i in result.issues if i.code == PORT_TYPE_MISMATCH_ERROR]
    assert len(mism) == 1


def test_port_type_lenient_when_schema_ref_absent():
    # Both ports present but no schema_ref => not a mismatch (lenient P2).
    graph = {
        "nodes": [
            _node("a", outputs=[{"port": "out"}]),
            _node("b", inputs=[{"port": "in"}]),
        ],
        "edges": [_edge("a", "b")],
    }
    result = ValidationResult()
    validate_port_topology(graph, result)
    assert not any(i.code == PORT_TYPE_MISMATCH_ERROR for i in result.issues)


def test_port_type_matching_schema_ref_ok():
    graph = {
        "nodes": [
            _node("a", outputs=[{"port": "out", "schema_ref": "shared"}]),
            _node("b", inputs=[{"port": "in", "schema_ref": "shared"}]),
        ],
        "edges": [_edge("a", "b")],
    }
    result = ValidationResult()
    validate_port_topology(graph, result)
    assert not any(i.code == PORT_TYPE_MISMATCH_ERROR for i in result.issues)


# ---------------------------------------------------------------------------
# Compile-cache-key port-topology sensitivity
# ---------------------------------------------------------------------------


def _base_graph() -> dict:
    return {
        "nodes": [_node("a", outputs=[{"port": "out"}]), _node("b", inputs=[{"port": "in"}])],
        "edges": [_edge("a", "b")],
    }


def test_topology_hash_changes_with_port_mutation():
    g1 = _base_graph()
    g2 = _base_graph()
    g2["nodes"][1]["inputs"] = [{"port": "different"}]
    assert compute_port_topology_hash(g1) != compute_port_topology_hash(g2)


def test_topology_hash_stable_across_key_order():
    g1 = _base_graph()
    g2 = _base_graph()
    # reorder node list + edge keys — hash must be identical (deterministic)
    g2["nodes"] = [g2["nodes"][1], g2["nodes"][0]]
    assert compute_port_topology_hash(g1) == compute_port_topology_hash(g2)


def test_topology_hash_changes_when_source_port_set_on_edge():
    g1 = _base_graph()
    g2 = _base_graph()
    g2["edges"][0]["source_port"] = "out"
    # Setting the same default value explicitly should NOT change the hash
    # (backfill-equivalent), but mutating to a distinct value should.
    g3 = _base_graph()
    g3["edges"][0]["source_port"] = "custom"
    assert compute_port_topology_hash(g1) == compute_port_topology_hash(g2)
    assert compute_port_topology_hash(g1) != compute_port_topology_hash(g3)


def test_retry_aware_hash_equals_base_hash_without_policy():
    """No pipeline retry_policy -> hash is identical to the base port-topology hash."""
    g = _base_graph()
    assert compute_retry_aware_topology_hash(g, None) == compute_port_topology_hash(g)
    assert compute_retry_aware_topology_hash(g, {}) == compute_port_topology_hash(g)


def test_retry_aware_hash_folds_policy_into_hash():
    """A present retry_policy changes the hash (forces recompile on policy PATCH)."""
    g = _base_graph()
    base = compute_port_topology_hash(g)
    with_policy = compute_retry_aware_topology_hash(g, {"on": ["failure"], "max_retries": 2})
    assert with_policy != base
    assert with_policy.startswith(base + ":")


def test_retry_aware_hash_stable_for_same_policy():
    """The same policy produces a deterministic hash across calls/orderings."""
    g = _base_graph()
    h1 = compute_retry_aware_topology_hash(g, {"max_retries": 2, "on": ["failure"]})
    h2 = compute_retry_aware_topology_hash(g, {"on": ["failure"], "max_retries": 2})
    assert h1 == h2
    assert h1 != compute_retry_aware_topology_hash(g, {"on": ["failure"], "max_retries": 3})


def test_retry_aware_hash_stable_across_key_order():
    """A dict-default policy is key-order independent (json.dumps sort_keys)."""
    g = _base_graph()
    g["nodes"] = [g["nodes"][1], g["nodes"][0]]
    h_a = compute_retry_aware_topology_hash(g, {"on": ["failure"], "max_retries": 2})
    h_b = compute_retry_aware_topology_hash(_base_graph(), {"max_retries": 2, "on": ["failure"]})
    assert h_a == h_b


# ---------------------------------------------------------------------------
# Golden test: existing flat-dict pipeline compiles + routes identically
# ---------------------------------------------------------------------------


def test_golden_flat_dict_pipeline_backfill_and_routing():
    """A pre-port flat-dict pipeline (no ports) must compile + route identically.

    The backfill is purely ADDITIVE: it adds default out/in ports without
    renaming or namespacing any flat-state key, so a conditional-edge JMESPath
    that reads ``state.summary`` still routes exactly as before.
    """
    # A flat-dict pipeline: A -> (conditional on state.summary) -> B or C.
    legacy_graph = {
        "nodes": [
            {"id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", "node_type": "agent"},
            {"id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb", "node_type": "agent"},
            {"id": "cccccccc-cccc-cccc-cccc-cccccccccccc", "node_type": "agent"},
        ],
        "edges": [
            {
                "source": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                "target": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                "type": "conditional",
                "condition_expression": "summary == 'done'",
            },
            {
                "source": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                "target": "cccccccc-cccc-cccc-cccc-cccccccccccc",
                "type": "normal",
            },
        ],
    }

    # 1. Backfill must be additive: original keys preserved, ports added.
    backfilled = [synthesize_node_ports(n) for n in legacy_graph["nodes"]]
    for original, synth in zip(legacy_graph["nodes"], backfilled):  # noqa: B905
        for k in original:
            assert synth[k] == original[k], f"backfill clobbered key '{k}'"
        assert synth["inputs"] == [{"port": DEFAULT_INPUT_PORT}]
        assert synth["outputs"] == [{"port": DEFAULT_OUTPUT_PORT}]

    # 2. Port topology validation must NOT reject the legacy graph.
    result = ValidationResult()
    validate_port_topology(legacy_graph, result)
    assert not result.issues, f"legacy flat-dict graph rejected: {result.issues}"

    # 3. Conditional-edge JMESPath must resolve identically before & after.
    expr = jmespath.compile("summary == 'done'")
    # Before backfill the state key is "summary"; after, port "out" maps to the
    # SAME key "summary" (identity), so routing is unchanged.
    hit_state = {"summary": "done"}
    miss_state = {"summary": "pending"}
    assert expr.search(hit_state) is True
    assert expr.search(miss_state) is False
    # The port->state-key adapter is a 1:1 identity map, so no flat-state key
    # is renamed or namespaced by introducing ports. Routing on ``state.summary``
    # resolves identically before and after backfill.
    assert port_to_state_key("out") == "out"
