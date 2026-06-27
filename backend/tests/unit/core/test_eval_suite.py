"""Unit tests for eval suite aggregation and pass_threshold blocking."""

from uuid import uuid4

from modulo.core.eval_engine import (
    EvalResult,
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
        assert result.blocking_failures == []

    def test_passing_suite_at_threshold(self) -> None:
        """Suite with score exactly equal to threshold should pass."""
        results = [_make_result(passed=True) for _ in range(3)] + [
            _make_result(passed=False)
        ]
        result = evaluate_suite(results, SUITE_ID, pass_threshold=0.75)
        assert result.passed is True
        assert result.aggregate_score == 0.75

    def test_failing_suite_below_threshold(self) -> None:
        """Suite with score below threshold should fail."""
        results = [_make_result(passed=True) for _ in range(2)] + [
            _make_result(passed=False) for _ in range(2)
        ]
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
        assert result.aggregate_score == 1.0
        assert result.total_evals == 0
        assert result.passed_evals == 0
        assert result.blocking_failures == []

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
        assert result.aggregate_score == 0.7
        assert result.passed is True
        assert result.blocking_failures == ["e1: failed"]
