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


@pytest.fixture
def connector():
    return JiraConnector(
        instance=_INSTANCE,
        creds={"email": EMAIL, "api_token": API_TOKEN},
    )


@pytest.fixture
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
    query = ConnectorQuery(resource="unknown")
    with pytest.raises(ValueError, match="Unsupported Jira resource"):
        await connector.query(query)


async def test_unsupported_write_resource(connector):
    payload = ConnectorPayload(resource="delete", data={})
    with pytest.raises(ValueError, match="Unsupported Jira write resource"):
        await connector.write(payload)


def test_connector_type(connector):
    assert connector.connector_type == ConnectorType.JIRA


def test_missing_credentials_raises():
    with pytest.raises(ValueError, match="Jira credentials must contain"):
        JiraConnector(instance=_INSTANCE, creds={})


@respx.mock
async def test_query_issue_http_error(connector):
    respx.get(f"{_BASE}/issue/NONEXISTENT").mock(return_value=httpx.Response(404))
    query = ConnectorQuery(resource="issue", filters={"issue_key": "NONEXISTENT"})
    with pytest.raises(ValueError, match="Jira API HTTP 404"):
        await connector.query(query)


@respx.mock
async def test_query_search_http_error(connector):
    respx.post(f"{_BASE}/search").mock(
        return_value=httpx.Response(400, json={"errorMessages": ["Field 'xyz' does not exist"]})
    )
    query = ConnectorQuery(resource="search", filters={"jql": "invalid jql"})
    with pytest.raises(ValueError, match="Jira API HTTP 400"):
        await connector.query(query)


@respx.mock
async def test_write_create_issue_http_error(connector):
    respx.post(f"{_BASE}/issue").mock(
        return_value=httpx.Response(400, json={"errors": {"summary": "Operation blocked"}})
    )
    payload = ConnectorPayload(
        resource="issue",
        data={"project": {"key": "PROJ"}, "summary": "Bad data", "issuetype": {"name": "Task"}},
    )
    with pytest.raises(ValueError, match="Jira API HTTP 400"):
        await connector.write(payload)


@respx.mock
async def test_write_update_missing_key(connector):
    payload = ConnectorPayload(
        resource="issue_update",
        data={"fields": {"summary": "No key provided"}},
    )
    with pytest.raises(ValueError, match="requires 'issue_key'"):
        await connector.write(payload)


@respx.mock
async def test_query_issue_comments(connector):
    comments_data = {
        "comments": [
            {"id": "10001", "body": "First comment", "author": {"displayName": "Alice"}},
            {"id": "10002", "body": "Second comment", "author": {"displayName": "Bob"}},
        ],
        "total": 2,
        "startAt": 0,
        "maxResults": 50,
    }
    respx.get(f"{_BASE}/issue/PROJ-123/comment").mock(return_value=httpx.Response(200, json=comments_data))
    result = await connector.query(ConnectorQuery(resource="issue_comments", filters={"issue_key": "PROJ-123"}))
    assert len(result.records) == 2
    assert result.total == 2
    assert result.records[0]["body"] == "First comment"


@respx.mock
async def test_query_issue_comments_pagination(connector):
    comments_data = {
        "comments": [{"id": "10001", "body": "First comment"}],
        "total": 3,
        "startAt": 0,
        "maxResults": 1,
    }
    respx.get(f"{_BASE}/issue/PROJ-123/comment").mock(return_value=httpx.Response(200, json=comments_data))
    result = await connector.query(
        ConnectorQuery(resource="issue_comments", filters={"issue_key": "PROJ-123"}, limit=1)
    )
    assert result.next_cursor == "1"


@respx.mock
async def test_query_issue_comments_missing_key(connector):
    query = ConnectorQuery(resource="issue_comments", filters={})
    with pytest.raises(ValueError, match="requires 'issue_key'"):
        await connector.query(query)


@respx.mock
async def test_write_issue_comment(connector):
    comment_response = {"id": "10001", "body": "Nice work!", "author": {"displayName": "Alice"}}
    respx.post(f"{_BASE}/issue/PROJ-123/comment").mock(return_value=httpx.Response(201, json=comment_response))
    result = await connector.write(
        ConnectorPayload(
            resource="issue_comment",
            data={"issue_key": "PROJ-123", "body": "Nice work!"},
        )
    )
    assert result["id"] == "10001"


@respx.mock
async def test_write_issue_comment_missing_body(connector):
    payload = ConnectorPayload(
        resource="issue_comment",
        data={"issue_key": "PROJ-123"},
    )
    with pytest.raises(ValueError, match="requires 'body'"):
        await connector.write(payload)


@respx.mock
async def test_write_issue_comment_missing_key(connector):
    payload = ConnectorPayload(
        resource="issue_comment",
        data={"body": "Nice work!"},
    )
    with pytest.raises(ValueError, match="requires 'issue_key'"):
        await connector.write(payload)


@respx.mock
async def test_query_transitions(connector):
    transitions_data = {
        "transitions": [
            {"id": "11", "name": "To Do", "to": {"statusCategory": {"key": "new"}}},
            {"id": "21", "name": "In Progress", "to": {"statusCategory": {"key": "indeterminate"}}},
            {"id": "31", "name": "Done", "to": {"statusCategory": {"key": "done"}}},
        ]
    }
    respx.get(f"{_BASE}/issue/PROJ-123/transitions").mock(return_value=httpx.Response(200, json=transitions_data))
    result = await connector.query(ConnectorQuery(resource="transitions", filters={"issue_key": "PROJ-123"}))
    assert len(result.records) == 3
    assert result.records[0]["name"] == "To Do"


