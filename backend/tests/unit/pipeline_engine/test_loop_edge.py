"""Unit tests for loop edge type in pipeline graphs."""

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from modulo.core.graph_validator import GraphValidator
from modulo.core.pipeline_engine.graph_cache import (
    _CACHE,
    _make_loop_router,
    build_graph_from_json,
)

pytestmark = pytest.mark.usefixtures("_auto_clear_cache")


@pytest.fixture(autouse=True)
def _auto_clear_cache() -> None:
    _CACHE.clear()


# ---------------------------------------------------------------------------
# _make_loop_router — pure function tests
# ---------------------------------------------------------------------------


class TestMakeLoopRouter:
    """Direct unit tests for the loop router function."""

    def test_always_loops_no_condition_no_max(self):
        router = _make_loop_router("source", "target", "exit", 0, None)
        state: dict[str, Any] = {"_iteration_counts": {}}
        for _ in range(10):
            assert router(state) == "target"
        # Counter increments each call
        assert state["_iteration_counts"]["source->target"] == 10

    def test_max_iterations_respected(self):
        router = _make_loop_router("source", "target", "exit", 3, None)
        state: dict[str, Any] = {"_iteration_counts": {}}
        assert router(state) == "target"  # 1 (count=1, < 3)
        assert router(state) == "target"  # 2 (count=2, < 3)
        assert router(state) == "exit"  # 3 (count=3, >= 3)
        assert router(state) == "exit"  # 4 (count=4, >= 3)

    def test_condition_truthy_loops(self):
        router = _make_loop_router("source", "target", "exit", 0, "iterations < `3`")
        state: dict[str, Any] = {"_iteration_counts": {"source->target": 0}, "iterations": 2}
        assert router(state) == "target"

    def test_condition_falsy_exits(self):
        router = _make_loop_router("source", "target", "exit", 0, "iterations >= `10`")
        state: dict[str, Any] = {"_iteration_counts": {"source->target": 0}, "iterations": 5}
        assert router(state) == "exit"

    def test_max_iterations_takes_precedence_over_condition(self):
        router = _make_loop_router("source", "target", "exit", 2, "whatever == `true`")
        state: dict[str, Any] = {"_iteration_counts": {}, "whatever": True}
        assert router(state) == "target"  # 1
        assert router(state) == "exit"  # 2 (count >= max_iterations)

    def test_unlimited_no_condition(self):
        router = _make_loop_router("source", "target", "exit", 0, None)
        state: dict[str, Any] = {"_iteration_counts": {}}
        for _ in range(100):
            route = router(state)
            assert route == "target"
        assert state["_iteration_counts"]["source->target"] == 100

    def test_unseeded_state_is_seeded_by_router(self):
        """The router must establish the counter when state was never seeded.

        This is the belt-and-suspenders guard for the prod bug where
        ``_seed_state`` did not seed ``_iteration_counts``: the router must
        place the dict into the shared state so its in-place mutation persists
        across steps and ``max_iterations`` can trip.
        """
        router = _make_loop_router("source", "target", "exit", 2, None)
        state: dict[str, Any] = {}
        assert router(state) == "target"  # 1 (count=1, < 2)
        assert router(state) == "exit"  # 2 (count=2, >= 2)
        assert router(state) == "exit"  # 3 (count=3, >= 2)
        assert state["_iteration_counts"]["source->target"] == 3

    def test_unseeded_state_does_not_overwrite_existing_counts(self):
        """A pre-seeded counter dict must never be replaced by the guard."""
        router = _make_loop_router("source", "target", "exit", 1, None)
        state: dict[str, Any] = {"_iteration_counts": {"source->target": 9}}
        assert router(state) == "exit"  # count=10, >= 1
        assert state["_iteration_counts"]["source->target"] == 10


# ---------------------------------------------------------------------------
# build_graph_from_json — integration tests
# ---------------------------------------------------------------------------

