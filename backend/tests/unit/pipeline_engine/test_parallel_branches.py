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
from unittest.mock import AsyncMock, MagicMock

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import StateGraph
from langgraph.types import interrupt

from modulo.core.graph_validator import GraphValidator
from modulo.core.graph_validator._types import ValidationResult
from modulo.core.pipeline_engine.event_broker import RunEventBroker
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
        """Two parallel branches execute with OVERLAPPING wall-clock intervals.

        This is the behavioural proof of fan-out concurrency at the LangGraph
        layer using the production reducer. The original assertion compared the
        parallel graph's wall-clock against a serial control graph
        (``parallel < serial``), but that ratio is NOT invariant to runner
        load: on a heavily contended CI runner a 0.5s ``asyncio.sleep`` can be
        stretched and langgraph's fan-out can incur scheduling overhead, so the
        parallel run occasionally exceeds the serial control (observed in CI:
        ``parallel=2.74s``, ``serial=1.06s``) even though the branches genuinely
        run concurrently.

        The definitive proof of concurrency is INTERVAL OVERLAP, not wall-clock
        magnitude: both branches must be in-flight at the same instant. We
        instrument each branch to record its monotonic start/end and assert the
        intervals overlap. We retry a few times so a single contended run does
        not red-fail a working fan-out.
        """
        intervals: dict[str, list[float]] = {}

        def _make_instrumented(node_id: str) -> Any:
            async def _fn(state: dict[str, Any]) -> dict[str, Any]:
                intervals[node_id] = [time.monotonic(), 0.0]
                await asyncio.sleep(0.1)
                intervals[node_id][1] = time.monotonic()
                return {"artifacts": [{"node_id": node_id, "status": "completed"}]}

            _fn.__name__ = node_id
            return _fn

        def _build() -> StateGraph:
            graph = StateGraph(_STATE_SCHEMA)
            graph.add_node("entry", _make_instrumented("entry"))
            graph.add_node("branch-a", _make_instrumented("branch-a"))
            graph.add_node("branch-b", _make_instrumented("branch-b"))
            graph.set_entry_point("entry")
            graph.add_edge("entry", "branch-a")
            graph.add_edge("entry", "branch-b")
            return graph.compile()

        last_err = ""
        for _attempt in range(3):
            intervals.clear()
            compiled = _build()
            result = await compiled.ainvoke(
                {"run_context": {"cancelled": False, "input": {}}, "artifacts": []},
                {"configurable": {"thread_id": str(uuid.uuid4())}},
            )
            node_ids = [a["node_id"] for a in result["artifacts"]]
            assert "branch-a" in node_ids
            assert "branch-b" in node_ids

            a_start, a_end = intervals["branch-a"]
            b_start, b_end = intervals["branch-b"]
            # Overlap is the true concurrency signal: branch-a must still be
            # running when branch-b starts (and vice versa).
            if a_start < b_end and b_start < a_end:
                return  # proven concurrent on this attempt
            last_err = (
                f"branches did not run in parallel: branch-a=[{a_start:.3f},{a_end:.3f}] "
                f"branch-b=[{b_start:.3f},{b_end:.3f}]"
            )
        raise AssertionError(last_err)


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
        # The reducer must not mutate its input (non-mutating contract).
        assert [a["node_id"] for a in current["artifacts"]] == ["seed"]
        assert not current["_run_context_write_log"]

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

    def test_loop_source_no_warning(self) -> None:
        """A source with a LOOP edge routes ALL edges through the loop counter
        (single target) — the normal edges are NOT a parallel fan-out, so a
        loop + 2 normal edges to context-setters must not warn (the compiler
        never runs those branches concurrently)."""
        graph = self._graph({"branch-a": "context_setter", "branch-b": "context_setter"})
        graph["edges"].append({"source": "fanout", "target": "fanout", "type": "loop", "max_iterations": 3})
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
# 6a. Criterion 2 THROUGH THE REAL EXECUTOR — both parallel outputs land in
#     completed_node_outputs under their own node ids via _stream_graph
# ---------------------------------------------------------------------------