@respx.mock
async def test_query_transitions_missing_key(connector):
    query = ConnectorQuery(resource="transitions", filters={})
    with pytest.raises(ValueError, match="requires 'issue_key'"):
        await connector.query(query)


@respx.mock
async def test_write_transition(connector):
    respx.post(f"{_BASE}/issue/PROJ-123/transitions").mock(return_value=httpx.Response(204))
    result = await connector.write(
        ConnectorPayload(
            resource="transition",
            data={"issue_key": "PROJ-123", "transition_id": "31"},
        )
    )
    assert result["issue_key"] == "PROJ-123"
    assert result["transitioned"] is True


@respx.mock
async def test_write_transition_missing_transition_id(connector):
    with pytest.raises(ValueError, match="requires 'transition_id'"):
        await connector.write(
            ConnectorPayload(
                resource="transition",
                data={"issue_key": "PROJ-123"},
            )
        )


@respx.mock
async def test_query_projects(connector):
    projects_data = [
        {"key": "PROJ", "name": "Project Alpha", "lead": {"displayName": "Alice"}},
        {"key": "SUP", "name": "Support", "lead": {"displayName": "Bob"}},
    ]
    respx.get(f"{_BASE}/project").mock(return_value=httpx.Response(200, json=projects_data))
    result = await connector.query(ConnectorQuery(resource="projects"))
    assert len(result.records) == 2
    assert result.records[0]["key"] == "PROJ"


@respx.mock
async def test_query_field_metadata(connector):
    createmeta = {
        "expand": "projects",
        "projects": [
            {
                "key": "PROJ",
                "name": "Project Alpha",
                "issuetypes": [
                    {
                        "id": "10001",
                        "name": "Task",
                        "subtask": False,
                        "fields": {
                            "summary": {"required": True, "name": "Summary", "key": "summary"},
                            "customfield_10001": {"required": False, "name": "Epic Link", "custom": True},
                        },
                    },
                    {"id": "10002", "name": "Bug", "subtask": False, "fields": {}},
                ],
            }
        ],
    }
    respx.get(f"{_BASE}/issue/createmeta").mock(return_value=httpx.Response(200, json=createmeta))
    result = await connector.query(ConnectorQuery(resource="field_metadata", filters={"project": "PROJ"}))
    assert result.total == 2
    assert result.records[0]["name"] == "Task"
    assert "customfield_10001" in result.records[0]["fields"]
    assert result.metadata["project"] == "PROJ"
    request = respx.calls.last.request
    assert request.url.params["projectKeys"] == "PROJ"
    assert request.url.params["expand"] == "projects.issuetypes.fields"


@respx.mock
async def test_query_field_metadata_unknown_project(connector):
    respx.get(f"{_BASE}/issue/createmeta").mock(
        return_value=httpx.Response(
            200,
            json={"expand": "projects", "projects": []},
        )
    )
    result = await connector.query(ConnectorQuery(resource="field_metadata", filters={"project": "NOPE"}))
    assert not result.records
    assert result.total == 0


@respx.mock
async def test_query_field_metadata_missing_project(connector):
    with pytest.raises(ValueError, match="requires 'project' filter"):
        await connector.query(ConnectorQuery(resource="field_metadata", filters={}))


@respx.mock
async def test_query_fields(connector):
    fields_data = [
        {"id": "summary", "name": "Summary", "custom": False, "schema": {"type": "string"}},
        {"id": "customfield_10001", "name": "Epic Link", "custom": True},
    ]
    respx.get(f"{_BASE}/field").mock(return_value=httpx.Response(200, json=fields_data))
    result = await connector.query(ConnectorQuery(resource="fields"))
    assert result.total == 2
    assert result.records[0]["id"] == "summary"
    assert result.records[1]["custom"] is True


@respx.mock
async def test_query_fields_rate_limit_metadata(connector):
    headers = {"X-RateLimit-Remaining": "7500", "X-RateLimit-Limit": "10000"}
    respx.get(f"{_BASE}/field").mock(
        return_value=httpx.Response(200, json=[{"id": "summary", "name": "Summary"}], headers=headers)
    )
    result = await connector.query(ConnectorQuery(resource="fields"))
    assert result.metadata["rate_limit"] == headers


@respx.mock
async def test_query_statuses(connector):
    statuses_data = [
        {
            "id": "10001",
            "name": "Task",
            "subtask": False,
            "statuses": [
                {"id": "1", "name": "To Do", "statusCategory": {"key": "new"}},
                {"id": "3", "name": "Done", "statusCategory": {"key": "done"}},
            ],
        }
    ]
    respx.get(f"{_BASE}/project/PROJ/statuses").mock(return_value=httpx.Response(200, json=statuses_data))
    result = await connector.query(ConnectorQuery(resource="statuses", filters={"project": "PROJ"}))
    assert result.total == 1
    assert [s["name"] for s in result.records[0]["statuses"]] == ["To Do", "Done"]
    assert result.metadata["project"] == "PROJ"


@respx.mock
async def test_query_statuses_missing_project(connector):
    with pytest.raises(ValueError, match="requires 'project' filter"):
        await connector.query(ConnectorQuery(resource="statuses", filters={}))


