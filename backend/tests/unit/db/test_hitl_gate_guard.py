"""Unit tests for the HITL-gate weakening guard primitive
(modulo.db.crud.hitl_gate_guard — hitl-gate-removal-guard-plan.md v19 §1/§3/§7)."""

from __future__ import annotations

import pytest

from modulo.db.crud.hitl_gate_guard import (
    REASON_CORRELATION_KEY_MISMATCH,
    REASON_INSUFFICIENT_ROLE,
    REASON_LEGACY_SNAPSHOT_AMBIGUOUS,
    REASON_MCP_NOT_PERMITTED,
    DiffResult,
    EdgeWeakening,
    HitlGateWeakeningDenied,
    apply_gated_edge_diff,
    build_gate_diff_payload,
    is_privileged_role,
)

pytestmark = pytest.mark.asyncio(loop_scope="module")

_SESSION = None  # the primitive's session param is reserved; comparison is pure


def _old_edge(source: str, target: str, edge_type: str = "normal", cfg: dict | None = None) -> dict:
    return {
        "source_node_id": source,
        "target_node_id": target,
        "edge_type": edge_type,
        "hitl_gate_config": cfg,
        "hitl_gate_config_present": cfg is not None,
    }


def _new_edge(source: str, target: str, edge_type: str = "normal", cfg: dict | None = None) -> dict:
    d = {
        "source_node_id": source,
        "target_node_id": target,
        "edge_type": edge_type,
    }
    d["hitl_gate_config"] = cfg
    d["hitl_gate_config_present"] = True
    return d


_GATE = {
    "human_only": True,
    "required_team_id": None,
    "condition": None,
    "eval_condition": None,
    "claim_expiry_minutes": 60,
}


# ---------------------------------------------------------------------------
# Field-level weakening: all four fields
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("old_cfg", "new_cfg", "expected"),
    [
        ({"human_only": True}, {"human_only": False}, ["human_only"]),
        ({"human_only": True}, {"human_only": True}, []),
        ({"required_team_id": "team-1"}, {"required_team_id": None}, ["required_team_id"]),
        ({"required_team_id": "team-1"}, {"required_team_id": "team-2"}, ["required_team_id"]),
        ({"required_team_id": "team-1"}, {"required_team_id": "team-1"}, []),
        ({"required_team_id": None}, {"required_team_id": "team-1"}, []),
        ({"condition": "a"}, {"condition": "b"}, ["condition"]),
        ({"condition": None}, {"condition": "expr"}, ["condition"]),
        ({"condition": "a"}, {"condition": "a"}, []),
        (
            {"eval_condition": {"name": "x", "threshold": 0.8}},
            {"eval_condition": {"name": "x", "threshold": 0.5}},
            ["eval_condition"],
        ),
        ({"eval_condition": None}, {"eval_condition": {"name": "x"}}, ["eval_condition"]),
        # claim_expiry_minutes is NOT weakening when shortened: a shorter expiry
        # is stricter — on expiry the claim is reset (run returns to
        # awaiting_human), it never releases the gate or auto-approves the run
        # (plan §1, review finding). REMOVING the field IS weakening: it drops
        # the expiry requirement without tightening anything.
        ({"claim_expiry_minutes": 60}, {"claim_expiry_minutes": 30}, []),
        ({"claim_expiry_minutes": 30}, {"claim_expiry_minutes": 60}, []),
        ({"claim_expiry_minutes": 30}, {"claim_expiry_minutes": 30}, []),
        ({"claim_expiry_minutes": 60}, {"claim_expiry_minutes": None}, ["claim_expiry_minutes"]),
    ],
    ids=[
        "human_only_weakened",
        "human_only_unchanged",
        "team_removed",
        "team_changed",
        "team_unchanged",
        "team_added",
        "condition_changed",
        "condition_added",
        "condition_unchanged",
        "eval_threshold_lowered",
        "eval_added",
        "expiry_shortened",
        "expiry_lengthened",
        "expiry_unchanged",
        "expiry_removed",
    ],
)
async def test_field_weakening_detection(old_cfg: dict, new_cfg: dict, expected: list[str]) -> None:
    old_cfg = {**_GATE, **old_cfg}
    new_cfg = {**_GATE, **new_cfg}
    diff = await apply_gated_edge_diff(
        _SESSION,
        [_old_edge("a", "b", cfg=old_cfg)],
        [_new_edge("a", "b", cfg=new_cfg)],
        is_privileged=True,
        caller_type="rest",
    )
    if expected:
        assert diff.has_weakening
        assert diff.weakened_edges[0].weakening_types == expected
        assert diff.denied is False  # privileged caller is allowed
    else:
        assert not diff.has_weakening


