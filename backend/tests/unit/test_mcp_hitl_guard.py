"""MCP structural HITL-gate exclusion tests (hitl-gate-removal-guard-plan.md v19 §3 item 5).

The MCP call site passes the literal caller_type="mcp"; the guarded function
hardcodes is_privileged=False, so an MCP-authenticated gate-weakening attempt
is denied regardless of MCP scope. Denials are audited.
"""

from __future__ import annotations

import uuid
from collections.abc import Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.asyncio(loop_scope="module")

_ORG = "00000000-0000-0000-0000-0000000000f1"
_USER = "00000000-0000-0000-0000-0000000000f2"
_PIPELINE = "00000000-0000-0000-0000-0000000000f3"
_NODE_A = "00000000-0000-0000-0000-0000000000a1"
_NODE_B = "00000000-0000-0000-0000-0000000000a2"

_GATE = {
    "label": "Approval gate",
    "description": "requires human approval",
    "human_only": True,
    "required_team_id": None,
    "condition": None,
    "eval_condition": None,
    "claim_expiry_minutes": 60,
}


class _PipelineRow:
    def __init__(self) -> None:
        self.id = uuid.UUID(_PIPELINE)
        self.organisation_id = uuid.UUID(_ORG)
        self.deleted_at = None
        self.graph_nodes_json = []
        self.owner_team_id = None


class _EdgeRow:
    def __init__(self) -> None:
        self.id = uuid.uuid4()
        self.source_node_id = uuid.UUID(_NODE_A)
        self.target_node_id = uuid.UUID(_NODE_B)
        self.edge_type = "normal"
        self.hitl_gate_config = dict(_GATE)


@pytest.fixture(autouse=True)
def _patch_auth() -> Generator[None, None, None]:
    """Set tenant context + mock validate_current_auth so the tool handler runs."""
    import modulo.api.mcp_server as _ms

    org_token = _ms._ctx_org_id.set(uuid.UUID(_ORG))
    user_token = _ms._ctx_user_id.set(uuid.UUID(_USER))
    try:
        with patch("modulo.api.mcp_server.validate_current_auth", return_value=True):
            yield
    finally:
        _ms._ctx_user_id.reset(user_token)
        _ms._ctx_org_id.reset(org_token)


def _patch_session_and_audit(
    monkeypatch: pytest.MonkeyPatch, *, old_edge: _EdgeRow | None
) -> tuple[AsyncMock, AsyncMock]:
    import modulo.api.mcp_server as _ms

    session = AsyncMock()
    pipeline = _PipelineRow()
    pipeline_result = MagicMock()
    pipeline_result.scalar_one_or_none.return_value = pipeline
    edge_result = MagicMock()
    edge_result.scalars.return_value = [old_edge] if old_edge else []

    calls = [pipeline_result, pipeline_result, edge_result]
    if old_edge:
        calls = [pipeline_result, pipeline_result, edge_result]
    else:
        calls = [pipeline_result, pipeline_result, edge_result]

    async def _execute(stmt: object, *args: object, **kwargs: object) -> MagicMock:
        if calls:
            return calls.pop(0)
        return MagicMock()

    session.execute = AsyncMock(side_effect=_execute)
    session.add = MagicMock()
    session.add_all = MagicMock()
    session.flush = AsyncMock()

    factory = MagicMock()
    factory.return_value.__aenter__.return_value = session

    audit = AsyncMock()
    monkeypatch.setattr("modulo.core.audit_logger.append_audit_event", audit)
    monkeypatch.setattr(_ms, "_session", factory)
    return session, audit


def _weakening_edge_payload() -> list[dict]:
    return [
        {
            "id": str(uuid.uuid4()),
            "source_node_id": _NODE_A,
            "target_node_id": _NODE_B,
            "edge_type": "normal",
            "hitl_gate_config": {**_GATE, "human_only": False},
            "hitl_gate_config_present": True,
        }
    ]


async def test_mcp_update_pipeline_graph_weakening_denied(monkeypatch: pytest.MonkeyPatch) -> None:
    """MCP gate-weakening is denied even for an admin-scoped MCP principal."""
    from modulo.api.mcp_server import _ctx_role, update_pipeline_graph

    _ctx_role.set("admin")  # privileged MCP scope — the structural exclusion still denies
    _session, audit = _patch_session_and_audit(monkeypatch, old_edge=_EdgeRow())

    result = await update_pipeline_graph(pipeline_id=_PIPELINE, nodes=[], edges=_weakening_edge_payload())

    assert result["error"] == "hitl_gate_removal_denied", result
    assert result["reason_code"] == "mcp-weakening-not-permitted"
    assert result["affected_edges"][0]["source_node_id"] == _NODE_A
    audit.assert_awaited_once()
    assert audit.call_args.kwargs["event_type"] == "hitl_gate_removal_denied"
    assert audit.call_args.kwargs["payload_json"]["caller_type"] == "mcp"
    assert audit.call_args.kwargs["payload_json"]["denied"] is True


async def test_mcp_structural_edge_deletion_denied(monkeypatch: pytest.MonkeyPatch) -> None:
    from modulo.api.mcp_server import _ctx_role, update_pipeline_graph

    _ctx_role.set("admin")
    _session, _audit = _patch_session_and_audit(monkeypatch, old_edge=_EdgeRow())

    result = await update_pipeline_graph(pipeline_id=_PIPELINE, nodes=[], edges=[])

    assert result["error"] == "hitl_gate_removal_denied"
    assert result["reason_code"] == "mcp-weakening-not-permitted"


async def test_mcp_non_weakening_graph_write_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """MCP can still write a graph that does not weaken an existing gate."""
    from modulo.api.mcp_server import _ctx_role, update_pipeline_graph

    _ctx_role.set("operator")
    # Old edge has no gate -> nothing to weaken.
    old = _EdgeRow()
    old.hitl_gate_config = None
    _session, audit = _patch_session_and_audit(monkeypatch, old_edge=old)

    result = await update_pipeline_graph(
        pipeline_id=_PIPELINE,
        nodes=[],
        edges=[
            {
                "id": str(uuid.uuid4()),
                "source_node_id": _NODE_A,
                "target_node_id": _NODE_B,
                "edge_type": "normal",
                "hitl_gate_config": None,
                "hitl_gate_config_present": True,
            }
        ],
    )

    assert "error" not in result, result
    audit.assert_not_awaited()


async def test_mcp_denial_audit_never_masked_by_audit_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """Even if the audit append fails, the denial is still surfaced to the caller."""
    from modulo.api.mcp_server import _ctx_role, update_pipeline_graph

    _ctx_role.set("admin")
    _session, audit = _patch_session_and_audit(monkeypatch, old_edge=_EdgeRow())
    audit.side_effect = RuntimeError("audit store down")

    result = await update_pipeline_graph(pipeline_id=_PIPELINE, nodes=[], edges=_weakening_edge_payload())

    assert result["error"] == "hitl_gate_removal_denied"
    assert result["reason_code"] == "mcp-weakening-not-permitted"
