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
from unittest.mock import MagicMock

import pytest

from modulo.connectors.base import connector_binding_required_acl_operations
from modulo.core.graph_validator import GraphValidator, ValidationResult
from tests.unit.graph_validator._helpers import (
    session_returning as _session_returning,
)


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
    c.visibility = "org"
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

    ``await_completion`` is a NODE-level field (a sibling of
    ``connector_binding``), so this pins both that it is read from the node and
    that it ADDS the read leg: the instance allows ``write`` only, so the
    derived ``read`` is the deciding failure — the test fails if the read leg is
    not derived.
    """
    cid = uuid.uuid4()
    instance = _connector_instance(cid, connector_type_id="github_actions_ci", allowed_operations=["write"])
    node = {
        "id": "a",
        "node_type": "agent",
        "await_completion": True,
        "connector_binding": {
            "instance_id": str(cid),
            "operation": "dispatch",
            "dispatch_action": "trigger_run",
        },
    }
    snap = _snapshot(graph_json=_node_graph(node), connector_bindings=[_binding(cid)])
    result = await GraphValidator().validate(snap, _session_returning([instance]))
    assert not result.is_valid
    issue = next(i for i in result.issues if i.code == "CONNECTOR_MISSING_OPERATIONS")
    assert "read" in issue.message


@pytest.mark.parametrize("action", ["get_run_status", "get_run_logs", "list_runs"])
async def test_dispatch_read_only_action_requires_read(action):
    """A read-only dispatch action is gated ``read`` at save time."""
    cid = uuid.uuid4()
    instance = _connector_instance(cid, connector_type_id="github_actions_ci", allowed_operations=["write"])
    node = {
        "id": "a",
        "node_type": "agent",
        "connector_binding": {
            "instance_id": str(cid),
            "operation": "dispatch",
            "dispatch_action": action,
        },
    }
    snap = _snapshot(graph_json=_node_graph(node), connector_bindings=[_binding(cid)])
    result = await GraphValidator().validate(snap, _session_returning([instance]))
    assert not result.is_valid
    issue = next(i for i in result.issues if i.code == "CONNECTOR_MISSING_OPERATIONS")
    assert "read" in issue.message


async def test_dispatch_missing_action_defaults_to_trigger_run_requiring_write():
    """An omitted ``dispatch_action`` is ``trigger_run`` at runtime (write-gated).

    Mirrors ``node_runner._make_node_fn`` / ``node_fires_dispatch_job``; the
    save-time check must derive the ``write`` requirement rather than skip.
    """
    cid = uuid.uuid4()
    instance = _connector_instance(cid, connector_type_id="github_actions_ci", allowed_operations=["read"])
    node = {
        "id": "a",
        "node_type": "agent",
        "connector_binding": {"instance_id": str(cid), "operation": "dispatch"},
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


async def test_unknown_verb_requires_read():
    """An unrecognised routed verb requires ``read``: the engine falls through to query.

    ``node_runner._run_connector_action`` routes every op that is neither
    ``dispatch`` nor ``write`` through ``connector.query()`` (read-gated), so a
    hand-written / legacy graph with ``operation="frobnicate"`` requires
    ``read`` exactly as ``query`` does. The instance allows ``write`` only, so
    the derived ``read`` is the deciding failure — the test fails if an unknown
    verb is skipped.
    """
    cid = uuid.uuid4()
    instance = _connector_instance(cid, allowed_operations=["write"])
    node = {
        "id": "a",
        "node_type": "agent",
        "connector_binding": {"instance_id": str(cid), "operation": "frobnicate"},
    }
    snap = _snapshot(graph_json=_node_graph(node), connector_bindings=[_binding(cid)])
    result = await GraphValidator().validate(snap, _session_returning([instance]))
    assert not result.is_valid
    issue = next(i for i in result.issues if i.code == "CONNECTOR_MISSING_OPERATIONS")
    assert "read" in issue.message


async def test_dispatch_unknown_action_derives_no_requirement():
    """A dispatch binding with an unknown NON-EMPTY action derives no requirement.

    The engine raises on the unknown action at graph build, so the save-time
    check must not invent a write gate the runtime never applies.
    """
    cid = uuid.uuid4()
    instance = _connector_instance(cid, connector_type_id="github_actions_ci", allowed_operations=["read"])
    node = {
        "id": "a",
        "node_type": "agent",
        "connector_binding": {"instance_id": str(cid), "operation": "dispatch", "dispatch_action": "frobnicate"},
    }
    snap = _snapshot(graph_json=_node_graph(node), connector_bindings=[_binding(cid)])
    result = await GraphValidator().validate(snap, _session_returning([instance]))
    assert result.is_valid


def test_helper_returns_empty_for_unrouted_node():
    """The helper's own guard: a node the engine never routes has no verb to gate."""
    node = {
        "id": "a",
        "node_type": "agent",
        "agent_id": "x",
        "connector_binding": {"instance_id": "00000000-0000-0000-0000-000000000000", "operation": "write"},
    }
    assert not connector_binding_required_acl_operations(node)


async def test_binding_check_tolerates_unreadable_graph():
    """A graph the check cannot read yields an empty node map (fail-open + log)."""
    cid = uuid.uuid4()
    instance = _connector_instance(cid, allowed_operations=["read"])
    result = ValidationResult()
    await GraphValidator()._check_connector_bindings(
        [_binding(cid, node_id="a")],
        _session_returning([instance]),
        result,
        graph_json=None,
    )
    assert result.is_valid


async def test_binding_check_tolerates_graph_without_node_list():
    """A graph whose ``nodes`` is not a list is unreadable: empty node map."""
    cid = uuid.uuid4()
    instance = _connector_instance(cid, allowed_operations=["read"])
    result = ValidationResult()
    await GraphValidator()._check_connector_bindings(
        [_binding(cid, node_id="a")],
        _session_returning([instance]),
        result,
        graph_json={"edges": []},
    )
    assert result.is_valid


async def test_binding_check_skips_non_dict_and_idless_nodes():
    """Malformed node entries never enter the id map; the binding then skips."""
    cid = uuid.uuid4()
    instance = _connector_instance(cid, allowed_operations=["read"])
    result = ValidationResult()
    await GraphValidator()._check_connector_bindings(
        [_binding(cid, node_id="a")],
        _session_returning([instance]),
        result,
        graph_json={"nodes": ["not-a-node", {"node_type": "agent"}], "edges": []},
    )
    assert result.is_valid