async def test_claim_expiry_minutes_field_removed_is_weakening() -> None:
    """REMOVING the claim_expiry_minutes key entirely (old=60 -> new absent)
    drops the expiry requirement and is flagged as weakening."""
    old_cfg = {**_GATE, "claim_expiry_minutes": 60}
    new_cfg = {k: v for k, v in _GATE.items() if k != "claim_expiry_minutes"}
    diff = await apply_gated_edge_diff(
        _SESSION,
        [_old_edge("a", "b", cfg=old_cfg)],
        [_new_edge("a", "b", cfg=new_cfg)],
        is_privileged=True,
        caller_type="rest",
    )
    assert diff.has_weakening
    assert diff.weakened_edges[0].weakening_types == ["claim_expiry_minutes"]


async def test_condition_weakening_on_human_only_false_edge() -> None:
    """condition/eval_condition weaken regardless of human_only (node_runner
    evaluates them before human_only is consulted — plan §1)."""
    old_cfg = {**_GATE, "human_only": False, "condition": "x == 1"}
    new_cfg = {**_GATE, "human_only": False, "condition": None}
    diff = await apply_gated_edge_diff(
        _SESSION,
        [_old_edge("a", "b", cfg=old_cfg)],
        [_new_edge("a", "b", cfg=new_cfg)],
        is_privileged=True,
        caller_type="rest",
    )
    assert diff.has_weakening
    assert "condition" in diff.weakened_edges[0].weakening_types


# ---------------------------------------------------------------------------
# Structural weakening
# ---------------------------------------------------------------------------


async def test_edge_deletion_is_structural_weakening() -> None:
    diff = await apply_gated_edge_diff(
        _SESSION,
        [_old_edge("a", "b", cfg=_GATE)],
        [],  # old gated edge's topology key absent
        is_privileged=True,
        caller_type="rest",
    )
    assert diff.has_weakening
    assert diff.weakened_edges[0].weakening_types == ["structural:edge_deleted"]
    assert diff.weakened_edges[0].reason_code == REASON_CORRELATION_KEY_MISMATCH
    assert diff.weakened_edges[0].correlation_key == ("a", "b", "normal")


async def test_gate_removal_via_explicit_null_is_structural_weakening() -> None:
    diff = await apply_gated_edge_diff(
        _SESSION,
        [_old_edge("a", "b", cfg=_GATE)],
        [_new_edge("a", "b", cfg=None)],
        is_privileged=True,
        caller_type="rest",
    )
    assert diff.has_weakening
    assert diff.weakened_edges[0].weakening_types == ["structural:gate_removed"]


async def test_structural_weakening_reason_for_unprivileged_is_correlation_mismatch() -> None:
    diff = await apply_gated_edge_diff(
        _SESSION,
        [_old_edge("a", "b", cfg=_GATE)],
        [],
        is_privileged=False,
        caller_type="rest",
    )
    assert diff.denied
    assert diff.reason_code == REASON_CORRELATION_KEY_MISMATCH


# ---------------------------------------------------------------------------
# Topology correlation key (never client-supplied id)
# ---------------------------------------------------------------------------


async def test_correlation_uses_topology_key_not_client_id() -> None:
    """A client that submits a new id for the same topological edge must not
    defeat the guard (plan iteration-17 finding)."""
    old_edge = {
        "id": "client-id-1",
        "source_node_id": "a",
        "target_node_id": "b",
        "edge_type": "normal",
        "hitl_gate_config": _GATE,
    }
    new_edge = {
        "id": "client-id-2",  # NEW client-supplied id, same topology
        "source_node_id": "a",
        "target_node_id": "b",
        "edge_type": "normal",
        "hitl_gate_config": None,
        "hitl_gate_config_present": True,
    }
    diff = await apply_gated_edge_diff(
        _SESSION,
        [old_edge],
        [new_edge],
        is_privileged=True,
        caller_type="rest",
    )
    assert diff.has_weakening
    assert diff.weakened_edges[0].correlation_key == ("a", "b", "normal")


# ---------------------------------------------------------------------------
# Presence signal
# ---------------------------------------------------------------------------


async def test_presence_signal_false_preserves_existing_value() -> None:
    """hitl_gate_config_present=False on a matching topology key means preserve
    the stored value — not weakening even with an explicit null value."""
    diff = await apply_gated_edge_diff(
        _SESSION,
        [_old_edge("a", "b", cfg=_GATE)],
        [
            {
                "source_node_id": "a",
                "target_node_id": "b",
                "edge_type": "normal",
                "hitl_gate_config": None,
                "hitl_gate_config_present": False,
            }
        ],
        is_privileged=True,
        caller_type="rest",
    )
    assert not diff.has_weakening


