"""Unit tests for the HITL gate kick-back router (graph_cache).

The reject→correction dispatch seam (FAR-210 follow-up) is wired in the
``_hitl_gate`` node builder, NOT in the kick-back router: the router still
kicks a rejection back to the plain ``reject_target``, and the correction
dispatch happens alongside in the gate node for gates that declare a
``correction_target``. These tests pin the router's existing behaviour.

FAR-541: the router also verifies the decision's gate stamp — the edge is
reachable without the gate consuming a decision (condition/autonomy skip), so
a stale rejection from an earlier gate must never kick THIS edge to the
reject target.
"""

from modulo.core.pipeline_engine.graph_cache import _make_gate_kickback_router

_GATE_ID = "hitl_gate_a_b"


def test_reject_routes_to_reject_target():
    router = _make_gate_kickback_router(normal_target="normal", reject_target_str="reject", gate_id=_GATE_ID)
    assert router({"_hitl_decision": {"action": "rejected", "gate_id": _GATE_ID}}) == "reject"


def test_approve_routes_to_normal_target():
    router = _make_gate_kickback_router(normal_target="normal", reject_target_str="reject", gate_id=_GATE_ID)
    assert router({"_hitl_decision": {"action": "approved", "gate_id": _GATE_ID}}) == "normal"


def test_no_decision_routes_to_normal_target():
    router = _make_gate_kickback_router(normal_target="normal", reject_target_str="reject", gate_id=_GATE_ID)
    assert router({}) == "normal"


def test_reject_with_correction_target_still_routes_to_reject_target():
    # The router does NOT route to the correction_target — the dispatch seam
    # lives in the gate node. The reject kick-back behaviour is preserved.
    router = _make_gate_kickback_router(normal_target="normal", reject_target_str="reject", gate_id=_GATE_ID)
    assert router({"_hitl_decision": {"action": "rejected", "gate_id": _GATE_ID}}) == "reject"


def test_foreign_rejection_routes_to_normal_target():
    """FAR-541: a stale rejection stamped for a DIFFERENT gate never misroutes
    this edge to the reject target."""
    router = _make_gate_kickback_router(normal_target="normal", reject_target_str="reject", gate_id=_GATE_ID)
    assert router({"_hitl_decision": {"action": "rejected", "gate_id": "hitl_gate_c_d"}}) == "normal"
    assert router({"_hitl_decision": {"action": "rejected"}}) == "normal"