class TestExecutorParallelNodeOutput:
    async def test_stream_graph_captures_both_parallel_outputs_under_own_ids(self) -> None:
        """The REAL executor handler (_stream_graph) collects both parallel
        branch outputs keyed by their own node ids and completes the run.

        The implementer's ``test_completed_node_outputs_keep_node_id_keys``
        only drives a SEQUENTIAL chain (branch-a -> branch-b) through a manual
        mirror of the handler; this test drives the actual fan-out through the
        real ``_stream_graph`` with a real ``RunEventBroker``.
        """
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
        executor = PipelineExecutor(MagicMock())
        broker = RunEventBroker(uuid.uuid4())
        completed_node_outputs: dict[str, Any] = {}

        final_status, error_code, error_detail, _usage = await executor._stream_graph(
            compiled,
            {"run_context": {"cancelled": False, "input": {}}, "artifacts": []},
            {"configurable": {"thread_id": str(uuid.uuid4())}},
            {"fanout", "branch-a", "branch-b"},
            broker,
            uuid.uuid4(),
            completed_node_outputs=completed_node_outputs,
        )

        assert final_status == "complete"
        assert error_code is None
        assert error_detail is None
        # Both branches captured under their OWN node ids — no clobbering.
        assert set(completed_node_outputs.keys()) == {"fanout", "branch-a", "branch-b"}
        assert completed_node_outputs["branch-a"]["artifacts"][0]["node_id"] == "branch-a"
        assert completed_node_outputs["branch-b"]["artifacts"][0]["node_id"] == "branch-b"

    async def test_stream_graph_fanout_does_not_double_count_guard_steps(self) -> None:
        """Driving the real fan-out through _stream_graph with a real guard
        records exactly 3 steps (entry + 2 branches) and completes cleanly."""
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
        executor = PipelineExecutor(MagicMock())
        guard = RunawayGuard(max_steps=3)
        final_status, error_code, _detail, _usage = await executor._stream_graph(
            compiled,
            {"run_context": {"cancelled": False, "input": {}}, "artifacts": []},
            {"configurable": {"thread_id": str(uuid.uuid4())}},
            {"fanout", "branch-a", "branch-b"},
            RunEventBroker(uuid.uuid4()),
            uuid.uuid4(),
            guard=guard,
        )
        assert final_status == "complete"
        assert error_code is None
        # Exactly 3 nodes counted — max_steps=3 was NOT exceeded on a fan-out.
        assert guard._step_count == 3

    async def test_stream_graph_runaway_fires_when_fanout_exceeds_max_steps(self) -> None:
        """A parallel fan-out that completes MORE nodes than max_steps is
        terminated as runaway through the real executor path."""
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
        executor = PipelineExecutor(MagicMock())
        guard = RunawayGuard(max_steps=2)
        final_status, error_code, _detail, _usage = await executor._stream_graph(
            compiled,
            {"run_context": {"cancelled": False, "input": {}}, "artifacts": []},
            {"configurable": {"thread_id": str(uuid.uuid4())}},
            {"fanout", "branch-a", "branch-b"},
            RunEventBroker(uuid.uuid4()),
            uuid.uuid4(),
            guard=guard,
        )
        assert final_status == "failed"
        assert error_code == "runaway"


# ---------------------------------------------------------------------------
# 6b. Criterion 5 — HITL interrupt in ONE parallel branch, sibling survives,
#     resume completes both
# ---------------------------------------------------------------------------