async def test_new_edge_with_omitted_key_is_preserved() -> None:
    """An edge dict that omits the hitl_gate_config key entirely (untouched by
    the client) defaults to preserve — no weakening."""
    diff = await apply_gated_edge_diff(
        _SESSION,
        [_old_edge("a", "b", cfg=_GATE)],
        [{"source_node_id": "a", "target_node_id": "b", "edge_type": "normal"}],
        is_privileged=True,
        caller_type="rest",
    )
    assert not diff.has_weakening


async def test_empty_old_edge_set_is_noop() -> None:
    """Edge creation with no prior row is never weakening (plan §1)."""
    diff = await apply_gated_edge_diff(
        _SESSION,
        [],
        [_new_edge("a", "b", cfg=_GATE)],
        is_privileged=False,
        caller_type="rest",
    )
    assert not diff.has_weakening
    assert not diff.denied


# ---------------------------------------------------------------------------
# Deep-copy invariant
# ---------------------------------------------------------------------------


async def test_primitive_does_not_mutate_inputs() -> None:
    old_cfg = {**_GATE, "human_only": True}
    old_edges = [_old_edge("a", "b", cfg=old_cfg)]
    snapshot_before = [dict(e) for e in old_edges]
    await apply_gated_edge_diff(
        _SESSION,
        old_edges,
        [_new_edge("a", "b", cfg={**_GATE, "human_only": False})],
        is_privileged=True,
        caller_type="rest",
    )
    assert old_edges == snapshot_before, "the primitive must deep-copy its inputs"


async def test_deepcopy_isolates_mutable_config_after_call() -> None:
    old_cfg = {**_GATE, "human_only": True}
    old_edges = [_old_edge("a", "b", cfg=old_cfg)]
    diff = await apply_gated_edge_diff(
        _SESSION,
        old_edges,
        [_new_edge("a", "b", cfg={**_GATE, "human_only": False})],
        is_privileged=True,
        caller_type="rest",
    )
    old_edges[0]["hitl_gate_config"]["human_only"] = False  # mutate after the fact
    assert diff.weakened_edges[0].weakening_types == ["human_only"]


# ---------------------------------------------------------------------------
# Privilege semantics + MCP structural exclusion
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("role", "expected"),
    [
        ("viewer", False),
        ("runner", False),
        ("operator", True),
        ("admin", True),
        (None, False),
        ("", False),
        ("unknown_role", False),
    ],
)
def test_is_privileged_role(role: str | None, expected: bool) -> None:
    assert is_privileged_role(role) is expected


async def test_non_privileged_weakening_is_denied_with_insufficient_role() -> None:
    diff = await apply_gated_edge_diff(
        _SESSION,
        [_old_edge("a", "b", cfg=_GATE)],
        [_new_edge("a", "b", cfg={**_GATE, "human_only": False})],
        is_privileged=False,
        caller_type="rest",
    )
    assert diff.denied
    assert diff.reason_code == REASON_INSUFFICIENT_ROLE


async def test_mcp_caller_type_forces_denial_even_when_is_privileged_true() -> None:
    """caller_type == 'mcp' hardcodes is_privileged False (plan §3 item 5)."""
    diff = await apply_gated_edge_diff(
        _SESSION,
        [_old_edge("a", "b", cfg=_GATE)],
        [_new_edge("a", "b", cfg={**_GATE, "human_only": False})],
        is_privileged=True,
        caller_type="mcp",
    )
    assert diff.denied
    assert diff.reason_code == REASON_MCP_NOT_PERMITTED


async def test_mcp_denied_for_structural_removal() -> None:
    diff = await apply_gated_edge_diff(
        _SESSION,
        [_old_edge("a", "b", cfg=_GATE)],
        [],
        is_privileged=True,
        caller_type="mcp",
    )
    assert diff.denied
    assert diff.reason_code == REASON_MCP_NOT_PERMITTED


# ---------------------------------------------------------------------------
# Legacy snapshot fail-closed
# ---------------------------------------------------------------------------


async def test_legacy_snapshot_missing_gate_is_fail_closed() -> None:
    """A historical snapshot edge with a missing/None gate field is treated as
    weakening with the distinct reason code (plan §1 / §3)."""
    diff = await apply_gated_edge_diff(
        _SESSION,
        [_old_edge("a", "b", cfg=_GATE)],
        [
            {
                "source_node_id": "a",
                "target_node_id": "b",
                "edge_type": "normal",
                "hitl_gate_config": None,
                "hitl_gate_config_present": True,
            }
        ],
        is_privileged=False,
        caller_type="rest",
        legacy_snapshot=True,
    )
    assert diff.denied
    assert diff.reason_code == REASON_LEGACY_SNAPSHOT_AMBIGUOUS


