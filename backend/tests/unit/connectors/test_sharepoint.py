"""Unit tests for SharePointConnector — HTTP responses are mocked via respx."""

import httpx
import pytest
import respx

from modulo.connectors.base import ConnectorPayload, ConnectorQuery, ConnectorType
from modulo.connectors.sharepoint import SharePointConnector

TOKEN = "test_sharepoint_token"
_API = "https://graph.microsoft.com/v1.0"


@pytest.fixture
def connector():
    return SharePointConnector(token=TOKEN)


def test_connector_type(connector):
    assert connector.connector_type == ConnectorType.SHAREPOINT


@respx.mock
async def test_health_check_ok(connector):
    respx.get(f"{_API}/sites/root").mock(return_value=httpx.Response(200, json={"displayName": "Contoso Portal"}))
    result = await connector.health_check()
    assert result.ok is True
    assert result.detail == "Contoso Portal"


@respx.mock
async def test_health_check_fail(connector):
    respx.get(f"{_API}/sites/root").mock(return_value=httpx.Response(401, text="Unauthorized"))
    result = await connector.health_check()
    assert result.ok is False
    assert "401" in result.detail


@respx.mock
async def test_health_check_corrupt_body_no_crash(connector):
    """A 200 with a non-dict body (list, string, ...) must not crash
    health_check — it reports ok with an empty detail instead of raising
    AttributeError on ``site_info.get(...)``."""
    respx.get(f"{_API}/sites/root").mock(return_value=httpx.Response(200, json=["not-a-site"]))
    result = await connector.health_check()
    assert result.ok is True
    assert not result.detail


@respx.mock
async def test_query_sites(connector):
    sites = {
        "value": [
            {"id": "site1", "displayName": "Site A"},
            {"id": "site2", "displayName": "Site B"},
        ]
    }
    respx.get(f"{_API}/sites").mock(return_value=httpx.Response(200, json=sites))
    result = await connector.query(ConnectorQuery(resource="sites"))
    assert len(result.records) == 2
    assert result.records[0]["displayName"] == "Site A"


@respx.mock
async def test_query_sites_with_search(connector):
    sites = {
        "value": [
            {"id": "site1", "displayName": "Contoso"},
        ]
    }
    respx.get(
        f"{_API}/sites",
        params={"search": "Contoso", "$top": 100},
    ).mock(return_value=httpx.Response(200, json=sites))
    result = await connector.query(ConnectorQuery(resource="sites", filters={"search": "Contoso"}))
    assert len(result.records) == 1
    assert result.records[0]["displayName"] == "Contoso"


@respx.mock
async def test_query_sites_with_cursor(connector):
    respx.get(
        f"{_API}/sites",
        params={"$skiptoken": "token123", "$top": 5},
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "value": [{"id": "site3", "displayName": "Site C"}],
                "@odata.nextLink": "https://graph.microsoft.com/v1.0/sites?$skiptoken=nexttoken&$top=5",
            },
        )
    )
    result = await connector.query(ConnectorQuery(resource="sites", cursor="token123", limit=5))
    assert len(result.records) == 1
    assert result.next_cursor == "nexttoken"
    assert result.total == 1


@respx.mock
async def test_query_sites_corrupt_body_no_crash(connector):
    """A corrupt/hostile response returning a non-dict body must not crash the
    list endpoints — it falls back to an empty page with no cursor."""
    respx.get(f"{_API}/sites").mock(return_value=httpx.Response(200, json=["garbage"]))
    result = await connector.query(ConnectorQuery(resource="sites"))
    assert not result.records
    assert result.next_cursor is None
    assert result.total == 0


@respx.mock
async def test_query_lists_corrupt_body_no_crash(connector):
    """A non-list ``value`` field must fall back to an empty page instead of
    raising TypeError on the bare slice chain."""
    respx.get(f"{_API}/sites/site1/lists").mock(return_value=httpx.Response(200, json={"value": "not-a-list"}))
    result = await connector.query(ConnectorQuery(resource="lists", filters={"site_id": "site1"}))
    assert not result.records
    assert result.next_cursor is None
    assert result.total == 0


