"""Unit tests for eval suite aggregation and pass_threshold blocking."""

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from modulo.core.eval_engine import (
    EvalResult,
    EvalSuiteBlockedError,
    SuiteEvalResult,
    evaluate_suite,
)


def _make_result(passed: bool, score: float | None = None, detail: str = "") -> EvalResult:
    return EvalResult(
        id=uuid4(),
        run_id=uuid4(),
        node_id="n1",
        eval_id=uuid4(),
        passed=passed,
        score=score,
        detail=detail,
    )


SUITE_ID = "test-suite"


class TestEvaluateSuite:
    def test_passing_suite_above_threshold(self) -> None:
        """Suite with score >= threshold should pass."""
        results = [_make_result(passed=True) for _ in range(4)]
        result = evaluate_suite(results, SUITE_ID, pass_threshold=0.75)
        assert result.passed is True
        assert result.aggregate_score == 1.0
        assert result.total_evals == 4
        assert result.passed_evals == 4
        assert not result.blocking_failures

    def test_passing_suite_at_threshold(self) -> None:
        """Suite with score exactly equal to threshold should pass."""
        results = [_make_result(passed=True) for _ in range(3)] + [_make_result(passed=False)]
        result = evaluate_suite(results, SUITE_ID, pass_threshold=0.75)
        assert result.passed is True
        assert result.aggregate_score == 0.75

    def test_failing_suite_below_threshold(self) -> None:
        """Suite with score below threshold should fail."""
        results = [_make_result(passed=True) for _ in range(2)] + [_make_result(passed=False) for _ in range(2)]
        result = evaluate_suite(results, SUITE_ID, pass_threshold=0.75)
        assert result.passed is False
        assert result.aggregate_score == 0.5
        assert result.total_evals == 4
        assert result.passed_evals == 2
        assert len(result.blocking_failures) == 2

    def test_suite_with_no_threshold_does_not_block(self) -> None:
        """Suite without a pass_threshold should always pass."""
        results = [_make_result(passed=False) for _ in range(5)]
        result = evaluate_suite(results, SUITE_ID, pass_threshold=None)
        assert result.passed is True
        assert result.aggregate_score == 0.0

    def test_suite_with_mixed_pass_fail_results(self) -> None:
        """Suite with mixed results returns correct counts and blocking failures."""
        results = [
            _make_result(passed=True, detail="ok"),
            _make_result(passed=False, detail="wrong output"),
            _make_result(passed=True, detail="ok"),
            _make_result(passed=False, detail="missing field"),
        ]
        result = evaluate_suite(results, SUITE_ID, pass_threshold=0.6)
        assert result.passed is False
        assert result.aggregate_score == 0.5
        assert result.passed_evals == 2
        assert result.total_evals == 4
        assert len(result.blocking_failures) == 2
        assert any("wrong output" in f for f in result.blocking_failures)
        assert any("missing field" in f for f in result.blocking_failures)

    def test_empty_suite_always_passes(self) -> None:
        """An empty suite with no eval results should pass."""
        result = evaluate_suite([], SUITE_ID, pass_threshold=0.8)
        assert result.passed is True
        assert result.aggregate_score == 0.0
        assert result.total_evals == 0
        assert result.passed_evals == 0
        assert not result.blocking_failures

    def test_suite_result_model_fields(self) -> None:
        """SuiteEvalResult should expose all expected fields."""
        result = SuiteEvalResult(
            suite_id="my-suite",
            total_evals=10,
            passed_evals=7,
            aggregate_score=0.7,
            passed=True,
            blocking_failures=["e1: failed"],
        )
        assert result.suite_id == "my-suite"
        assert result.total_evals == 10
        assert result.passed_evals == 7
        assert result.aggregate_score == pytest.approx(0.7)
        assert result.passed is True
        assert result.blocking_failures == ["e1: failed"]


