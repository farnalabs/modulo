"""Unit tests for the FAR-374 Phase 1 eval_suite promotion.

Covers:
  * the ``eval_maturity`` feature flag registration + fail-closed read,
  * a pinning assertion that ``evaluate_suite`` keeps its exact signature and
    behaviour (it must stay byte-identical for existing production callers).
"""

import uuid

import pytest

from modulo.core.eval_engine import EvalResult, SuiteEvalResult, evaluate_suite
from modulo.core.feature_flags import FeatureFlagRegistry, eval_maturity_enabled
from modulo.db.models.eval_suite import SENTINEL_LEGACY_SUITE_ID, EvalSuite


class _StubPlan:
    """PlanContext stub returning a fixed value for ``eval_maturity``."""

    def __init__(self, maturity: bool, raise_on_read: bool = False) -> None:
        self._maturity = maturity
        self._raise = raise_on_read

    def feature_enabled(self, name: str) -> bool:
        if self._raise:
            raise RuntimeError("boom")
        if name == "eval_maturity":
            return self._maturity
        return False


def test_eval_maturity_flag_is_registered() -> None:
    """The ``eval_maturity`` flag exists in the catalog (community tier)."""
    flag = FeatureFlagRegistry().get_flag("eval_maturity")
    assert flag is not None
    assert flag.tier == "community"
    assert "FAR-374" in flag.description


def test_eval_maturity_enabled_true_when_flag_on() -> None:
    assert eval_maturity_enabled(_StubPlan(maturity=True)) is True


def test_eval_maturity_enabled_false_when_flag_off() -> None:
    assert eval_maturity_enabled(_StubPlan(maturity=False)) is False


def test_eval_maturity_enabled_fails_closed_on_error() -> None:
    """Any exception reading the flag must fail CLOSED to the legacy path."""
    assert eval_maturity_enabled(_StubPlan(maturity=True, raise_on_read=True)) is False


def test_eval_maturity_enabled_fails_closed_on_none_plan() -> None:
    assert eval_maturity_enabled(None) is False


def test_sentinel_legacy_suite_id_is_reserved() -> None:
    assert SENTINEL_LEGACY_SUITE_ID == "__NO_SUITE__"


def test_eval_suite_model_columns_present() -> None:
    """The model exposes the Phase 1 surface and inherits org-scoping."""
    cols = EvalSuite.__table__.columns
    for name in (
        "id",
        "organisation_id",
        "owner_team_id",
        "visibility",
        "name",
        "description",
        "eval_definition_ids",
        "input_set_ref",
        "legacy_suite_id",
        "created_at",
        "updated_at",
    ):
        assert name in cols, f"EvalSuite missing column {name}"


# ---------------------------------------------------------------------------
# evaluate_suite pinning — signature + behaviour must stay unchanged
# ---------------------------------------------------------------------------


def _make_result(passed: bool, detail: str = "") -> EvalResult:
    return EvalResult(
        id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        node_id="n1",
        eval_id=uuid.uuid4(),
        passed=passed,
        score=None,
        detail=detail,
    )


def test_evaluate_suite_signature_unchanged() -> None:
    """``evaluate_suite`` still takes (eval_results, suite_id, pass_threshold)
    positionally and returns a SuiteEvalResult — production callers unchanged."""
    import inspect

    sig = inspect.signature(evaluate_suite)
    params = list(sig.parameters)
    assert params[:3] == ["eval_results", "suite_id", "pass_threshold"]


def test_evaluate_suite_behaviour_pinning() -> None:
    """Output for a fixed input is deterministic (behaviour unchanged)."""
    results = [_make_result(passed=True), _make_result(passed=False, detail="bad")]
    out = evaluate_suite(results, "pin-suite", pass_threshold=0.6)
    assert isinstance(out, SuiteEvalResult)
    assert out.suite_id == "pin-suite"
    assert out.total_evals == 2
    assert out.passed_evals == 1
    assert out.aggregate_score == pytest.approx(0.5)
    assert out.passed is False
    # blocking_failures is formatted as "{eval_id}: {detail}" (unchanged behaviour).
    assert len(out.blocking_failures) == 1
    assert out.blocking_failures[0].endswith(": bad")
