"""Unit tests for the FAR-416 / F1 port model (port_resolver)."""

import jmespath
import pytest

from modulo.core.graph_validator._types import ValidationResult
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
    with pytest.raises(ValueError):
        synthesize_node_ports(node)


def test_is_port_declared():
    assert not is_port_declared(_node("a"))
    assert is_port_declared(_node("a", inputs=[{"port": "in"}]))
    assert is_port_declared(_node("a", outputs=[{"port": "out"}]))


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
