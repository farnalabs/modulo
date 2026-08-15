"""Unit tests for eval suite aggregation and pass_threshold blocking."""

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


class TestEvalResultCascadeConfig:
    """DB-level cascade from eval_definitions to eval_results is configured (PRD 8.17).

    eval_results.eval_id carries ``ondelete="CASCADE"`` so deleting an eval
    definition removes its stored results. Verified against the SQLAlchemy FK
    metadata (the behaviour is DB-enforced at the constraint level).
    """

    def test_eval_result_fk_cascades_on_eval_delete(self) -> None:
        from modulo.db.models.eval_result import EvalResult

        fk = next(iter(EvalResult.__table__.c.eval_id.foreign_keys))
        assert fk.ondelete == "CASCADE"
        assert fk.column.table.name == "eval_definitions"