class TestEvalSuiteBlockedError:
    def test_constructor_sets_fields(self) -> None:
        """EvalSuiteBlockedError stores suite_id, score, and threshold."""
        err = EvalSuiteBlockedError("suite-1", 0.3, 0.8)
        assert err.suite_id == "suite-1"
        assert err.score == pytest.approx(0.3)
        assert err.threshold == pytest.approx(0.8)
        assert "0.30" in str(err)
        assert "0.80" in str(err)
        assert "suite-1" in str(err)

    def test_constructor_boundary_threshold_exact(self) -> None:
        """Boundary case: score equal to threshold should be treated as pass
        by evaluate_suite; EvalSuiteBlockedError should still construct."""
        err = EvalSuiteBlockedError("suite-1", 0.8, 0.8)
        assert err.score == err.threshold

    def test_constructor_zero_score(self) -> None:
        """Boundary case: score of 0.0 models complete failure."""
        err = EvalSuiteBlockedError("suite-1", 0.0, 0.5)
        assert err.score == 0.0
        assert err.threshold == 0.5

    def test_constructor_high_threshold(self) -> None:
        """Boundary case: threshold of 1.0 models perfection requirement."""
        err = EvalSuiteBlockedError("suite-1", 0.99, 1.0)
        assert err.score == pytest.approx(0.99)
        assert err.threshold == 1.0


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
    """A mock EvalDefinition ORM row with the attributes the executor reads."""
    row = MagicMock()
    row.id = eval_id or uuid4()
    row.node_id = node_id
    row.name = name
    row.eval_type = "regex"
    row.config_json = {"pattern": "x"}
    row.failure_behaviour = "warn"
    row.pass_threshold = pass_threshold
    row.suite_id = suite_id
    row.pipeline_id = pipeline_id or uuid4()
    return row


