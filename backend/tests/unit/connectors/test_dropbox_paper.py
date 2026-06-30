"""Unit tests for DropboxPaperConnector — HTTP responses are mocked via respx."""

import json

import httpx
import pytest
import respx

from modulo.connectors.base import ConnectorPayload, ConnectorQuery, ConnectorType
from modulo.connectors.dropbox_paper import DropboxPaperConnector

TOKEN = "sl.AAAABBBBCCCCDDDD"
_BASE = "https://api.dropboxapi.com/2"


@pytest.fixture()
def connector():
    return DropboxPaperConnector(token=TOKEN)


def _mock_response(
    status: int = 200, json: dict | None = None, text: str = "", headers: dict | None = None
) -> httpx.Response:
    if json is not None:
        return httpx.Response(status, json=json, headers=headers or {})
    return httpx.Response(status, text=text, headers=headers or {})


def test_connector_type(connector):
    assert connector.connector_type == ConnectorType.DROPBOX_PAPER


@respx.mock
async def test_health_check_ok(connector):
    route = respx.post(f"{_BASE}/users/get_current_account").mock(
        return_value=_mock_response(json={"email": "admin@example.com"})
    )
    result = await connector.health_check()
    assert result.ok is True
    assert result.detail == "Authenticated as admin@example.com"
    assert route.called
    auth = route.calls[0].request.headers.get("Authorization")
    assert auth == f"Bearer {TOKEN}"


@respx.mock
async def test_health_check_fail(connector):
    respx.post(f"{_BASE}/users/get_current_account").mock(return_value=_mock_response(status=401, text="Unauthorized"))
    result = await connector.health_check()
    assert result.ok is False
    assert "401" in result.detail


@respx.mock
async def test_query_docs(connector):
    json_body = {
        "doc_ids": ["doc1", "doc2"],
        "cursor": {"value": "next_cursor_val"},
    }
    route = respx.post(f"{_BASE}/paper/docs/list").mock(return_value=_mock_response(json=json_body))
    result = await connector.query(ConnectorQuery(resource="docs", filters={"filter_by": "docs_created"}, limit=10))
    assert len(result.records) == 2
    assert result.records[0]["doc_id"] == "doc1"
    assert result.records[1]["doc_id"] == "doc2"
    assert result.next_cursor == "next_cursor_val"
    assert route.called
    sent = route.calls[0].request.content
    payload = json.loads(sent)
    assert payload["limit"] == 10
    assert payload["filter_by"] == "docs_created"


@respx.mock
async def test_query_docs_with_sort(connector):
    route = respx.post(f"{_BASE}/paper/docs/list").mock(return_value=_mock_response(json={"doc_ids": []}))
    await connector.query(
        ConnectorQuery(
            resource="docs",
            filters={"filter_by": "docs_created", "sort_by": "modified", "sort_order": "ascending"},
        )
    )
    sent = json.loads(route.calls[0].request.content)
    assert sent["sort_by"] == "modified"
    assert sent["sort_order"] == "ascending"


@respx.mock
async def test_query_doc(connector):
    content = "# My Paper Doc\n\nHello world."
    doc_id = "abc123"
    route = respx.post(f"{_BASE}/paper/docs/download").mock(return_value=_mock_response(text=content))
    result = await connector.query(ConnectorQuery(resource="doc", filters={"doc_id": doc_id}))
    assert len(result.records) == 1
    assert result.records[0]["doc_id"] == doc_id
    assert result.records[0]["content"] == content
    assert route.called
    arg_header = route.calls[0].request.headers.get("Dropbox-API-Arg")
    assert arg_header == f'{{"doc_id": "{doc_id}"}}'


@respx.mock
async def test_query_doc_missing_doc_id(connector):
    with pytest.raises(ValueError, match="requires 'doc_id' filter"):
        await connector.query(ConnectorQuery(resource="doc", filters={}))


@respx.mock
async def test_query_folders(connector):
    entries = [
        {".tag": "folder", "name": "My Folder", "id": "id:folder1"},
        {".tag": "file", "name": "notes.md", "id": "id:file1"},
    ]
    route = respx.post(f"{_BASE}/files/list_folder").mock(
        return_value=_mock_response(json={"entries": entries, "cursor": "cursor_val"})
    )
    result = await connector.query(ConnectorQuery(resource="folders", filters={"path": "/Paper", "recursive": True}))
    assert len(result.records) == 2
    assert result.records[0]["name"] == "My Folder"
    assert result.next_cursor == "cursor_val"
    sent = json.loads(route.calls[0].request.content)
    assert sent["path"] == "/Paper"
    assert sent["recursive"] is True


@respx.mock
async def test_query_folders_with_cursor(connector):
    route = respx.post(f"{_BASE}/files/list_folder").mock(
        return_value=_mock_response(json={"entries": [], "cursor": None})
    )
    await connector.query(ConnectorQuery(resource="folders", cursor="prev_cursor"))
    sent = json.loads(route.calls[0].request.content)
    assert sent["cursor"] == "prev_cursor"


@respx.mock
async def test_query_docs_with_cursor(connector):
    route = respx.post(f"{_BASE}/paper/docs/list").mock(
        return_value=_mock_response(json={"doc_ids": [], "cursor": None})
    )
    await connector.query(ConnectorQuery(resource="docs", cursor="page_token"))
    sent = json.loads(route.calls[0].request.content)
    assert sent["cursor"] == "page_token"


async def test_query_unsupported_resource(connector):
    with pytest.raises(ValueError, match="Unsupported Dropbox Paper resource"):
        await connector.query(ConnectorQuery(resource="users"))


@respx.mock
async def test_write_doc(connector):
    title = "New Paper Doc"
    content = "# Hello\n\nThis is a test."
    result_headers = {
        "Dropbox-API-Result": '{"doc_id": "new_doc_123", "title": "New Paper Doc", "url": "https://paper.dropbox.com/doc/New-Paper-Doc-abc123"}'
    }
    route = respx.post(f"{_BASE}/paper/docs/create").mock(
        return_value=_mock_response(status=200, json={}, headers=result_headers)
    )
    result = await connector.write(ConnectorPayload(resource="doc", data={"title": title, "content": content}))
    assert result["doc_id"] == "new_doc_123"
    assert result["title"] == "New Paper Doc"
    assert route.called
    assert route.calls[0].request.url.params["import_format"] == "markdown"
    arg_header = route.calls[0].request.headers.get("Dropbox-API-Arg")
    assert arg_header == f'{{"path": "/{title}"}}'
    assert route.calls[0].request.content.decode("utf-8") == content


@respx.mock
async def test_write_doc_default_title(connector):
    route = respx.post(f"{_BASE}/paper/docs/create").mock(
        return_value=_mock_response(status=200, json={}, headers={"Dropbox-API-Result": '{"doc_id": "d1"}'})
    )
    result = await connector.write(ConnectorPayload(resource="doc", data={"content": "Some content"}))
    assert result["doc_id"] == "d1"
    arg_header = route.calls[0].request.headers.get("Dropbox-API-Arg")
    assert "Untitled" in arg_header


async def test_write_unsupported_resource(connector):
    with pytest.raises(ValueError, match="Unsupported Dropbox Paper write resource"):
        await connector.write(ConnectorPayload(resource="folder", data={}))


@respx.mock
async def test_auth_header_bearer_token(connector):
    route = respx.post(f"{_BASE}/users/get_current_account").mock(
        return_value=_mock_response(json={"email": "a@b.com"})
    )
    await connector.health_check()
    auth = route.calls[0].request.headers.get("Authorization")
    assert auth == f"Bearer {TOKEN}"