@respx.mock
async def test_query_statuses_http_error(connector):
    respx.get(f"{_BASE}/project/NOPE/statuses").mock(return_value=httpx.Response(404))
    with pytest.raises(ValueError, match="Jira API HTTP 404"):
        await connector.query(ConnectorQuery(resource="statuses", filters={"project": "NOPE"}))


@respx.mock
async def test_search_pagination_cursor(connector):
    search_body = {
        "issues": [{"id": "1", "key": "PROJ-1", "fields": {"summary": "Task 1"}}],
        "total": 10,
        "startAt": 0,
        "maxResults": 1,
    }
    respx.post(f"{_BASE}/search").mock(return_value=httpx.Response(200, json=search_body))
    result = await connector.query(
        ConnectorQuery(resource="search", filters={"jql": "project = PROJ", "max_results": 1})
    )
    assert len(result.records) == 1
    assert result.total == 10
    assert result.next_cursor == "1"


@respx.mock
async def test_search_pagination_last_page(connector):
    search_body = {
        "issues": [{"id": "1", "key": "PROJ-1", "fields": {"summary": "Task 1"}}],
        "total": 1,
        "startAt": 0,
        "maxResults": 50,
    }
    respx.post(f"{_BASE}/search").mock(return_value=httpx.Response(200, json=search_body))
    result = await connector.query(ConnectorQuery(resource="search", filters={"jql": "project = PROJ"}))
    assert result.total == 1
    assert result.next_cursor is None


@respx.mock
async def test_retry_429_then_success(connector):
    respx.get(f"{_BASE}/myself").mock(
        side_effect=[
            httpx.Response(429),
            httpx.Response(200, json={"displayName": "Alice"}),
        ]
    )
    result = await connector.health_check()
    assert result.ok is True


@respx.mock
async def test_retry_429_exhausted(connector):
    respx.get(f"{_BASE}/myself").mock(
        side_effect=[
            httpx.Response(429),
            httpx.Response(429),
            httpx.Response(429),
            httpx.Response(429),
        ]
    )
    result = await connector.health_check()
    assert result.ok is False
    assert "429" in result.detail


@respx.mock
async def test_304_not_modified(connector):
    respx.get(f"{_BASE}/issue/PROJ-123").mock(return_value=httpx.Response(304))
    with pytest.raises(ValueError, match="304 Not Modified"):
        await connector.query(ConnectorQuery(resource="issue", filters={"issue_key": "PROJ-123"}))


# --- X-RateLimit-* metadata tests ---


@respx.mock
async def test_query_issue_rate_limit_metadata(connector):
    """Query results expose Jira Cloud X-RateLimit-* headers via metadata."""
    issue_data = {"id": "10001", "key": "PROJ-123", "fields": {"summary": "Fix bug"}}
    headers = {
        "X-RateLimit-Limit": "10000",
        "X-RateLimit-Remaining": "9780",
        "X-RateLimit-Reset": "1754160000",
    }
    respx.get(f"{_BASE}/issue/PROJ-123").mock(return_value=httpx.Response(200, json=issue_data, headers=headers))
    result = await connector.query(ConnectorQuery(resource="issue", filters={"issue_key": "PROJ-123"}))
    assert result.records[0]["key"] == "PROJ-123"
    assert result.metadata["rate_limit"] == headers


@respx.mock
async def test_query_search_rate_limit_metadata(connector):
    """Search results expose rate-limit metadata too."""
    search_body = {"issues": [{"id": "1", "key": "PROJ-1"}], "total": 1}
    headers = {
        "X-RateLimit-Limit": "10000",
        "X-RateLimit-Remaining": "9000",
        "X-RateLimit-Reset": "1754160000",
    }
    respx.post(f"{_BASE}/search").mock(return_value=httpx.Response(200, json=search_body, headers=headers))
    result = await connector.query(ConnectorQuery(resource="search", filters={"jql": "project = PROJ"}))
    assert result.total == 1
    assert result.metadata["rate_limit"] == headers


@respx.mock
async def test_rate_limit_metadata_empty_when_absent(connector):
    """No rate-limit headers on the response -> empty metadata dict."""
    issue_data = {"id": "10001", "key": "PROJ-123"}
    respx.get(f"{_BASE}/issue/PROJ-123").mock(return_value=httpx.Response(200, json=issue_data))
    result = await connector.query(ConnectorQuery(resource="issue", filters={"issue_key": "PROJ-123"}))
    assert not result.metadata["rate_limit"]


@respx.mock
async def test_query_projects_rate_limit_metadata(connector):
    """List resources expose rate-limit metadata as well."""
    headers = {"X-RateLimit-Remaining": "500", "X-RateLimit-Limit": "10000"}
    respx.get(f"{_BASE}/project").mock(return_value=httpx.Response(200, json=[{"key": "PROJ"}], headers=headers))
    result = await connector.query(ConnectorQuery(resource="projects"))
    assert len(result.records) == 1
    assert result.metadata["rate_limit"] == headers


# --- issue_assign / issue_label / issue_delete write resources ---


@respx.mock
async def test_write_issue_assign_by_account_id(connector):
    respx.put(f"{_BASE}/issue/PROJ-123").mock(return_value=httpx.Response(204))
    result = await connector.write(
        ConnectorPayload(
            resource="issue_assign",
            data={"issue_key": "PROJ-123", "account_id": "712020:abc123"},
        )
    )
    assert result["issue_key"] == "PROJ-123"
    assert result["assignee"] == {"accountId": "712020:abc123"}
    request = respx.calls.last.request
    assert request.method == "PUT"
    assert request.url.path == "/rest/api/3/issue/PROJ-123"
    assert request.read() == b'{"fields":{"assignee":{"accountId":"712020:abc123"}}}'


