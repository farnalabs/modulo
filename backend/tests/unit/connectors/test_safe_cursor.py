"""Unit tests for the shared safe-cursor coercion helper.

``modulo.connectors._safe_cursor.safe_cursor`` guards cursor-paginated
connectors (n8n, Notion, CircleCI, Linear) against corrupt or hostile
``next_*`` response fields. A non-string cursor flowing into the next
request's query params or JSON body makes httpx raise on dict/list values and
silently mis-serialise booleans/numbers, breaking pagination. The
per-connector tests exercise ``safe_cursor`` indirectly; these tests lock the
shared contract directly so the bool/empty/non-string matrix stays consistent
across every consumer (mirrors ``test_safe_int``).

The contract intentionally keeps *any* non-empty string — including
whitespace-only tokens — as a pass-through cursor: the guard's job is to keep
non-string garbage out of the next request, not to validate opaque cursor
syntax. An empty string is the only falsy string, so it is the only string
that falls back to ``None``.
"""

import pytest

from modulo.connectors._safe_cursor import safe_cursor


@pytest.mark.parametrize(
    "value",
    ["abc", "0", "next-page-token", "cursor=42&more=1", "  spaced  ", " "],
)
def test_safe_cursor_passes_through_non_empty_strings(value: str) -> None:
    """Any non-empty string is a meaningful cursor and must round-trip."""
    assert safe_cursor(value) == value


@pytest.mark.parametrize(
    "value",
    [None, True, False, 0, 42, -7, 1.5, 10**30, float("nan"), float("inf")],
    ids=["none", "true", "false", "zero", "positive", "negative", "float", "huge", "nan", "inf"],
)
def test_safe_cursor_rejects_non_string_scalars(value: object) -> None:
    """bool/number cursors would be silently mis-serialised by httpx. ``True``
    is rejected even though ``True == 1``: coercion would turn a flag into a
    cursor and paginate against a nonsense token."""
    assert safe_cursor(value) is None


@pytest.mark.parametrize("value", [[1], (1,), {"page": 2}, {1, 2}, b"abc", bytearray(b"abc"), object()])
def test_safe_cursor_rejects_containers_and_bytes(value: object) -> None:
    """dict/list values make httpx raise on serialisation, and bytes are not
    ``str`` subclasses, so none may be emitted as a cursor."""
    assert safe_cursor(value) is None


def test_safe_cursor_rejects_empty_string() -> None:
    """The empty string is falsy and must not be emitted — an empty cursor
    would loop the client on the same page forever."""
    assert safe_cursor("") is None


class _UpperStr(str):
    """A str subclass exercising the ``isinstance(value, str)`` branch."""


def test_safe_cursor_accepts_str_subclasses() -> None:
    """A non-empty ``str`` subclass is still a string cursor."""
    assert safe_cursor(_UpperStr("token")) == "token"


def test_safe_cursor_rejects_empty_str_subclass() -> None:
    """An empty ``str`` subclass is falsy and falls back to ``None``."""
    assert safe_cursor(_UpperStr("")) is None
