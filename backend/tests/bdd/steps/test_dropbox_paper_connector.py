"""Step definitions for the Dropbox Paper connector BDD feature.

Wires ``features/connectors/dropbox_paper.feature`` into the executing suite (the
2026-09-16 product-map review pass) by driving the REAL
``DropboxPaperConnector`` against a respx-mocked Dropbox API v2 — mirroring
``backend/tests/unit/connectors/test_dropbox_paper.py`` so the executing BDD surface
locks the same contract the unit suite does: account validation via
``/users/get_current_account``, listing Paper docs / downloading a doc as markdown /
listing folders, creating a Paper doc (markdown import), and failing closed on
unsupported query/write resources.
"""

import asyncio
import contextlib

import httpx
import pytest
import respx
from pytest_bdd import given, parsers, scenarios, then, when

with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/connectors/dropbox_paper.feature")

_TOKEN = "sl.AAAABBBBCCCCDDDD"
_BASE = "https://api.dropboxapi.com/2"


@pytest.fixture
def ctx() -> dict:
    """Shared mutable context dict for the Dropbox Paper connector scenarios."""
    return {}


@given("a Dropbox Paper connector configured with valid token")
def dropbox_paper_connector_valid(ctx: dict) -> None:
    from modulo.connectors.dropbox_paper import DropboxPaperConnector

    ctx["connector"] = DropboxPaperConnector(token=_TOKEN)
    ctx["valid"] = True


@given("a Dropbox Paper connector configured with invalid token")
def dropbox_paper_connector_invalid(ctx: dict) -> None:
    from modulo.connectors.dropbox_paper import DropboxPaperConnector

    ctx["connector"] = DropboxPaperConnector(token=_TOKEN)
    ctx["valid"] = False


@when("the connector checks health")
def dropbox_paper_health_check(ctx: dict) -> None:
    connector = ctx["connector"]
    response = (
        httpx.Response(200, json={"email": "admin@example.com"})
        if ctx["valid"]
        else httpx.Response(401, text="Unauthorized")
    )
    with respx.mock:
        respx.post(f"{_BASE}/users/get_current_account").mock(return_value=response)
        ctx["health_result"] = asyncio.run(connector.health_check())


@when(parsers.parse('the connector lists Paper docs with filter "{filter_by}"'))
def dropbox_paper_query_docs(filter_by: str, ctx: dict) -> None:
    from modulo.connectors.base import ConnectorQuery

    json_body = {
        "doc_ids": ["doc1", "doc2"],
        "cursor": {"value": "next_cursor_val"},
    }
    with respx.mock:
        respx.post(f"{_BASE}/paper/docs/list").mock(return_value=httpx.Response(200, json=json_body))
        ctx["query_result"] = asyncio.run(
            ctx["connector"].query(ConnectorQuery(resource="docs", filters={"filter_by": filter_by}, limit=10))
        )


@when(parsers.parse('the connector downloads doc "{doc_id}"'))
def dropbox_paper_query_doc(doc_id: str, ctx: dict) -> None:
    from modulo.connectors.base import ConnectorQuery

    content = "# My Paper Doc\n\nHello world."
    with respx.mock:
        respx.post(f"{_BASE}/paper/docs/download").mock(return_value=httpx.Response(200, text=content))
        ctx["query_result"] = asyncio.run(
            ctx["connector"].query(ConnectorQuery(resource="doc", filters={"doc_id": doc_id}))
        )


@when(parsers.parse('the connector lists folders at path "{path}"'))
def dropbox_paper_query_folders(path: str, ctx: dict) -> None:
    from modulo.connectors.base import ConnectorQuery

    entries = [
        {".tag": "folder", "name": "My Folder", "id": "id:folder1"},
        {".tag": "file", "name": "notes.md", "id": "id:file1"},
    ]
    with respx.mock:
        respx.post(f"{_BASE}/files/list_folder").mock(
            return_value=httpx.Response(200, json={"entries": entries, "cursor": "cursor_val"})
        )
        ctx["query_result"] = asyncio.run(
            ctx["connector"].query(ConnectorQuery(resource="folders", filters={"path": path, "recursive": True}))
        )


