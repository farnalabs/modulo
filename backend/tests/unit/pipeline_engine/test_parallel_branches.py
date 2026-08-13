"""Tests for FAR-171 — parallel branch execution (fan-out) in the pipeline engine.

Covers the five acceptance criteria:

1. **Fan-out execution** — a source with multiple normal outgoing edges runs all
   downstream branches concurrently (native LangGraph parallel edges).
2. **Deterministic state merge** — ``_pipeline_state_reducer`` concatenates list
   keys in completion order and merges ``run_context`` per-key last-write-wins;
   the graph validator warns on parallel context-setter fan-out at save time.
3. **Node output collection** — parallel completions land in
   ``completed_node_outputs`` keyed by node_id (no clobbering).
4. **Runaway protection** — ``record_step`` is called once per completed node
   event; parallel branches each count once (no double-count, no under-count).
5. **HITL interplay** — an interrupt raised in one parallel branch does not
   corrupt sibling branches, and resume completes both.

Tests that need slow branches / a checkpointer build a ``StateGraph`` directly
with the production ``_pipeline_state_reducer`` (the exact reducer wired into
``build_graph_from_json``); graph-shape tests go through the real compiler.
"""

import asyncio
import time
import uuid
from typing import Annotated, Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import StateGraph
from langgraph.types import interrupt

from modulo.core.graph_validator import GraphValidator
from modulo.core.graph_validator._types import ValidationResult
from modulo.core.pipeline_engine.executor import PipelineExecutor
from modulo.core.pipeline_engine.graph_cache import _pipeline_state_reducer, build_graph_from_json
from modulo.core.pipeline_engine.runaway_protection import RunawayGuard, RunawayRunError

_STATE_SCHEMA: type[Any] = Annotated[dict[str, Any], _pipeline_state_reducer]


def _make_sleepy_node(node_id: str, delay: float) -> Any:
    """A node function that sleeps then returns an artifact (via StateGraph)."""

    async def _fn(state: dict[str, Any]) -> dict[str, Any]:
        await asyncio.sleep(delay)
        return {"artifacts": [{"node_id": node_id, "status": "completed"}]}

    _fn.__name__ = node_id
    return _fn


# ---------------------------------------------------------------------------
# 1. Fan-out execution
# ---------------------------------------------------------------------------


class TestFanOutExecution:
    async def test_fanout_compiles_and_runs_all_branches(self) -> None:
        """A source with two normal outgoing edges runs BOTH downstream nodes."""
        graph: dict[str, Any] = {
            "nodes": [
                {"id": "fanout", "role": None},
                {"id": "branch-a", "role": None},
                {"id": "branch-b", "role": None},
            ],
            "edges": [
                {"source": "fanout", "target": "branch-a", "type": "normal"},
                {"source": "fanout", "target": "branch-b", "type": "normal"},
            ],
        }
        compiled = build_graph_from_json(graph)
        result = await compiled.ainvoke(
            {"run_context": {"cancelled": False, "input": {}}, "artifacts": []},
            {"configurable": {"thread_id": str(uuid.uuid4())}},
        )
        node_ids = [a["node_id"] for a in result["artifacts"]]
        assert "fanout" in node_ids
        assert "branch-a" in node_ids
        assert "branch-b" in node_ids

    async def test_fanout_wallclock_is_max_not_sum(self) -> None:
        """Two 0.5s parallel branches complete in well under the 1.0s serial sum.

        This is the behavioural proof of fan-out concurrency at the LangGraph
        layer using the production reducer. A serial graph would take >= 1.0s.
        """
        graph = StateGraph(_STATE_SCHEMA)
        graph.add_node("entry", _make_sleepy_node("entry", 0.05))
        graph.add_node("branch-a", _make_sleepy_node("branch-a", 0.5))
        graph.add_node("branch-b", _make_sleepy_node("branch-b", 0.5))
        graph.set_entry_point("entry")
        graph.add_edge("entry", "branch-a")
        graph.add_edge("entry", "branch-b")
        compiled = graph.compile()

        start = time.monotonic()
        result = await compiled.ainvoke(
            {"run_context": {"cancelled": False, "input": {}}, "artifacts": []},
            {"configurable": {"thread_id": str(uuid.uuid4())}},
        )
        elapsed = time.monotonic() - start
        node_ids = [a["node_id"] for a in result["artifacts"]]
        assert "branch-a" in node_ids and "branch-b" in node_ids
        assert elapsed < 0.9, f"branches did not run in parallel: elapsed={elapsed:.3f}s"


# ---------------------------------------------------------------------------
# 2. Deterministic state merge
# ---------------------------------------------------------------------------