async def test_legacy_snapshot_field_weakening_fail_closed() -> None:
    diff = await apply_gated_edge_diff(
        _SESSION,
        [_old_edge("a", "b", cfg=_GATE)],
        [_new_edge("a", "b", cfg={**_GATE, "human_only": False})],
        is_privileged=False,
        caller_type="rest",
        legacy_snapshot=True,
    )
    assert diff.denied
    assert diff.reason_code == REASON_LEGACY_SNAPSHOT_AMBIGUOUS


async def test_legacy_snapshot_no_change_is_allowed_for_privileged() -> None:
    diff = await apply_gated_edge_diff(
        _SESSION,
        [_old_edge("a", "b", cfg=_GATE)],
        [_new_edge("a", "b", cfg=dict(_GATE))],
        is_privileged=True,
        caller_type="rest",
        legacy_snapshot=True,
    )
    assert not diff.has_weakening
    assert not diff.denied


# ---------------------------------------------------------------------------
# Audit payload builder schema parity
# ---------------------------------------------------------------------------


def test_denial_payload_schema_matches_allowed_payload_schema() -> None:
    """The shared payload builder (plan §3 item 9) emits the same schema for
    denied and allowed weakening events — cross-path audit schema consistency."""
    denied = DiffResult(
        weakened_edges=[
            EdgeWeakening(
                correlation_key=("a", "b", "normal"),
                weakening_types=["human_only"],
                reason_code=REASON_INSUFFICIENT_ROLE,
            )
        ],
        has_weakening=True,
        denied=True,
        reason_code=REASON_INSUFFICIENT_ROLE,
        caller_type="rest",
    )
    allowed = DiffResult(
        weakened_edges=[
            EdgeWeakening(
                correlation_key=("a", "b", "normal"),
                weakening_types=["human_only"],
                reason_code=REASON_INSUFFICIENT_ROLE,
            )
        ],
        has_weakening=True,
        denied=False,
        reason_code=None,
        caller_type="rest",
    )
    p_denied = build_gate_diff_payload(denied, "rest")
    p_allowed = build_gate_diff_payload(allowed, "rest")
    assert set(p_denied) == set(p_allowed)
    assert set(p_allowed) == {"caller_type", "reason_code", "denied", "affected_edges"}
    assert p_denied["affected_edges"][0] == p_allowed["affected_edges"][0]
    assert p_denied["caller_type"] == "rest"
    assert p_denied["denied"] is True
    assert p_allowed["denied"] is False


def test_denial_payload_names_edges_by_topology_key() -> None:
    denied = DiffResult(
        weakened_edges=[
            EdgeWeakening(
                correlation_key=("a", "b", "reject"),
                weakening_types=["structural:edge_deleted"],
                reason_code=REASON_CORRELATION_KEY_MISMATCH,
            )
        ],
        has_weakening=True,
        denied=True,
        reason_code=REASON_CORRELATION_KEY_MISMATCH,
        caller_type="rest",
    )
    payload = build_gate_diff_payload(denied, "rest")
    edge = payload["affected_edges"][0]
    assert edge["source_node_id"] == "a"
    assert edge["target_node_id"] == "b"
    assert edge["edge_type"] == "reject"


def test_hitl_gate_weakening_denied_carries_payload() -> None:
    exc = HitlGateWeakeningDenied(
        reason_code=REASON_INSUFFICIENT_ROLE,
        correlation_keys=[("a", "b", "normal")],
        weakening_types=["human_only"],
        payload_json={"denied": True},
    )
    assert exc.reason_code == REASON_INSUFFICIENT_ROLE
    assert exc.correlation_keys == [("a", "b", "normal")]
    assert exc.payload_json == {"denied": True}
    assert "hitl gate weakening denied" in str(exc)


async def test_topology_bypass_same_edge_different_edge_type_is_structural() -> None:
    """Old (a,b,'normal') gated edge vs new (a,b,'reject') — the old gated
    topology key is absent → structural weakening, not silently preserved."""
    diff = await apply_gated_edge_diff(
        _SESSION,
        [_old_edge("a", "b", cfg=_GATE)],
        [_new_edge("a", "b", edge_type="reject", cfg=None)],
        is_privileged=False,
        caller_type="rest",
    )
    assert diff.denied