@respx.mock
async def test_write_issue_assign_by_email(connector):
    users = [{"accountId": "712020:email_user"}]
    respx.get(f"{_BASE}/user/search").mock(return_value=httpx.Response(200, json=users))
    respx.put(f"{_BASE}/issue/PROJ-123").mock(return_value=httpx.Response(204))
    result = await connector.write(
        ConnectorPayload(
            resource="issue_assign",
            data={"issue_key": "PROJ-123", "email": "alice@example.com"},
        )
    )
    assert result["assignee"] == {"accountId": "712020:email_user"}
    search_request = respx.calls[0].request
    assert search_request.url.params["query"] == "alice@example.com"
    assert respx.calls[1].request.url.path == "/rest/api/3/issue/PROJ-123"


@respx.mock
async def test_write_issue_assign_by_display_name(connector):
    users = [{"accountId": "712020:dn_user"}]
    respx.get(f"{_BASE}/user/search").mock(return_value=httpx.Response(200, json=users))
    respx.put(f"{_BASE}/issue/PROJ-123").mock(return_value=httpx.Response(204))
    result = await connector.write(
        ConnectorPayload(
            resource="issue_assign",
            data={"issue_key": "PROJ-123", "display_name": "Alice Example"},
        )
    )
    assert result["assignee"] == {"accountId": "712020:dn_user"}
    assert respx.calls[0].request.url.params["query"] == "Alice Example"


@respx.mock
async def test_write_issue_assign_unassign_with_null(connector):
    respx.put(f"{_BASE}/issue/PROJ-123").mock(return_value=httpx.Response(204))
    result = await connector.write(
        ConnectorPayload(
            resource="issue_assign",
            data={"issue_key": "PROJ-123", "account_id": None},
        )
    )
    assert result["assignee"] is None
    assert respx.calls.last.request.read() == b'{"fields":{"assignee":null}}'


@respx.mock
async def test_write_issue_assign_unassign_with_flag(connector):
    respx.put(f"{_BASE}/issue/PROJ-123").mock(return_value=httpx.Response(204))
    result = await connector.write(
        ConnectorPayload(
            resource="issue_assign",
            data={"issue_key": "PROJ-123", "unassign": True},
        )
    )
    assert result["assignee"] is None


@respx.mock
async def test_write_issue_assign_missing_key(connector):
    with pytest.raises(ValueError, match="requires 'issue_key'"):
        await connector.write(
            ConnectorPayload(
                resource="issue_assign",
                data={"account_id": "712020:abc"},
            )
        )


@respx.mock
async def test_write_issue_assign_no_identifier(connector):
    with pytest.raises(ValueError, match="requires 'account_id', 'email', 'display_name', or 'unassign'"):
        await connector.write(
            ConnectorPayload(
                resource="issue_assign",
                data={"issue_key": "PROJ-123"},
            )
        )


@respx.mock
async def test_write_issue_assign_email_not_found(connector):
    respx.get(f"{_BASE}/user/search").mock(return_value=httpx.Response(200, json=[]))
    with pytest.raises(ValueError, match="Jira user not found for email"):
        await connector.write(
            ConnectorPayload(
                resource="issue_assign",
                data={"issue_key": "PROJ-123", "email": "nobody@example.com"},
            )
        )


@respx.mock
async def test_write_issue_label_add_and_remove(connector):
    current_issue = {
        "id": "10001",
        "key": "PROJ-123",
        "fields": {"labels": ["bug", "stale"]},
    }
    respx.get(f"{_BASE}/issue/PROJ-123").mock(return_value=httpx.Response(200, json=current_issue))
    respx.put(f"{_BASE}/issue/PROJ-123").mock(return_value=httpx.Response(204))
    result = await connector.write(
        ConnectorPayload(
            resource="issue_label",
            data={"issue_key": "PROJ-123", "add": ["backend"], "remove": ["stale"]},
        )
    )
    assert result["labels"] == ["bug", "backend"]
    request = respx.calls.last.request
    assert request.method == "PUT"
    assert request.read() == b'{"fields":{"labels":["bug","backend"]}}'


@respx.mock
async def test_write_issue_label_add_only_deduplicates(connector):
    current_issue = {"id": "10001", "key": "PROJ-123", "fields": {"labels": ["bug"]}}
    respx.get(f"{_BASE}/issue/PROJ-123").mock(return_value=httpx.Response(200, json=current_issue))
    respx.put(f"{_BASE}/issue/PROJ-123").mock(return_value=httpx.Response(204))
    result = await connector.write(
        ConnectorPayload(
            resource="issue_label",
            data={"issue_key": "PROJ-123", "add": ["bug", "backend"]},
        )
    )
    assert result["labels"] == ["bug", "backend"]


@respx.mock
async def test_write_issue_label_missing_add_remove(connector):
    with pytest.raises(ValueError, match="requires 'add' and/or 'remove'"):
        await connector.write(
            ConnectorPayload(
                resource="issue_label",
                data={"issue_key": "PROJ-123"},
            )
        )


@respx.mock
async def test_write_issue_label_missing_key(connector):
    with pytest.raises(ValueError, match="requires 'issue_key'"):
        await connector.write(
            ConnectorPayload(
                resource="issue_label",
                data={"add": ["bug"]},
            )
        )


