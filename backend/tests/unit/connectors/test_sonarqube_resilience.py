"""Resilience tests for SonarQubeConnector — corrupt/non-finite paging handling."""

import httpx
import pytest
import respx

from modulo.connectors._safe_page import safe_paging_total as _paging_total
from modulo.connectors.base import ConnectorQuery
from modulo.connectors.sonarqube import SonarQubeConnector, _next_page_cursor

TOKEN = "sqp_test_token"
BASE_URL = "https://sonarqube.company.com"
API_BASE = f"{BASE_URL}/api"


@pytest.fixture
def connector():
    return SonarQubeConnector(token=TOKEN, base_url=BASE_URL)


# --- _next_page_cursor coercion edge cases ---


def test_next_page_cursor_non_finite_float_returns_none():
    """inf/nan paging values must not crash pagination or loop forever."""
    assert _next_page_cursor({"paging": {"pageIndex": 1, "total": float("inf")}}, 100) is None
    assert _next_page_cursor({"paging": {"pageIndex": 1, "total": float("nan")}}, 100) is None
    assert _next_page_cursor({"paging": {"pageIndex": float("inf"), "total": 5}}, 100) is None


def test_next_page_cursor_garbage_values_return_none():
    """Non-numeric paging values fall back to defaults and disable pagination."""
    assert _next_page_cursor({"paging": {"pageIndex": "abc", "total": "xyz"}}, 100) is None
    assert _next_page_cursor({"paging": {"pageIndex": [], "total": 5}}, 100) is None
    assert _next_page_cursor({"paging": {"pageIndex": True, "total": True}}, 100) is None


def test_next_page_cursor_missing_paging_returns_none():
    """Absent paging block behaves as a single-page result."""
    assert _next_page_cursor({}, 100) is None
    assert _next_page_cursor({"paging": {}}, 100) is None


def test_next_page_cursor_non_dict_paging_returns_none():
    """A non-dict paging value (list/string) must not raise AttributeError."""
    assert _next_page_cursor({"paging": []}, 100) is None
    assert _next_page_cursor({"paging": "garbage"}, 100) is None
    assert _next_page_cursor({"paging": 42}, 100) is None


def test_next_page_cursor_more_pages_returns_cursor():
    """When total exceeds the pages seen so far, the next page number is returned."""
    assert _next_page_cursor({"paging": {"pageIndex": 1, "total": 250}}, 100) == "2"
    assert _next_page_cursor({"paging": {"pageIndex": 2, "total": 250}}, 100) == "3"


def test_next_page_cursor_last_page_returns_none():
    """Once all pages are seen, pagination stops."""
    assert _next_page_cursor({"paging": {"pageIndex": 1, "total": 100}}, 100) is None
    assert _next_page_cursor({"paging": {"pageIndex": 3, "total": 250}}, 100) is None


# --- _paging_total coercion edge cases ---


def test_paging_total_non_finite_float_returns_zero():
    """inf/nan totals must not poison the reported result count."""
    assert _paging_total({"paging": {"total": float("inf")}}, "paging", "total") == 0
    assert _paging_total({"paging": {"total": float("nan")}}, "paging", "total") == 0


def test_paging_total_garbage_values_return_zero():
    """Garbage totals fall back to zero rather than crashing."""
    assert _paging_total({"paging": {"total": "abc"}}, "paging", "total") == 0
    assert _paging_total({"paging": {"total": True}}, "paging", "total") == 0


def test_paging_total_missing_returns_none():
    """A missing total keeps the historical None behaviour."""
    assert _paging_total({}, "paging", "total") is None
    assert _paging_total({"paging": {}}, "paging", "total") is None


def test_paging_total_non_dict_paging_returns_none():
    """A non-dict paging value (list/string) must not raise AttributeError."""
    assert _paging_total({"paging": []}, "paging", "total") is None
    assert _paging_total({"paging": "garbage"}, "paging", "total") is None
    assert _paging_total({"paging": 42}, "paging", "total") is None


def test_paging_total_valid_values_coerce():
    """Finite numeric totals coerce to int unchanged."""
    assert _paging_total({"paging": {"total": 42}}, "paging", "total") == 42
    assert _paging_total({"paging": {"total": "42"}}, "paging", "total") == 42
    assert _paging_total({"paging": {"total": 0}}, "paging", "total") == 0


# --- End-to-end: query with corrupt paging ---


@respx.mock
async def test_query_projects_corrupt_total_does_not_crash(connector):
    """A corrupt 'total: 1e999' (json parses to inf) must not crash or loop forever."""
    respx.get(f"{API_BASE}/projects/search").mock(
        return_value=httpx.Response(
            200,
            text=(
                '{"components": [{"key": "com.example:my-app"}],'
                ' "paging": {"pageIndex": 1, "pageSize": 100, "total": 1e999}}'
            ),
        ),
    )
    result = await connector.query(ConnectorQuery(resource="projects", limit=100))
    assert len(result.records) == 1
    assert result.next_cursor is None
    assert result.total == 0


@respx.mock
async def test_query_projects_corrupt_page_index_does_not_crash(connector):
    """A corrupt 'pageIndex: 1e999' (json parses to inf) must not crash."""
    respx.get(f"{API_BASE}/projects/search").mock(
        return_value=httpx.Response(
            200,
            text=(
                '{"components": [{"key": "com.example:my-app"}],'
                ' "paging": {"pageIndex": 1e999, "pageSize": 100, "total": 5}}'
            ),
        ),
    )
    result = await connector.query(ConnectorQuery(resource="projects", limit=100))
    assert len(result.records) == 1
    assert result.next_cursor is None


@respx.mock
async def test_query_projects_garbage_paging_does_not_crash(connector):
    """Non-numeric paging values fall back to defaults and disable pagination."""
    respx.get(f"{API_BASE}/projects/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "components": [{"key": "com.example:my-app"}],
                "paging": {"pageIndex": "abc", "pageSize": 100, "total": []},
            },
        ),
    )
    result = await connector.query(ConnectorQuery(resource="projects", limit=100))
    assert len(result.records) == 1
    assert result.next_cursor is None
    assert result.total == 0


@respx.mock
async def test_query_issues_corrupt_total_does_not_crash(connector):
    """Corrupt paging on issues/search behaves identically to projects/search."""
    respx.get(f"{API_BASE}/issues/search").mock(
        return_value=httpx.Response(
            200,
            text=('{"issues": [{"key": "ISSUE1"}], "paging": {"pageIndex": 1, "pageSize": 100, "total": 1e999}}'),
        ),
    )
    result = await connector.query(
        ConnectorQuery(resource="issues", filters={"component": "proj1"}, limit=100),
    )
    assert len(result.records) == 1
    assert result.next_cursor is None
    assert result.total == 0
