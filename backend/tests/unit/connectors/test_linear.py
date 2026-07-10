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
                "labels": {"nodes": [{"id": "l1", "name": "bug", "color": "#ff0000"}]},
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
    assert result.records[0]["labels"]["nodes"][0]["name"] == "bug"


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
                        "labels": {"nodes": []},
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
                    "labels": {"nodes": [{"id": "l1", "name": "bug", "color": "#ff0000"}]},
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
                    "labels": {"nodes": [{"id": "l1", "name": "bug", "color": "#ff0000"}]},
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


@respx.mock
async def test_query_missing_id_raises(connector):
    with pytest.raises(ValueError, match="requires 'id' filter"):
        await connector.query(ConnectorQuery(resource="issue", filters={}))


@respx.mock
async def test_write_update_missing_id_raises(connector):
    with pytest.raises(ValueError, match="Missing 'id' in update payload"):
        await connector.write(ConnectorPayload(resource="issue_update", data={"title": "No id"}))


@respx.mock
async def test_write_update_failure(connector):
    respx.post(_GRAPHQL).mock(
        return_value=httpx.Response(200, json={"data": {"issueUpdate": {"success": False, "issue": None}}})
    )
    with pytest.raises(ValueError, match="Failed to update Linear issue"):
        await connector.write(ConnectorPayload(resource="issue_update", data={"id": "issue-1", "title": "Fail"}))


@respx.mock
async def test_write_graphql_error(connector):
    respx.post(_GRAPHQL).mock(return_value=httpx.Response(200, json={"errors": [{"message": "Not authorized"}]}))
    with pytest.raises(ValueError, match="Linear API error"):
        await connector.write(ConnectorPayload(resource="issue", data={"title": "X", "teamId": "t1"}))


@respx.mock
async def test_retry_429_then_success(connector):
    route = respx.post(_GRAPHQL)
    route.side_effect = [
        httpx.Response(429, headers={"Retry-After": "0"}, text="Rate limited"),
        httpx.Response(429, headers={"Retry-After": "0"}, text="Rate limited"),
        _mock_response({"data": {"viewer": {"id": "u1", "name": "Alice", "email": "a@a.com"}}}),
    ]
    result = await connector.health_check()
    assert result.ok is True
    assert result.detail == "Alice"


@respx.mock
async def test_retry_502_then_success(connector):
    respx.post(_GRAPHQL).mock(
        side_effect=[
            httpx.Response(502, text="Bad Gateway"),
            httpx.Response(502, text="Bad Gateway"),
            _mock_response({"data": {"viewer": {"id": "u1", "name": "Bob", "email": "b@b.com"}}}),
        ]
    )
    result = await connector.health_check()
    assert result.ok is True
    assert result.detail == "Bob"


@respx.mock
async def test_retry_exhaustion_429(connector):
    respx.post(_GRAPHQL).mock(return_value=httpx.Response(429, text="Rate limited"))
    result = await connector.health_check()
    assert result.ok is False
    assert "429" in result.detail


@respx.mock
async def test_retry_exhaustion_connection_error(connector):
    respx.post(_GRAPHQL).mock(side_effect=httpx.ConnectError("Connection refused"))
    result = await connector.health_check()
    assert result.ok is False
    assert "connection" in result.detail.lower()


@respx.mock
async def test_retry_exhaustion_timeout(connector):
    respx.post(_GRAPHQL).mock(side_effect=httpx.TimeoutException("Timed out"))
    result = await connector.health_check()
    assert result.ok is False
    assert "timeout" in result.detail.lower()


@respx.mock
async def test_304_not_modified(connector):
    respx.post(_GRAPHQL).mock(return_value=httpx.Response(304))
    result = await connector.health_check()
    assert result.ok is False
    assert "304" in result.detail