_LOOP_SELF_GRAPH: dict[str, Any] = {
    "nodes": [
        {"id": "entry", "role": None},
        {"id": "worker", "role": None},
        {"id": "done", "role": None},
    ],
    "edges": [
        {"source": "entry", "target": "worker", "type": "normal"},
        {
            "source": "worker",
            "target": "worker",
            "type": "loop",
            "max_iterations": 3,
            "default_target": "done",
        },
    ],
}

_LOOP_WITH_CONDITION_GRAPH: dict[str, Any] = {
    "nodes": [
        {"id": "entry", "role": None},
        {"id": "worker", "role": None},
        {"id": "done", "role": None},
    ],
    "edges": [
        {"source": "entry", "target": "worker", "type": "normal"},
        {
            "source": "worker",
            "target": "worker",
            "type": "loop",
            "condition_expression": "retry_count < `3`",
            "default_target": "done",
        },
    ],
}

_LOOP_WITH_NORMAL_FALLBACK_GRAPH: dict[str, Any] = {
    "nodes": [
        {"id": "entry", "role": None},
        {"id": "worker", "role": None},
        {"id": "done", "role": None},
    ],
    "edges": [
        {"source": "entry", "target": "worker", "type": "normal"},
        {
            "source": "worker",
            "target": "worker",
            "type": "loop",
            "max_iterations": 5,
            "default_target": "done",
        },
        {"source": "worker", "target": "done", "type": "normal"},
    ],
}


class TestBuildGraphWithLoop:
    """Integration tests for build_graph_from_json with loop edges."""

    def test_loop_edge_compiles_successfully(self):
        compiled = build_graph_from_json(_LOOP_SELF_GRAPH)
        assert compiled is not None

    async def test_loop_with_max_iterations_executes_and_terminates(self):
        """Worker runs max_iterations times then routes to done and stops."""
        compiled = build_graph_from_json(_LOOP_SELF_GRAPH)
        initial_state: dict[str, Any] = {
            "run_context": {"cancelled": False, "input": {}},
            "_iteration_counts": {},
            "artifacts": [],
        }
        config = {"configurable": {"thread_id": str(uuid.uuid4())}}
        result = await compiled.ainvoke(initial_state, config)
        # entry + worker (3 iterations) + done = 5 artifacts
        assert len(result["artifacts"]) == 5
        node_ids = [a["node_id"] for a in result["artifacts"]]
        assert node_ids == ["entry", "worker", "worker", "worker", "done"]

    async def test_loop_with_condition_respected(self):
        compiled = build_graph_from_json(_LOOP_WITH_CONDITION_GRAPH)
        initial_state: dict[str, Any] = {
            "run_context": {"cancelled": False, "input": {}},
            "_iteration_counts": {},
            "retry_count": 5,
            "artifacts": [],
        }
        config = {"configurable": {"thread_id": str(uuid.uuid4())}}
        result = await compiled.ainvoke(initial_state, config)
        # retry_count=5, condition is "retry_count < 3" which is falsy
        # So worker runs once, exits to done
        # entry + worker + done = 3 artifacts
        node_ids = [a["node_id"] for a in result["artifacts"]]
        assert node_ids == ["entry", "worker", "done"]

    async def test_loop_with_normal_fallback(self):
        """Loop edge coexists with normal edge from same source."""
        compiled = build_graph_from_json(_LOOP_WITH_NORMAL_FALLBACK_GRAPH)
        initial_state: dict[str, Any] = {
            "run_context": {"cancelled": False, "input": {}},
            "_iteration_counts": {},
            "artifacts": [],
        }
        config = {"configurable": {"thread_id": str(uuid.uuid4())}}
        result = await compiled.ainvoke(initial_state, config)
        node_ids = [a["node_id"] for a in result["artifacts"]]
        assert "entry" in node_ids
        assert "worker" in node_ids
        assert "done" in node_ids
        # Worker runs up to 5 times (max_iterations) then exits
        assert len(result["artifacts"]) == 7  # entry + worker*5 + done

    def test_loop_requires_default_target_or_normal_fallback(self):
        graph: dict[str, Any] = {
            "nodes": [
                {"id": "a", "role": None},
                {"id": "b", "role": None},
            ],
            "edges": [
                {"source": "a", "target": "b", "type": "loop", "max_iterations": 3},
            ],
        }
        with pytest.raises(ValueError, match="default_target"):
            build_graph_from_json(graph)

    async def test_loop_terminates_when_state_seeded_via_seed_state(self):
        """The executor's _seed_state must seed the loop counter so graphs terminate.

        Regression for the prod infinite-loop bug: _seed_state previously
        returned state WITHOUT ``_iteration_counts``, so the loop router's
        counter mutation was lost across steps (LangGraph shallow-copies the
        state dict per superstep) and ``max_iterations`` never tripped — the
        graph looped until the recursion limit. This exercises the real seeding
        path ``execute()`` uses and asserts the graph reaches the default target.
        """
        from modulo.core.pipeline_engine.executor import _seed_state

        snapshot = MagicMock()
        snapshot.run_context_defaults = {}
        snapshot.default_autonomy_level = None
        initial_state = _seed_state(snapshot, {})
        assert initial_state["_iteration_counts"] == {}

        graph: dict[str, Any] = {
            "nodes": [
                {"id": "entry", "role": None},
                {"id": "worker", "role": None},
                {"id": "end", "role": None},
            ],
            "edges": [
                {"source": "entry", "target": "worker", "type": "normal"},
                {
                    "source": "worker",
                    "target": "worker",
                    "type": "loop",
                    "max_iterations": 2,
                    "default_target": "end",
                },
            ],
        }
        compiled = build_graph_from_json(graph)
        config = {"configurable": {"thread_id": str(uuid.uuid4())}}
        result = await compiled.ainvoke(initial_state, config)
        node_ids = [a["node_id"] for a in result["artifacts"]]
        assert node_ids == ["entry", "worker", "worker", "end"]