@when(parsers.parse('the connector creates a Paper doc titled "{title}" with markdown content'))
def dropbox_paper_write_doc(title: str, ctx: dict) -> None:
    from modulo.connectors.base import ConnectorPayload

    content = "# Meeting Notes\n\nAction items for the sprint review."
    result_headers = {
        "Dropbox-API-Result": (
            '{"doc_id": "new_doc_123", "title": "Meeting Notes", "url": '
            '"https://paper.dropbox.com/doc/Meeting-Notes-abc123"}'
        )
    }
    with respx.mock:
        respx.post(f"{_BASE}/paper/docs/create").mock(return_value=httpx.Response(200, json={}, headers=result_headers))
        ctx["write_result"] = asyncio.run(
            ctx["connector"].write(ConnectorPayload(resource="doc", data={"title": title, "content": content}))
        )


@when(parsers.parse('the connector queries unsupported resource "{resource}"'))
def dropbox_paper_query_unsupported(resource: str, ctx: dict) -> None:
    from modulo.connectors.base import ConnectorQuery

    with pytest.raises(ValueError, match="Unsupported Dropbox Paper resource"):
        asyncio.run(ctx["connector"].query(ConnectorQuery(resource=resource)))
    ctx["raised_error"] = True


@when(parsers.parse('the connector writes to unsupported resource "{resource}"'))
def dropbox_paper_write_unsupported(resource: str, ctx: dict) -> None:
    from modulo.connectors.base import ConnectorPayload

    with pytest.raises(ValueError, match="Unsupported Dropbox Paper write resource"):
        asyncio.run(ctx["connector"].write(ConnectorPayload(resource=resource, data={})))
    ctx["raised_error"] = True


@then("the health check reports the authenticated email")
def dropbox_paper_health_reports_email(ctx: dict) -> None:
    result = ctx["health_result"]
    assert result is not None, "No health check result"
    assert result.ok is True, f"Expected healthy, got: {result.detail}"
    assert "admin@example.com" in result.detail, f"Expected email in detail, got: {result.detail}"


@then(parsers.parse('the health check returns "{status}"'))
def dropbox_paper_health_result(status: str, ctx: dict) -> None:
    result = ctx["health_result"]
    assert result is not None, "No health check result"
    if status == "healthy":
        assert result.ok is True, f"Expected healthy, got: {result.detail}"
    else:
        assert result.ok is False, f"Expected unhealthy, got: {result.detail}"


@then("the result contains doc IDs")
def dropbox_paper_result_contains_docs(ctx: dict) -> None:
    result = ctx["query_result"]
    assert result is not None, "No query result"
    assert result.records, "Expected doc records"
    assert result.records[0]["doc_id"] == "doc1", result.records
    assert result.records[1]["doc_id"] == "doc2", result.records


@then("the result contains the doc content as markdown")
def dropbox_paper_result_contains_doc_content(ctx: dict) -> None:
    result = ctx["query_result"]
    assert result is not None, "No query result"
    assert result.records, "Expected doc record"
    assert result.records[0]["doc_id"] == "abc123", result.records
    assert result.records[0]["content"].startswith("# My Paper Doc"), result.records


@then("the result contains folder entries")
def dropbox_paper_result_contains_folders(ctx: dict) -> None:
    result = ctx["query_result"]
    assert result is not None, "No query result"
    assert result.records, "Expected folder entries"
    assert result.records[0]["name"] == "My Folder", result.records
    assert result.records[1]["name"] == "notes.md", result.records


@then("the doc is created successfully and returns metadata")
def dropbox_paper_doc_created(ctx: dict) -> None:
    result = ctx["write_result"]
    assert result is not None, "No write result"
    assert result["doc_id"] == "new_doc_123", result
    assert result["title"] == "Meeting Notes", result


@then("the connector raises an error")
def dropbox_paper_raised_error(ctx: dict) -> None:
    assert ctx.get("raised_error") is True, "Expected the connector to raise an error"