@respx.mock
async def test_query_search_with_pagination(connector):
    page1 = {
        "data": {
            "searchIssues": {
                "nodes": [
                    {
                        "id": "i1",
                        "identifier": "PROJ-1",
                        "title": "First",
                        "description": None,
                        "priority": 0,
                        "state": {"id": "s1", "name": "Todo"},
                        "assignee": None,
                        "team": {"id": "t1", "name": "Eng", "key": "PROJ"},
                        "labels": {"nodes": []},
                        "createdAt": "2024-01-01T00:00:00Z",
                        "updatedAt": "2024-01-01T00:00:00Z",
                        "url": "https://linear.app/team/issue/PROJ-1",
                    }
                ],
                "pageInfo": {"hasNextPage": True, "endCursor": "cursor-abc"},
            }
        }
    }
    page2 = {
        "data": {
            "searchIssues": {
                "nodes": [
                    {
                        "id": "i2",
                        "identifier": "PROJ-2",
                        "title": "Second",
                        "description": None,
                        "priority": 1,
                        "state": {"id": "s2", "name": "In Progress"},
                        "assignee": None,
                        "team": {"id": "t1", "name": "Eng", "key": "PROJ"},
                        "labels": {"nodes": []},
                        "createdAt": "2024-01-02T00:00:00Z",
                        "updatedAt": "2024-01-02T00:00:00Z",
                        "url": "https://linear.app/team/issue/PROJ-2",
                    }
                ],
                "pageInfo": {"hasNextPage": False, "endCursor": "cursor-def"},
            }
        }
    }
    respx.post(_GRAPHQL).mock(side_effect=[_mock_response(page1), _mock_response(page2)])

    q1 = ConnectorQuery(resource="search", filters={"query": "bug"}, limit=1)
    r1 = await connector.query(q1)
    assert len(r1.records) == 1
    assert r1.next_cursor == "cursor-abc"

    q2 = ConnectorQuery(resource="search", filters={"query": "bug"}, limit=1, cursor="cursor-abc")
    r2 = await connector.query(q2)
    assert len(r2.records) == 1
    assert r2.next_cursor is None


@respx.mock
async def test_query_issue_comments(connector):
    comments_data = {
        "data": {
            "issue": {
                "comments": {
                    "nodes": [
                        {
                            "id": "c1",
                            "body": "Looks good",
                            "user": {"id": "u1", "name": "Alice", "email": "a@a.com"},
                            "createdAt": "2024-01-01T00:00:00Z",
                            "updatedAt": "2024-01-01T00:00:00Z",
                        },
                    ]
                }
            }
        }
    }
    respx.post(_GRAPHQL).mock(return_value=_mock_response(comments_data))
    result = await connector.query(ConnectorQuery(resource="issue_comments", filters={"issueId": "issue-1"}))
    assert len(result.records) == 1
    assert result.records[0]["body"] == "Looks good"


@respx.mock
async def test_query_issue_comments_missing_issue_id(connector):
    with pytest.raises(ValueError, match="requires 'issueId' filter"):
        await connector.query(ConnectorQuery(resource="issue_comments", filters={}))


@respx.mock
async def test_write_issue_comment(connector):
    comment_data = {
        "data": {
            "commentCreate": {
                "success": True,
                "comment": {
                    "id": "c1",
                    "body": "Nice work",
                    "user": {"id": "u1", "name": "Alice", "email": "a@a.com"},
                    "createdAt": "2024-01-01T00:00:00Z",
                    "updatedAt": "2024-01-01T00:00:00Z",
                },
            }
        }
    }
    respx.post(_GRAPHQL).mock(return_value=_mock_response(comment_data))
    result = await connector.write(
        ConnectorPayload(resource="issue_comment", data={"issueId": "issue-1", "body": "Nice work"})
    )
    assert result["id"] == "c1"
    assert result["body"] == "Nice work"


@respx.mock
async def test_write_issue_comment_failure(connector):
    respx.post(_GRAPHQL).mock(
        return_value=_mock_response({"data": {"commentCreate": {"success": False, "comment": None}}})
    )
    with pytest.raises(ValueError, match="Failed to create Linear issue comment"):
        await connector.write(ConnectorPayload(resource="issue_comment", data={"issueId": "i1", "body": "Fail"}))