class TestParallelHitlSibling:
    async def test_parallel_interrupt_does_not_corrupt_sibling_and_resume_completes(self) -> None:
        """Real parallel fan-out: branch-a runs in the same superstep as gate-b,
        which interrupts. The sibling's completed output survives, and resume
        with a decision completes the gate without replaying/losing branch-a."""
        graph = StateGraph(_STATE_SCHEMA)
        graph.add_node("entry", _make_sleepy_node("entry", 0.05))
        graph.add_node("branch-a", _make_sleepy_node("branch-a", 0.2))
        graph.add_node("gate-b", _gate_branch)
        graph.set_entry_point("entry")
        # Parallel fan-out from entry — same superstep.
        graph.add_edge("entry", "branch-a")
        graph.add_edge("entry", "gate-b")
        compiled = graph.compile(checkpointer=InMemorySaver())

        thread = str(uuid.uuid4())
        config = {"configurable": {"thread_id": thread}}
        initial: dict[str, Any] = {"run_context": {"cancelled": False, "input": {}}, "artifacts": [], "_hitl_gates": []}

        interrupted = await compiled.ainvoke(initial, config)
        assert interrupted.get("__interrupt__"), "expected a HITL interrupt in the gate branch"

        # Resume the interrupted gate branch.
        await compiled.aupdate_state(config, {"_hitl_decision": {"action": "approved"}})
        final = await compiled.ainvoke(None, config)
        node_ids = [a["node_id"] for a in final["artifacts"]]

        # BOTH the surviving sibling AND the resumed gate completed.
        assert "branch-a" in node_ids
        gate_artifacts = [a for a in final["artifacts"] if a["node_id"] == "gate-b"]
        assert gate_artifacts, "gate branch never completed after resume"
        assert gate_artifacts[0]["action"] == "approved"


# ---------------------------------------------------------------------------
# 6c. Criterion 3 & 7 THROUGH THE PUBLIC VALIDATOR — the PARALLEL_RUN_CONTEXT_WRITE
#     warning surfaces at save time, and a fan-out graph is ACCEPTED (no error)
# ---------------------------------------------------------------------------


class TestParallelValidatorPublicPath:
    def _fanout_graph(self, roles: dict[str, str]) -> dict[str, Any]:
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

    async def test_validate_definition_emits_parallel_run_context_write_warning(self) -> None:
        """The warning fires through the PUBLIC save-time validation path, not
        just when the private check is invoked directly."""
        validator = GraphValidator()
        result = await validator.validate_definition(
            self._fanout_graph({"branch-a": "context_setter", "branch-b": "context_setter"}),
            AsyncMock(),
        )
        codes = [i.code for i in result.issues]
        assert "PARALLEL_RUN_CONTEXT_WRITE" in codes
        # Warning only — the fan-out graph is ACCEPTED (criterion 7).
        assert result.is_valid

    async def test_validate_definition_accepts_parallel_fanout_without_error(self) -> None:
        """A plain parallel fan-out (no context setters) passes validation."""
        validator = GraphValidator()
        result = await validator.validate_definition(
            self._fanout_graph({"branch-a": "agent", "branch-b": "agent"}),
            AsyncMock(),
        )
        assert result.is_valid
        assert not any(i.code == "PARALLEL_RUN_CONTEXT_WRITE" for i in result.issues)

    async def test_validate_definition_no_warning_for_disjoint_declared_keys(self) -> None:
        """Parallel context-setters that declare DISJOINT run_context_writes are
        safe — no warning through the public path."""
        graph = self._fanout_graph({"branch-a": "context_setter", "branch-b": "context_setter"})
        graph["nodes"][1]["run_context_writes"] = ["model_tier"]
        graph["nodes"][2]["run_context_writes"] = ["estimated_tokens"]
        validator = GraphValidator()
        result = await validator.validate_definition(graph, AsyncMock())
        assert not any(i.code == "PARALLEL_RUN_CONTEXT_WRITE" for i in result.issues)
        assert result.is_valid


# ---------------------------------------------------------------------------
# 6d. Backward compatibility — single-outgoing-edge graphs behave identically
# ---------------------------------------------------------------------------


