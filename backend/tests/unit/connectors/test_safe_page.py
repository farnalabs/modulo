"""Unit tests for the shared safe-page extraction helper.

``modulo.connectors._safe_page.safe_records`` guards list-pagination parsing
in the Azure Repos (``value``) and Bitbucket (``values``) connectors against
corrupt or hostile response bodies. A non-dict body (list, string, number,
...) would otherwise crash the connector with ``AttributeError`` on the bare
``body.get(key, [])`` chain, and a non-list page field would otherwise come
back as a bare string as the records list. The per-connector tests exercise
``safe_records`` indirectly; these tests lock the shared contract directly so
the non-dict/non-list matrix stays consistent across every consumer (mirrors
``test_safe_int``).
"""

from typing import Any

import pytest

from modulo.connectors._safe_page import safe_records

KEYS = ["value", "values"]


@pytest.mark.parametrize("key", KEYS)
def test_safe_records_returns_list_page(key: str) -> None:
    """A dict body with a list page field round-trips unchanged."""
    assert safe_records({key: [{"id": "r1"}]}, key) == [{"id": "r1"}]


@pytest.mark.parametrize("key", KEYS)
@pytest.mark.parametrize("bad", ["not-a-list", 5, {"id": "r1"}, True, 1.5])
def test_safe_records_rejects_non_list_page(key: str, bad: Any) -> None:
    """A non-list page field falls back to an empty page, not a bare value."""
    assert not safe_records({key: bad}, key)


@pytest.mark.parametrize("key", KEYS)
@pytest.mark.parametrize("body", [[1], "garbage", None, 42, 3.14, True])
def test_safe_records_rejects_non_dict_body(key: str, body: Any) -> None:
    """A non-dict body falls back to an empty page instead of crashing."""
    assert not safe_records(body, key)


@pytest.mark.parametrize("key", KEYS)
def test_safe_records_missing_key_returns_empty(key: str) -> None:
    """A dict missing the page key behaves like an empty page."""
    assert not safe_records({}, key)


def test_safe_records_key_mismatch_returns_empty() -> None:
    """The key is the only difference between connectors: ``value`` vs ``values``."""
    assert not safe_records({"value": [{"id": "r1"}]}, "values")
    assert not safe_records({"values": [{"id": "r1"}]}, "value")
