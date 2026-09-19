"""Hypothesis property-based tests for the cost clamping and token coercion cores.

Targets:
* ``clamp_reported`` from ``modulo.core.cost_controller.breakdown.aggregate``
  -- self-reported model cost clamping (defense-in-depth at enrichment).
* ``coerce_reported_token`` from ``modulo.core.cost_controller.breakdown.params``
  -- tri-state reported-token coercion (the ONE shared rule, FAR-491).

Properties assert INVARIANTS:
* Values below the floor are rejected (return None).
* Values above the band are clamped.
* Booleans are always rejected.
* Non-finite values are always rejected.
* The result is always within bounds.
* Coercion is idempotent for valid integers.
"""

from __future__ import annotations

from decimal import Decimal

import hypothesis.strategies as st
from hypothesis import given, settings

from modulo.core.cost_controller.breakdown.aggregate import clamp_reported
from modulo.core.cost_controller.breakdown.constants import (
    MAX_REPORTABLE_BAND_USD,
    MAX_REPORTABLE_USD_MIN,
    MAX_SELF_REPORTED_USD,
)
from modulo.core.cost_controller.breakdown.params import (
    MAX_REPORTABLE_TOKEN_COUNT,
    coerce_reported_token,
)

# ---------------------------------------------------------------------------
# 1. clamp_reported -- self-reported model cost clamping
# ---------------------------------------------------------------------------


class TestClampReportedProperties:
    """Properties for the defense-in-depth cost clamping function."""

    @given(value=st.booleans())
    def test_booleans_are_rejected(self, value: bool) -> None:
        """Boolean inputs always return None (treated as absent)."""
        assert clamp_reported(value) is None

    @given(
        value=st.one_of(
            st.just(float("nan")),
            st.just(float("inf")),
            st.just(float("-inf")),
        )
    )
    def test_non_finite_are_rejected(self, value: float) -> None:
        """NaN and Inf inputs always return None."""
        assert clamp_reported(value) is None

    @given(value=st.integers(max_value=0))
    def test_zero_and_negative_below_floor_are_rejected(self, value: int) -> None:
        """Values below the reportable floor are rejected."""
        d = Decimal(value)
        if d < MAX_REPORTABLE_USD_MIN:
            assert clamp_reported(d) is None

    @given(
        value=st.decimals(
            min_value=MAX_REPORTABLE_USD_MIN,
            max_value=MAX_REPORTABLE_BAND_USD,
            places=6,
        )
    )
    @settings(max_examples=50)
    def test_in_band_values_are_not_clamped(self, value: Decimal) -> None:
        """Values within [floor, band] pass through unclamped."""
        result = clamp_reported(value)
        assert result is not None
        clamped, was_clamped, oob = result
        assert clamped == value
        assert was_clamped is False
        assert oob is False

    @given(
        value=st.decimals(
            min_value=MAX_REPORTABLE_BAND_USD + Decimal("0.000001"),
            max_value=MAX_SELF_REPORTED_USD,
            places=6,
        )
    )
    @settings(max_examples=50)
    def test_above_band_is_clamped_to_band(self, value: Decimal) -> None:
        """Values above the band ceiling are clamped to the band."""
        result = clamp_reported(value)
        assert result is not None
        clamped, was_clamped, oob = result
        assert clamped == MAX_REPORTABLE_BAND_USD
        assert was_clamped is True
        assert oob is True

    @given(
        value=st.decimals(
            min_value=MAX_SELF_REPORTED_USD + Decimal(1),
            max_value=Decimal(999999),
            places=2,
        )
    )
    @settings(max_examples=30)
    def test_above_cap_is_clamped_to_band(self, value: Decimal) -> None:
        """Values above the self-reported cap are clamped to the band."""
        result = clamp_reported(value)
        assert result is not None
        clamped, was_clamped, oob = result
        assert clamped == MAX_REPORTABLE_BAND_USD
        assert was_clamped is True
        assert oob is True

    @given(value=st.just("not_a_number"))
    def test_non_numeric_string_is_rejected(self, value: str) -> None:
        """Non-numeric strings return None."""
        assert clamp_reported(value) is None  # type: ignore[arg-type]

    @given(value=st.just(None))
    def test_none_is_rejected(self, value: object) -> None:
        """None returns None."""
        assert clamp_reported(value) is None  # type: ignore[arg-type]

    @given(
        value=st.decimals(
            min_value=MAX_REPORTABLE_USD_MIN,
            max_value=MAX_REPORTABLE_BAND_USD,
            places=6,
        )
    )
    @settings(max_examples=30)
    def test_clamped_value_never_exceeds_band(self, value: Decimal) -> None:
        """The returned clamped value is always <= the band ceiling."""
        result = clamp_reported(value)
        if result is not None:
            clamped, _, _ = result
            assert clamped <= MAX_REPORTABLE_BAND_USD

    @given(
        value=st.decimals(
            min_value=MAX_REPORTABLE_USD_MIN,
            max_value=Decimal(999999),
            places=6,
        )
    )
    @settings(max_examples=50)
    def test_result_always_within_floor_and_band(self, value: Decimal) -> None:
        """If a result is returned, clamped value is in [floor, band]."""
        result = clamp_reported(value)
        if result is not None:
            clamped, _, _ = result
            assert clamped >= MAX_REPORTABLE_USD_MIN
            assert clamped <= MAX_REPORTABLE_BAND_USD

    @given(
        value=st.decimals(
            min_value=MAX_REPORTABLE_USD_MIN,
            max_value=MAX_REPORTABLE_BAND_USD - Decimal(1),
            places=6,
        )
    )
    @settings(max_examples=20)
    def test_sub_band_oob_is_false(self, value: Decimal) -> None:
        """Values below the band should NOT be flagged out-of-band-high."""
        result = clamp_reported(value)
        if result is not None:
            _, _, oob = result
            assert oob is False


