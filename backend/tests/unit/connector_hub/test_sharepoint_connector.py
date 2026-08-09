"""Unit tests for SharePointConnector — HTTP responses are mocked via httpx + respx."""

import httpx
import pytest
import respx

from modulo.connectors.base import ConnectorPayload, ConnectorQuery, ConnectorType
from modulo.connectors.sharepoint import SharePointConnector

TOKEN = "sp_test_token"
_BASE = "https://graph.microsoft.com/v1.0"


@pytest.fixture
def connector():
    return SharePointConnector(token=TOKEN)


# ---------------------------------------------------------------------------
# connector_type
# ---------------------------------------------------------------------------


def test_connector_type(connector):
    assert connector.connector_type == ConnectorType.SHAREPOINT


# ---------------------------------------------------------------------------
# health_check
# ---------------------------------------------------------------------------


@respx.mock
async def test_health_check_ok(connector):
    respx.get(f"{_BASE}/sites/root").mock(
        return_value=httpx.Response(200, json={"displayName": "Root Site"}),
    )
    result = await connector.health_check()
    assert result.ok is True
    assert result.detail == "Root Site"


@respx.mock
async def test_health_check_invalid_token(connector):
    respx.get(f"{_BASE}/sites/root").mock(return_value=httpx.Response(401, text="Unauthorized"))
    result = await connector.health_check()
    assert result.ok is False
    assert "401" in result.detail


@respx.mock
async def test_health_check_http_error(connector):
    respx.get(f"{_BASE}/sites/root").mock(return_value=httpx.Response(500, text="Internal Error"))
    result = await connector.health_check()
    assert result.ok is False
    assert "500" in result.detail


@respx.mock
async def test_health_check_connect_error(connector):
    respx.get(f"{_BASE}/sites/root").mock(side_effect=httpx.ConnectError("boom"))
    result = await connector.health_check()
    assert result.ok is False
    assert "connection error" in result.detail


# ---------------------------------------------------------------------------
# query — sites
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_sites(connector):
    body = {"value": [{"id": "s1", "displayName": "Team Site"}]}
    respx.get(f"{_BASE}/sites").mock(return_value=httpx.Response(200, json=body))
    result = await connector.query(ConnectorQuery(resource="sites", filters={"search": "team"}))
    assert len(result.records) == 1
    assert result.records[0]["id"] == "s1"


# ---------------------------------------------------------------------------
# query — lists
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_lists(connector):
    body = {"value": [{"id": "l1", "displayName": "Tasks"}]}
    respx.get(f"{_BASE}/sites/s1/lists").mock(return_value=httpx.Response(200, json=body))
    result = await connector.query(ConnectorQuery(resource="lists", filters={"site_id": "s1"}))
    assert len(result.records) == 1
    assert result.records[0]["id"] == "l1"


async def test_query_lists_missing_site_id(connector):
    with pytest.raises(ValueError, match="'site_id' filter"):
        await connector.query(ConnectorQuery(resource="lists"))


# ---------------------------------------------------------------------------
# query — list_items
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_list_items(connector):
    body = {"value": [{"id": 1, "fields": {"Title": "Task 1"}}]}
    respx.get(f"{_BASE}/sites/s1/lists/l1/items").mock(return_value=httpx.Response(200, json=body))
    result = await connector.query(
        ConnectorQuery(resource="list_items", filters={"site_id": "s1", "list_id": "l1"}),
    )
    assert len(result.records) == 1


async def test_query_list_items_missing_filters(connector):
    with pytest.raises(ValueError, match="'site_id' and 'list_id' filters"):
        await connector.query(ConnectorQuery(resource="list_items", filters={"site_id": "s1"}))


# ---------------------------------------------------------------------------
# query — drive
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_drive_root(connector):
    body = {"value": [{"name": "file.txt"}]}
    respx.get(f"{_BASE}/sites/s1/drives/d1/root/children").mock(return_value=httpx.Response(200, json=body))
    result = await connector.query(
        ConnectorQuery(resource="drive", filters={"site_id": "s1", "drive_id": "d1"}),
    )
    assert len(result.records) == 1


