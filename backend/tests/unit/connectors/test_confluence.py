"""Unit tests for ConfluenceConnector list-parsing hardening using respx mock transports."""

import httpx
import pytest
import respx

from modulo.connectors.base import ConnectorQuery, ConnectorType
from modulo.connectors.confluence import ConfluenceConnector

INSTANCE = "my-domain.atlassian.net/wiki"
TOKEN = "bearer_token"
_BASE = f"https://{INSTANCE}"


@pytest.fixture
def connector() -> ConfluenceConnector:
    return ConfluenceConnector(instance=INSTANCE, creds={"token": TOKEN})


def test_connector_type(connector: ConfluenceConnector) -> None:
    assert connector.connector_type == ConnectorType.CONFLUENCE


# ---------------------------------------------------------------------------
# Corrupt list payload hardening
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_pages_corrupt_body_no_crash(connector: ConfluenceConnector) -> None:
    """A non-dict body from the pages endpoint must degrade to an empty page
    instead of crashing with AttributeError on ``.get()``."""
    respx.get(f"{_BASE}/wiki/api/v2/pages").mock(return_value=httpx.Response(200, json=["garbage"]))
    result = await connector.query(ConnectorQuery(resource="pages"))
    assert result.records == []


@respx.mock
async def test_query_pages_non_list_results_value_no_crash(connector: ConfluenceConnector) -> None:
    """A corrupt body placing a non-list in ``results`` must fall back to an
    empty page instead of returning a bare string as the records list."""
    respx.get(f"{_BASE}/wiki/api/v2/pages").mock(return_value=httpx.Response(200, json={"results": "not-a-list"}))
    result = await connector.query(ConnectorQuery(resource="pages"))
    assert result.records == []


@respx.mock
async def test_query_spaces_corrupt_body_no_crash(connector: ConfluenceConnector) -> None:
    """A non-dict body from the spaces endpoint must degrade to an empty page."""
    respx.get(f"{_BASE}/wiki/api/v2/spaces").mock(return_value=httpx.Response(200, json=["garbage"]))
    result = await connector.query(ConnectorQuery(resource="spaces"))
    assert result.records == []


@respx.mock
async def test_query_content_corrupt_body_no_crash(connector: ConfluenceConnector) -> None:
    """A non-dict body from the content search endpoint must degrade to an empty page."""
    respx.get(f"{_BASE}/wiki/rest/api/content/search", params={"cql": "text~bug"}).mock(
        return_value=httpx.Response(200, json=["garbage"])
    )
    result = await connector.query(ConnectorQuery(resource="content", filters={"cql": "text~bug"}))
    assert result.records == []


@respx.mock
async def test_query_children_corrupt_body_no_crash(connector: ConfluenceConnector) -> None:
    """A non-dict body from the children endpoint must degrade to an empty page."""
    respx.get(f"{_BASE}/wiki/api/v2/pages/p1/children").mock(return_value=httpx.Response(200, json=["garbage"]))
    result = await connector.query(ConnectorQuery(resource="children", filters={"page_id": "p1"}))
    assert result.records == []


@respx.mock
async def test_query_labels_corrupt_body_no_crash(connector: ConfluenceConnector) -> None:
    """A non-dict body from the labels endpoint must degrade to an empty page."""
    respx.get(f"{_BASE}/wiki/api/v2/pages/p1/labels").mock(return_value=httpx.Response(200, json=["garbage"]))
    result = await connector.query(ConnectorQuery(resource="labels", filters={"page_id": "p1"}))
    assert result.records == []
