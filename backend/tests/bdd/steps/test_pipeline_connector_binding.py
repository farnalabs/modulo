"""Step definitions for swappable connector binding.

Wires ``features/connectors/swappable_binding.feature`` into the executing
suite (the 2026-09-16 product-map review pass) by driving the REAL
binding surfaces the pipeline save path uses:

- ``modulo.core.team_visibility.extract_connector_bindings`` — the pure
  extraction that turns per-node ``connector_binding`` objects into snapshot
  ``connector_bindings_json``. Swapping a node's binding swaps the extracted
  binding without touching the graph topology; an unbound node extracts nothing.
- ``modulo.core.graph_validator.GraphValidator.validate_definition`` — the
  save-time connector-capability check (``_check_connector_bindings``) that
  rejects a binding to a missing instance (``CONNECTOR_NOT_FOUND``) or one
  whose allowed operations do not cover the node's required operations
  (``CONNECTOR_MISSING_OPERATIONS``), and accepts an active binding that
  covers them.

The session is a mocked sqlalchemy ``AsyncSession`` returning the connector
instance rows — the same DB-free pattern ``backend/tests/unit/graph_validator/``
uses, so no live database is required to execute these scenarios.
"""

import asyncio
import contextlib
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/connectors/swappable_binding.feature")


@pytest.fixture
def ctx() -> dict:
    """Shared mutable context dict for the swappable-binding scenarios."""
    return {}


def _instance_uuid(name: str) -> uuid.UUID:
    """Deterministic UUID for a named connector instance."""
    return uuid.uuid5(uuid.NAMESPACE_DNS, f"connector:{name}")


def _connector_instance(
    instance_id: uuid.UUID,
    *,
    allowed_operations: list[str] | None = None,
) -> MagicMock:
    instance = MagicMock()
    instance.id = instance_id
    instance.name = f"conn-{str(instance_id)[:8]}"
    instance.status = "active"
    instance.allowed_operations = allowed_operations or []
    instance.config_json = {}
    return instance


def _session_returning(rows: list[Any]) -> AsyncMock:
    """Mock session whose execute() returns the given rows via .scalars().all()."""
    session = AsyncMock()
    scalars_result = MagicMock()
    scalars_result.all.return_value = rows
    execute_result = MagicMock()
    execute_result.scalars.return_value = scalars_result
    session.execute = AsyncMock(return_value=execute_result)
    return session


def _extract_bindings(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from modulo.core.team_visibility import extract_connector_bindings

    return extract_connector_bindings(nodes)


def _run_validation(ctx: dict, *, required_operations: list[str] | None) -> None:
    from modulo.core.graph_validator import GraphValidator

    name = ctx["instance_name"]
    instance_ids = [str(_instance_uuid(name))]
    rows = (
        [_connector_instance(_instance_uuid(name), allowed_operations=ctx.get("allowed_operations"))]
        if ctx.get("allowed_operations") is not None
        else []
    )
    bindings = [
        {
            "node_id": ctx["node_id"],
            "connector_instance_id": instance_ids[0],
            "required_operations": required_operations or [],
        }
    ]
    graph = {"nodes": ctx["nodes"], "edges": []}
    result = asyncio.run(
        GraphValidator().validate_definition(
            graph,
            _session_returning(rows),
            connector_bindings=bindings,
        )
    )
    ctx["result"] = result


@given(parsers.parse('a pipeline graph with a node "{node}" bound to connector instance "{instance}"'))
def _graph_bound(ctx: dict, node: str, instance: str) -> None:
    ctx["node_id"] = node
    ctx["instance_name"] = instance
    ctx["allowed_operations"] = None
    binding = {"instance_id": str(_instance_uuid(instance))}
    ctx["nodes"] = [{"id": node, "node_type": "agent", "connector_binding": binding}]


@given(parsers.parse('a pipeline graph with a node "{node}" and no connector binding'))
def _graph_unbound(ctx: dict, node: str) -> None:
    ctx["node_id"] = node
    ctx["instance_name"] = None
    ctx["allowed_operations"] = None
    ctx["nodes"] = [{"id": node, "node_type": "agent"}]


@when(parsers.parse('the node "{node}" binding is swapped to connector instance "{instance}"'))
def _swap_binding(ctx: dict, node: str, instance: str) -> None:
    for n in ctx["nodes"]:
        if n.get("id") == node:
            n["connector_binding"] = {"instance_id": str(_instance_uuid(instance))}
    ctx["extracted"] = _extract_bindings(ctx["nodes"])


@when("I extract the graph's connector bindings")
def _extract_bindings_step(ctx: dict) -> None:
    ctx["extracted"] = _extract_bindings(ctx["nodes"])


@when("I validate the graph with its connector bindings")
def _validate_with_bindings(ctx: dict) -> None:
    _run_validation(ctx, required_operations=[])


@given(parsers.parse('connector instance "{instance}" allows only "{op}"'))
def _instance_allows_only(ctx: dict, instance: str, op: str) -> None:
    ctx["allowed_operations"] = [op]


@given(parsers.parse('connector instance "{instance}" allows "{a}" and "{b}"'))
def _instance_allows_both(ctx: dict, instance: str, a: str, b: str) -> None:
    ctx["allowed_operations"] = [a, b]


@when(parsers.parse('I validate the graph with required operations "{ops}"'))
def _validate_with_required_ops(ctx: dict, ops: str) -> None:
    _run_validation(ctx, required_operations=[ops])


@then(parsers.parse('the graph carries exactly one connector binding pointing at "{instance}"'))
def _one_binding_points_at(ctx: dict, instance: str) -> None:
    expected = {"node_id": ctx["node_id"], "connector_instance_id": str(_instance_uuid(instance))}
    assert ctx["extracted"] == [expected], f"expected exactly one binding {expected}, got: {ctx['extracted']}"


@then(parsers.parse('no connector binding references "{instance}"'))
def _no_binding_references(ctx: dict, instance: str) -> None:
    assert not any(b.get("connector_instance_id") == str(_instance_uuid(instance)) for b in ctx["extracted"]), ctx[
        "extracted"
    ]


@then("no connector binding is extracted")
def _no_binding_extracted(ctx: dict) -> None:
    assert not ctx["extracted"], ctx["extracted"]


@then(parsers.parse('the graph is rejected with error code "{code}"'))
def _graph_rejected(ctx: dict, code: str) -> None:
    result = ctx["result"]
    assert result is not None, "no validation result"
    assert not result.is_valid, "expected the graph to be rejected"
    assert any(issue.code == code for issue in result.issues), (
        f"expected error code {code!r}, got: {[i.code for i in result.issues]}"
    )


@then("the graph is valid")
def _graph_valid(ctx: dict) -> None:
    result = ctx["result"]
    assert result is not None, "no validation result"
    assert result.is_valid, f"expected a valid graph, got issues: {result.issues}"
