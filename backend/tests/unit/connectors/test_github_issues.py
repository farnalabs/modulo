"""Unit tests for GitHubConnector issues/labels/milestones/comments — HTTP responses are mocked via httpx."""

import json

import httpx
import pytest
import respx

from modulo.connectors.base import ConnectorPayload, ConnectorQuery
from modulo.connectors.github import GitHubConnector

TOKEN = "ghp_test_token"


@pytest.fixture()
def connector():
    return GitHubConnector(token=TOKEN)


# ---------------------------------------------------------------------------
# Query: issues
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_issues_basic(connector):
    issues = [
        {"number": 1, "title": "Bug fix", "state": "open"},
        {"number": 2, "title": "Feature request", "state": "open"},
    ]
    respx.get("https://api.github.com/repos/owner/repo/issues").mock(
        return_value=httpx.Response(200, json=issues)
    )
    result = await connector.query(
        ConnectorQuery(resource="issues", filters={"repo": "owner/repo"})
    )
    assert len(result.records) == 2
    assert result.records[0]["title"] == "Bug fix"


@respx.mock
async def test_query_issues_with_filters(connector):
    issues = [{"number": 3, "title": "Critical bug", "state": "open"}]
    route = respx.get("https://api.github.com/repos/owner/repo/issues").mock(
        return_value=httpx.Response(200, json=issues)
    )
    result = await connector.query(
        ConnectorQuery(
            resource="issues",
            filters={"repo": "owner/repo", "state": "open", "labels": "bug,urgent", "sort": "created"},
        )
    )
    assert len(result.records) == 1
    assert route.calls.last.request.url.params.get("state") == "open"
    assert route.calls.last.request.url.params.get("labels") == "bug,urgent"


@respx.mock
async def test_query_issues_empty(connector):
    respx.get("https://api.github.com/repos/owner/repo/issues").mock(
        return_value=httpx.Response(200, json=[])
    )
    result = await connector.query(
        ConnectorQuery(resource="issues", filters={"repo": "owner/repo"})
    )
    assert result.total == 0
    assert result.records == []


@respx.mock
async def test_query_issues_with_pagination(connector):
    issues = [{"number": i, "title": f"Issue {i}"} for i in range(5)]
    respx.get("https://api.github.com/repos/owner/repo/issues?per_page=5").mock(
        return_value=httpx.Response(200, json=issues)
    )
    result = await connector.query(
        ConnectorQuery(resource="issues", filters={"repo": "owner/repo"}, limit=5)
    )
    assert len(result.records) == 5


@respx.mock
async def test_query_issues_missing_repo(connector):
    with pytest.raises(KeyError):
        await connector.query(ConnectorQuery(resource="issues"))


@respx.mock
async def test_query_issues_http_error(connector):
    respx.get("https://api.github.com/repos/owner/repo/issues").mock(
        return_value=httpx.Response(403, text="Forbidden")
    )
    with pytest.raises(ValueError, match="403"):
        await connector.query(
            ConnectorQuery(resource="issues", filters={"repo": "owner/repo"})
        )


# ---------------------------------------------------------------------------
# Query: issue (single)
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_single_issue(connector):
    issue = {"number": 42, "title": "The answer", "state": "open", "body": "Details"}
    respx.get("https://api.github.com/repos/owner/repo/issues/42").mock(
        return_value=httpx.Response(200, json=issue)
    )
    result = await connector.query(
        ConnectorQuery(resource="issue", filters={"repo": "owner/repo", "issue_number": 42})
    )
    assert result.records[0]["number"] == 42


@respx.mock
async def test_query_single_issue_not_found(connector):
    respx.get("https://api.github.com/repos/owner/repo/issues/999").mock(
        return_value=httpx.Response(404, text="Not Found")
    )
    with pytest.raises(ValueError, match="404"):
        await connector.query(
            ConnectorQuery(resource="issue", filters={"repo": "owner/repo", "issue_number": 999})
        )


# ---------------------------------------------------------------------------
# Query: labels
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_labels(connector):
    labels = [
        {"name": "bug", "color": "d73a4a"},
        {"name": "enhancement", "color": "a2eeef"},
    ]
    respx.get("https://api.github.com/repos/owner/repo/labels").mock(
        return_value=httpx.Response(200, json=labels)
    )
    result = await connector.query(
        ConnectorQuery(resource="labels", filters={"repo": "owner/repo"})
    )
    assert len(result.records) == 2
    assert result.records[0]["name"] == "bug"


