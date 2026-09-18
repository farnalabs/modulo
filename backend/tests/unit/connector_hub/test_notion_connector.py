"""Unit tests for NotionConnector — HTTP responses are mocked via httpx + respx."""

import httpx
import pytest
import respx

from modulo.connectors.base import ConnectorPayload, ConnectorQuery, ConnectorType
from modulo.connectors.notion import NotionConnector

TOKEN = "ntn_test_token"
_BASE = "https://api.notion.com/v1"


@pytest.fixture
def connector():
    return NotionConnector(token=TOKEN)


# ---------------------------------------------------------------------------
# health_check
# ---------------------------------------------------------------------------


@respx.mock
async def test_health_check_ok(connector):
    respx.get(f"{_BASE}/users").mock(
        return_value=httpx.Response(200, json={"results": [{"id": "u1"}, {"id": "u2"}]}),
    )
    result = await connector.health_check()
    assert result.ok is True
    assert "2 users" in result.detail


@respx.mock
async def test_health_check_fail(connector):
    respx.get(f"{_BASE}/users").mock(return_value=httpx.Response(401, text="Unauthorized"))
    result = await connector.health_check()
    assert result.ok is False
    assert "401" in result.detail


# ---------------------------------------------------------------------------
# connector_type
# ---------------------------------------------------------------------------


def test_connector_type(connector):
    assert connector.connector_type == ConnectorType.NOTION


# ---------------------------------------------------------------------------
# query — databases (search)
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_databases(connector):
    results = [
        {"id": "db1", "title": [{"plain_text": "Project Tracker"}], "object": "database"},
        {"id": "db2", "title": [{"plain_text": "Bug Tracker"}], "object": "database"},
    ]
    respx.post(f"{_BASE}/search").mock(
        return_value=httpx.Response(200, json={"results": results, "next_cursor": None}),
    )
    result = await connector.query(ConnectorQuery(resource="databases"))
    assert result.total == 2
    assert result.records[0]["id"] == "db1"


@respx.mock
async def test_query_databases_with_query(connector):
    results = [{"id": "db1", "title": [{"plain_text": "Project Tracker"}], "object": "database"}]
    respx.post(f"{_BASE}/search").mock(
        return_value=httpx.Response(200, json={"results": results, "next_cursor": None}),
    )
    result = await connector.query(ConnectorQuery(resource="databases", filters={"query": "Project"}))
    assert result.total == 1


@respx.mock
async def test_query_databases_with_cursor(connector):
    results = [{"id": "db2", "object": "database"}]
    respx.post(f"{_BASE}/search").mock(
        return_value=httpx.Response(200, json={"results": results, "next_cursor": "cursor_abc"}),
    )
    result = await connector.query(ConnectorQuery(resource="databases", cursor="cursor_prev"))
    assert result.total == 1
    assert result.next_cursor == "cursor_abc"


@respx.mock
async def test_query_databases_non_string_cursor_not_emitted(connector):
    """A corrupt non-string next_cursor must not be emitted as a pagination cursor."""
    results = [{"id": "db1", "object": "database"}]
    respx.post(f"{_BASE}/search").mock(
        return_value=httpx.Response(200, json={"results": results, "next_cursor": {"page": 2}}),
    )
    result = await connector.query(ConnectorQuery(resource="databases"))
    assert result.total == 1
    assert result.next_cursor is None


# ---------------------------------------------------------------------------
# query — single database
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_database(connector):
    db = {"id": "db1", "title": [{"plain_text": "Project Tracker"}], "object": "database"}
    respx.get(f"{_BASE}/databases/db1").mock(
        return_value=httpx.Response(200, json=db),
    )
    result = await connector.query(ConnectorQuery(resource="database", filters={"database_id": "db1"}))
    assert len(result.records) == 1
    assert result.records[0]["id"] == "db1"


async def test_query_database_missing_id(connector):
    query = ConnectorQuery(resource="database")
    with pytest.raises(ValueError, match="'database_id' filter"):
        await connector.query(query)


# ---------------------------------------------------------------------------
# query — pages (database query)
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_pages(connector):
    pages = [{"id": "p1", "object": "page"}, {"id": "p2", "object": "page"}]
    respx.post(f"{_BASE}/databases/db1/query").mock(
        return_value=httpx.Response(200, json={"results": pages, "next_cursor": None}),
    )
    result = await connector.query(ConnectorQuery(resource="pages", filters={"database_id": "db1"}))
    assert result.total == 2
    assert result.records[0]["id"] == "p1"


