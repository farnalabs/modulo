"""Unit tests for the DERIVE-FROM-ROUTED-VERB connector capability requirement.

FAR-1667: the save-time connector-binding check consumes the ACL and derives the
required operations from the node's *routed* connector verb (via
``connector_binding_required_acl_operations``), rather than reading a
``required_operations`` key that no producer ever populated. These tests pin the
derivation: which verb maps to which ACL operation, when ``await_completion``
adds a read leg, and which node shapes are never checked at all.
"""

import logging
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from modulo.core.graph_validator import GraphValidator


def _snapshot(
    graph_json: dict[str, Any] | None = None,
    connector_bindings: list[dict[str, Any]] | None = None,
) -> MagicMock:
    snap = MagicMock()
    snap.graph_json = graph_json or {"nodes": [], "edges": []}
    snap.schema_pins_json = []
    snap.connector_bindings_json = connector_bindings or []
    snap.model_backend_pins_json = []
    return snap


def _session_returning(rows: list[Any]) -> AsyncMock:
    """Mock session whose execute() returns the given rows via .scalars().all()."""
    session = AsyncMock()
    scalars_result = MagicMock()
    scalars_result.all.return_value = rows
    execute_result = MagicMock()
    execute_result.scalars.return_value = scalars_result
    session.execute = AsyncMock(return_value=execute_result)
    return session


def _connector_instance(
    cid: uuid.UUID,
    *,
    connector_type_id: str = "filesystem",
    allowed_operations: Any = None,
) -> MagicMock:
    c = MagicMock()
    c.id = cid
    c.name = f"conn-{cid}"
    c.status = "active"
    c.connector_type_id = connector_type_id
    c.allowed_operations = allowed_operations if allowed_operations is not None else []
    c.config_json = {}
    return c


def _node_graph(node: dict[str, Any]) -> dict[str, Any]:
    return {"nodes": [node], "edges": []}


def _binding(cid: uuid.UUID, node_id: str = "a") -> dict[str, Any]:
    return {"node_id": node_id, "connector_instance_id": str(cid)}


async def test_routed_query_binding_requires_read():
    """A routed ``query`` binding requires the ``read`` ACL operation."""
    cid = uuid.uuid4()
    instance = _connector_instance(cid, allowed_operations=["write"])
    node = {"id": "a", "node_type": "agent", "connector_binding": {"instance_id": str(cid), "operation": "query"}}
    snap = _snapshot(graph_json=_node_graph(node), connector_bindings=[_binding(cid)])
    result = await GraphValidator().validate(snap, _session_returning([instance]))
    assert not result.is_valid
    issue = next(i for i in result.issues if i.code == "CONNECTOR_MISSING_OPERATIONS")
    assert "read" in issue.message


async def test_write_verb_requires_write():
    """A routed ``write`` binding requires the ``write`` ACL operation."""
    cid = uuid.uuid4()
    instance = _connector_instance(cid, allowed_operations=["read"])
    node = {"id": "a", "node_type": "agent", "connector_binding": {"instance_id": str(cid), "operation": "write"}}
    snap = _snapshot(graph_json=_node_graph(node), connector_bindings=[_binding(cid)])
    result = await GraphValidator().validate(snap, _session_returning([instance]))
    assert not result.is_valid
    assert any(i.code == "CONNECTOR_MISSING_OPERATIONS" for i in result.issues)


async def test_dispatch_trigger_run_requires_write():
    """``dispatch`` / ``trigger_run`` is gated ``write`` (there is no dispatch ACL op)."""
    cid = uuid.uuid4()
    instance = _connector_instance(cid, connector_type_id="github_actions_ci", allowed_operations=["read"])
    node = {
        "id": "a",
        "node_type": "agent",
        "connector_binding": {"instance_id": str(cid), "operation": "dispatch", "dispatch_action": "trigger_run"},
    }
    snap = _snapshot(graph_json=_node_graph(node), connector_bindings=[_binding(cid)])
    result = await GraphValidator().validate(snap, _session_returning([instance]))
    assert not result.is_valid
    assert any(i.code == "CONNECTOR_MISSING_OPERATIONS" for i in result.issues)