@respx.mock
async def test_query_labels_empty(connector):
    respx.get("https://api.github.com/repos/owner/repo/labels").mock(
        return_value=httpx.Response(200, json=[])
    )
    result = await connector.query(
        ConnectorQuery(resource="labels", filters={"repo": "owner/repo"})
    )
    assert result.total == 0


# ---------------------------------------------------------------------------
# Query: milestones
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_milestones(connector):
    milestones = [
        {"number": 1, "title": "v1.0", "state": "open"},
        {"number": 2, "title": "v2.0", "state": "closed"},
    ]
    respx.get("https://api.github.com/repos/owner/repo/milestones").mock(
        return_value=httpx.Response(200, json=milestones)
    )
    result = await connector.query(
        ConnectorQuery(resource="milestones", filters={"repo": "owner/repo"})
    )
    assert len(result.records) == 2


@respx.mock
async def test_query_milestones_with_state_filter(connector):
    milestones = [{"number": 1, "title": "v1.0", "state": "open"}]
    route = respx.get("https://api.github.com/repos/owner/repo/milestones").mock(
        return_value=httpx.Response(200, json=milestones)
    )
    await connector.query(
        ConnectorQuery(
            resource="milestones",
            filters={"repo": "owner/repo", "state": "open", "sort": "due_on", "direction": "asc"},
        )
    )
    assert route.calls.last.request.url.params.get("state") == "open"
    assert route.calls.last.request.url.params.get("sort") == "due_on"


# ---------------------------------------------------------------------------
# Query: issue_comments
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_issue_comments(connector):
    comments = [
        {"id": 1, "body": "First comment", "user": {"login": "user1"}},
        {"id": 2, "body": "Second comment", "user": {"login": "user2"}},
    ]
    respx.get("https://api.github.com/repos/owner/repo/issues/42/comments").mock(
        return_value=httpx.Response(200, json=comments)
    )
    result = await connector.query(
        ConnectorQuery(
            resource="issue_comments",
            filters={"repo": "owner/repo", "issue_number": 42},
        )
    )
    assert len(result.records) == 2
    assert result.records[0]["body"] == "First comment"


# ---------------------------------------------------------------------------
# Query: issue_events
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_issue_events(connector):
    events = [
        {"id": 1, "event": "labeled", "actor": {"login": "user1"}},
    ]
    respx.get("https://api.github.com/repos/owner/repo/issues/42/events").mock(
        return_value=httpx.Response(200, json=events)
    )
    result = await connector.query(
        ConnectorQuery(
            resource="issue_events",
            filters={"repo": "owner/repo", "issue_number": 42},
        )
    )
    assert len(result.records) == 1
    assert result.records[0]["event"] == "labeled"


# ---------------------------------------------------------------------------
# Query: assignees
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_assignees(connector):
    assignees = [
        {"login": "user1", "id": 101},
        {"login": "user2", "id": 102},
    ]
    respx.get("https://api.github.com/repos/owner/repo/assignees").mock(
        return_value=httpx.Response(200, json=assignees)
    )
    result = await connector.query(
        ConnectorQuery(resource="assignees", filters={"repo": "owner/repo"})
    )
    assert len(result.records) == 2
    assert result.records[0]["login"] == "user1"


# ---------------------------------------------------------------------------
# Query: timeline
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_timeline(connector):
    events = [
        {"id": 1, "event": "commented", "created_at": "2024-01-01T00:00:00Z"},
    ]
    respx.get("https://api.github.com/repos/owner/repo/issues/42/timeline").mock(
        return_value=httpx.Response(200, json=events)
    )
    result = await connector.query(
        ConnectorQuery(
            resource="timeline",
            filters={"repo": "owner/repo", "issue_number": 42},
        )
    )
    assert len(result.records) == 1
    assert result.records[0]["event"] == "commented"


# ---------------------------------------------------------------------------
# Write: issue (create)
# ---------------------------------------------------------------------------


