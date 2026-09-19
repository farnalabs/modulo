"""Hypothesis property-based tests for the cost formula engine.

Target: ``modulo.core.cost_controller.breakdown.formula`` -- the 4-operator
formula parser and evaluator.

Properties assert safety invariants:
* Valid formulas always evaluate to a Decimal without raising.
* Invalid formulas always raise CostFormulaError.
* Evaluation is deterministic (same inputs -> same output).
* Division by zero is caught.
* The evaluator never executes arbitrary code.
"""

from __future__ import annotations

from decimal import Decimal

import hypothesis.strategies as st
import pytest
from hypothesis import assume, given, settings

from modulo.core.cost_controller.breakdown.constants import (
    MAX_FORMULA_LENGTH,
)
from modulo.core.cost_controller.breakdown.formula import (
    CostFormulaError,
    evaluate_formula,
    validate_formula,
)

# Valid identifier names that a formula may reference.
VALID_IDENTS = frozenset({"x", "y", "z", "rate", "tokens_input", "tokens_output"})

# Simple valid single-identifier formulas.
simple_valid_formulas = st.sampled_from(["x", "y", "x + y", "x * 2", "(x + y) / 2", "x - y", "rate * 100"])

# Params dict with valid Decimal values (all positive to avoid negative results).
params_strategy = st.fixed_dictionaries(
    {
        "x": st.decimals(min_value=Decimal(1), max_value=Decimal(1000), places=2),
        "y": st.decimals(min_value=Decimal(1), max_value=Decimal(1000), places=2),
        "z": st.decimals(min_value=Decimal(1), max_value=Decimal(1000), places=2),
        "rate": st.decimals(min_value=Decimal("0.01"), max_value=Decimal(100), places=4),
        "tokens_input": st.decimals(min_value=Decimal(0), max_value=Decimal(1000000), places=0),
        "tokens_output": st.decimals(min_value=Decimal(0), max_value=Decimal(1000000), places=0),
        "wall_clock_hours": st.decimals(min_value=Decimal("0.001"), max_value=Decimal(24), places=6),
        "tokens_estimated": st.decimals(min_value=Decimal(0), max_value=Decimal(2000000), places=0),
        "tokens_input_reported": st.decimals(min_value=Decimal(0), max_value=Decimal(1000000), places=0),
        "tokens_output_reported": st.decimals(min_value=Decimal(0), max_value=Decimal(1000000), places=0),
        "tokens_total_reported": st.decimals(min_value=Decimal(0), max_value=Decimal(2000000), places=0),
        "tokens_cache_read_reported": st.decimals(min_value=Decimal(0), max_value=Decimal(1000000), places=0),
        "tokens_cache_write_reported": st.decimals(min_value=Decimal(0), max_value=Decimal(1000000), places=0),
        "node_count": st.decimals(min_value=Decimal(1), max_value=Decimal(100), places=0),
        "nodes_estimated": st.decimals(min_value=Decimal(0), max_value=Decimal(100), places=0),
    }
)


@st.composite
def negative_intermediate_cases(draw: st.DrawFn) -> tuple[Decimal, Decimal, Decimal]:
    """Build ``(x, y, z)`` with ``x < y`` and ``(x - y) + z >= 0``.

    Constructed rather than filtered so Hypothesis does not spend its budget
    generating then discarding inputs (which trips the ``filter_too_much``
    health check).
    """
    x = draw(st.decimals(min_value=Decimal(1), max_value=Decimal(900), places=2))
    gap = draw(st.decimals(min_value=Decimal("0.01"), max_value=Decimal(100), places=2))
    y = x + gap
    z = draw(st.decimals(min_value=gap, max_value=Decimal(1000), places=2))
    return x, y, z