@respx.mock
async def test_write_issue_delete(connector):
    respx.delete(f"{_BASE}/issue/PROJ-123").mock(return_value=httpx.Response(204))
    result = await connector.write(
        ConnectorPayload(
            resource="issue_delete",
            data={"issue_key": "PROJ-123"},
        )
    )
    assert result["issue_key"] == "PROJ-123"
    assert result["deleted"] is True
    request = respx.calls.last.request
    assert request.method == "DELETE"
    assert request.url.path == "/rest/api/3/issue/PROJ-123"


@respx.mock
async def test_write_issue_delete_missing_key(connector):
    with pytest.raises(ValueError, match="requires 'issue_key'"):
        await connector.write(
            ConnectorPayload(
                resource="issue_delete",
                data={},
            )
        )


# --- base_url (self-hosted / Jira Data Center) ---


def test_default_base_url_is_cloud():
    c = JiraConnector(instance=_INSTANCE, creds={"email": EMAIL, "api_token": API_TOKEN})
    assert c._base_url == f"https://{_INSTANCE}/rest/api/3"


def test_custom_base_url_used_as_is():
    c = JiraConnector(
        instance="jira.example.com",
        creds={"email": EMAIL, "api_token": API_TOKEN},
        base_url="https://jira.example.com/rest/api/2",
    )
    assert c._base_url == "https://jira.example.com/rest/api/2"


def test_custom_base_url_trailing_slash_stripped():
    c = JiraConnector(
        instance="jira.example.com",
        creds={"email": EMAIL, "api_token": API_TOKEN},
        base_url="https://jira.example.com/rest/api/2/",
    )
    assert c._base_url == "https://jira.example.com/rest/api/2"


def test_custom_base_url_bare_host_appends_api_path():
    c = JiraConnector(
        instance="jira.example.com",
        creds={"email": EMAIL, "api_token": API_TOKEN},
        base_url="https://jira.example.com",
    )
    assert c._base_url == "https://jira.example.com/rest/api/3"


@respx.mock
async def test_self_hosted_health_check_hits_custom_base_url():
    c = JiraConnector(
        instance="jira.example.com",
        creds={"email": EMAIL, "api_token": API_TOKEN},
        base_url="https://jira.example.com/rest/api/2",
    )
    respx.get("https://jira.example.com/rest/api/2/myself").mock(
        return_value=httpx.Response(200, json={"displayName": "Alice"})
    )
    result = await c.health_check()
    assert result.ok is True
    assert result.detail == "Alice"


@respx.mock
async def test_self_hosted_query_issue():
    c = JiraConnector(
        instance="jira.example.com",
        creds={"token": "pat"},
        base_url="https://jira.example.com/rest/api/2",
    )
    respx.get("https://jira.example.com/rest/api/2/issue/PROJ-1").mock(
        return_value=httpx.Response(200, json={"id": "1", "key": "PROJ-1"})
    )
    result = await c.query(ConnectorQuery(resource="issue", filters={"issue_key": "PROJ-1"}))
    assert result.records[0]["key"] == "PROJ-1"


# --- issue_attachments query ---


@respx.mock
async def test_query_issue_attachments(connector):
    issue_data = {
        "id": "10001",
        "key": "PROJ-123",
        "fields": {
            "attachment": [
                {"id": "10000", "filename": "spec.pdf", "mimeType": "application/pdf", "size": 1234},
                {"id": "10001", "filename": "notes.txt", "mimeType": "text/plain", "size": 88},
            ]
        },
    }
    respx.get(f"{_BASE}/issue/PROJ-123").mock(return_value=httpx.Response(200, json=issue_data))
    result = await connector.query(ConnectorQuery(resource="issue_attachments", filters={"issue_key": "PROJ-123"}))
    assert result.total == 2
    assert result.records[0]["filename"] == "spec.pdf"


@respx.mock
async def test_query_issue_attachments_empty(connector):
    respx.get(f"{_BASE}/issue/PROJ-123").mock(
        return_value=httpx.Response(200, json={"id": "10001", "key": "PROJ-123", "fields": {}})
    )
    result = await connector.query(ConnectorQuery(resource="issue_attachments", filters={"issue_key": "PROJ-123"}))
    assert not result.records


@respx.mock
async def test_query_issue_attachments_missing_key(connector):
    with pytest.raises(ValueError, match="requires 'issue_key'"):
        await connector.query(ConnectorQuery(resource="issue_attachments", filters={}))


# --- issue_attachment write (multipart upload) ---


@respx.mock
async def test_write_issue_attachment_content(connector):
    created = [{"id": "10000", "filename": "notes.txt", "size": 10}]
    respx.post(f"{_BASE}/issue/PROJ-123/attachments").mock(return_value=httpx.Response(201, json=created))
    result = await connector.write(
        ConnectorPayload(
            resource="issue_attachment",
            data={"issue_key": "PROJ-123", "filename": "notes.txt", "content": "hello world"},
        )
    )
    assert result[0]["filename"] == "notes.txt"
    request = respx.calls.last.request
    assert request.method == "POST"
    assert request.headers.get("X-Atlassian-Token") == "no-check"
    assert "multipart/form-data" in request.headers.get("content-type", "")
    assert b"hello world" in request.content


