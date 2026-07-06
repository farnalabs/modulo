"""Unit tests for ConfluenceConnector — HTTP responses are mocked via httpx + respx."""

import httpx
import pytest
import respx

from modulo.connectors.base import ConnectorPayload, ConnectorQuery, ConnectorType
from modulo.connectors.confluence import ConfluenceConnector

INSTANCE = "my-domain.atlassian.net/wiki"
TOKEN = "bearer_token"
EMAIL = "user@example.com"
API_TOKEN = "api_token_123"
_BASE = f"https://{INSTANCE}"


@pytest.fixture()
def connector():
    return ConfluenceConnector(instance=INSTANCE, creds={"token": TOKEN})


@pytest.fixture()
def connector_basic_auth():
    return ConfluenceConnector(instance=INSTANCE, creds={"email": EMAIL, "api_token": API_TOKEN})


# ---------------------------------------------------------------------------
# connector_type
# ---------------------------------------------------------------------------


def test_connector_type(connector):
    assert connector.connector_type == ConnectorType.CONFLUENCE


# ---------------------------------------------------------------------------
# health_check
# ---------------------------------------------------------------------------


@respx.mock
async def test_health_check_ok(connector):
    respx.get(f"{_BASE}/wiki/rest/api/user/current").mock(
        return_value=httpx.Response(200, json={"displayName": "Alice Smith"}),
    )
    result = await connector.health_check()
    assert result.ok is True
    assert result.detail == "Alice Smith"


@respx.mock
async def test_health_check_fail(connector):
    respx.get(f"{_BASE}/wiki/rest/api/user/current").mock(return_value=httpx.Response(401, text="Unauthorized"))
    result = await connector.health_check()
    assert result.ok is False
    assert "401" in result.detail


# ---------------------------------------------------------------------------
# query — pages
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_pages(connector):
    pages = {
        "results": [
            {"id": "p1", "title": "Page One", "spaceId": "s1"},
            {"id": "p2", "title": "Page Two", "spaceId": "s1"},
        ]
    }
    respx.get(f"{_BASE}/wiki/api/v2/pages").mock(return_value=httpx.Response(200, json=pages))
    result = await connector.query(ConnectorQuery(resource="pages"))
    assert len(result.records) == 2
    assert result.records[0]["title"] == "Page One"


@respx.mock
async def test_query_pages_with_space_id(connector):
    pages = {"results": [{"id": "p1", "title": "Space Page", "spaceId": "s1"}]}
    respx.get(f"{_BASE}/wiki/api/v2/pages", params={"spaceId": "s1", "limit": 50}).mock(
        return_value=httpx.Response(200, json=pages)
    )
    result = await connector.query(ConnectorQuery(resource="pages", filters={"space_id": "s1", "limit": 50}))
    assert len(result.records) == 1
    assert result.records[0]["spaceId"] == "s1"


# ---------------------------------------------------------------------------
# query — single page
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_single_page(connector):
    page = {"id": "p1", "title": "Single Page", "spaceId": "s1", "version": {"number": 2}}
    respx.get(f"{_BASE}/wiki/api/v2/pages/p1").mock(return_value=httpx.Response(200, json=page))
    result = await connector.query(ConnectorQuery(resource="page", filters={"page_id": "p1"}))
    assert len(result.records) == 1
    assert result.records[0]["title"] == "Single Page"


async def test_query_single_page_missing_id(connector):
    with pytest.raises(ValueError, match="'page_id' filter"):
        await connector.query(ConnectorQuery(resource="page"))


# ---------------------------------------------------------------------------
# query — spaces
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_spaces(connector):
    spaces = {
        "results": [
            {"id": "s1", "name": "Space One", "key": "SP1"},
            {"id": "s2", "name": "Space Two", "key": "SP2"},
        ]
    }
    respx.get(f"{_BASE}/wiki/api/v2/spaces").mock(return_value=httpx.Response(200, json=spaces))
    result = await connector.query(ConnectorQuery(resource="spaces"))
    assert len(result.records) == 2
    assert result.records[0]["name"] == "Space One"


