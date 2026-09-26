"""Unit tests for eval-related executor and HITL gate behaviour.

Retired suite-aggregation tests (evaluate_suite, EvalSuiteBlockedError,
SuiteEvalResult) removed in FAR-1105 chunk 5c.
"""

import asyncio
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest


class TestEvalResultCascadeConfig:
    """DB-level cascade from evals to eval_results is configured (PRD 8.17).

    eval_results.eval_id carries ``ondelete="CASCADE"`` so deleting an eval
    removes its stored results. Verified against the SQLAlchemy FK metadata
    (the behaviour is DB-enforced at the constraint level). After the FAR-1100
    read cutover the FK targets the ``evals`` table, not ``eval_definitions``.
    """

    def test_eval_result_fk_cascades_on_eval_delete(self) -> None:
        from modulo.db.models.eval_result import EvalResult

        fk = next(iter(EvalResult.__table__.c.eval_id.foreign_keys))
        assert fk.ondelete == "CASCADE"
        assert fk.column.table.name == "evals"


# ---------------------------------------------------------------------------
# Executor eval-definition loading — pipeline-level (no node_id) defs excluded
# ---------------------------------------------------------------------------


def _make_eval_row(
    *,
    eval_id: uuid.UUID | None = None,
    node_id: uuid.UUID | None = None,
    name: str = "eval",
    suite_id: str | None = None,
    pass_threshold: float | None = None,
    pipeline_id: uuid.UUID | None = None,
) -> MagicMock:
    """A mock ``Eval`` ORM row with the attributes the executor reads."""
    row = MagicMock()
    row.id = eval_id or uuid4()
    row.node_id = node_id
    row.name = name
    row.eval_type = "regex"
    row.config_json = {"pattern": "x"}
    row.pass_threshold = pass_threshold
    row.suite_id = suite_id
    row.version = 1
    row.pipeline_id = pipeline_id or uuid4()
    return row


def _make_policy_gate_row(*, action: str = "warn") -> MagicMock:
    """A mock ``PolicyGate`` ORM row carrying the ``action`` the executor reads."""
    gate = MagicMock()
    gate.action = action
    return gate


class TestBuildEvalDefsByNode:
    def test_pipeline_level_def_without_node_id_is_skipped(self) -> None:
        """An eval definition with no node_id is pipeline-level — the executor
        must NOT attach it to any node for eval-before-interrupt."""
        from modulo.core.pipeline_engine.executor import PipelineExecutor

        node_uuid = uuid4()
        rows = [
            (_make_eval_row(node_id=node_uuid, name="node-scoped"), _make_policy_gate_row()),
            (_make_eval_row(node_id=None, name="pipeline-level"), None),
        ]
        org_id = uuid4()
        pipeline_id = uuid4()

        by_node = PipelineExecutor._build_eval_defs_by_node(rows, org_id, pipeline_id)

        assert list(by_node.keys()) == [str(node_uuid)]
        attached = by_node[str(node_uuid)]
        assert len(attached) == 1
        assert attached[0].name == "node-scoped"

    def test_empty_rows_returns_empty_map(self) -> None:
        from modulo.core.pipeline_engine.executor import PipelineExecutor

        by_node = PipelineExecutor._build_eval_defs_by_node([], uuid4(), uuid4())
        assert by_node == {}

    def test_node_id_used_as_dict_key(self) -> None:
        """The DTO key is the node id string — gate nodes use it to look up evals."""
        from modulo.core.pipeline_engine.executor import PipelineExecutor

        node_uuid = uuid4()
        rows = [
            (_make_eval_row(node_id=node_uuid, name="e1"), _make_policy_gate_row()),
            (_make_eval_row(node_id=node_uuid, name="e2"), _make_policy_gate_row()),
        ]
        by_node = PipelineExecutor._build_eval_defs_by_node(rows, uuid4(), uuid4())
        assert len(by_node[str(node_uuid)]) == 2


class TestLoadEvalDefsForPipeline:
    async def test_only_node_scoped_defs_are_loaded(self) -> None:
        """The executor's loading query filters to node_id IS NOT NULL — a
        pipeline-level def (no node_id) never reaches the eval-before-interrupt
        gate function."""
        from modulo.core.pipeline_engine.executor import PipelineExecutor

        session = AsyncMock()
        node_scoped = _make_eval_row(node_id=uuid4(), name="node-eval")
        pipeline_level = _make_eval_row(node_id=None, name="pipeline-eval")
        result = MagicMock()
        result.scalars.return_value.all.return_value = [node_scoped, pipeline_level]
        session.execute = AsyncMock(return_value=result)

        executor = PipelineExecutor(MagicMock())
        await executor._load_eval_defs_for_pipeline(session, uuid4())

        # Assert on the compiled WHERE clause, not on the mock rows: the mock
        # returns whatever the SQL returns, so checking the returned names can
        # never detect a dropped `node_id IS NOT NULL` filter.
        stmt = session.execute.call_args[0][0]
        where_sql = str(stmt.whereclause) if stmt.whereclause is not None else ""
        assert "node_id" in where_sql, f"loading query must filter on node_id, got: {where_sql}"
        assert "IS NOT NULL" in where_sql, f"loading query must filter node_id IS NOT NULL, got: {where_sql}"