# ---------------------------------------------------------------------------
# GraphValidator — loop edge validation tests
# ---------------------------------------------------------------------------


def _session_mock() -> AsyncMock:
    session = AsyncMock()
    scalars_result = MagicMock()
    scalars_result.all.return_value = []
    execute_result = MagicMock()
    execute_result.scalars.return_value = scalars_result
    session.execute = AsyncMock(return_value=execute_result)
    return session


class TestGraphValidatorLoopEdges:
    """Loop edge validation via GraphValidator."""

    async def test_valid_loop_edge_accepted(self):
        graph: dict[str, Any] = {
            "nodes": [{"id": "a"}, {"id": "b"}],
            "edges": [
                {"source": "a", "target": "b", "type": "normal"},
                {
                    "source": "b",
                    "target": "b",
                    "type": "loop",
                    "max_iterations": 5,
                    "default_target": "a",
                },
            ],
        }
        result = await GraphValidator().validate_definition(graph, _session_mock())
        assert result.is_valid

    async def test_loop_edge_missing_default_target_is_error(self):
        graph: dict[str, Any] = {
            "nodes": [{"id": "a"}, {"id": "b"}],
            "edges": [
                {"source": "a", "target": "b", "type": "normal"},
                {"source": "b", "target": "b", "type": "loop", "max_iterations": 3},
            ],
        }
        result = await GraphValidator().validate_definition(graph, _session_mock())
        assert not result.is_valid
        assert any(i.code == "LOOP_MISSING_DEFAULT_TARGET" for i in result.issues)

    async def test_loop_edge_nonexistent_default_target_is_error(self):
        graph: dict[str, Any] = {
            "nodes": [{"id": "a"}, {"id": "b"}],
            "edges": [
                {"source": "a", "target": "b", "type": "normal"},
                {
                    "source": "b",
                    "target": "b",
                    "type": "loop",
                    "max_iterations": 3,
                    "default_target": "nonexistent",
                },
            ],
        }
        result = await GraphValidator().validate_definition(graph, _session_mock())
        assert not result.is_valid
        assert any(i.code == "LOOP_DEFAULT_TARGET_NOT_FOUND" for i in result.issues)

    async def test_loop_edge_invalid_max_iterations(self):
        graph: dict[str, Any] = {
            "nodes": [{"id": "a"}, {"id": "b"}],
            "edges": [
                {"source": "a", "target": "b", "type": "normal"},
                {
                    "source": "b",
                    "target": "b",
                    "type": "loop",
                    "max_iterations": -1,
                    "default_target": "a",
                },
            ],
        }
        result = await GraphValidator().validate_definition(graph, _session_mock())
        assert not result.is_valid
        assert any(i.code == "LOOP_INVALID_MAX_ITERATIONS" for i in result.issues)

    async def test_loop_edge_string_max_iterations_rejected(self):
        graph: dict[str, Any] = {
            "nodes": [{"id": "a"}, {"id": "b"}],
            "edges": [
                {"source": "a", "target": "b", "type": "normal"},
                {
                    "source": "b",
                    "target": "b",
                    "type": "loop",
                    "max_iterations": "five",
                    "default_target": "a",
                },
            ],
        }
        result = await GraphValidator().validate_definition(graph, _session_mock())
        assert not result.is_valid
        assert any(i.code == "LOOP_INVALID_MAX_ITERATIONS" for i in result.issues)

    async def test_loop_edge_invalid_condition_expression(self):
        graph: dict[str, Any] = {
            "nodes": [{"id": "a"}, {"id": "b"}],
            "edges": [
                {"source": "a", "target": "b", "type": "normal"},
                {
                    "source": "b",
                    "target": "b",
                    "type": "loop",
                    "condition_expression": "invalid [[",
                    "default_target": "a",
                },
            ],
        }
        result = await GraphValidator().validate_definition(graph, _session_mock())
        assert not result.is_valid
        assert any(i.code == "LOOP_INVALID_EXPRESSION" for i in result.issues)

    async def test_loop_edge_valid_condition_expression_accepted(self):
        graph: dict[str, Any] = {
            "nodes": [{"id": "a"}, {"id": "b"}],
            "edges": [
                {"source": "a", "target": "b", "type": "normal"},
                {
                    "source": "b",
                    "target": "b",
                    "type": "loop",
                    "condition_expression": "status == 'pending'",
                    "default_target": "a",
                },
            ],
        }
        result = await GraphValidator().validate_definition(graph, _session_mock())
        assert result.is_valid

    async def test_loop_edge_does_not_block_entry_point(self):
        """A node with only loop edges (no normal inbound) is not the entry point."""
        graph: dict[str, Any] = {
            "nodes": [{"id": "entry"}, {"id": "worker"}],
            "edges": [
                {"source": "entry", "target": "worker", "type": "normal"},
                {
                    "source": "worker",
                    "target": "worker",
                    "type": "loop",
                    "max_iterations": 3,
                    "default_target": "entry",
                },
            ],
        }
        result = await GraphValidator().validate_definition(graph, _session_mock())
        assert result.is_valid

    async def test_loop_edge_excluded_from_reachability_bfs(self):
        """Loop edges are excluded from BFS, so a self-looping node doesn't break reachability."""
        graph: dict[str, Any] = {
            "nodes": [{"id": "entry"}, {"id": "worker"}],
            "edges": [
                {"source": "entry", "target": "worker", "type": "normal"},
                {
                    "source": "worker",
                    "target": "worker",
                    "type": "loop",
                    "max_iterations": 3,
                    "default_target": "entry",
                },
            ],
        }
        result = await GraphValidator().validate_definition(graph, _session_mock())
        assert result.is_valid
        # No unreachable-node warning
        assert not any(i.code == "TOPOLOGY_UNREACHABLE" for i in result.issues)

    async def test_loop_edge_skipped_in_schema_compatibility(self):
        """Loop edges should be skipped in schema compatibility checks."""
        graph: dict[str, Any] = {
            "nodes": [{"id": "entry"}, {"id": "worker"}],
            "edges": [
                {"source": "entry", "target": "worker", "type": "normal"},
                {
                    "source": "worker",
                    "target": "worker",
                    "type": "loop",
                    "max_iterations": 3,
                    "default_target": "entry",
                },
            ],
        }
        result = await GraphValidator().validate_definition(graph, _session_mock())
        assert result.is_valid