@respx.mock
async def test_query_pages_with_filter_and_sorts(connector):
    pages = [{"id": "p1", "object": "page"}]
    respx.post(f"{_BASE}/databases/db1/query").mock(
        return_value=httpx.Response(200, json={"results": pages, "next_cursor": None}),
    )
    result = await connector.query(
        ConnectorQuery(
            resource="pages",
            filters={
                "database_id": "db1",
                "filter": {"property": "Status", "status": {"equals": "Done"}},
                "sorts": [{"property": "Created", "direction": "descending"}],
            },
        )
    )
    assert result.total == 1


async def test_query_pages_missing_database_id(connector):
    query = ConnectorQuery(resource="pages")
    with pytest.raises(ValueError, match="'database_id' filter"):
        await connector.query(query)


# ---------------------------------------------------------------------------
# query — single page
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_page(connector):
    page = {"id": "p1", "object": "page", "properties": {"title": {"title": [{"plain_text": "Hello"}]}}}
    respx.get(f"{_BASE}/pages/p1").mock(
        return_value=httpx.Response(200, json=page),
    )
    result = await connector.query(ConnectorQuery(resource="page", filters={"page_id": "p1"}))
    assert len(result.records) == 1
    assert result.records[0]["id"] == "p1"


async def test_query_page_missing_id(connector):
    query = ConnectorQuery(resource="page")
    with pytest.raises(ValueError, match="'page_id' filter"):
        await connector.query(query)


# ---------------------------------------------------------------------------
# query — blocks
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_blocks(connector):
    blocks = [{"id": "b1", "type": "paragraph"}, {"id": "b2", "type": "heading_1"}]
    respx.get(f"{_BASE}/blocks/block_id_1/children").mock(
        return_value=httpx.Response(200, json={"results": blocks, "next_cursor": None}),
    )
    result = await connector.query(ConnectorQuery(resource="blocks", filters={"block_id": "block_id_1"}))
    assert result.total == 2
    assert result.records[0]["type"] == "paragraph"


async def test_query_blocks_missing_block_id(connector):
    query = ConnectorQuery(resource="blocks")
    with pytest.raises(ValueError, match="'block_id' filter"):
        await connector.query(query)


# ---------------------------------------------------------------------------
# query — users
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_users(connector):
    users = [{"id": "u1", "name": "Alice"}, {"id": "u2", "name": "Bob"}]
    respx.get(f"{_BASE}/users").mock(
        return_value=httpx.Response(200, json={"results": users, "next_cursor": None}),
    )
    result = await connector.query(ConnectorQuery(resource="users"))
    assert result.total == 2


@respx.mock
async def test_query_users_with_cursor(connector):
    users = [{"id": "u3", "name": "Charlie"}]
    respx.get(f"{_BASE}/users").mock(
        return_value=httpx.Response(200, json={"results": users, "next_cursor": "next_page"}),
    )
    result = await connector.query(ConnectorQuery(resource="users", cursor="prev_page"))
    assert result.total == 1
    assert result.next_cursor == "next_page"


# ---------------------------------------------------------------------------
# query — unknown resource
# ---------------------------------------------------------------------------


async def test_query_unsupported_resource(connector):
    query = ConnectorQuery(resource="unknown")
    with pytest.raises(ValueError, match="Unsupported Notion resource"):
        await connector.query(query)


# ---------------------------------------------------------------------------
# query — corrupt payload resilience
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_databases_non_list_results_no_crash(connector):
    """A corrupt body placing a non-list in ``results`` must fall back to an empty page."""
    respx.post(f"{_BASE}/search").mock(return_value=httpx.Response(200, json={"results": "corrupt"}))
    result = await connector.query(ConnectorQuery(resource="databases"))
    assert not result.records
    assert result.total == 0


@respx.mock
async def test_query_databases_non_dict_body_no_crash(connector):
    """A corrupt/hostile non-dict body must degrade to an empty page."""
    respx.post(f"{_BASE}/search").mock(return_value=httpx.Response(200, json=["not-a-dict"]))
    result = await connector.query(ConnectorQuery(resource="databases"))
    assert not result.records
    assert result.total == 0