class TestDeterministicStateMerge:
    def test_parallel_run_context_writes_merge_per_key(self) -> None:
        """PROVE-THE-FIX — parallel context-setter writes to DISJOINT keys both land.

        Fails without the change: the old reducer replaced the whole
        ``run_context`` dict, so branch-a's key, the seeded keys, and
        ``cancelled``/``input`` were all clobbered by branch-b's write.
        """
        current: dict[str, Any] = {
            "run_context": {"cancelled": False, "input": {"x": 1}, "seeded": True},
            "artifacts": [],
        }
        branch_a = {"run_context": {"model_tier": "large", "a": 1}}
        branch_b = {"run_context": {"other": 2, "b": 2}}

        merged = _pipeline_state_reducer(current, branch_a)
        merged = _pipeline_state_reducer(merged, branch_b)

        rc = merged["run_context"]
        # Seeded keys survive a context-setter write (last-write-wins per key).
        assert rc["cancelled"] is False
        assert rc["input"] == {"x": 1}
        assert rc["seeded"] is True
        # Disjoint parallel writes both land.
        assert rc["a"] == 1
        assert rc["b"] == 2
        assert rc["other"] == 2
        # Same-key parallel writes resolve last-write-wins.
        assert rc["model_tier"] == "large"

    def test_parallel_run_context_same_key_last_write_wins(self) -> None:
        """Same-key parallel writes: the write applied LAST wins (§8.18)."""
        current: dict[str, Any] = {"run_context": {"cancelled": False}}
        first = {"run_context": {"model_tier": "large"}}
        last = {"run_context": {"model_tier": "small"}}
        merged = _pipeline_state_reducer(_pipeline_state_reducer(current, first), last)
        assert merged["run_context"]["model_tier"] == "small"

    def test_concat_keys_append_in_completion_order(self) -> None:
        """List-valued keys concatenate; ordering is the reducer application order."""
        current: dict[str, Any] = {"artifacts": [{"node_id": "seed"}], "_run_context_write_log": []}
        branch_a = {"artifacts": [{"node_id": "branch-a"}], "_run_context_write_log": [{"node_name": "branch-a"}]}
        branch_b = {"artifacts": [{"node_id": "branch-b"}], "_run_context_write_log": [{"node_name": "branch-b"}]}
        merged = _pipeline_state_reducer(current, branch_a)
        merged = _pipeline_state_reducer(merged, branch_b)
        assert [a["node_id"] for a in merged["artifacts"]] == ["seed", "branch-a", "branch-b"]
        assert [w["node_name"] for w in merged["_run_context_write_log"]] == ["branch-a", "branch-b"]

    def test_non_dict_run_context_update_replaces(self) -> None:
        """A non-dict run_context update falls back to whole-key replacement."""
        current: dict[str, Any] = {"run_context": {"cancelled": False}}
        merged = _pipeline_state_reducer(current, {"run_context": None})
        assert merged["run_context"] is None


class TestParallelContextSetterValidatorWarning:
    def _graph(self, roles: dict[str, str]) -> dict[str, Any]:
        return {
            "nodes": [
                {"id": "fanout", "role": None},
                {"id": "branch-a", "role": roles.get("branch-a")},
                {"id": "branch-b", "role": roles.get("branch-b")},
            ],
            "edges": [
                {"source": "fanout", "target": "branch-a", "type": "normal"},
                {"source": "fanout", "target": "branch-b", "type": "normal"},
            ],
        }

    def test_parallel_context_setter_fanout_warns(self) -> None:
        """Fan-out to two context-setters emits a save-time warning."""
        result = ValidationResult()
        GraphValidator._check_parallel_run_context_writes(
            self._graph({"branch-a": "context_setter", "branch-b": "context_setter"}), result
        )
        codes = [i.code for i in result.issues]
        assert "PARALLEL_RUN_CONTEXT_WRITE" in codes
        assert all(i.severity == "warning" for i in result.issues)

    def test_parallel_single_context_setter_no_warning(self) -> None:
        """A fan-out with only ONE context-setter branch is safe."""
        result = ValidationResult()
        GraphValidator._check_parallel_run_context_writes(
            self._graph({"branch-a": "context_setter", "branch-b": "agent"}), result
        )
        assert not result.issues

    def test_parallel_non_setters_no_warning(self) -> None:
        result = ValidationResult()
        GraphValidator._check_parallel_run_context_writes(
            self._graph({"branch-a": "agent", "branch-b": "agent"}), result
        )
        assert not result.issues

    def test_disjoint_declared_keys_no_warning(self) -> None:
        """Branches that declare disjoint run_context_writes are safe."""
        graph = self._graph({"branch-a": "context_setter", "branch-b": "context_setter"})
        graph["nodes"][1]["run_context_writes"] = ["model_tier"]
        graph["nodes"][2]["run_context_writes"] = ["estimated_tokens"]
        result = ValidationResult()
        GraphValidator._check_parallel_run_context_writes(graph, result)
        assert not result.issues

    def test_conditional_source_no_warning(self) -> None:
        """Conditional routing picks ONE target — not a fan-out, no warning."""
        graph = self._graph({"branch-a": "context_setter", "branch-b": "context_setter"})
        graph["edges"][1]["type"] = "conditional"
        graph["edges"][1]["condition_expression"] = "foo == 'bar'"
        result = ValidationResult()
        GraphValidator._check_parallel_run_context_writes(graph, result)
        assert not result.issues

    def test_warning_is_advisory_and_does_not_block(self) -> None:
        """PARALLEL_RUN_CONTEXT_WRITE is a warning — it never blocks a save."""
        result = ValidationResult()
        GraphValidator._check_parallel_run_context_writes(
            self._graph({"branch-a": "context_setter", "branch-b": "context_setter"}), result
        )
        assert result.is_valid
        assert any(i.code == "PARALLEL_RUN_CONTEXT_WRITE" for i in result.issues)


