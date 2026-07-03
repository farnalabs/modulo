"""Unit tests for JiraConnector — HTTP responses are mocked via httpx."""

import httpx
import pytest
import respx

from modulo.connectors.base import ConnectorPayload, ConnectorQuery, ConnectorType
from modulo.connectors.jira import JiraConnector

_INSTANCE = "test-domain.atlassian.net"
_BASE = f"https://{_INSTANCE}/rest/api/3"
EMAIL = "user@example.com"
API_TOKEN = "jira_api_token"


@pytest.fixture()
def connector():
    return JiraConnector(
        instance=_INSTANCE,
        creds={"email": EMAIL, "api_token": API_TOKEN},
    )


@pytest.fixture()
def connector_token():
    return JiraConnector(
        instance=_INSTANCE,
        creds={"token": "pat_token"},
    )


@respx.mock
async def test_health_check_ok(connector):
    respx.get(f"{_BASE}/myself").mock(return_value=httpx.Response(200, json={"displayName": "Alice"}))
    result = await connector.health_check()
    assert result.ok is True
    assert result.detail == "Alice"


@respx.mock
async def test_health_check_fail(connector):
    respx.get(f"{_BASE}/myself").mock(return_value=httpx.Response(401, text="Unauthorized"))
    result = await connector.health_check()
    assert result.ok is False
    assert "401" in result.detail


@respx.mock
async def test_query_issue(connector):
    issue_data = {"id": "10001", "key": "PROJ-123", "fields": {"summary": "Fix bug"}}
    respx.get(f"{_BASE}/issue/PROJ-123").mock(return_value=httpx.Response(200, json=issue_data))
    result = await connector.query(ConnectorQuery(resource="issue", filters={"issue_key": "PROJ-123"}))
    assert result.records[0]["key"] == "PROJ-123"


@respx.mock
async def test_query_search(connector):
    search_body = {
        "issues": [{"id": "1", "key": "PROJ-1", "fields": {"summary": "Task 1"}}],
        "total": 1,
    }
    respx.post(f"{_BASE}/search").mock(return_value=httpx.Response(200, json=search_body))
    result = await connector.query(ConnectorQuery(resource="search", filters={"jql": "project = PROJ"}))
    assert len(result.records) == 1
    assert result.total == 1


@respx.mock
async def test_write_create_issue(connector):
    created = {"id": "10002", "key": "PROJ-124", "self": "https://..."}
    respx.post(f"{_BASE}/issue").mock(return_value=httpx.Response(201, json=created))
    result = await connector.write(
        ConnectorPayload(
            resource="issue",
            data={
                "project": {"key": "PROJ"},
                "summary": "New task",
                "issuetype": {"name": "Task"},
            },
        )
    )
    assert result["key"] == "PROJ-124"


@respx.mock
async def test_write_update_issue(connector):
    respx.put(f"{_BASE}/issue/PROJ-123").mock(return_value=httpx.Response(204))
    result = await connector.write(
        ConnectorPayload(
            resource="issue_update",
            data={
                "issue_key": "PROJ-123",
                "fields": {"summary": "Updated summary"},
            },
        )
    )
    assert result["issue_key"] == "PROJ-123"
    assert result["updated"] is True


@respx.mock
async def test_health_check_token_auth(connector_token):
    respx.get(f"{_BASE}/myself").mock(return_value=httpx.Response(200, json={"displayName": "Bob"}))
    result = await connector_token.health_check()
    assert result.ok is True

    # Verify Bearer token was sent
    request = respx.calls.last.request
    assert request.headers.get("Authorization") == "Bearer pat_token"


async def test_unsupported_query_resource(connector):
    with pytest.raises(ValueError, match="Unsupported Jira resource"):
        await connector.query(ConnectorQuery(resource="unknown"))


async def test_unsupported_write_resource(connector):
    with pytest.raises(ValueError, match="Unsupported Jira write resource"):
        await connector.write(ConnectorPayload(resource="delete", data={}))


def test_connector_type(connector):
    assert connector.connector_type == ConnectorType.JIRA


def test_missing_credentials_raises():
    with pytest.raises(ValueError, match="Jira credentials must contain"):
        JiraConnector(instance=_INSTANCE, creds={})


@respx.mock
async def test_query_issue_http_error(connector):
    respx.get(f"{_BASE}/issue/NONEXISTENT").mock(return_value=httpx.Response(404))
    with pytest.raises(ValueError, match="Jira API HTTP 404"):
        await connector.query(ConnectorQuery(resource="issue", filters={"issue_key": "NONEXISTENT"}))


@respx.mock
async def test_query_search_http_error(connector):
    respx.post(f"{_BASE}/search").mock(return_value=httpx.Response(400, json={"errorMessages": ["Field 'xyz' does not exist"]}))
    with pytest.raises(ValueError, match="Jira API HTTP 400"):
        await connector.query(ConnectorQuery(resource="search", filters={"jql": "invalid jql"}))


@respx.mock
async def test_write_create_issue_http_error(connector):
    respx.post(f"{_BASE}/issue").mock(return_value=httpx.Response(400, json={"errors": {"summary": "Operation blocked"}}))
    with pytest.raises(ValueError, match="Jira API HTTP 400"):
        await connector.write(
            ConnectorPayload(
                resource="issue",
                data={"project": {"key": "PROJ"}, "summary": "Bad data", "issuetype": {"name": "Task"}},
            )
        )


@respx.mock
async def test_write_update_missing_key(connector):
    with pytest.raises(ValueError, match="requires 'issue_key'"):
        await connector.write(
            ConnectorPayload(
                resource="issue_update",
                data={"fields": {"summary": "No key provided"}},
            )
        )