@respx.mock
async def test_write_issue_attachment_bytes(connector):
    created = [{"id": "10000", "filename": "data.bin", "size": 4}]
    respx.post(f"{_BASE}/issue/PROJ-123/attachments").mock(return_value=httpx.Response(201, json=created))
    result = await connector.write(
        ConnectorPayload(
            resource="issue_attachment",
            data={"issue_key": "PROJ-123", "filename": "data.bin", "file": b"\x00\x01\x02\x03"},
        )
    )
    assert result[0]["filename"] == "data.bin"
    assert b"\x00\x01\x02\x03" in respx.calls.last.request.content


@respx.mock
async def test_write_issue_attachment_missing_filename(connector):
    with pytest.raises(ValueError, match="requires 'filename'"):
        await connector.write(
            ConnectorPayload(
                resource="issue_attachment",
                data={"issue_key": "PROJ-123", "content": "hello"},
            )
        )


@respx.mock
async def test_write_issue_attachment_missing_content(connector):
    with pytest.raises(ValueError, match="requires 'content' or 'file'"):
        await connector.write(
            ConnectorPayload(
                resource="issue_attachment",
                data={"issue_key": "PROJ-123", "filename": "a.txt"},
            )
        )


@respx.mock
async def test_write_issue_attachment_both_content_and_file(connector):
    with pytest.raises(ValueError, match="exactly one of 'content' or 'file'"):
        await connector.write(
            ConnectorPayload(
                resource="issue_attachment",
                data={"issue_key": "PROJ-123", "filename": "a.txt", "content": "hi", "file": b"hi"},
            )
        )


@respx.mock
async def test_write_issue_attachment_missing_key(connector):
    with pytest.raises(ValueError, match="requires 'issue_key'"):
        await connector.write(
            ConnectorPayload(
                resource="issue_attachment",
                data={"filename": "a.txt", "content": "hi"},
            )
        )


@respx.mock
async def test_write_issue_attachment_http_error(connector):
    respx.post(f"{_BASE}/issue/PROJ-123/attachments").mock(return_value=httpx.Response(400))
    with pytest.raises(ValueError, match="Jira API HTTP 400"):
        await connector.write(
            ConnectorPayload(
                resource="issue_attachment",
                data={"issue_key": "PROJ-123", "filename": "a.txt", "content": "hi"},
            )
        )


# --- issue_remote_links query + issue_remote_link / remote_link_delete writes ---


@respx.mock
async def test_query_issue_remote_links(connector):
    links = [
        {
            "id": "10001",
            "self": "https://jira/rest/api/3/issue/PROJ-123/remotelink/10001",
            "object": {"url": "https://example.com/pr/42", "title": "PR #42"},
        },
        {
            "id": "10002",
            "self": "https://jira/rest/api/3/issue/PROJ-123/remotelink/10002",
            "object": {"url": "https://example.com/wiki/1", "title": "Design doc"},
        },
    ]
    respx.get(f"{_BASE}/issue/PROJ-123/remotelink").mock(return_value=httpx.Response(200, json=links))
    result = await connector.query(ConnectorQuery(resource="issue_remote_links", filters={"issue_key": "PROJ-123"}))
    assert result.total == 2
    assert result.records[0]["object"]["url"] == "https://example.com/pr/42"


@respx.mock
async def test_query_issue_remote_links_empty(connector):
    respx.get(f"{_BASE}/issue/PROJ-123/remotelink").mock(return_value=httpx.Response(200, json=[]))
    result = await connector.query(ConnectorQuery(resource="issue_remote_links", filters={"issue_key": "PROJ-123"}))
    assert not result.records


@respx.mock
async def test_query_issue_remote_links_missing_key(connector):
    with pytest.raises(ValueError, match="requires 'issue_key'"):
        await connector.query(ConnectorQuery(resource="issue_remote_links", filters={}))


@respx.mock
async def test_write_issue_remote_link(connector):
    created = {"id": "10001", "self": "https://jira/rest/api/3/issue/PROJ-123/remotelink/10001"}
    respx.post(f"{_BASE}/issue/PROJ-123/remotelink").mock(return_value=httpx.Response(201, json=created))
    result = await connector.write(
        ConnectorPayload(
            resource="issue_remote_link",
            data={"issue_key": "PROJ-123", "url": "https://example.com/pr/42", "title": "PR #42"},
        )
    )
    assert result["id"] == "10001"
    request = respx.calls.last.request
    assert request.method == "POST"
    assert request.read() == b'{"object":{"url":"https://example.com/pr/42","title":"PR #42"}}'


@respx.mock
async def test_write_issue_remote_link_url_only(connector):
    respx.post(f"{_BASE}/issue/PROJ-123/remotelink").mock(return_value=httpx.Response(201, json={"id": "1"}))
    result = await connector.write(
        ConnectorPayload(
            resource="issue_remote_link",
            data={"issue_key": "PROJ-123", "url": "https://example.com"},
        )
    )
    assert result["id"] == "1"
    assert respx.calls.last.request.read() == b'{"object":{"url":"https://example.com"}}'


@respx.mock
async def test_write_issue_remote_link_missing_url(connector):
    with pytest.raises(ValueError, match="requires 'url'"):
        await connector.write(
            ConnectorPayload(
                resource="issue_remote_link",
                data={"issue_key": "PROJ-123"},
            )
        )


@respx.mock
async def test_write_issue_remote_link_missing_key(connector):
    with pytest.raises(ValueError, match="requires 'issue_key'"):
        await connector.write(
            ConnectorPayload(
                resource="issue_remote_link",
                data={"url": "https://example.com"},
            )
        )