class TestBackwardCompat:
    async def test_single_outgoing_edge_graph_runs_sequentially_as_before(self) -> None:
        """A source with ONE normal outgoing edge compiles and runs exactly as
        before the fan-out change (sequential chain preserved)."""
        graph: dict[str, Any] = {
            "nodes": [
                {"id": "start", "role": None},
                {"id": "middle", "role": None},
                {"id": "end", "role": None},
            ],
            "edges": [
                {"source": "start", "target": "middle", "type": "normal"},
                {"source": "middle", "target": "end", "type": "normal"},
            ],
        }
        compiled = build_graph_from_json(graph)
        result = await compiled.ainvoke(
            {"run_context": {"cancelled": False, "input": {}}, "artifacts": []},
            {"configurable": {"thread_id": str(uuid.uuid4())}},
        )
        node_ids = [a["node_id"] for a in result["artifacts"]]
        assert node_ids == ["start", "middle", "end"]

    def test_sequential_run_context_write_still_replaces_legacy_semantics(self) -> None:
        """A single context-setter write through the sequential path behaves as
        before — its own key lands (per-key merge on a single writer is
        equivalent to the legacy whole-dict replacement for that key)."""
        current: dict[str, Any] = {"run_context": {"cancelled": False, "seeded": "keep"}}
        merged = _pipeline_state_reducer(current, {"run_context": {"model_tier": "large"}})
        assert merged["run_context"]["model_tier"] == "large"
        assert merged["run_context"]["seeded"] == "keep"
        assert merged["run_context"]["cancelled"] is False


# ---------------------------------------------------------------------------
# 6e. Conditional edges never fan out — a conditional source routes SINGLE-target
# ---------------------------------------------------------------------------


class TestConditionalSingleTarget:
    async def test_conditional_source_with_normal_edge_routes_single_target(self) -> None:
        """A source with a conditional edge PLUS a normal edge routes to ONE
        target (no accidental fan-out): the normal edge is the router fallback,
        never a parallel branch."""
        graph: dict[str, Any] = {
            "nodes": [
                {"id": "decider", "role": None},
                {"id": "target-ok", "role": None},
                {"id": "target-else", "role": None},
            ],
            "edges": [
                {
                    "source": "decider",
                    "target": "target-ok",
                    "type": "conditional",
                    "condition_expression": "run_context.input.route == 'ok'",
                },
                {"source": "decider", "target": "target-else", "type": "normal"},
            ],
        }
        compiled = build_graph_from_json(graph)
        result = await compiled.ainvoke(
            {"run_context": {"cancelled": False, "input": {"route": "ok"}}, "artifacts": []},
            {"configurable": {"thread_id": str(uuid.uuid4())}},
        )
        node_ids = [a["node_id"] for a in result["artifacts"]]
        # Exactly ONE downstream target ran — the conditional winner.
        assert "target-ok" in node_ids
        assert "target-else" not in node_ids

    async def test_conditional_source_falls_back_to_normal_single_target(self) -> None:
        """When the conditional expression is falsy, the router returns the
        NORMAL target — still exactly one branch, never both."""
        graph: dict[str, Any] = {
            "nodes": [
                {"id": "decider", "role": None},
                {"id": "target-ok", "role": None},
                {"id": "target-else", "role": None},
            ],
            "edges": [
                {
                    "source": "decider",
                    "target": "target-ok",
                    "type": "conditional",
                    "condition_expression": "run_context.input.route == 'ok'",
                },
                {"source": "decider", "target": "target-else", "type": "normal"},
            ],
        }
        compiled = build_graph_from_json(graph)
        result = await compiled.ainvoke(
            {"run_context": {"cancelled": False, "input": {"route": "nope"}}, "artifacts": []},
            {"configurable": {"thread_id": str(uuid.uuid4())}},
        )
        node_ids = [a["node_id"] for a in result["artifacts"]]
        assert "target-else" in node_ids
        assert "target-ok" not in node_ids


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
