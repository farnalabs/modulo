"""Unit tests for the shared safe-int coercion helper.

``modulo.connectors._safe_int.safe_int`` guards pagination parsing and cost
aggregation in Jira, Slack, Opsgenie, Snyk, SonarQube and the dashboard route
against corrupt/non-finite values — Python's json parser produces ``inf`` for
overflowing literals such as ``1e999``, so a hostile API response must not be
able to crash a connector or poison a cursor. The per-connector tests exercise
``safe_int`` indirectly; these tests lock the shared contract directly so the
bool/non-finite/unparseable matrix stays consistent across every consumer.
"""

from decimal import Decimal

import pytest

from modulo.connectors._safe_int import safe_int

DEFAULT = 7


@pytest.mark.parametrize("value", [True, False])
def test_safe_int_rejects_booleans(value: bool) -> None:
    """``bool`` is rejected even though ``True == 1`` — coercion would silently
    turn a flag into an offset."""
    assert safe_int(value, DEFAULT) == DEFAULT


@pytest.mark.parametrize(
    "value",
    [
        None,
        [1],
        (1,),
        {"page": 2},
        {1, 2},
        object(),
        1.5 + 0j,
    ],
)
def test_safe_int_rejects_non_numeric_types(value: object) -> None:
    assert safe_int(value, DEFAULT) == DEFAULT


@pytest.mark.parametrize(
    "value",
    [float("inf"), float("-inf"), float("nan")],
)
def test_safe_int_rejects_non_finite_floats(value: float) -> None:
    """Overflowing json literals parse to ``inf``/``nan``; int() on them raises."""
    assert safe_int(value, DEFAULT) == DEFAULT


@pytest.mark.parametrize(
    "value",
    [Decimal("Infinity"), Decimal("-Infinity"), Decimal("NaN"), Decimal("sNaN")],
)
def test_safe_int_rejects_non_finite_decimals(value: Decimal) -> None:
    assert safe_int(value, DEFAULT) == DEFAULT


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (42, 42),
        (0, 0),
        (-3, -3),
        (10**30, 10**30),
    ],
)
def test_safe_int_passes_through_ints(value: int, expected: int) -> None:
    assert safe_int(value, DEFAULT) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0.0, 0),
        (0.9, 0),
        (1.5, 1),
        (42.9, 42),
        (-1.9, -1),
        (-42.0, -42),
        (1e20, 10**20),
    ],
)
def test_safe_int_truncates_floats_toward_zero(value: float, expected: int) -> None:
    assert safe_int(value, DEFAULT) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("42", 42),
        ("0", 0),
        ("-42", -42),
        ("+42", 42),
        ("  42  ", 42),
        ("1_000", 1000),
        ("100000000000000000000", 10**20),
    ],
)
def test_safe_int_parses_numeric_strings(value: str, expected: int) -> None:
    assert safe_int(value, DEFAULT) == expected


@pytest.mark.parametrize(
    "value",
    [
        "",
        "   ",
        "not-a-number",
        "1.5",
        "1e3",
        "0x2a",
        "inf",
        "-inf",
        "nan",
        "12,5",
    ],
    ids=["empty", "spaces", "not-a-number", "decimal", "scientific", "hex", "inf", "-inf", "nan", "decimal-comma"],
)
def test_safe_int_rejects_unparseable_strings(value: str) -> None:
    assert safe_int(value, DEFAULT) == DEFAULT


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (b"42", 42),
        (b"  7  ", 7),
        (bytearray(b"42"), 42),
    ],
)
def test_safe_int_parses_bytes_and_bytearray(value: object, expected: int) -> None:
    assert safe_int(value, DEFAULT) == expected


@pytest.mark.parametrize("value", [b"", b"nope", bytearray(b"nope")])
def test_safe_int_rejects_unparseable_bytes(value: object) -> None:
    assert safe_int(value, DEFAULT) == DEFAULT


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (Decimal(42), 42),
        (Decimal("42.9"), 42),
        (Decimal("-1.9"), -1),
        (Decimal(0), 0),
    ],
)
def test_safe_int_truncates_finite_decimals(value: Decimal, expected: int) -> None:
    assert safe_int(value, DEFAULT) == expected


def test_safe_int_custom_default_used_for_invalid() -> None:
    """The caller-supplied default is returned for every failure class."""
    assert safe_int(None, -1) == -1
    assert safe_int(float("inf"), -1) == -1
    assert safe_int("garbage", -1) == -1
    assert safe_int(True, -1) == -1


def test_safe_int_default_is_zero_when_omitted() -> None:
    assert safe_int(None) == 0
    assert safe_int(float("nan")) == 0
    assert safe_int("junk") == 0
    assert safe_int(42) == 42