@respx.mock
async def test_write_remote_link_delete(connector):
    respx.delete(f"{_BASE}/issue/PROJ-123/remotelink/10001").mock(return_value=httpx.Response(204))
    result = await connector.write(
        ConnectorPayload(
            resource="remote_link_delete",
            data={"issue_key": "PROJ-123", "link_id": "10001"},
        )
    )
    assert result["deleted"] is True
    request = respx.calls.last.request
    assert request.method == "DELETE"
    assert request.url.path == "/rest/api/3/issue/PROJ-123/remotelink/10001"


@respx.mock
async def test_write_remote_link_delete_missing_link_id(connector):
    with pytest.raises(ValueError, match="requires 'link_id'"):
        await connector.write(
            ConnectorPayload(
                resource="remote_link_delete",
                data={"issue_key": "PROJ-123"},
            )
        )


@respx.mock
async def test_write_remote_link_delete_missing_key(connector):
    with pytest.raises(ValueError, match="requires 'issue_key'"):
        await connector.write(
            ConnectorPayload(
                resource="remote_link_delete",
                data={"link_id": "10001"},
            )
        )


# --- project_components / project_versions queries ---


@respx.mock
async def test_query_project_components(connector):
    components = [
        {"id": "10000", "name": "Backend", "lead": {"displayName": "Alice"}},
        {"id": "10001", "name": "Frontend", "lead": {"displayName": "Bob"}},
    ]
    respx.get(f"{_BASE}/project/PROJ/components").mock(return_value=httpx.Response(200, json=components))
    result = await connector.query(ConnectorQuery(resource="project_components", filters={"project": "PROJ"}))
    assert result.total == 2
    assert result.records[0]["name"] == "Backend"
    assert result.metadata["project"] == "PROJ"


@respx.mock
async def test_query_project_components_missing_project(connector):
    with pytest.raises(ValueError, match="requires 'project' filter"):
        await connector.query(ConnectorQuery(resource="project_components", filters={}))


@respx.mock
async def test_query_project_components_http_error(connector):
    respx.get(f"{_BASE}/project/NOPE/components").mock(return_value=httpx.Response(404))
    with pytest.raises(ValueError, match="Jira API HTTP 404"):
        await connector.query(ConnectorQuery(resource="project_components", filters={"project": "NOPE"}))


@respx.mock
async def test_query_project_versions(connector):
    versions = [
        {"id": "10000", "name": "1.0.0", "released": True},
        {"id": "10001", "name": "1.1.0", "released": False},
    ]
    respx.get(f"{_BASE}/project/PROJ/versions").mock(return_value=httpx.Response(200, json=versions))
    result = await connector.query(ConnectorQuery(resource="project_versions", filters={"project": "PROJ"}))
    assert result.total == 2
    assert result.records[0]["name"] == "1.0.0"
    assert result.metadata["project"] == "PROJ"


@respx.mock
async def test_query_project_versions_missing_project(connector):
    with pytest.raises(ValueError, match="requires 'project' filter"):
        await connector.query(ConnectorQuery(resource="project_versions", filters={}))


@respx.mock
async def test_query_project_versions_http_error(connector):
    respx.get(f"{_BASE}/project/NOPE/versions").mock(return_value=httpx.Response(404))
    with pytest.raises(ValueError, match="Jira API HTTP 404"):
        await connector.query(ConnectorQuery(resource="project_versions", filters={"project": "NOPE"}))


# --- self-hosted / Jira Data Center support ---

_SELF_HOSTED_BASE = "https://jira.example.com/rest/api/2"


def test_constructor_self_hosted_base_url():
    connector = JiraConnector(instance="", creds={"token": "pat"}, base_url=_SELF_HOSTED_BASE)
    assert connector._base_url == _SELF_HOSTED_BASE


def test_constructor_api_version():
    connector = JiraConnector(instance="jira.example.com", creds={"token": "pat"}, api_version=2)
    assert connector._base_url == _SELF_HOSTED_BASE


def test_constructor_missing_instance_and_base_url():
    with pytest.raises(ValueError, match="requires 'instance' or 'base_url'"):
        JiraConnector(instance="", creds={"token": "pat"})


@respx.mock
async def test_health_check_self_hosted():
    connector = JiraConnector(instance="", creds={"token": "pat"}, base_url=_SELF_HOSTED_BASE)
    respx.get(f"{_SELF_HOSTED_BASE}/myself").mock(return_value=httpx.Response(200, json={"displayName": "Alice"}))
    result = await connector.health_check()
    assert result.ok is True
    assert result.detail == "Alice"
    request = respx.calls.last.request
    assert request.url.path == "/rest/api/2/myself"


@respx.mock
async def test_query_issue_self_hosted():
    connector = JiraConnector(instance="", creds={"token": "pat"}, base_url=_SELF_HOSTED_BASE)
    respx.get(f"{_SELF_HOSTED_BASE}/issue/PROJ-123").mock(
        return_value=httpx.Response(200, json={"id": "10001", "key": "PROJ-123"})
    )
    result = await connector.query(ConnectorQuery(resource="issue", filters={"issue_key": "PROJ-123"}))
    assert result.records[0]["key"] == "PROJ-123"
    assert respx.calls.last.request.url.path == "/rest/api/2/issue/PROJ-123"


# --- attachment support ---