# ---------------------------------------------------------------------------
# HITL gate condition edge cases — falsy values and invalid expressions
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _interrupt_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the gate's interrupt() raise GraphInterrupt so a truthy condition
    reaching the interrupt is distinguishable from a falsy-condition skip."""

    def raise_interrupt(value: Any) -> None:
        from langgraph.errors import GraphInterrupt
        from langgraph.types import Interrupt

        raise GraphInterrupt((Interrupt(value=value),))

    monkeypatch.setattr("modulo.core.pipeline_engine.node_runner.interrupt", raise_interrupt)


class TestHitlGateConditionFalsy:
    def test_condition_false_literal_skips_gate(self) -> None:
        """JMESPath condition evaluating to the ``False`` literal → falsy → skip."""
        from modulo.core.pipeline_engine.node_runner import make_hitl_gate_fn

        async def _run() -> dict[str, Any]:
            node_fn = make_hitl_gate_fn({"gate_id": "g", "condition": "ready == `false`"})
            return await node_fn({"ready": True, "artifacts": []})

        result = asyncio.run(_run())
        assert result["artifacts"][0]["status"] == "condition_skipped"

    def test_condition_empty_list_skips_gate(self) -> None:
        """JMESPath condition returning an empty list → falsy → gate skipped."""
        from modulo.core.pipeline_engine.node_runner import make_hitl_gate_fn

        async def _run() -> dict[str, Any]:
            node_fn = make_hitl_gate_fn({"gate_id": "g", "condition": "items"})
            return await node_fn({"items": [], "artifacts": []})

        result = asyncio.run(_run())
        assert result["artifacts"][0]["status"] == "condition_skipped"

    def test_condition_empty_dict_skips_gate(self) -> None:
        """JMESPath condition returning an empty dict → falsy → gate skipped."""
        from modulo.core.pipeline_engine.node_runner import make_hitl_gate_fn

        async def _run() -> dict[str, Any]:
            node_fn = make_hitl_gate_fn({"gate_id": "g", "condition": "obj"})
            return await node_fn({"obj": {}, "artifacts": []})

        result = asyncio.run(_run())
        assert result["artifacts"][0]["status"] == "condition_skipped"

    def test_condition_non_empty_list_is_truthy(self) -> None:
        """A non-empty list is truthy — the gate proceeds to the interrupt."""
        from langgraph.errors import GraphInterrupt

        from modulo.core.pipeline_engine.node_runner import make_hitl_gate_fn

        async def _run() -> None:
            node_fn = make_hitl_gate_fn({"gate_id": "g", "condition": "items"})
            await node_fn({"items": [1], "artifacts": [], "_hitl_gates": []})

        with pytest.raises(GraphInterrupt):
            asyncio.run(_run())

    def test_condition_invalid_expression_raises_value_error(self) -> None:
        """An unparseable JMESPath expression raises ValueError (percolates as
        a node error instead of silently treating the gate as pass-through)."""
        from modulo.core.pipeline_engine.node_runner import make_hitl_gate_fn

        async def _run() -> None:
            node_fn = make_hitl_gate_fn({"gate_id": "g", "condition": "items[0"})
            await node_fn({"artifacts": []})

        with pytest.raises(ValueError, match="Invalid HITL gate condition expression"):
            asyncio.run(_run())

    def test_condition_runtime_error_percolates_as_jmespath_error(self) -> None:
        """A JMESPath runtime error during search (e.g. type error in a function)
        propagates as a JMESPathError node error rather than being swallowed."""
        import jmespath

        from modulo.core.pipeline_engine.node_runner import make_hitl_gate_fn

        async def _run() -> None:
            node_fn = make_hitl_gate_fn({"gate_id": "g", "condition": "abs(score)"})
            await node_fn({"score": "not-a-number", "artifacts": []})

        with pytest.raises(jmespath.exceptions.JMESPathError):
            asyncio.run(_run())


class TestEvalBlockShortCircuitsRemainingEvals:
    def test_block_failure_skips_remaining_evals(self) -> None:
        """A block-level eval failure short-circuits the eval loop — evals
        listed AFTER the failing one are never evaluated. This is the
        eval_block.feature "remaining evals are not evaluated" expectation."""
        from modulo.core.eval_engine import EvalBlockedError, EvalDefinition, EvalType
        from modulo.core.pipeline_engine.node_runner import make_hitl_gate_fn

        async def _run() -> None:
            eval_blocking = EvalDefinition(
                id=uuid4(),
                org_id=uuid4(),
                name="blocking-eval",
                eval_type=EvalType.REGEX,
                config={"pattern": "pass", "field": "level"},
                failure_behaviour="block",
            )
            eval_after = EvalDefinition(
                id=uuid4(),
                org_id=uuid4(),
                name="should-not-run",
                eval_type=EvalType.REGEX,
                config={"pattern": "ALWAYS-PASS", "field": "level"},
                failure_behaviour="block",
            )
            node_fn = make_hitl_gate_fn(
                {"gate_id": "g"},
                eval_definitions=[eval_blocking, eval_after],
            )
            await node_fn({"level": "fail", "artifacts": [], "_hitl_gates": []})

        with pytest.raises(EvalBlockedError, match="blocking-eval"):
            asyncio.run(_run())
        # The raised error names the FIRST eval ("blocking-eval"). If the loop
        # had reached "should-not-run", its pattern ALWAYS-PASS also fails to
        # match state {"level": "fail"} (re.search is literal here), so it
        # would raise a SECOND EvalBlockedError naming "should-not-run". The
        # single raise on the first failing eval proves the loop stopped there.
