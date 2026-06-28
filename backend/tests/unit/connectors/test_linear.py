"""Unit tests for LinearConnector — HTTP responses are mocked via httpx."""

import httpx
import pytest
import respx

from modulo.connectors.base import ConnectorPayload, ConnectorQuery, ConnectorType
from modulo.connectors.linear import LinearConnector

API_KEY = "lin_api_key_xxxx"
_GRAPHQL = "https://api.linear.app/graphql"
_BASE = "https://api.linear.app"


@pytest.fixture()
def connector():
    return LinearConnector(api_key=API_KEY)


def _mock_response(body: dict) -> httpx.Response:
    return httpx.Response(200, json=body)


@respx.mock
async def test_health_check_ok(connector):
    respx.post(_GRAPHQL).mock(
        return_value=_mock_response({"data": {"viewer": {"id": "u1", "name": "Alice", "email": "alice@example.com"}}})
    )
    result = await connector.health_check()
    assert result.ok is True
    assert result.detail == "Alice"


@respx.mock
async def test_health_check_fail(connector):
    respx.post(_GRAPHQL).mock(return_value=httpx.Response(401, text="Unauthorized"))
    result = await connector.health_check()
    assert result.ok is False
    assert "401" in result.detail


@respx.mock
async def test_health_check_no_viewer(connector):
    respx.post(_GRAPHQL).mock(return_value=_mock_response({"data": {"viewer": None}}))
    result = await connector.health_check()
    assert result.ok is False


@respx.mock
async def test_query_issue(connector):
    issue_data = {
        "data": {
            "issue": {
                "id": "issue-1",
                "identifier": "PROJ-123",
                "title": "Fix login bug",
                "description": "Users cannot log in",
                "priority": 2,
                "state": {"id": "state-1", "name": "In Progress"},
                "assignee": None,
                "team": {"id": "team-1", "name": "Engineering", "key": "PROJ"},
                "createdAt": "2024-01-01T00:00:00Z",
                "updatedAt": "2024-01-02T00:00:00Z",
                "url": "https://linear.app/team/issue/PROJ-123",
            }
        }
    }
    respx.post(_GRAPHQL).mock(return_value=_mock_response(issue_data))
    result = await connector.query(ConnectorQuery(resource="issue", filters={"id": "issue-1"}))
    assert len(result.records) == 1
    assert result.records[0]["identifier"] == "PROJ-123"
    assert result.records[0]["title"] == "Fix login bug"


@respx.mock
async def test_query_issue_not_found(connector):
    respx.post(_GRAPHQL).mock(return_value=_mock_response({"data": {"issue": None}}))
    result = await connector.query(ConnectorQuery(resource="issue", filters={"id": "nonexistent"}))
    assert len(result.records) == 0


@respx.mock
async def test_query_search(connector):
    search_data = {
        "data": {
            "searchIssues": {
                "nodes": [
                    {
                        "id": "issue-2",
                        "identifier": "PROJ-456",
                        "title": "Another bug",
                        "description": None,
                        "priority": 1,
                        "state": {"id": "state-2", "name": "Todo"},
                        "assignee": None,
                        "team": {"id": "team-1", "name": "Engineering", "key": "PROJ"},
                        "createdAt": "2024-01-03T00:00:00Z",
                        "updatedAt": "2024-01-03T00:00:00Z",
                        "url": "https://linear.app/team/issue/PROJ-456",
                    }
                ]
            }
        }
    }
    respx.post(_GRAPHQL).mock(return_value=_mock_response(search_data))
    result = await connector.query(ConnectorQuery(resource="search", filters={"query": "bug"}))
    assert len(result.records) == 1
    assert result.records[0]["identifier"] == "PROJ-456"


@respx.mock
async def test_write_create_issue(connector):
    create_data = {
        "data": {
            "issueCreate": {
                "success": True,
                "issue": {
                    "id": "issue-3",
                    "identifier": "PROJ-789",
                    "title": "New feature",
                    "description": "Implement feature",
                    "priority": 0,
                    "state": {"id": "state-2", "name": "Todo"},
                    "assignee": None,
                    "team": {"id": "team-1", "name": "Engineering", "key": "PROJ"},
                    "createdAt": "2024-01-04T00:00:00Z",
                    "updatedAt": "2024-01-04T00:00:00Z",
                    "url": "https://linear.app/team/issue/PROJ-789",
                },
            }
        }
    }
    respx.post(_GRAPHQL).mock(return_value=_mock_response(create_data))
    result = await connector.write(
        ConnectorPayload(
            resource="issue",
            data={"title": "New feature", "teamId": "team-1", "description": "Implement feature"},
        )
    )
    assert result["identifier"] == "PROJ-789"
    assert result["title"] == "New feature"


@respx.mock
async def test_write_update_issue(connector):
    update_data = {
        "data": {
            "issueUpdate": {
                "success": True,
                "issue": {
                    "id": "issue-1",
                    "identifier": "PROJ-123",
                    "title": "Updated title",
                    "description": "Users cannot log in",
                    "priority": 2,
                    "state": {"id": "state-1", "name": "In Progress"},
                    "assignee": None,
                    "team": {"id": "team-1", "name": "Engineering", "key": "PROJ"},
                    "createdAt": "2024-01-01T00:00:00Z",
                    "updatedAt": "2024-01-05T00:00:00Z",
                    "url": "https://linear.app/team/issue/PROJ-123",
                },
            }
        }
    }
    respx.post(_GRAPHQL).mock(return_value=_mock_response(update_data))
    result = await connector.write(
        ConnectorPayload(
            resource="issue_update",
            data={"id": "issue-1", "title": "Updated title"},
        )
    )
    assert result["title"] == "Updated title"


@respx.mock
async def test_write_create_issue_failure(connector):
    respx.post(_GRAPHQL).mock(return_value=_mock_response({"data": {"issueCreate": {"success": False, "issue": None}}}))
    with pytest.raises(ValueError, match="Failed to create Linear issue"):
        await connector.write(
            ConnectorPayload(
                resource="issue",
                data={"title": "Failing issue", "teamId": "team-1"},
            )
        )


@respx.mock
async def test_graphql_error_response(connector):
    respx.post(_GRAPHQL).mock(return_value=_mock_response({"errors": [{"message": "Not authenticated"}]}))
    with pytest.raises(ValueError, match="Linear API error"):
        await connector.query(ConnectorQuery(resource="issue", filters={"id": "issue-1"}))


async def test_unsupported_query_resource(connector):
    with pytest.raises(ValueError, match="Unsupported Linear resource"):
        await connector.query(ConnectorQuery(resource="unknown"))


async def test_unsupported_write_resource(connector):
    with pytest.raises(ValueError, match="Unsupported Linear write resource"):
        await connector.write(ConnectorPayload(resource="delete", data={}))


def test_connector_type(connector):
    assert connector.connector_type == ConnectorType.LINEAR