@respx.mock
async def test_query_attachments(connector):
    issue_body = {
        "id": "10001",
        "key": "PROJ-123",
        "fields": {
            "attachment": [
                {"id": "20001", "filename": "a.txt", "mimeType": "text/plain", "size": 5},
                {"id": "20002", "filename": "b.png", "mimeType": "image/png", "size": 9},
            ]
        },
    }
    respx.get(f"{_BASE}/issue/PROJ-123").mock(return_value=httpx.Response(200, json=issue_body))
    result = await connector.query(ConnectorQuery(resource="attachments", filters={"issue_key": "PROJ-123"}))
    assert result.total == 2
    assert result.records[0]["filename"] == "a.txt"
    request = respx.calls.last.request
    assert request.url.params["fields"] == "attachment"


@respx.mock
async def test_query_attachments_empty(connector):
    respx.get(f"{_BASE}/issue/PROJ-123").mock(
        return_value=httpx.Response(200, json={"id": "10001", "key": "PROJ-123", "fields": {}})
    )
    result = await connector.query(ConnectorQuery(resource="attachments", filters={"issue_key": "PROJ-123"}))
    assert not result.records
    assert result.total == 0


@respx.mock
async def test_query_attachments_missing_key(connector):
    with pytest.raises(ValueError, match="requires 'issue_key'"):
        await connector.query(ConnectorQuery(resource="attachments", filters={}))


@respx.mock
async def test_query_attachment_download(connector):
    respx.get(f"{_BASE}/attachment/20001/content").mock(
        return_value=httpx.Response(
            200,
            content=b"hello",
            headers={"content-type": "text/plain; charset=utf-8"},
        )
    )
    result = await connector.query(ConnectorQuery(resource="attachment", filters={"attachment_id": "20001"}))
    record = result.records[0]
    assert record["attachment_id"] == "20001"
    assert record["encoding"] == "base64"
    assert record["content_type"] == "text/plain; charset=utf-8"
    import base64

    assert base64.b64decode(record["content"]) == b"hello"
    assert respx.calls.last.request.url.path == "/rest/api/3/attachment/20001/content"


@respx.mock
async def test_query_attachment_missing_id(connector):
    with pytest.raises(ValueError, match="requires 'attachment_id'"):
        await connector.query(ConnectorQuery(resource="attachment", filters={}))


@respx.mock
async def test_write_attachment_content(connector):
    respx.post(f"{_BASE}/issue/PROJ-123/attachments").mock(
        return_value=httpx.Response(201, json=[{"id": "20001", "filename": "a.txt"}])
    )
    result = await connector.write(
        ConnectorPayload(
            resource="attachment",
            data={"issue_key": "PROJ-123", "filename": "a.txt", "content": "hello world"},
        )
    )
    assert result["issue_key"] == "PROJ-123"
    assert result["attachments"][0]["filename"] == "a.txt"
    request = respx.calls.last.request
    assert request.method == "POST"
    assert request.url.path == "/rest/api/3/issue/PROJ-123/attachments"
    assert request.headers.get("X-Atlassian-Token") == "no-check"


@respx.mock
async def test_write_attachment_bytes(connector):
    respx.post(f"{_BASE}/issue/PROJ-123/attachments").mock(
        return_value=httpx.Response(201, json=[{"id": "20002", "filename": "b.png"}])
    )
    result = await connector.write(
        ConnectorPayload(
            resource="attachment",
            data={"issue_key": "PROJ-123", "filename": "b.png", "file": b"\x89PNG", "mime_type": "image/png"},
        )
    )
    assert result["attachments"][0]["filename"] == "b.png"


@respx.mock
async def test_write_attachment_content_bytes(connector):
    respx.post(f"{_BASE}/issue/PROJ-123/attachments").mock(
        return_value=httpx.Response(201, json=[{"id": "20003", "filename": "c.bin"}])
    )
    result = await connector.write(
        ConnectorPayload(
            resource="attachment",
            data={
                "issue_key": "PROJ-123",
                "filename": "c.bin",
                "content": b"\x00\x01\x02",
                "mime_type": "application/octet-stream",
            },
        )
    )
    assert result["attachments"][0]["filename"] == "c.bin"
    request = respx.calls.last.request
    assert b"\x00\x01\x02" in request.content


@respx.mock
async def test_write_attachment_missing_filename(connector):
    with pytest.raises(ValueError, match="requires 'filename'"):
        await connector.write(
            ConnectorPayload(
                resource="attachment",
                data={"issue_key": "PROJ-123", "content": "hello"},
            )
        )


@respx.mock
async def test_write_attachment_missing_content(connector):
    with pytest.raises(ValueError, match="requires 'content' or 'file'"):
        await connector.write(
            ConnectorPayload(
                resource="attachment",
                data={"issue_key": "PROJ-123", "filename": "a.txt"},
            )
        )


@respx.mock
async def test_write_attachment_both_content_and_file(connector):
    with pytest.raises(ValueError, match="exactly one of 'content' or 'file'"):
        await connector.write(
            ConnectorPayload(
                resource="attachment",
                data={"issue_key": "PROJ-123", "filename": "a.txt", "content": "hello", "file": b"hello"},
            )
        )


@respx.mock
async def test_write_attachment_missing_key(connector):
    with pytest.raises(ValueError, match="requires 'issue_key'"):
        await connector.write(
            ConnectorPayload(
                resource="attachment",
                data={"filename": "a.txt", "content": "hello"},
            )
        )