async def test_dispatch_trigger_run_await_completion_requires_write_and_read():
    """``await_completion`` adds a ``read`` leg (the wait loop polls status/logs).

    The instance allows ``read`` only, so the missing operation is ``write`` and
    the message must reflect that first failure.
    """
    cid = uuid.uuid4()
    instance = _connector_instance(cid, connector_type_id="github_actions_ci", allowed_operations=["read"])
    node = {
        "id": "a",
        "node_type": "agent",
        "connector_binding": {
            "instance_id": str(cid),
            "operation": "dispatch",
            "dispatch_action": "trigger_run",
            "await_completion": True,
        },
    }
    snap = _snapshot(graph_json=_node_graph(node), connector_bindings=[_binding(cid)])
    result = await GraphValidator().validate(snap, _session_returning([instance]))
    assert not result.is_valid
    issue = next(i for i in result.issues if i.code == "CONNECTOR_MISSING_OPERATIONS")
    assert "write" in issue.message


async def test_unrouted_agent_with_agent_id_is_not_checked():
    """An ``agent`` node with an ``agent_id`` never routes to the connector.

    Its binding is dead configuration: an allowlist that would RAISE if the
    binding were checked must still validate because the check is skipped.
    """
    cid = uuid.uuid4()
    instance = _connector_instance(cid, allowed_operations=["read"])
    node = {
        "id": "a",
        "node_type": "agent",
        "agent_id": "x",
        "connector_binding": {"instance_id": str(cid), "operation": "write"},
    }
    snap = _snapshot(graph_json=_node_graph(node), connector_bindings=[_binding(cid)])
    result = await GraphValidator().validate(snap, _session_returning([instance]))
    assert result.is_valid


async def test_sandbox_agent_binding_not_checked():
    """A ``sandbox_agent`` binding is inert — never capability-checked."""
    cid = uuid.uuid4()
    instance = _connector_instance(cid, allowed_operations=["read"])
    node = {
        "id": "a",
        "node_type": "sandbox_agent",
        "template_id": "opencode",
        "agent_prompt": "do the thing",
        "agent_commands": ["echo hi"],
        "connector_binding": {"instance_id": str(cid), "operation": "write"},
    }
    snap = _snapshot(graph_json=_node_graph(node), connector_bindings=[_binding(cid)])
    result = await GraphValidator().validate(snap, _session_returning([instance]))
    assert result.is_valid


async def test_legacy_qualified_allowlist_grants_derived_read():
    """FAR-1594 parity: the ACL canonicalises a same-type legacy spelling."""
    cid = uuid.uuid4()
    instance = _connector_instance(cid, connector_type_id="github", allowed_operations=["github.read"])
    node = {"id": "a", "node_type": "agent", "connector_binding": {"instance_id": str(cid), "operation": "query"}}
    snap = _snapshot(graph_json=_node_graph(node), connector_bindings=[_binding(cid)])
    result = await GraphValidator().validate(snap, _session_returning([instance]))
    assert result.is_valid


async def test_binding_node_missing_from_graph_skips_with_warning(caplog):
    """A binding whose node is absent from the graph is skipped, loudly.

    A binding row referencing a node the graph does not contain is a harness
    bug, not customer input: fail open with a log rather than reject the save.
    """
    cid = uuid.uuid4()
    instance = _connector_instance(cid, allowed_operations=["read"])
    snap = _snapshot(
        graph_json={"nodes": [{"id": "other", "node_type": "agent"}], "edges": []},
        connector_bindings=[_binding(cid, node_id="ghost")],
    )
    with caplog.at_level(logging.WARNING):
        result = await GraphValidator().validate(snap, _session_returning([instance]))
    assert result.is_valid
    assert "graph_validator.connector_binding_node_missing" in caplog.text


async def test_malformed_allowlist_fails_closed():
    """A malformed allowlist restricts to nothing: every derived op is denied."""
    cid = uuid.uuid4()
    instance = _connector_instance(cid, allowed_operations={"read": 1})
    node = {"id": "a", "node_type": "agent", "connector_binding": {"instance_id": str(cid), "operation": "query"}}
    snap = _snapshot(graph_json=_node_graph(node), connector_bindings=[_binding(cid)])
    result = await GraphValidator().validate(snap, _session_returning([instance]))
    assert not result.is_valid
    assert any(i.code == "CONNECTOR_MISSING_OPERATIONS" for i in result.issues)