class TestBuildEvalDefsByNode:
    def test_pipeline_level_def_without_node_id_is_skipped(self) -> None:
        """An eval definition with no node_id is pipeline-level — the executor
        must NOT attach it to any node for eval-before-interrupt."""
        from modulo.core.pipeline_engine.executor import PipelineExecutor

        node_uuid = uuid4()
        rows = [
            _make_eval_row(node_id=node_uuid, name="node-scoped"),
            _make_eval_row(node_id=None, name="pipeline-level"),
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
        rows = [_make_eval_row(node_id=node_uuid, name="e1"), _make_eval_row(node_id=node_uuid, name="e2")]
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
        assert "node_id" in where_sql and "IS NOT NULL" in where_sql, (
            f"loading query must filter node_id IS NOT NULL, got: {where_sql}"
        )


# ---------------------------------------------------------------------------
# Executor _check_eval_suites — suite dedup + first-found threshold
# ---------------------------------------------------------------------------


class TestCheckEvalSuites:
    def _session_with_rows(self, row_batches: list[list[Any]]) -> AsyncMock:
        """Session whose execute() returns one MagicMock result per batch."""
        session = AsyncMock()
        results = []
        for batch in row_batches:
            r = MagicMock()
            r.scalars.return_value.all.return_value = batch
            results.append(r)

        async def _execute(stmt: Any) -> Any:
            return results.pop(0)

        session.execute = _execute
        return session

    async def test_dedupes_suite_ids_via_set(self) -> None:
        """Two definitions sharing a suite_id produce a single per-suite query —
        the dedup avoids redundant queries against the same suite."""
        from modulo.core.pipeline_engine.executor import PipelineExecutor

        pipeline_id = uuid4()
        run_id = uuid4()
        suite = "shared-suite"
        def_a = _make_eval_row(name="a", suite_id=suite, pass_threshold=0.8, pipeline_id=pipeline_id)
        def_b = _make_eval_row(name="b", suite_id=suite, pass_threshold=0.7, pipeline_id=pipeline_id)
        result_row = MagicMock()
        result_row.id = uuid4()
        result_row.run_id = run_id
        result_row.node_id = uuid4()
        result_row.eval_id = def_a.id
        result_row.passed = True
        result_row.score = 0.9
        result_row.detail = ""
        result_row.evaluated_at = datetime.now(UTC)

        executor = PipelineExecutor(MagicMock())
        # execute call 1: suite definitions; call 2: per-suite defs; call 3: results
        session = self._session_with_rows([[def_a, def_b], [def_a, def_b], [result_row]])
        results = await executor._check_eval_suites(session, run_id, pipeline_id)

        assert len(results) == 1, "shared suite_id must be checked once, not per-definition"
        assert results[0].suite_id == suite

    async def test_first_found_pass_threshold_is_used(self) -> None:
        """When multiple defs in a suite carry thresholds, the first found
        threshold is the suite's blocking threshold."""
        from modulo.core.pipeline_engine.executor import PipelineExecutor

        pipeline_id = uuid4()
        run_id = uuid4()
        suite = "multi-threshold-suite"
        # def_a is returned first and carries threshold 0.8; def_b later with 0.5.
        def_a = _make_eval_row(name="a", suite_id=suite, pass_threshold=0.8, pipeline_id=pipeline_id)
        def_b = _make_eval_row(name="b", suite_id=suite, pass_threshold=0.5, pipeline_id=pipeline_id)

        result_rows = []
        for i in range(2):
            row = MagicMock()
            row.id = uuid4()
            row.run_id = run_id
            row.node_id = uuid4()
            row.eval_id = def_a.id
            row.passed = i == 0
            row.score = 0.9 if i == 0 else 0.6
            row.detail = "ok" if i == 0 else "below 0.8"
            row.evaluated_at = datetime.now(UTC)
            result_rows.append(row)

        executor = PipelineExecutor(MagicMock())
        session = self._session_with_rows([[def_a, def_b], [def_a, def_b], result_rows])
        with pytest.raises(EvalSuiteBlockedError) as exc_info:
            await executor._check_eval_suites(session, run_id, pipeline_id)

        # 0.6 score < 0.8 threshold → blocked on the FIRST threshold, not 0.5.
        assert exc_info.value.suite_id == suite
        assert exc_info.value.score == pytest.approx(0.5)  # aggregate 1/2 passed
        assert exc_info.value.threshold == pytest.approx(0.8)

    async def test_no_threshold_defs_means_no_blocking(self) -> None:
        """Definitions in a suite without thresholds never block the run — the
        loader's first query filters to defs WITH a threshold, so a threshold-less
        suite yields no suite check at all."""
        from modulo.core.pipeline_engine.executor import PipelineExecutor

        pipeline_id = uuid4()
        run_id = uuid4()
        suite = "no-threshold"
        def_a = _make_eval_row(name="a", suite_id=suite, pass_threshold=None, pipeline_id=pipeline_id)

        row = MagicMock()
        row.id = uuid4()
        row.run_id = run_id
        row.node_id = uuid4()
        row.eval_id = def_a.id
        row.passed = False
        row.score = 0.1
        row.detail = "failed but no threshold"
        row.evaluated_at = datetime.now(UTC)

        executor = PipelineExecutor(MagicMock())
        # First query filters pass_threshold IS NOT NULL → empty → no suite check.
        session = self._session_with_rows([[]])
        captured: list[Any] = []
        orig_execute = session.execute

        async def _capture_execute(stmt: Any) -> Any:
            captured.append(stmt)
            return await orig_execute(stmt)

        session.execute = _capture_execute
        results = await executor._check_eval_suites(session, run_id, pipeline_id)
        assert results == []

        # The threshold-less contract lives in the SQL filter, not just the
        # empty-result early return — assert the query excludes defs without a
        # pass_threshold so the test can detect a dropped filter.
        assert captured, "expected the suite-loading query to be executed"
        stmt = captured[0]
        where_sql = str(stmt.whereclause) if stmt.whereclause is not None else ""
        assert "pass_threshold" in where_sql and "IS NOT NULL" in where_sql, (
            f"suite query must filter pass_threshold IS NOT NULL, got: {where_sql}"
        )


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
