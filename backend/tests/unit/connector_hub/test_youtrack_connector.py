"""Unit tests for YouTrackConnector — HTTP responses are mocked via httpx + respx."""

import httpx
import pytest
import respx

from modulo.connectors.base import ConnectorPayload, ConnectorQuery, ConnectorType
from modulo.connectors.youtrack import YouTrackConnector

TOKEN = "yt_perm_token_123"
_BASE = "https://youtrack.mycompany.com/api"


@pytest.fixture()
def connector():
    return YouTrackConnector(token=TOKEN)


@pytest.fixture()
def connector_custom_url():
    return YouTrackConnector(token=TOKEN, base_url="https://youtrack.example.com/api")


# ---------------------------------------------------------------------------
# connector_type
# ---------------------------------------------------------------------------


def test_connector_type(connector):
    assert connector.connector_type == ConnectorType.YOUTRACK


# ---------------------------------------------------------------------------
# health_check
# ---------------------------------------------------------------------------


@respx.mock
async def test_health_check_ok(connector):
    respx.get(f"{_BASE}/users/me").mock(
        return_value=httpx.Response(200, json={"name": "Alice Smith", "login": "alice"}),
    )
    result = await connector.health_check()
    assert result.ok is True
    assert result.detail == "Alice Smith"


@respx.mock
async def test_health_check_ok_fallback_login(connector):
    respx.get(f"{_BASE}/users/me").mock(
        return_value=httpx.Response(200, json={"login": "bot-user"}),
    )
    result = await connector.health_check()
    assert result.ok is True
    assert result.detail == "bot-user"


@respx.mock
async def test_health_check_fail(connector):
    respx.get(f"{_BASE}/users/me").mock(return_value=httpx.Response(401, text="Unauthorized"))
    result = await connector.health_check()
    assert result.ok is False
    assert "401" in result.detail


@respx.mock
async def test_health_check_http_error(connector):
    respx.get(f"{_BASE}/users/me").mock(return_value=httpx.Response(500, text="Server Error"))
    result = await connector.health_check()
    assert result.ok is False
    assert "500" in result.detail


@respx.mock
async def test_health_check_network_error(connector):
    respx.get(f"{_BASE}/users/me").mock(side_effect=httpx.ConnectError("connection refused"))
    result = await connector.health_check()
    assert result.ok is False
    assert "connection refused" in result.detail


# ---------------------------------------------------------------------------
# query — issues (list)
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_issues(connector):
    issues = [
        {"id": "1-1", "idReadable": "PRJ-1", "summary": "First issue"},
        {"id": "1-2", "idReadable": "PRJ-2", "summary": "Second issue"},
    ]
    respx.get(f"{_BASE}/issues").mock(return_value=httpx.Response(200, json=issues))
    result = await connector.query(ConnectorQuery(resource="issues"))
    assert result.total == 2
    assert result.records[0]["idReadable"] == "PRJ-1"
    assert result.records[0]["summary"] == "First issue"


@respx.mock
async def test_query_issues_with_query_filter(connector):
    issues = [{"id": "1-1", "idReadable": "PRJ-1", "summary": "Bug found"}]
    respx.get(f"{_BASE}/issues").mock(return_value=httpx.Response(200, json=issues))
    result = await connector.query(ConnectorQuery(resource="issues", filters={"query": "project: PRJ-1"}))
    assert result.total == 1
    assert result.records[0]["summary"] == "Bug found"


@respx.mock
async def test_query_issues_with_pagination(connector):
    issues = [{"id": "1-1", "idReadable": "PRJ-1", "summary": "First"}]
    respx.get(f"{_BASE}/issues").mock(return_value=httpx.Response(200, json=issues))
    result = await connector.query(ConnectorQuery(resource="issues", filters={"skip": 10, "top": 5}, limit=5))
    assert result.total == 1


@respx.mock
async def test_query_issues_with_fields(connector):
    issues = [{"id": "1-1", "idReadable": "PRJ-1"}]
    respx.get(f"{_BASE}/issues").mock(return_value=httpx.Response(200, json=issues))
    result = await connector.query(ConnectorQuery(resource="issues", filters={"fields": "id,idReadable,summary"}))
    assert result.total == 1


# ---------------------------------------------------------------------------
# query — single issue
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_single_issue(connector):
    issue = {"id": "1-1", "idReadable": "PRJ-42", "summary": "Fix login bug"}
    respx.get(f"{_BASE}/issues/PRJ-42").mock(return_value=httpx.Response(200, json=issue))
    result = await connector.query(ConnectorQuery(resource="issue", filters={"issue_id": "PRJ-42"}))
    assert len(result.records) == 1
    assert result.records[0]["summary"] == "Fix login bug"


@respx.mock
async def test_query_single_issue_with_fields(connector):
    issue = {"id": "1-1", "idReadable": "PRJ-42"}
    respx.get(f"{_BASE}/issues/PRJ-42").mock(return_value=httpx.Response(200, json=issue))
    result = await connector.query(
        ConnectorQuery(resource="issue", filters={"issue_id": "PRJ-42", "fields": "id,idReadable"})
    )
    assert len(result.records) == 1


async def test_query_single_issue_missing_id(connector):
    with pytest.raises(ValueError, match="'issue_id' filter"):
        await connector.query(ConnectorQuery(resource="issue"))