@respx.mock
async def test_write_create_issue(connector):
    created = {"number": 100, "title": "New bug", "state": "open", "id": 500}
    respx.post("https://api.github.com/repos/owner/repo/issues").mock(
        return_value=httpx.Response(201, json=created)
    )
    result = await connector.write(
        ConnectorPayload(
            resource="issue",
            data={
                "repo": "owner/repo",
                "title": "New bug",
                "body": "Found a bug",
                "labels": ["bug"],
                "assignees": ["user1"],
            },
        )
    )
    assert result["number"] == 100
    assert result["title"] == "New bug"


@respx.mock
async def test_write_create_issue_minimal(connector):
    created = {"number": 101, "title": "Minimal", "state": "open"}
    route = respx.post("https://api.github.com/repos/owner/repo/issues").mock(
        return_value=httpx.Response(201, json=created)
    )
    await connector.write(
        ConnectorPayload(
            resource="issue",
            data={"repo": "owner/repo", "title": "Minimal"},
        )
    )
    sent = json.loads(route.calls.last.request.content)
    assert sent["title"] == "Minimal"
    assert "body" not in sent


@respx.mock
async def test_write_create_issue_http_error(connector):
    respx.post("https://api.github.com/repos/owner/repo/issues").mock(
        return_value=httpx.Response(422, text="Unprocessable")
    )
    with pytest.raises(ValueError, match="422"):
        await connector.write(
            ConnectorPayload(
                resource="issue",
                data={"repo": "owner/repo", "title": "Bad issue"},
            )
        )


# ---------------------------------------------------------------------------
# Write: issue_update
# ---------------------------------------------------------------------------


@respx.mock
async def test_write_update_issue(connector):
    updated = {"number": 42, "title": "Updated", "state": "closed"}
    route = respx.patch("https://api.github.com/repos/owner/repo/issues/42").mock(
        return_value=httpx.Response(200, json=updated)
    )
    result = await connector.write(
        ConnectorPayload(
            resource="issue_update",
            data={"repo": "owner/repo", "issue_number": 42, "state": "closed", "title": "Updated"},
        )
    )
    assert result["state"] == "closed"
    sent = json.loads(route.calls.last.request.content)
    assert sent["state"] == "closed"
    assert sent["title"] == "Updated"


@respx.mock
async def test_write_update_issue_partial(connector):
    route = respx.patch("https://api.github.com/repos/owner/repo/issues/42").mock(
        return_value=httpx.Response(200, json={"number": 42})
    )
    await connector.write(
        ConnectorPayload(
            resource="issue_update",
            data={"repo": "owner/repo", "issue_number": 42, "body": "New body text"},
        )
    )
    sent = json.loads(route.calls.last.request.content)
    assert sent["body"] == "New body text"
    assert "title" not in sent


# ---------------------------------------------------------------------------
# Write: issue_comment
# ---------------------------------------------------------------------------


@respx.mock
async def test_write_issue_comment(connector):
    comment = {"id": 999, "body": "Nice work"}
    route = respx.post("https://api.github.com/repos/owner/repo/issues/42/comments").mock(
        return_value=httpx.Response(201, json=comment)
    )
    result = await connector.write(
        ConnectorPayload(
            resource="issue_comment",
            data={"repo": "owner/repo", "issue_number": 42, "body": "Nice work"},
        )
    )
    assert result["id"] == 999
    assert json.loads(route.calls.last.request.content)["body"] == "Nice work"


# ---------------------------------------------------------------------------
# Write: issue_label (add labels to issue)
# ---------------------------------------------------------------------------


@respx.mock
async def test_write_issue_label(connector):
    result_data = {"labels": [{"name": "bug"}, {"name": "urgent"}]}
    route = respx.post("https://api.github.com/repos/owner/repo/issues/42/labels").mock(
        return_value=httpx.Response(200, json=result_data)
    )
    result = await connector.write(
        ConnectorPayload(
            resource="issue_label",
            data={"repo": "owner/repo", "issue_number": 42, "labels": ["bug", "urgent"]},
        )
    )
    assert len(result["labels"]) == 2
    assert json.loads(route.calls.last.request.content)["labels"] == ["bug", "urgent"]


# ---------------------------------------------------------------------------
# Write: issue_reaction
# ---------------------------------------------------------------------------


