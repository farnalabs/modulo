"""Unit tests for the shared safe-datetime coercion helper.

``modulo.connectors._safe_datetime.safe_datetime`` guards ticket-tracker
connectors (GitHub Issues, Trello) against corrupt or hostile
``created_at``/``updated_at``/``dateLastActivity`` response fields. A
non-string or unparseable timestamp flowing into ``datetime.fromisoformat``
raises ``ValueError``/``TypeError`` and takes down the whole list query. The
per-connector tests exercise ``safe_datetime`` indirectly; these tests lock
the shared contract directly so the bool/empty/non-string matrix stays
consistent across every consumer (mirrors ``test_safe_cursor`` /
``test_safe_int``).
"""

import pytest

from modulo.connectors._safe_datetime import safe_datetime


@pytest.mark.parametrize(
    "value",
    [
        "2025-01-15T10:00:00Z",
        "2025-01-15T10:00:00.000Z",
        "2025-01-15T10:00:00+00:00",
        "2025-01-15T10:00:00-05:00",
        "2025-01-15",
    ],
)
def test_safe_datetime_passes_through_parseable_iso_strings(value: str) -> None:
    """Well-formed ISO 8601 timestamps must round-trip to a ``datetime``."""
    assert safe_datetime(value) is not None


@pytest.mark.parametrize(
    "value",
    [None, True, False, 0, 42, 1.5, float("nan"), float("inf")],
    ids=["none", "true", "false", "zero", "positive", "float", "nan", "inf"],
)
def test_safe_datetime_rejects_non_string_scalars(value: object) -> None:
    """bool/number timestamps would crash ``fromisoformat`` with a TypeError."""
    assert safe_datetime(value) is None


@pytest.mark.parametrize(
    "value",
    [[1], (1,), {"$date": "2025-01-15T10:00:00Z"}, {1, 2}, b"2025-01-15", bytearray(b"x")],
)
def test_safe_datetime_rejects_containers_and_bytes(value: object) -> None:
    """dict/list/bytes values are never valid timestamps."""
    assert safe_datetime(value) is None


@pytest.mark.parametrize(
    "value",
    ["", "not-a-date", "2025-13-99T10:00:00Z", "2025-01-15T99:00:00Z", "   "],
)
def test_safe_datetime_rejects_unparseable_strings(value: str) -> None:
    """Malformed or empty strings must not crash the caller."""
    assert safe_datetime(value) is None


class _UpperStr(str):
    """A str subclass exercising the ``isinstance(value, str)`` branch."""


def test_safe_datetime_accepts_str_subclasses() -> None:
    """A parseable ``str`` subclass is still a valid timestamp string."""
    assert safe_datetime(_UpperStr("2025-01-15T10:00:00Z")) is not None