@respx.mock
async def test_query_teams(connector):
    teams_data = {
        "data": {
            "teams": {
                "nodes": [
                    {"id": "t1", "name": "Engineering", "key": "ENG", "description": "Builds stuff"},
                    {"id": "t2", "name": "Design", "key": "DSN", "description": "Makes it pretty"},
                ]
            }
        }
    }
    respx.post(_GRAPHQL).mock(return_value=_mock_response(teams_data))
    result = await connector.query(ConnectorQuery(resource="teams"))
    assert len(result.records) == 2
    assert result.records[0]["key"] == "ENG"
    assert result.records[1]["name"] == "Design"


@respx.mock
async def test_query_team_projects(connector):
    projects_data = {
        "data": {
            "team": {
                "projects": {
                    "nodes": [
                        {
                            "id": "p1",
                            "name": "Q4 Launch",
                            "description": "Big release",
                            "state": "planned",
                            "startDate": None,
                            "targetDate": None,
                        },
                    ]
                }
            }
        }
    }
    respx.post(_GRAPHQL).mock(return_value=_mock_response(projects_data))
    result = await connector.query(ConnectorQuery(resource="team_projects", filters={"teamId": "t1"}))
    assert len(result.records) == 1
    assert result.records[0]["name"] == "Q4 Launch"


@respx.mock
async def test_query_team_states(connector):
    states_data = {
        "data": {
            "team": {
                "states": {
                    "nodes": [
                        {"id": "s1", "name": "Todo", "type": "unstarted", "position": 0},
                        {"id": "s2", "name": "In Progress", "type": "started", "position": 1},
                    ]
                }
            }
        }
    }
    respx.post(_GRAPHQL).mock(return_value=_mock_response(states_data))
    result = await connector.query(ConnectorQuery(resource="team_states", filters={"teamId": "t1"}))
    assert len(result.records) == 2
    assert result.records[0]["type"] == "unstarted"


@respx.mock
async def test_query_team_labels(connector):
    labels_data = {
        "data": {
            "team": {
                "labels": {
                    "nodes": [
                        {"id": "l1", "name": "bug", "color": "#ff0000"},
                        {"id": "l2", "name": "feature", "color": "#00ff00"},
                    ]
                }
            }
        }
    }
    respx.post(_GRAPHQL).mock(return_value=_mock_response(labels_data))
    result = await connector.query(ConnectorQuery(resource="team_labels", filters={"teamId": "t1"}))
    assert len(result.records) == 2
    assert result.records[1]["name"] == "feature"


@respx.mock
async def test_query_team_cycles(connector):
    cycles_data = {
        "data": {
            "team": {
                "cycles": {
                    "nodes": [
                        {
                            "id": "cy1",
                            "name": "Sprint 24",
                            "startsAt": "2024-06-01T00:00:00Z",
                            "endsAt": "2024-06-14T00:00:00Z",
                            "completedAt": None,
                        },
                    ]
                }
            }
        }
    }
    respx.post(_GRAPHQL).mock(return_value=_mock_response(cycles_data))
    result = await connector.query(ConnectorQuery(resource="team_cycles", filters={"teamId": "t1"}))
    assert len(result.records) == 1
    assert result.records[0]["name"] == "Sprint 24"


@respx.mock
async def test_query_team_projects_missing_team_id(connector):
    with pytest.raises(ValueError, match="requires 'teamId' filter"):
        await connector.query(ConnectorQuery(resource="team_projects", filters={}))


@respx.mock
async def test_query_team_states_missing_team_id(connector):
    with pytest.raises(ValueError, match="requires 'teamId' filter"):
        await connector.query(ConnectorQuery(resource="team_states", filters={}))


@respx.mock
async def test_query_team_labels_missing_team_id(connector):
    with pytest.raises(ValueError, match="requires 'teamId' filter"):
        await connector.query(ConnectorQuery(resource="team_labels", filters={}))


@respx.mock
async def test_query_team_cycles_missing_team_id(connector):
    with pytest.raises(ValueError, match="requires 'teamId' filter"):
        await connector.query(ConnectorQuery(resource="team_cycles", filters={}))