# ---------------------------------------------------------------------------
# 2. coerce_reported_token -- tri-state token coercion
# ---------------------------------------------------------------------------


class TestCoerceReportedTokenProperties:
    """Properties for the shared reported-token coercion function."""

    @given(value=st.booleans())
    def test_booleans_are_rejected(self, value: bool) -> None:
        """Booleans always return None."""
        assert coerce_reported_token(value) is None

    @given(value=st.integers(min_value=0, max_value=MAX_REPORTABLE_TOKEN_COUNT))
    @settings(max_examples=100)
    def test_valid_non_negative_integers_are_accepted(self, value: int) -> None:
        """Non-negative integers within range pass through as int."""
        result = coerce_reported_token(value)
        assert result == value
        assert isinstance(result, int)

    @given(value=st.integers(max_value=-1))
    def test_negative_integers_are_rejected(self, value: int) -> None:
        """Negative integers return None."""
        assert coerce_reported_token(value) is None

    @given(value=st.integers(min_value=MAX_REPORTABLE_TOKEN_COUNT + 1))
    def test_above_ceiling_integers_are_rejected(self, value: int) -> None:
        """Integers above MAX_REPORTABLE_TOKEN_COUNT return None."""
        assert coerce_reported_token(value) is None

    @given(value=st.integers(min_value=0, max_value=10000))
    def test_zero_is_valid(self, value: int) -> None:
        """A valid 0 is accepted (it IS a real report)."""
        if value == 0:
            assert coerce_reported_token(0) == 0

    @given(
        value=st.floats(
            min_value=0,
            max_value=float(MAX_REPORTABLE_TOKEN_COUNT),
            allow_nan=False,
            allow_infinity=False,
        )
    )
    @settings(max_examples=50)
    def test_integral_floats_are_coerced_to_int(self, value: float) -> None:
        """An integral float (e.g. 1234.0) is accepted and normalized to int."""
        if value == int(value) and value >= 0:
            result = coerce_reported_token(value)
            assert result == int(value)
            assert isinstance(result, int)

    @given(
        value=st.one_of(
            st.just(float("nan")),
            st.just(float("inf")),
            st.just(float("-inf")),
        )
    )
    def test_non_finite_floats_are_rejected(self, value: float) -> None:
        """NaN and Inf floats return None."""
        assert coerce_reported_token(value) is None

    @given(value=st.just(None))
    def test_none_is_rejected(self, value: object) -> None:
        """None returns None."""
        assert coerce_reported_token(value) is None  # type: ignore[arg-type]

    @given(value=st.text(min_size=1, max_size=50))
    def test_non_numeric_strings_are_rejected(self, value: str) -> None:
        """Non-numeric strings return None."""
        assert coerce_reported_token(value) is None  # type: ignore[arg-type]

    @given(value=st.integers(min_value=0, max_value=MAX_REPORTABLE_TOKEN_COUNT))
    @settings(max_examples=50)
    def test_idempotent_for_valid_integers(self, value: int) -> None:
        """coerce(coerce(x)) == coerce(x) for valid integers."""
        first = coerce_reported_token(value)
        second = coerce_reported_token(first)
        assert first == second

    @given(value=st.integers(min_value=0, max_value=10000))
    def test_result_is_never_negative(self, value: int) -> None:
        """The returned value is always >= 0."""
        result = coerce_reported_token(value)
        if result is not None:
            assert result >= 0

    @given(value=st.integers(min_value=0, max_value=MAX_REPORTABLE_TOKEN_COUNT))
    def test_result_never_exceeds_ceiling(self, value: int) -> None:
        """The returned value is always <= MAX_REPORTABLE_TOKEN_COUNT."""
        result = coerce_reported_token(value)
        if result is not None:
            assert result <= MAX_REPORTABLE_TOKEN_COUNT

    @given(value=st.integers(min_value=0, max_value=10000))
    def test_result_type_is_int_or_none(self, value: int) -> None:
        """Return type is int or None, never float or str."""
        result = coerce_reported_token(value)
        assert result is None or isinstance(result, int)