@respx.mock
async def test_query_sites_non_string_next_link(connector):
    """A corrupt response placing a non-string in ``@odata.nextLink`` must not
    crash urlparse or leak a non-string cursor into the next request."""
    respx.get(f"{_API}/sites").mock(
        return_value=httpx.Response(200, json={"value": [{"id": "site1"}], "@odata.nextLink": 123}),
    )
    result = await connector.query(ConnectorQuery(resource="sites"))
    assert len(result.records) == 1
    assert result.next_cursor is None

    respx.get(f"{_API}/sites").mock(
        return_value=httpx.Response(200, json={"value": [{"id": "site1"}], "@odata.nextLink": {"$skiptoken": "x"}}),
    )
    result = await connector.query(ConnectorQuery(resource="sites"))
    assert len(result.records) == 1
    assert result.next_cursor is None


@respx.mock
async def test_query_drive_empty_skiptoken_no_cursor(connector):
    """A nextLink whose ``$skiptoken`` is empty must emit no cursor — an empty
    string is not a meaningful pagination cursor."""
    respx.get(f"{_API}/sites/site1/drives/drive1/root/children").mock(
        return_value=httpx.Response(
            200,
            json={
                "value": [{"id": "file1", "name": "document.docx"}],
                "@odata.nextLink": f"{_API}/sites/site1/drives/drive1/root/children?$skiptoken=&$top=5",
            },
        )
    )
    result = await connector.query(
        ConnectorQuery(
            resource="drive",
            filters={"site_id": "site1", "drive_id": "drive1", "path": "/"},
            limit=5,
        )
    )
    assert len(result.records) == 1
    assert result.next_cursor is None


@respx.mock
async def test_query_lists(connector):
    lists = {
        "value": [
            {"id": "list1", "displayName": "Documents"},
            {"id": "list2", "displayName": "Issues"},
        ]
    }
    respx.get(f"{_API}/sites/site1/lists").mock(return_value=httpx.Response(200, json=lists))
    result = await connector.query(ConnectorQuery(resource="lists", filters={"site_id": "site1"}))
    assert len(result.records) == 2
    assert result.records[0]["displayName"] == "Documents"


@respx.mock
async def test_query_lists_missing_site_id(connector):
    with pytest.raises(ValueError, match="requires 'site_id' filter"):
        await connector.query(ConnectorQuery(resource="lists", filters={}))


@respx.mock
async def test_query_list_items(connector):
    items = {
        "value": [
            {"id": "item1", "fields": {"Title": "Task 1"}},
            {"id": "item2", "fields": {"Title": "Task 2"}},
        ]
    }
    respx.get(f"{_API}/sites/site1/lists/list1/items").mock(return_value=httpx.Response(200, json=items))
    result = await connector.query(
        ConnectorQuery(
            resource="list_items",
            filters={"site_id": "site1", "list_id": "list1"},
        )
    )
    assert len(result.records) == 2
    assert result.records[0]["fields"]["Title"] == "Task 1"


@respx.mock
async def test_query_list_items_missing_filters(connector):
    with pytest.raises(ValueError, match="requires 'site_id' and 'list_id' filters"):
        await connector.query(ConnectorQuery(resource="list_items", filters={"site_id": "site1"}))


@respx.mock
async def test_query_drive_root(connector):
    children = {
        "value": [
            {"id": "file1", "name": "document.docx"},
            {"id": "file2", "name": "folder1"},
        ]
    }
    respx.get(f"{_API}/sites/site1/drives/drive1/root/children").mock(return_value=httpx.Response(200, json=children))
    result = await connector.query(
        ConnectorQuery(
            resource="drive",
            filters={"site_id": "site1", "drive_id": "drive1", "path": "/"},
        )
    )
    assert len(result.records) == 2
    assert result.records[0]["name"] == "document.docx"