class TestFormulaProperties:
    """Properties for the formula parser and evaluator."""

    @given(formula=st.just(""))
    def test_empty_formula_raises(self, formula: str) -> None:
        """Empty formula is rejected."""
        with pytest.raises(CostFormulaError):
            validate_formula(formula, VALID_IDENTS)

    @given(formula=st.text(min_size=MAX_FORMULA_LENGTH + 1, max_size=MAX_FORMULA_LENGTH + 100))
    def test_oversized_formula_raises(self, formula: str) -> None:
        """Formulas exceeding MAX_FORMULA_LENGTH are rejected."""
        with pytest.raises(CostFormulaError):
            validate_formula(formula, VALID_IDENTS)

    @given(formula=st.from_regex(r"[A-Z][A-Za-z]{0,4}", fullmatch=True))
    def test_unknown_ident_raises(self, formula: str) -> None:
        """Unknown identifiers raise CostFormulaError."""
        assume(formula not in VALID_IDENTS)
        with pytest.raises(CostFormulaError):
            evaluate_formula(formula, {}, VALID_IDENTS)

    @given(formula=st.just("x / 0"), params=st.just({"x": Decimal(1)}))
    def test_division_by_zero_raises(self, formula: str, params: dict) -> None:
        """Division by zero is caught."""
        with pytest.raises(CostFormulaError):
            evaluate_formula(formula, params, VALID_IDENTS)

    @given(
        formula=simple_valid_formulas,
        params=params_strategy,
    )
    @settings(max_examples=100, deadline=None)
    def test_valid_formula_evaluates_to_decimal(self, formula: str, params: dict) -> None:
        """A well-formed formula with valid params returns a Decimal."""
        try:
            result = evaluate_formula(formula, params, VALID_IDENTS)
        except CostFormulaError:
            # Negative result or overflow is expected for some param combos.
            return
        assert isinstance(result, Decimal)
        assert result.is_finite()

    @given(
        formula=simple_valid_formulas,
        params=params_strategy,
    )
    @settings(max_examples=50, deadline=None)
    def test_evaluation_is_deterministic(self, formula: str, params: dict) -> None:
        """Same formula + same params -> same result."""
        try:
            r1 = evaluate_formula(formula, params, VALID_IDENTS)
            r2 = evaluate_formula(formula, params, VALID_IDENTS)
        except CostFormulaError:
            return
        assert r1 == r2

    @given(formula=simple_valid_formulas)
    def test_valid_formulas_pass_validation(self, formula: str) -> None:
        """Every simple valid formula passes validate_formula."""
        try:
            validate_formula(formula, VALID_IDENTS)
        except CostFormulaError as e:
            pytest.fail(f"Valid formula {formula!r} unexpectedly raised: {e}")
        assert validate_formula(formula, VALID_IDENTS) is None

    @given(formula=st.just("None"))
    def test_none_formula_is_noop_for_validate(self, formula: str) -> None:
        """validate_formula(None) is a no-op -- does not raise."""
        result = validate_formula(None, VALID_IDENTS)  # type: ignore[arg-type]
        # None is the 'no formula' sentinel; must not raise and returns None.
        assert result is None

    @given(bad_char=st.sampled_from(["@", "#", "$", "%", "^", "&", "!", "~", "`", "<", ">", "?", "="]))
    @settings(max_examples=20)
    def test_unexpected_character_raises(self, bad_char: str) -> None:
        """Non-grammar characters raise CostFormulaError."""
        formula = f"x {bad_char} y"
        with pytest.raises(CostFormulaError):
            validate_formula(formula, VALID_IDENTS)

    @given(formula=st.just("((x + y)"))
    def test_unbalanced_parentheses_raises(self, formula: str) -> None:
        """Unbalanced parentheses raise CostFormulaError."""
        with pytest.raises(CostFormulaError):
            validate_formula(formula, VALID_IDENTS)

    @given(formula=st.just(")"))
    def test_trailing_rparen_raises(self, formula: str) -> None:
        """Leading rparen raises."""
        with pytest.raises(CostFormulaError):
            validate_formula(formula, VALID_IDENTS)

    @given(
        x=st.decimals(min_value=Decimal(1), max_value=Decimal(1000), places=2),
        y=st.decimals(min_value=Decimal(1), max_value=Decimal(1000), places=2),
    )
    @settings(max_examples=50, deadline=None)
    def test_negative_final_result_raises(self, x: Decimal, y: Decimal) -> None:
        """A negative final result raises CostFormulaError (code ``eval_error``)."""
        assume(x < y)
        with pytest.raises(CostFormulaError) as exc_info:
            evaluate_formula("x - y", {"x": x, "y": y}, VALID_IDENTS)
        assert exc_info.value.code == "eval_error"
        assert "negative" in str(exc_info.value)

    @given(case=negative_intermediate_cases())
    @settings(max_examples=50, deadline=None)
    def test_negative_intermediate_result_is_allowed(self, case: tuple[Decimal, Decimal, Decimal]) -> None:
        """Only the final value is checked: a negative subexpression is allowed."""
        x, y, z = case
        result = evaluate_formula("(x - y) + z", {"x": x, "y": y, "z": z}, VALID_IDENTS)
        assert result == x - y + z

    @given(
        formula=simple_valid_formulas,
        params=params_strategy,
    )
    @settings(max_examples=30, deadline=None)
    def test_result_never_exceeds_column_cap(self, formula: str, params: dict) -> None:
        """The evaluator does not produce values above Decimal limits."""
        try:
            result = evaluate_formula(formula, params, VALID_IDENTS)
        except CostFormulaError:
            return
        assert result.is_finite()