@respx.mock
async def test_query_spaces_with_type(connector):
    spaces = {"results": [{"id": "s1", "name": "Global Space", "key": "GS"}]}
    respx.get(f"{_BASE}/wiki/api/v2/spaces", params={"limit": 50, "type": "global"}).mock(
        return_value=httpx.Response(200, json=spaces)
    )
    result = await connector.query(ConnectorQuery(resource="spaces", filters={"limit": 50, "type": "global"}))
    assert len(result.records) == 1


# ---------------------------------------------------------------------------
# query — single space
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_single_space(connector):
    space = {"id": "s1", "name": "Single Space", "key": "SS", "description": "A space"}
    respx.get(f"{_BASE}/wiki/api/v2/spaces/s1").mock(return_value=httpx.Response(200, json=space))
    result = await connector.query(ConnectorQuery(resource="space", filters={"space_id": "s1"}))
    assert len(result.records) == 1
    assert result.records[0]["name"] == "Single Space"


async def test_query_single_space_missing_id(connector):
    with pytest.raises(ValueError, match="'space_id' filter"):
        await connector.query(ConnectorQuery(resource="space"))


# ---------------------------------------------------------------------------
# query — content (CQL search)
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_content_search(connector):
    results = {
        "results": [
            {"id": "c1", "title": "Found Page", "type": "page"},
        ]
    }
    respx.get(f"{_BASE}/wiki/rest/api/content/search", params={"cql": "text~bug"}).mock(
        return_value=httpx.Response(200, json=results)
    )
    result = await connector.query(ConnectorQuery(resource="content", filters={"cql": "text~bug"}))
    assert len(result.records) == 1
    assert result.records[0]["title"] == "Found Page"


async def test_query_content_missing_cql(connector):
    with pytest.raises(ValueError, match="'cql' filter"):
        await connector.query(ConnectorQuery(resource="content"))


# ---------------------------------------------------------------------------
# query — children
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_children(connector):
    children = {
        "results": [
            {"id": "c1", "title": "Child One"},
            {"id": "c2", "title": "Child Two"},
        ]
    }
    respx.get(f"{_BASE}/wiki/api/v2/pages/p1/children").mock(return_value=httpx.Response(200, json=children))
    result = await connector.query(ConnectorQuery(resource="children", filters={"page_id": "p1"}))
    assert len(result.records) == 2
    assert result.records[0]["title"] == "Child One"


async def test_query_children_missing_page_id(connector):
    with pytest.raises(ValueError, match="'page_id' filter"):
        await connector.query(ConnectorQuery(resource="children"))


# ---------------------------------------------------------------------------
# query — labels
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_labels(connector):
    labels = {
        "results": [
            {"id": "l1", "name": "documentation"},
            {"id": "l2", "name": "how-to"},
        ]
    }
    respx.get(f"{_BASE}/wiki/api/v2/pages/p1/labels").mock(return_value=httpx.Response(200, json=labels))
    result = await connector.query(ConnectorQuery(resource="labels", filters={"page_id": "p1"}))
    assert len(result.records) == 2
    assert result.records[0]["name"] == "documentation"


async def test_query_labels_missing_page_id(connector):
    with pytest.raises(ValueError, match="'page_id' filter"):
        await connector.query(ConnectorQuery(resource="labels"))


# ---------------------------------------------------------------------------
# query — unknown resource
# ---------------------------------------------------------------------------


async def test_query_unsupported_resource(connector):
    with pytest.raises(ValueError, match="Unsupported Confluence resource"):
        await connector.query(ConnectorQuery(resource="unknown"))


# ---------------------------------------------------------------------------
# write — create page
# ---------------------------------------------------------------------------