@respx.mock
async def test_query_drive_subpath(connector):
    children = {
        "value": [
            {"id": "file3", "name": "subfile.txt"},
        ]
    }
    respx.get(f"{_API}/sites/site1/drives/drive1/root:/subfolder:/children").mock(
        return_value=httpx.Response(200, json=children)
    )
    result = await connector.query(
        ConnectorQuery(
            resource="drive",
            filters={
                "site_id": "site1",
                "drive_id": "drive1",
                "path": "/subfolder",
            },
        )
    )
    assert len(result.records) == 1
    assert result.records[0]["name"] == "subfile.txt"


@respx.mock
async def test_query_drive_missing_filters(connector):
    with pytest.raises(ValueError, match="requires 'site_id' and 'drive_id' filters"):
        await connector.query(ConnectorQuery(resource="drive", filters={}))


@respx.mock
async def test_query_file(connector):
    content = "Hello, SharePoint!"
    respx.get(f"{_API}/sites/site1/drives/drive1/root:/path/to/file.txt:/content").mock(
        return_value=httpx.Response(200, text=content)
    )
    result = await connector.query(
        ConnectorQuery(
            resource="file",
            filters={
                "site_id": "site1",
                "drive_id": "drive1",
                "path": "/path/to/file.txt",
            },
        )
    )
    assert len(result.records) == 1
    assert result.records[0]["content"] == "Hello, SharePoint!"
    assert result.records[0]["path"] == "/path/to/file.txt"


@respx.mock
async def test_query_file_missing_filters(connector):
    with pytest.raises(
        ValueError,
        match="requires 'site_id', 'drive_id', and 'path' filters",
    ):
        await connector.query(ConnectorQuery(resource="file", filters={"site_id": "site1"}))


async def test_query_unsupported_resource(connector):
    with pytest.raises(ValueError, match="Unsupported SharePoint resource"):
        await connector.query(ConnectorQuery(resource="unknown"))


@respx.mock
async def test_write_list_item(connector):
    created = {"id": "new-item-1", "fields": {"Title": "New Task"}}
    respx.post(f"{_API}/sites/site1/lists/list1/items").mock(return_value=httpx.Response(201, json=created))
    result = await connector.write(
        ConnectorPayload(
            resource="list_item",
            data={
                "site_id": "site1",
                "list_id": "list1",
                "fields": {"Title": "New Task"},
            },
        )
    )
    assert result["id"] == "new-item-1"
    assert result["fields"]["Title"] == "New Task"


@respx.mock
async def test_write_list_item_missing_data(connector):
    with pytest.raises(
        ValueError,
        match="requires 'site_id', 'list_id', and 'fields'",
    ):
        await connector.write(ConnectorPayload(resource="list_item", data={"site_id": "site1"}))


@respx.mock
async def test_write_file(connector):
    uploaded = {"id": "file-123", "name": "uploaded.txt", "size": 42}
    respx.put(f"{_API}/sites/site1/drives/drive1/root:/uploaded.txt:/content").mock(
        return_value=httpx.Response(201, json=uploaded)
    )
    result = await connector.write(
        ConnectorPayload(
            resource="file",
            data={
                "site_id": "site1",
                "drive_id": "drive1",
                "path": "/uploaded.txt",
                "content": "file content here",
            },
        )
    )
    assert result["id"] == "file-123"
    assert result["name"] == "uploaded.txt"


@respx.mock
async def test_write_file_missing_data(connector):
    with pytest.raises(
        ValueError,
        match="requires 'site_id', 'drive_id', 'path', and 'content'",
    ):
        await connector.write(ConnectorPayload(resource="file", data={"site_id": "site1"}))


async def test_write_unsupported_resource(connector):
    with pytest.raises(ValueError, match="Unsupported SharePoint write resource"):
        await connector.write(ConnectorPayload(resource="site", data={}))