# ---------------------------------------------------------------------------
# query — projects (list)
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_projects(connector):
    projects = [
        {"id": "p1", "name": "Project Alpha", "shortName": "PA"},
        {"id": "p2", "name": "Project Beta", "shortName": "PB"},
    ]
    respx.get(f"{_BASE}/admin/projects").mock(return_value=httpx.Response(200, json=projects))
    result = await connector.query(ConnectorQuery(resource="projects"))
    assert result.total == 2
    assert result.records[0]["name"] == "Project Alpha"


# ---------------------------------------------------------------------------
# query — single project
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_single_project(connector):
    project = {"id": "p1", "name": "Project Alpha", "shortName": "PA"}
    respx.get(f"{_BASE}/admin/projects/p1").mock(return_value=httpx.Response(200, json=project))
    result = await connector.query(ConnectorQuery(resource="project", filters={"project_id": "p1"}))
    assert len(result.records) == 1
    assert result.records[0]["name"] == "Project Alpha"


async def test_query_single_project_missing_id(connector):
    with pytest.raises(ValueError, match="'project_id' filter"):
        await connector.query(ConnectorQuery(resource="project"))


# ---------------------------------------------------------------------------
# query — users
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_users(connector):
    users = [
        {"id": "u1", "name": "Alice", "login": "alice"},
        {"id": "u2", "name": "Bob", "login": "bob"},
    ]
    respx.get(f"{_BASE}/users").mock(return_value=httpx.Response(200, json=users))
    result = await connector.query(ConnectorQuery(resource="users"))
    assert result.total == 2
    assert result.records[0]["name"] == "Alice"


@respx.mock
async def test_query_users_with_query(connector):
    users = [{"id": "u1", "name": "Alice", "login": "alice"}]
    respx.get(f"{_BASE}/users").mock(return_value=httpx.Response(200, json=users))
    result = await connector.query(ConnectorQuery(resource="users", filters={"query": "alice"}))
    assert result.total == 1


# ---------------------------------------------------------------------------
# query — unknown resource
# ---------------------------------------------------------------------------


async def test_query_unsupported_resource(connector):
    with pytest.raises(ValueError, match="Unsupported YouTrack query resource"):
        await connector.query(ConnectorQuery(resource="unknown"))


# ---------------------------------------------------------------------------
# write — create issue
# ---------------------------------------------------------------------------


@respx.mock
async def test_write_create_issue(connector):
    created = {"id": "1-10", "idReadable": "PRJ-50", "summary": "New bug"}
    respx.post(f"{_BASE}/issues").mock(return_value=httpx.Response(200, json=created))
    result = await connector.write(
        ConnectorPayload(
            resource="issue",
            data={"summary": "New bug", "project": {"id": "p1"}},
        )
    )
    assert result["id"] == "1-10"
    assert result["idReadable"] == "PRJ-50"


# ---------------------------------------------------------------------------
# write — update issue
# ---------------------------------------------------------------------------


@respx.mock
async def test_write_update_issue(connector):
    updated = {"id": "1-1", "idReadable": "PRJ-42", "summary": "Updated summary"}
    respx.post(f"{_BASE}/issues/1-1").mock(return_value=httpx.Response(200, json=updated))
    result = await connector.write(
        ConnectorPayload(
            resource="issue_update",
            data={"id": "1-1", "summary": "Updated summary"},
        )
    )
    assert result["summary"] == "Updated summary"


async def test_write_update_issue_missing_id(connector):
    with pytest.raises(ValueError, match="Missing 'id' in issue_update"):
        await connector.write(ConnectorPayload(resource="issue_update", data={"summary": "Orphan"}))


# ---------------------------------------------------------------------------
# write — add comment
# ---------------------------------------------------------------------------


@respx.mock
async def test_write_comment(connector):
    comment = {"id": "c1", "text": "Looking into it"}
    respx.post(f"{_BASE}/issues/PRJ-42/comments").mock(return_value=httpx.Response(200, json=comment))
    result = await connector.write(
        ConnectorPayload(
            resource="comment",
            data={"issue_id": "PRJ-42", "text": "Looking into it"},
        )
    )
    assert result["text"] == "Looking into it"


async def test_write_comment_missing_fields(connector):
    with pytest.raises(ValueError, match="comment requires 'issue_id' and 'text'"):
        await connector.write(ConnectorPayload(resource="comment", data={"issue_id": "PRJ-42"}))


# ---------------------------------------------------------------------------
# write — unknown resource
# ---------------------------------------------------------------------------


async def test_write_unsupported_resource(connector):
    with pytest.raises(ValueError, match="Unsupported YouTrack write resource"):
        await connector.write(ConnectorPayload(resource="delete", data={}))


# ---------------------------------------------------------------------------
# HTTP error propagation
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_http_error_propagation(connector):
    respx.get(f"{_BASE}/issues").mock(return_value=httpx.Response(500, text="Internal Error"))
    with pytest.raises(httpx.HTTPStatusError):
        await connector.query(ConnectorQuery(resource="issues"))


# ---------------------------------------------------------------------------
# Custom base_url
# ---------------------------------------------------------------------------


@respx.mock
async def test_custom_base_url(connector_custom_url):
    respx.get("https://youtrack.example.com/api/users/me").mock(
        return_value=httpx.Response(200, json={"name": "Custom User"}),
    )
    result = await connector_custom_url.health_check()
    assert result.ok is True
    assert result.detail == "Custom User"
