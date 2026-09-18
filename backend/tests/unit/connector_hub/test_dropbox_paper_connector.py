"""Unit tests for DropboxPaperConnector — HTTP responses are mocked via httpx + respx."""

import json

import httpx
import pytest
import respx

from modulo.connectors.base import ConnectorPayload, ConnectorQuery, ConnectorType
from modulo.connectors.dropbox_paper import DropboxPaperConnector

TOKEN = "dp_test_token"
_BASE = "https://api.dropboxapi.com/2"


@pytest.fixture
def connector():
    return DropboxPaperConnector(token=TOKEN)


# ---------------------------------------------------------------------------
# connector_type
# ---------------------------------------------------------------------------


def test_connector_type(connector):
    assert connector.connector_type == ConnectorType.DROPBOX_PAPER


# ---------------------------------------------------------------------------
# health_check
# ---------------------------------------------------------------------------


@respx.mock
async def test_health_check_ok(connector):
    respx.post(f"{_BASE}/users/get_current_account").mock(
        return_value=httpx.Response(200, json={"email": "alice@example.com"}),
    )
    result = await connector.health_check()
    assert result.ok is True
    assert "alice@example.com" in result.detail


@respx.mock
async def test_health_check_invalid_token(connector):
    respx.post(f"{_BASE}/users/get_current_account").mock(return_value=httpx.Response(401, text="Unauthorized"))
    result = await connector.health_check()
    assert result.ok is False
    assert "401" in result.detail


@respx.mock
async def test_health_check_http_error(connector):
    respx.post(f"{_BASE}/users/get_current_account").mock(return_value=httpx.Response(500, text="Internal Error"))
    result = await connector.health_check()
    assert result.ok is False
    assert "500" in result.detail


@respx.mock
async def test_health_check_connect_error(connector):
    respx.post(f"{_BASE}/users/get_current_account").mock(side_effect=httpx.ConnectError("boom"))
    result = await connector.health_check()
    assert result.ok is False
    assert "connection error" in result.detail


# ---------------------------------------------------------------------------
# query — docs
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_docs(connector):
    body = {"doc_ids": ["d1", "d2"], "cursor": {"value": "abc"}}
    respx.post(f"{_BASE}/paper/docs/list").mock(return_value=httpx.Response(200, json=body))
    result = await connector.query(ConnectorQuery(resource="docs"))
    assert result.total == 2
    assert result.records[0] == {"doc_id": "d1"}
    assert result.next_cursor == "abc"


@respx.mock
async def test_query_docs_no_cursor(connector):
    respx.post(f"{_BASE}/paper/docs/list").mock(return_value=httpx.Response(200, json={"doc_ids": ["d1"]}))
    result = await connector.query(ConnectorQuery(resource="docs"))
    assert result.next_cursor is None


@respx.mock
async def test_query_docs_with_cursor(connector):
    body = {"doc_ids": ["d1"], "cursor": {"value": "next"}}
    respx.post(f"{_BASE}/paper/docs/list").mock(return_value=httpx.Response(200, json=body))
    result = await connector.query(ConnectorQuery(resource="docs", cursor="prev"))
    assert result.next_cursor == "next"


# ---------------------------------------------------------------------------
# query — doc
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_doc(connector):
    respx.post(f"{_BASE}/paper/docs/download").mock(return_value=httpx.Response(200, text="# Title\ncontent"))
    result = await connector.query(ConnectorQuery(resource="doc", filters={"doc_id": "d1"}))
    assert result.total == 1
    assert result.records[0]["doc_id"] == "d1"
    assert "# Title" in result.records[0]["content"]


async def test_query_doc_missing_doc_id(connector):
    with pytest.raises(ValueError, match="'doc_id' filter"):
        await connector.query(ConnectorQuery(resource="doc"))


# ---------------------------------------------------------------------------
# query — folders
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_folders(connector):
    body = {"entries": [{"name": "docs", ".tag": "folder"}], "cursor": {"value": "cur1"}}
    respx.post(f"{_BASE}/files/list_folder").mock(return_value=httpx.Response(200, json=body))
    result = await connector.query(ConnectorQuery(resource="folders", filters={"path": "/"}))
    assert result.total == 1
    assert result.records[0]["name"] == "docs"
    assert result.next_cursor == "cur1"


# ---------------------------------------------------------------------------
# query — unsupported resource
# ---------------------------------------------------------------------------


async def test_query_unsupported_resource(connector):
    with pytest.raises(ValueError, match="Unsupported Dropbox Paper resource"):
        await connector.query(ConnectorQuery(resource="invalid"))


# ---------------------------------------------------------------------------
# write — doc
# ---------------------------------------------------------------------------


@respx.mock
async def test_write_doc(connector):
    result_headers = {"url": "https://paper.dropbox.com/doc/xyz", "title": "My Doc"}
    respx.post(f"{_BASE}/paper/docs/create").mock(
        return_value=httpx.Response(
            200,
            headers={"Dropbox-API-Result": json.dumps(result_headers)},
        ),
    )
    result = await connector.write(
        ConnectorPayload(resource="doc", data={"title": "My Doc", "content": "# Body"}),
    )
    assert result["title"] == "My Doc"


# ---------------------------------------------------------------------------
# write — unsupported resource
# ---------------------------------------------------------------------------


async def test_write_unsupported_resource(connector):
    with pytest.raises(ValueError, match="Unsupported Dropbox Paper write resource"):
        await connector.write(ConnectorPayload(resource="invalid", data={}))


# ---------------------------------------------------------------------------
# HTTP error propagation
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_http_error(connector):
    respx.post(f"{_BASE}/paper/docs/list").mock(return_value=httpx.Response(500, text="Internal Error"))
    with pytest.raises(httpx.HTTPStatusError):
        await connector.query(ConnectorQuery(resource="docs"))