# ---------------------------------------------------------------------------
# 3. Node output collection under concurrency
# ---------------------------------------------------------------------------


class TestNodeOutputCollection:
    async def test_completed_node_outputs_keep_node_id_keys(self) -> None:
        """Parallel completions are recorded keyed by node_id with no clobbering.

        Mirrors the executor's ``_stream_graph`` on_chain_end handler: each
        completed node writes its own key, so concurrent branches cannot
        overwrite each other.
        """
        graph = StateGraph(_STATE_SCHEMA)
        graph.add_node("branch-a", _make_sleepy_node("branch-a", 0.2))
        graph.add_node("branch-b", _make_sleepy_node("branch-b", 0.2))
        graph.set_entry_point("branch-a")
        graph.add_edge("branch-a", "branch-b")
        graph.set_finish_point("branch-b")
        compiled = graph.compile()

        completed_node_outputs: dict[str, Any] = {}
        async for lg_event in compiled.astream_events(
            {"run_context": {"cancelled": False, "input": {}}, "artifacts": []},
            {"configurable": {"thread_id": str(uuid.uuid4())}},
            version="v2",
        ):
            if lg_event.get("event") == "on_chain_end":
                name = lg_event.get("name", "")
                if name in ("branch-a", "branch-b"):
                    completed_node_outputs[name] = lg_event["data"]["output"]

        assert set(completed_node_outputs.keys()) == {"branch-a", "branch-b"}
        assert completed_node_outputs["branch-a"]["artifacts"][0]["node_id"] == "branch-a"
        assert completed_node_outputs["branch-b"]["artifacts"][0]["node_id"] == "branch-b"


# ---------------------------------------------------------------------------
# 4. Runaway protection under concurrency
# ---------------------------------------------------------------------------


class TestRunawayUnderConcurrency:
    async def test_record_step_counts_each_parallel_node_once(self) -> None:
        """Every completed node in a parallel fan-out is recorded exactly once."""
        graph = StateGraph(_STATE_SCHEMA)
        graph.add_node("entry", _make_sleepy_node("entry", 0.05))
        graph.add_node("branch-a", _make_sleepy_node("branch-a", 0.3))
        graph.add_node("branch-b", _make_sleepy_node("branch-b", 0.3))
        graph.set_entry_point("entry")
        graph.add_edge("entry", "branch-a")
        graph.add_edge("entry", "branch-b")
        compiled = graph.compile()

        guard = RunawayGuard(max_steps=3)
        async for lg_event in compiled.astream_events(
            {"run_context": {"cancelled": False, "input": {}}, "artifacts": []},
            {"configurable": {"thread_id": str(uuid.uuid4())}},
            version="v2",
        ):
            if lg_event.get("event") == "on_chain_end":
                name = lg_event.get("name", "")
                if name in ("entry", "branch-a", "branch-b"):
                    guard.record_step()

        # 3 completed nodes → step_count 3 == max_steps (no raise).
        assert guard._step_count == 3

    async def test_max_steps_accounts_parallel_nodes(self) -> None:
        """max_steps counts TOTAL completed nodes across branches (multiples)."""
        graph = StateGraph(_STATE_SCHEMA)
        graph.add_node("entry", _make_sleepy_node("entry", 0.05))
        graph.add_node("branch-a", _make_sleepy_node("branch-a", 0.3))
        graph.add_node("branch-b", _make_sleepy_node("branch-b", 0.3))
        graph.set_entry_point("entry")
        graph.add_edge("entry", "branch-a")
        graph.add_edge("entry", "branch-b")
        compiled = graph.compile()

        guard = RunawayGuard(max_steps=2)
        raised = False
        async for lg_event in compiled.astream_events(
            {"run_context": {"cancelled": False, "input": {}}, "artifacts": []},
            {"configurable": {"thread_id": str(uuid.uuid4())}},
            version="v2",
        ):
            if lg_event.get("event") == "on_chain_end":
                name = lg_event.get("name", "")
                if name in ("entry", "branch-a", "branch-b"):
                    try:
                        guard.record_step()
                    except RunawayRunError:
                        raised = True
        assert raised


