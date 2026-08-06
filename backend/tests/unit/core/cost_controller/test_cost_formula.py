"""Unit tests for the safe 4-operator formula engine (§2.5)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from modulo.core.cost_controller.breakdown.constants import MAX_FORMULA_DEPTH, MAX_FORMULA_LENGTH
from modulo.core.cost_controller.breakdown.formula import CostFormulaError, evaluate_formula, validate_formula
from modulo.core.cost_controller.breakdown.params import (
    _DEAD_PARAMS,
    _PARAM_REGISTRY,
    CALCULATED_ALLOWED_IDENTS,
    SELF_REPORTED_ALLOWED_IDENTS,
)

_SANDBOX = CALCULATED_ALLOWED_IDENTS


def _params(**kw: Decimal) -> dict[str, Decimal]:
    return kw


def test_rate_times_wall_clock() -> None:
    result = evaluate_formula(
        "rate * wall_clock_hours",
        _params(rate=Decimal("0.1332"), wall_clock_hours=Decimal("1.0")),
        _SANDBOX,
    )
    assert result == Decimal("0.13320")


def test_token_formula() -> None:
    result = evaluate_formula(
        "tokens_input * input_token_rate + tokens_output * output_token_rate",
        _params(
            tokens_input=Decimal(50),
            tokens_output=Decimal(10),
            input_token_rate=Decimal("0.00001"),
            output_token_rate=Decimal("0.00003"),
        ),
        _SANDBOX,
    )
    assert result == Decimal("0.00080")


def test_precedence() -> None:
    assert evaluate_formula("2 + 3 * 4", _params(), _SANDBOX) == Decimal(14)
    assert evaluate_formula("(2 + 3) * 4", _params(), _SANDBOX) == Decimal(20)


def test_unary_minus_parse_legal_and_double_negative() -> None:
    assert evaluate_formula("2 - -3", _params(), _SANDBOX) == Decimal(5)


def test_negative_result_is_eval_error() -> None:
    with pytest.raises(CostFormulaError) as exc_info:
        evaluate_formula("1 - 5", _params(), _SANDBOX)
    assert exc_info.value.code == "eval_error"


def test_decimal_end_to_end() -> None:
    result = evaluate_formula("0.1 + 0.2", _params(), _SANDBOX)
    assert result.quantize(Decimal("0.000001")) == Decimal("0.3")


def test_division_by_zero_raises() -> None:
    with pytest.raises((CostFormulaError, ArithmeticError)):
        evaluate_formula("rate / wall_clock_hours", _params(rate=Decimal(1), wall_clock_hours=Decimal(0)), _SANDBOX)


def test_unknown_identifier_rejected() -> None:
    with pytest.raises(CostFormulaError) as exc_info:
        validate_formula("bogus_param + 1", _SANDBOX)
    assert exc_info.value.code == "unknown_identifier"


def test_reported_rejected_in_calculated() -> None:
    with pytest.raises(CostFormulaError) as exc_info:
        validate_formula("reported + 1", _SANDBOX)
    assert exc_info.value.code == "unknown_identifier"


def test_formula_too_long_rejected() -> None:
    with pytest.raises(CostFormulaError) as exc_info:
        validate_formula("1 + 1 + 1" * 200, _SANDBOX)
    assert exc_info.value.code == "formula_too_long"


def test_max_length_boundary() -> None:
    formula = "1 + 1"
    assert len(formula) <= MAX_FORMULA_LENGTH
    validate_formula(formula, _SANDBOX)  # no raise


def test_depth_exceeded() -> None:
    deep = "(" * (MAX_FORMULA_DEPTH + 1) + "1" + ")" * (MAX_FORMULA_DEPTH + 1)
    with pytest.raises(CostFormulaError) as exc_info:
        validate_formula(deep, _SANDBOX)
    assert exc_info.value.code == "depth_exceeded"


def test_depth_exactly_max_accepted() -> None:
    ok = "(" * MAX_FORMULA_DEPTH + "1" + ")" * MAX_FORMULA_DEPTH
    validate_formula(ok, _SANDBOX)  # no raise
    assert evaluate_formula(ok, _params(), _SANDBOX) == Decimal(1)


def test_attack_strings_rejected() -> None:
    attacks = [
        "__import__('os').system('id')",
        "()[().__class__.__bases__[0].__subclasses__()]",
        "open('/etc/passwd')",
        "1 if True else 2",
        "a or b",
        "not a",
        "a == b",
        "a < b",
        "abs(-1).__class__",
        "pow(2,3)",
        "eval(1)",
        'exec("x")',
        "round(2.5)",
    ]
    for attack in attacks:
        with pytest.raises(CostFormulaError):
            validate_formula(attack, _SANDBOX)


def test_empty_and_whitespace_rejected() -> None:
    for formula in ("", "   ", "\t\n"):
        with pytest.raises(CostFormulaError) as exc_info:
            validate_formula(formula, _SANDBOX)
        assert exc_info.value.code == "empty_expression"


def test_non_ascii_homoglyph_rejected() -> None:
    with pytest.raises(CostFormulaError) as exc_info:
        validate_formula("2 \u2217 3", _SANDBOX)  # full-width asterisk U+2217 (intentional)
    assert exc_info.value.code == "unexpected_character"
    with pytest.raises(CostFormulaError) as exc_info:
        validate_formula("2\u00a03", _SANDBOX)  # NBSP
    assert exc_info.value.code == "unexpected_character"


def test_out_of_position_tokens_rejected() -> None:
    for formula in ("1 2", "() 3", "1 2 3"):
        with pytest.raises(CostFormulaError):
            validate_formula(formula, _SANDBOX)


def test_param_registry_dead_params_absent() -> None:
    for dead in _DEAD_PARAMS:
        assert dead not in _PARAM_REGISTRY, f"dead param {dead} must be absent from the registry"


def test_registry_has_expected_identifiers() -> None:
    expected = {
        "rate",
        "e2b_rate",
        "input_token_rate",
        "output_token_rate",
        "wall_clock_hours",
        "tokens_input",
        "tokens_output",
        "tokens_estimated",
        "node_count",
        "nodes_estimated",
        "reported",
    }
    assert set(_PARAM_REGISTRY) == expected


def test_calculated_allowed_excludes_reported() -> None:
    assert "reported" not in CALCULATED_ALLOWED_IDENTS
    assert frozenset({"reported"}) == SELF_REPORTED_ALLOWED_IDENTS


def test_validate_formula_none_is_noop() -> None:
    validate_formula(None, _SANDBOX)  # no raise, no identifier validation attempted


def test_newline_inside_formula_is_unexpected_character() -> None:
    # The tokenizer's catch-all '.' does not match a newline, so the regex
    # fails to match -> the unexpected_character path fires.
    with pytest.raises(CostFormulaError) as exc_info:
        validate_formula("1\n", _SANDBOX)
    assert exc_info.value.code == "unexpected_character"


def test_missing_rparen_is_unexpected_token() -> None:
    with pytest.raises(CostFormulaError) as exc_info:
        validate_formula("(1 + 2", _SANDBOX)
    assert exc_info.value.code == "unexpected_token"


def test_trailing_operator_is_unexpected_token() -> None:
    with pytest.raises(CostFormulaError) as exc_info:
        validate_formula("1 +", _SANDBOX)
    assert exc_info.value.code == "unexpected_token"
    assert "end of formula" in str(exc_info.value)


def test_stray_operator_in_primary_is_unexpected_token() -> None:
    with pytest.raises(CostFormulaError) as exc_info:
        validate_formula("1 + * 2", _SANDBOX)
    assert exc_info.value.code == "unexpected_token"


def test_non_finite_param_result_is_eval_error() -> None:
    with pytest.raises(CostFormulaError) as exc_info:
        evaluate_formula("wall_clock_hours", {"wall_clock_hours": Decimal("Infinity")}, _SANDBOX)
    assert exc_info.value.code == "eval_error"
