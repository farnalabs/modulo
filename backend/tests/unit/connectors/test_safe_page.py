"""Unit tests for the shared safe-page extraction helpers.

``modulo.connectors._safe_page.safe_records`` guards list-pagination parsing
in the Azure Repos (``value``) and Bitbucket (``values``) connectors against
corrupt or hostile response bodies. A non-dict body (list, string, number,
...) would otherwise crash the connector with ``AttributeError`` on the bare
``body.get(key, [])`` chain, and a non-list page field would otherwise come
back as a bare string as the records list. The per-connector tests exercise
``safe_records`` indirectly; these tests lock the shared contract directly so
the non-dict/non-list matrix stays consistent across every consumer (mirrors
``test_safe_int``).

``safe_paging_total`` centralises the ``_paging_total`` extraction that the
Azure Pipelines (``count``), Azure Repos (``count``), Bitbucket (``size``),
Opsgenie (``totalCount``), PagerDuty (``total``), and SonarQube
(``paging.total``) connectors previously re-implemented per-connector.
"""

from typing import Any

import pytest

from modulo.connectors._safe_page import safe_paging_total, safe_records

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


def test_safe_paging_total_flat_key() -> None:
    """A single nesting level (Azure ``count``, Bitbucket ``size``) round-trips."""
    assert safe_paging_total({"count": 7}, "count") == 7
    assert safe_paging_total({"size": 7}, "size") == 7


def test_safe_paging_total_nested_key() -> None:
    """A nested path (SonarQube ``paging.total``) resolves through dicts."""
    assert safe_paging_total({"paging": {"total": 5}}, "paging", "total") == 5


def test_safe_paging_total_missing_key_returns_none() -> None:
    """A missing total keeps the historical ``None`` behaviour."""
    assert safe_paging_total({"count": 1}, "size") is None
    assert safe_paging_total({"paging": {}}, "paging", "total") is None


def test_safe_paging_total_non_dict_body_returns_none() -> None:
    """A non-dict body (list, string, number, ...) is treated as absent."""
    for body in ([1], "garbage", None, 42, 3.14, True):
        assert safe_paging_total(body, "count") is None


def test_safe_paging_total_non_dict_mid_path_returns_none() -> None:
    """A non-dict hop in the path stops resolution without crashing."""
    assert safe_paging_total({"paging": "not-a-dict"}, "paging", "total") is None


@pytest.mark.parametrize(
    "bad",
    ["inf", "-inf", "nan", float("inf"), float("nan"), "not-a-number", "5x", {}],
    ids=["inf-str", "-inf-str", "nan-str", "inf-float", "nan-float", "not-a-number", "5x", "dict"],
)
def test_safe_paging_total_rejects_non_finite_or_unparseable(bad: Any) -> None:
    """Non-finite floats and unparseable values fall back to the safe default (0)."""
    assert safe_paging_total({"total": bad}, "total") == 0


def test_safe_paging_total_zero_is_kept() -> None:
    """A real zero total is preserved, unlike a missing field."""
    assert safe_paging_total({"count": 0}, "count") == 0