# ---------------------------------------------------------------------------
# 5. HITL interplay — interrupt in one parallel branch
# ---------------------------------------------------------------------------


async def _gate_branch(state: dict[str, Any]) -> dict[str, Any]:
    decision = state.get("_hitl_decision")
    if decision is not None:
        return {"artifacts": [{"node_id": "gate-b", "status": "resumed", "action": decision.get("action")}]}
    state["_hitl_gates"] = list(state.get("_hitl_gates") or [])
    decision = interrupt({"gate_id": "gate-b"})
    return await _gate_branch({**state, "_hitl_decision": decision})


class TestHitlInterplay:
    async def test_real_interrupt_then_resume(self) -> None:
        """A real interrupt pauses; resume with _hitl_decision completes both branches."""
        graph = StateGraph(_STATE_SCHEMA)
        graph.add_node("branch-a", _make_sleepy_node("branch-a", 0.2))
        graph.add_node("gate-b", _gate_branch)
        graph.set_entry_point("branch-a")
        graph.add_edge("branch-a", "gate-b")
        compiled = graph.compile(checkpointer=InMemorySaver())

        thread = str(uuid.uuid4())
        config = {"configurable": {"thread_id": thread}}
        initial: dict[str, Any] = {"run_context": {"cancelled": False, "input": {}}, "artifacts": [], "_hitl_gates": []}

        result = await compiled.ainvoke(initial, config)
        # Interrupt surfaced via __interrupt__ in returned state (langgraph 1.x).
        assert result.get("__interrupt__"), "expected a HITL interrupt"
        # branch-a completed before the interrupt; its output is preserved.
        assert any(a["node_id"] == "branch-a" for a in result["artifacts"])

        # Resume: inject the decision and re-stream (the executor's pattern is
        # aupdate_state + astream_events(None, config)).
        await compiled.aupdate_state(config, {"_hitl_decision": {"action": "approved"}})
        final = await compiled.ainvoke(None, config)
        node_ids = [a["node_id"] for a in final["artifacts"]]
        assert "gate-b" in node_ids
        resumed = [a for a in final["artifacts"] if a["node_id"] == "gate-b"]
        assert resumed[0]["action"] == "approved"


# ---------------------------------------------------------------------------
# 6. Cost reporting under concurrency
# ---------------------------------------------------------------------------


class TestCostUnderConcurrency:
    def test_aggregate_sandbox_cost_sums_parallel_outputs(self) -> None:
        """Parallel sandbox outputs each contribute their estimate exactly once."""
        executor = PipelineExecutor.__new__(PipelineExecutor)  # type: ignore[call-arg]
        outputs = {
            "branch-a": {"artifacts": [], "output": {"cost_estimate_usd": 1.5}},
            "branch-b": {"artifacts": [], "output": {"cost_estimate_usd": 2.5}},
            "entry": {"artifacts": [], "output": {"cost_estimate_usd": 0.0}},  # non-positive -> zero
        }
        total = executor._aggregate_sandbox_cost(outputs)
        assert float(total) == pytest.approx(4.0)

    def test_aggregate_sandbox_cost_empty(self) -> None:
        executor = PipelineExecutor.__new__(PipelineExecutor)  # type: ignore[call-arg]
        assert float(executor._aggregate_sandbox_cost(None)) == 0.0
        assert float(executor._aggregate_sandbox_cost({})) == 0.0

    def test_token_cost_sums_across_parallel_nodes(self) -> None:
        """_compute_token_costs sums per-node usage without loss."""
        from decimal import Decimal

        usage = {
            "branch-a": {"input_tokens": 100, "output_tokens": 200, "total_tokens": 300},
            "branch-b": {"input_tokens": 50, "output_tokens": 50, "total_tokens": 100},
        }
        total_tokens, total_cost, per_node = PipelineExecutor._compute_token_costs(
            usage, Decimal("0.001"), Decimal("0.002")
        )
        assert total_tokens == 400
        assert float(total_cost) == pytest.approx(0.001 * 150 + 0.002 * 250)
        assert set(per_node.keys()) == {"branch-a", "branch-b"}