@respx.mock
async def test_query_pages_non_list_results_no_crash(connector):
    """A corrupt ``results`` field on a database query must degrade gracefully."""
    respx.post(f"{_BASE}/databases/db1/query").mock(return_value=httpx.Response(200, json={"results": "corrupt"}))
    result = await connector.query(ConnectorQuery(resource="pages", filters={"database_id": "db1"}))
    assert not result.records
    assert result.total == 0


@respx.mock
async def test_health_check_non_dict_body_no_crash(connector):
    """A corrupt non-dict /users body must not crash the health check."""
    respx.get(f"{_BASE}/users").mock(return_value=httpx.Response(200, json=["not-a-dict"]))
    result = await connector.health_check()
    assert result.ok is True
    assert "0 users" in result.detail


# ---------------------------------------------------------------------------
# write — create page
# ---------------------------------------------------------------------------


@respx.mock
async def test_write_page(connector):
    created = {"id": "p_new", "object": "page", "url": "https://notion.so/p_new"}
    respx.post(f"{_BASE}/pages").mock(
        return_value=httpx.Response(200, json=created),
    )
    result = await connector.write(
        ConnectorPayload(
            resource="page",
            data={
                "parent": {"database_id": "db1"},
                "properties": {"Name": {"title": [{"text": {"content": "New Page"}}]}},
            },
        )
    )
    assert result["id"] == "p_new"


# ---------------------------------------------------------------------------
# write — create database
# ---------------------------------------------------------------------------


@respx.mock
async def test_write_database(connector):
    created = {"id": "db_new", "object": "database"}
    respx.post(f"{_BASE}/databases").mock(
        return_value=httpx.Response(200, json=created),
    )
    result = await connector.write(
        ConnectorPayload(
            resource="database",
            data={
                "parent": {"page_id": "parent_page"},
                "title": [{"type": "text", "text": {"content": "New DB"}}],
                "properties": {"Name": {"title": {}}},
            },
        )
    )
    assert result["id"] == "db_new"


# ---------------------------------------------------------------------------
# write — block_append
# ---------------------------------------------------------------------------


@respx.mock
async def test_write_block_append(connector):
    created = {"results": [{"id": "block_new", "type": "paragraph"}]}
    respx.patch(f"{_BASE}/blocks/block_id_1/children").mock(
        return_value=httpx.Response(200, json=created),
    )
    result = await connector.write(
        ConnectorPayload(
            resource="block_append",
            data={
                "block_id": "block_id_1",
                "children": [{"object": "block", "type": "paragraph", "paragraph": {"rich_text": []}}],
            },
        )
    )
    assert "results" in result


async def test_write_block_append_missing_block_id(connector):
    with pytest.raises(ValueError, match="'block_id' in data"):
        await connector.write(
            ConnectorPayload(
                resource="block_append",
                data={"children": []},
            )
        )


# ---------------------------------------------------------------------------
# write — page_update
# ---------------------------------------------------------------------------


@respx.mock
async def test_write_page_update(connector):
    updated = {"id": "p1", "object": "page", "properties": {}}
    respx.patch(f"{_BASE}/pages/p1").mock(
        return_value=httpx.Response(200, json=updated),
    )
    result = await connector.write(
        ConnectorPayload(
            resource="page_update",
            data={"id": "p1", "properties": {"Status": {"select": {"name": "Done"}}}},
        )
    )
    assert result["id"] == "p1"


async def test_write_page_update_missing_id(connector):
    with pytest.raises(ValueError, match="'id' in data"):
        await connector.write(
            ConnectorPayload(
                resource="page_update",
                data={"properties": {}},
            )
        )


# ---------------------------------------------------------------------------
# write — unknown resource
# ---------------------------------------------------------------------------


async def test_write_unsupported_resource(connector):
    with pytest.raises(ValueError, match="Unsupported Notion write resource"):
        await connector.write(ConnectorPayload(resource="delete", data={}))


# ---------------------------------------------------------------------------
# HTTP error propagation
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_http_error(connector):
    respx.post(f"{_BASE}/search").mock(return_value=httpx.Response(500, text="Internal Error"))
    with pytest.raises(httpx.HTTPStatusError):
        await connector.query(ConnectorQuery(resource="databases"))


@respx.mock
async def test_write_http_error(connector):
    respx.post(f"{_BASE}/pages").mock(return_value=httpx.Response(429, text="Rate Limited"))
    with pytest.raises(httpx.HTTPStatusError):
        await connector.write(ConnectorPayload(resource="page", data={}))
