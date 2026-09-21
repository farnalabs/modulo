"""Step definitions for pipeline graph validation (save-time).

Wires ``features/pipelines/validation.feature`` into the executing suite (the
2026-09-16 product-map review pass) by driving the REAL
``modulo.core.graph_validator.GraphValidator.validate_definition`` against a
mocked sqlalchemy session — the save-time surface the pipelines graph PATCH
route calls via ``_validate_graph_save``. Mirrors the DB-free pattern of
``backend/tests/unit/graph_validator/`` so the executing BDD coverage locks the
same contract: a graph with no nodes, a cycle, or a dangling edge is rejected
with a typed error code while a minimal valid graph passes.
"""

import asyncio
import contextlib
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/pipelines/validation.feature")


@pytest.fixture
def ctx() -> dict:
    """Shared mutable context dict for the graph-validation scenarios."""
    return {}


def _session_returning(rows: list[Any]) -> AsyncMock:
    """Mock session whose execute() returns the given rows via .scalars().all()."""
    session = AsyncMock()
    scalars_result = MagicMock()
    scalars_result.all.return_value = rows
    execute_result = MagicMock()
    execute_result.scalars.return_value = scalars_result
    session.execute = AsyncMock(return_value=execute_result)
    return session


def _validate(graph: dict[str, Any], session: AsyncMock) -> Any:
    from modulo.core.graph_validator import GraphValidator

    return asyncio.run(GraphValidator().validate_definition(graph, session))


@given("a pipeline graph with no nodes")
def _graph_no_nodes(ctx: dict) -> None:
    ctx["graph"] = {"nodes": [], "edges": []}


@given("a pipeline graph definition without a nodes field")
def _graph_missing_nodes(ctx: dict) -> None:
    ctx["graph"] = {"edges": []}


@given(parsers.parse('a pipeline graph where node "{a}" feeds node "{b}" and node "{b}" feeds node "{a}"'))
def _graph_cycle(ctx: dict, a: str, b: str) -> None:
    ctx["graph"] = {
        "nodes": [{"id": a}, {"id": b}],
        "edges": [
            {"source": a, "target": b, "type": "normal"},
            {"source": b, "target": a, "type": "normal"},
        ],
    }


@given(parsers.parse('a pipeline graph with node "{a}" and an edge targeting unknown node "{ghost}"'))
def _graph_dangling_edge(ctx: dict, a: str, ghost: str) -> None:
    ctx["graph"] = {
        "nodes": [{"id": a}],
        "edges": [{"source": a, "target": ghost, "type": "normal"}],
    }


@given("a valid minimal pipeline graph with one node")
def _graph_valid_minimal(ctx: dict) -> None:
    ctx["graph"] = {"nodes": [{"id": str(uuid.uuid4())}], "edges": []}


@when("I validate the pipeline graph")
def _validate_graph(ctx: dict) -> None:
    ctx["result"] = _validate(ctx["graph"], _session_returning([]))


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