@respx.mock
async def test_query_drive_subpath(connector):
    body = {"value": [{"name": "nested.md"}]}
    respx.get(f"{_BASE}/sites/s1/drives/d1/root:/docs:/children").mock(
        return_value=httpx.Response(200, json=body),
    )
    result = await connector.query(
        ConnectorQuery(resource="drive", filters={"site_id": "s1", "drive_id": "d1", "path": "/docs"}),
    )
    assert len(result.records) == 1


async def test_query_drive_missing_filters(connector):
    with pytest.raises(ValueError, match="'site_id' and 'drive_id' filters"):
        await connector.query(ConnectorQuery(resource="drive", filters={"site_id": "s1"}))


# ---------------------------------------------------------------------------
# query — file
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_file(connector):
    respx.get(f"{_BASE}/sites/s1/drives/d1/root:/notes.md:/content").mock(
        return_value=httpx.Response(200, text="hello world"),
    )
    result = await connector.query(
        ConnectorQuery(
            resource="file",
            filters={"site_id": "s1", "drive_id": "d1", "path": "notes.md"},
        ),
    )
    assert len(result.records) == 1
    assert result.records[0]["content"] == "hello world"


async def test_query_file_missing_filters(connector):
    with pytest.raises(ValueError, match="'site_id', 'drive_id', and 'path' filters"):
        await connector.query(
            ConnectorQuery(resource="file", filters={"site_id": "s1", "drive_id": "d1"}),
        )


# ---------------------------------------------------------------------------
# query — unsupported resource
# ---------------------------------------------------------------------------


async def test_query_unsupported_resource(connector):
    with pytest.raises(ValueError, match="Unsupported SharePoint resource"):
        await connector.query(ConnectorQuery(resource="invalid"))


# ---------------------------------------------------------------------------
# write — list_item
# ---------------------------------------------------------------------------


@respx.mock
async def test_write_list_item(connector):
    created = {"id": 99, "fields": {"Title": "Task 1"}}
    respx.post(f"{_BASE}/sites/s1/lists/l1/items").mock(return_value=httpx.Response(200, json=created))
    result = await connector.write(
        ConnectorPayload(
            resource="list_item",
            data={"site_id": "s1", "list_id": "l1", "fields": {"Title": "Task 1"}},
        ),
    )
    assert result["id"] == 99


async def test_write_list_item_missing_fields(connector):
    with pytest.raises(ValueError, match="'site_id', 'list_id', and 'fields' in data"):
        await connector.write(
            ConnectorPayload(resource="list_item", data={"site_id": "s1", "list_id": "l1"}),
        )


# ---------------------------------------------------------------------------
# write — file
# ---------------------------------------------------------------------------


@respx.mock
async def test_write_file(connector):
    created = {"name": "notes.md", "webUrl": "https://graph.microsoft.com/v1.0/notes.md"}
    respx.put(f"{_BASE}/sites/s1/drives/d1/root:/notes.md:/content").mock(
        return_value=httpx.Response(200, json=created),
    )
    result = await connector.write(
        ConnectorPayload(
            resource="file",
            data={"site_id": "s1", "drive_id": "d1", "path": "notes.md", "content": "hi"},
        ),
    )
    assert result["name"] == "notes.md"


async def test_write_file_missing_fields(connector):
    with pytest.raises(ValueError, match="'site_id', 'drive_id', 'path', and 'content' in data"):
        await connector.write(
            ConnectorPayload(resource="file", data={"site_id": "s1", "drive_id": "d1", "path": "x"}),
        )


# ---------------------------------------------------------------------------
# write — unsupported resource
# ---------------------------------------------------------------------------


async def test_write_unsupported_resource(connector):
    with pytest.raises(ValueError, match="Unsupported SharePoint write resource"):
        await connector.write(ConnectorPayload(resource="invalid", data={}))


# ---------------------------------------------------------------------------
# HTTP error propagation
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_http_error(connector):
    respx.get(f"{_BASE}/sites").mock(return_value=httpx.Response(500, text="Internal Error"))
    with pytest.raises(httpx.HTTPStatusError):
        await connector.query(ConnectorQuery(resource="sites"))