@respx.mock
async def test_write_create_page(connector):
    created = {"id": "p_new", "title": "New Page", "spaceId": "s1", "version": {"number": 1}}
    respx.post(f"{_BASE}/wiki/api/v2/pages").mock(return_value=httpx.Response(200, json=created))
    result = await connector.write(
        ConnectorPayload(
            resource="page",
            data={"spaceId": "s1", "title": "New Page", "body": {"representation": "storage", "value": "<p>Hello</p>"}},
        )
    )
    assert result["id"] == "p_new"
    assert result["title"] == "New Page"


# ---------------------------------------------------------------------------
# write — update page
# ---------------------------------------------------------------------------


@respx.mock
async def test_write_update_page(connector):
    respx.put(f"{_BASE}/wiki/api/v2/pages/p1").mock(
        return_value=httpx.Response(200, json={"id": "p1", "title": "Updated", "version": {"number": 2}})
    )
    result = await connector.write(
        ConnectorPayload(
            resource="page_update",
            data={"id": "p1", "title": "Updated", "body": {"representation": "storage", "value": "<p>Updated</p>"}},
        )
    )
    assert result["updated"] is True
    assert result["id"] == "p1"


async def test_write_update_page_missing_id(connector):
    with pytest.raises(ValueError, match="'id' in data"):
        await connector.write(ConnectorPayload(resource="page_update", data={"title": "Orphan"}))


# ---------------------------------------------------------------------------
# write — add label
# ---------------------------------------------------------------------------


@respx.mock
async def test_write_add_label(connector):
    respx.post(f"{_BASE}/wiki/api/v2/pages/p1/labels").mock(return_value=httpx.Response(200, json={"name": "how-to"}))
    result = await connector.write(
        ConnectorPayload(
            resource="label",
            data={"page_id": "p1", "label": "how-to"},
        )
    )
    assert result["page_id"] == "p1"
    assert result["label"] == "how-to"
    assert result["created"] is True


async def test_write_add_label_missing_data(connector):
    with pytest.raises(ValueError, match="requires 'page_id' and 'label'"):
        await connector.write(ConnectorPayload(resource="label", data={"page_id": "p1"}))
    with pytest.raises(ValueError, match="requires 'page_id' and 'label'"):
        await connector.write(ConnectorPayload(resource="label", data={"label": "how-to"}))


# ---------------------------------------------------------------------------
# write — unknown resource
# ---------------------------------------------------------------------------


async def test_write_unsupported_resource(connector):
    with pytest.raises(ValueError, match="Unsupported Confluence write resource"):
        await connector.write(ConnectorPayload(resource="delete", data={}))


# ---------------------------------------------------------------------------
# HTTP error propagation
# ---------------------------------------------------------------------------


@respx.mock
async def test_http_error_propagation(connector):
    respx.get(f"{_BASE}/wiki/api/v2/pages").mock(return_value=httpx.Response(500, text="Internal Server Error"))
    with pytest.raises(httpx.HTTPStatusError):
        await connector.query(ConnectorQuery(resource="pages"))


# ---------------------------------------------------------------------------
# credential handling
# ---------------------------------------------------------------------------


async def test_raises_on_missing_creds():
    with pytest.raises(ValueError, match="must contain either"):
        ConfluenceConnector(instance=INSTANCE, creds={})


def test_basic_auth_sets_auth_property(connector_basic_auth):
    assert connector_basic_auth._auth is not None
    assert connector_basic_auth._token is None


def test_bearer_token_sets_token(connector):
    assert connector._token == TOKEN
    assert connector._auth is None


@respx.mock
async def test_bearer_auth_header_sent(connector):
    pages = {"results": []}
    route = respx.get(f"{_BASE}/wiki/api/v2/pages").mock(return_value=httpx.Response(200, json=pages))
    await connector.query(ConnectorQuery(resource="pages"))
    request = route.calls[0].request
    assert request.headers["Authorization"] == f"Bearer {TOKEN}"
