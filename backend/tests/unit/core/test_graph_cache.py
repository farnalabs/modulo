"""Unit tests for the HITL gate kick-back router (graph_cache).

The reject→correction dispatch seam (FAR-210 follow-up) is wired in the
``_hitl_review`` node builder, NOT in the kick-back router: the router still
kicks a rejection back to the plain ``reject_target``, and the correction
dispatch happens alongside in the gate node for gates that declare a
``correction_target``. These tests pin the router's existing behaviour.

FAR-541: the router also verifies the decision's gate stamp — the edge is
reachable without the gate consuming a decision (condition/autonomy skip), so
a stale rejection from an earlier gate must never kick THIS edge to the
reject target.
"""

import pytest

from modulo.core.pipeline_engine.graph_cache import (
    _make_gate_kickback_router,
    _reject_unknown_graph_fields,
)

_REVIEW_ID = "hitl_review_a_b"


def test_graph_allowlist_accepts_compiler_and_persisted_fields():
    graph = {
        "nodes": [
            {
                "id": "node-1",
                "routing_mode": "llm",
                "default_target": "fallback",
                "execution_metadata": {"attempt": 2},
            }
        ],
        "edges": [
            {
                "id": "edge-1",
                "default_target": "done",
                "max_iterations": 3,
                "routing_label": "retry",
            }
        ],
    }
    _reject_unknown_graph_fields(graph)
    assert graph["edges"][0]["max_iterations"] == 3


def test_graph_allowlist_rejects_unknown_fields():
    with pytest.raises(ValueError, match="unknown fields"):
        _reject_unknown_graph_fields({"nodes": [{"id": "node-1", "typo": True}], "edges": []})


def test_reject_routes_to_reject_target():
    router = _make_gate_kickback_router(normal_target="normal", reject_target_str="reject", review_id=_REVIEW_ID)
    assert router({"_hitl_decision": {"action": "rejected", "review_id": _REVIEW_ID}}) == "reject"


def test_approve_routes_to_normal_target():
    router = _make_gate_kickback_router(normal_target="normal", reject_target_str="reject", review_id=_REVIEW_ID)
    assert router({"_hitl_decision": {"action": "approved", "review_id": _REVIEW_ID}}) == "normal"


def test_no_decision_routes_to_normal_target():
    router = _make_gate_kickback_router(normal_target="normal", reject_target_str="reject", review_id=_REVIEW_ID)
    assert router({}) == "normal"


def test_reject_with_correction_target_still_routes_to_reject_target():
    # The router does NOT route to the correction_target — the dispatch seam
    # lives in the gate node. The reject kick-back behaviour is preserved.
    router = _make_gate_kickback_router(normal_target="normal", reject_target_str="reject", review_id=_REVIEW_ID)
    assert router({"_hitl_decision": {"action": "rejected", "review_id": _REVIEW_ID}}) == "reject"


def test_foreign_rejection_routes_to_normal_target():
    """FAR-541: a stale rejection stamped for a DIFFERENT gate never misroutes
    this edge to the reject target."""
    router = _make_gate_kickback_router(normal_target="normal", reject_target_str="reject", review_id=_REVIEW_ID)
    assert router({"_hitl_decision": {"action": "rejected", "review_id": "hitl_review_c_d"}}) == "normal"
    assert router({"_hitl_decision": {"action": "rejected"}}) == "normal"