@respx.mock
async def test_write_issue_reaction(connector):
    reaction = {"id": 1, "content": "+1", "user": {"login": "user1"}}
    route = respx.post("https://api.github.com/repos/owner/repo/issues/42/reactions").mock(
        return_value=httpx.Response(201, json=reaction)
    )
    result = await connector.write(
        ConnectorPayload(
            resource="issue_reaction",
            data={"repo": "owner/repo", "issue_number": 42, "content": "+1"},
        )
    )
    assert result["content"] == "+1"
    assert "application/vnd.github.squirrel-girl-preview" in route.calls.last.request.headers.get("Accept", "")


# ---------------------------------------------------------------------------
# Write: label (create)
# ---------------------------------------------------------------------------


@respx.mock
async def test_write_create_label(connector):
    label = {"name": "bug", "color": "d73a4a", "description": "Bug report"}
    route = respx.post("https://api.github.com/repos/owner/repo/labels").mock(
        return_value=httpx.Response(201, json=label)
    )
    result = await connector.write(
        ConnectorPayload(
            resource="label",
            data={"repo": "owner/repo", "name": "bug", "color": "d73a4a", "description": "Bug report"},
        )
    )
    assert result["name"] == "bug"
    sent = json.loads(route.calls.last.request.content)
    assert sent["color"] == "d73a4a"
    assert sent["description"] == "Bug report"


@respx.mock
async def test_write_create_label_minimal(connector):
    route = respx.post("https://api.github.com/repos/owner/repo/labels").mock(
        return_value=httpx.Response(201, json={"name": "urgent", "color": "ff0000"})
    )
    await connector.write(
        ConnectorPayload(
            resource="label",
            data={"repo": "owner/repo", "name": "urgent", "color": "ff0000"},
        )
    )
    sent = json.loads(route.calls.last.request.content)
    assert "description" not in sent


# ---------------------------------------------------------------------------
# Write: milestone (create)
# ---------------------------------------------------------------------------


@respx.mock
async def test_write_create_milestone(connector):
    milestone = {"number": 1, "title": "v1.0", "description": "First release"}
    route = respx.post("https://api.github.com/repos/owner/repo/milestones").mock(
        return_value=httpx.Response(201, json=milestone)
    )
    result = await connector.write(
        ConnectorPayload(
            resource="milestone",
            data={
                "repo": "owner/repo", "title": "v1.0",
                "description": "First release", "due_on": "2024-12-31T00:00:00Z",
            },
        )
    )
    assert result["title"] == "v1.0"
    sent = json.loads(route.calls.last.request.content)
    assert sent["due_on"] == "2024-12-31T00:00:00Z"


# ---------------------------------------------------------------------------
# Error paths — unsupported resource
# ---------------------------------------------------------------------------


async def test_unsupported_query_resource(connector):
    with pytest.raises(ValueError, match="Unsupported GitHub resource"):
        await connector.query(ConnectorQuery(resource="unknown"))


async def test_unsupported_write_resource(connector):
    with pytest.raises(ValueError, match="Unsupported GitHub write resource"):
        await connector.write(ConnectorPayload(resource="branch", data={}))


# ---------------------------------------------------------------------------
# Missing filters error paths
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_issues_missing_issue_number(connector):
    with pytest.raises(KeyError):
        await connector.query(
            ConnectorQuery(resource="issue", filters={"repo": "owner/repo"})
        )


@respx.mock
async def test_query_issue_comments_missing_issue_number(connector):
    with pytest.raises(KeyError):
        await connector.query(
            ConnectorQuery(resource="issue_comments", filters={"repo": "owner/repo"})
        )


@respx.mock
async def test_write_issue_missing_title(connector):
    with pytest.raises(KeyError):
        await connector.write(
            ConnectorPayload(
                resource="issue",
                data={"repo": "owner/repo"},
            )
        )


@respx.mock
async def test_write_issue_comment_missing_body(connector):
    with pytest.raises(KeyError):
        await connector.write(
            ConnectorPayload(
                resource="issue_comment",
                data={"repo": "owner/repo", "issue_number": 1},
            )
        )


@respx.mock
async def test_write_label_missing_name(connector):
    with pytest.raises(KeyError):
        await connector.write(
            ConnectorPayload(
                resource="label",
                data={"repo": "owner/repo", "color": "ff0000"},
            )
        )


@respx.mock
async def test_write_milestone_missing_title(connector):
    with pytest.raises(KeyError):
        await connector.write(
            ConnectorPayload(
                resource="milestone",
                data={"repo": "owner/repo"},
            )
        )
