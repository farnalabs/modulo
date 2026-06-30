"""Extended unit tests for GitLabConnector — issues, labels, milestones, notes, CI."""

import httpx
import pytest
import respx

from modulo.connectors.base import ConnectorPayload, ConnectorQuery
from modulo.connectors.gitlab import GitLabConnector

TOKEN = "glpat_test_token"
_API = "https://gitlab.com/api/v4"


@pytest.fixture()
def connector():
    return GitLabConnector(token=TOKEN)


# ---------------------------------------------------------------------------
# Query: issues
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_issues(connector):
    route = respx.get(f"{_API}/projects/group%2Fproject/issues")
    route.mock(return_value=httpx.Response(200, json=[{"id": 1, "iid": 42, "title": "Bug"}]))
    result = await connector.query(ConnectorQuery(resource="issues", filters={"project": "group/project"}))
    assert len(result.records) == 1
    assert result.records[0]["iid"] == 42


@respx.mock
async def test_query_issues_with_filters(connector):
    route = respx.get(f"{_API}/projects/group%2Fproject/issues")
    route.mock(return_value=httpx.Response(200, json=[{"id": 2, "iid": 43}]))
    result = await connector.query(ConnectorQuery(
        resource="issues",
        filters={"project": "group/project", "state": "closed", "labels": "bug", "milestone": "Sprint 1"},
    ))
    url = str(route.calls.last.request.url)
    assert "state=closed" in url
    assert "labels=bug" in url
    assert "milestone=Sprint+1" in url or "milestone=Sprint%201" in url
    assert len(result.records) == 1


@respx.mock
async def test_query_issues_search(connector):
    route = respx.get(f"{_API}/projects/group%2Fproject/issues")
    route.mock(return_value=httpx.Response(200, json=[]))
    result = await connector.query(ConnectorQuery(
        resource="issues",
        filters={"project": "group/project", "search": "login bug"},
    ))
    assert "search=login+bug" in str(route.calls.last.request.url)
    assert len(result.records) == 0


@respx.mock
async def test_query_issues_sort(connector):
    route = respx.get(f"{_API}/projects/group%2Fproject/issues")
    route.mock(return_value=httpx.Response(200, json=[]))
    await connector.query(ConnectorQuery(
        resource="issues",
        filters={"project": "group/project", "sort": "asc", "order_by": "created_at"},
    ))
    url = str(route.calls.last.request.url)
    assert "sort=asc" in url
    assert "order_by=created_at" in url


@respx.mock
async def test_query_issues_assignee(connector):
    route = respx.get(f"{_API}/projects/group%2Fproject/issues")
    route.mock(return_value=httpx.Response(200, json=[]))
    await connector.query(ConnectorQuery(
        resource="issues",
        filters={"project": "group/project", "assignee_id": 42},
    ))
    assert "assignee_id=42" in str(route.calls.last.request.url)


@respx.mock
async def test_query_issues_limit(connector):
    route = respx.get(f"{_API}/projects/group%2Fproject/issues")
    route.mock(return_value=httpx.Response(200, json=[]))
    await connector.query(ConnectorQuery(resource="issues", filters={"project": "group/project"}, limit=5))
    assert "per_page=5" in str(route.calls.last.request.url)


# ---------------------------------------------------------------------------
# Query: single issue
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_issue(connector):
    route = respx.get(f"{_API}/projects/group%2Fproject/issues/42")
    route.mock(return_value=httpx.Response(200, json={"id": 1, "iid": 42, "title": "Bug"}))
    result = await connector.query(ConnectorQuery(resource="issue", filters={"project": "group/project", "iid": 42}))
    assert result.records[0]["iid"] == 42
    assert result.records[0]["title"] == "Bug"


# ---------------------------------------------------------------------------
# Query: labels
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_labels(connector):
    route = respx.get(f"{_API}/projects/group%2Fproject/labels")
    route.mock(return_value=httpx.Response(200, json=[{"id": 1, "name": "bug"}]))
    result = await connector.query(ConnectorQuery(resource="labels", filters={"project": "group/project"}))
    assert len(result.records) == 1
    assert result.records[0]["name"] == "bug"


@respx.mock
async def test_query_label(connector):
    route = respx.get(f"{_API}/projects/group%2Fproject/labels/5")
    route.mock(return_value=httpx.Response(200, json={"id": 5, "name": "feature"}))
    result = await connector.query(
        ConnectorQuery(resource="label", filters={"project": "group/project", "label_id": 5}),
    )
    assert result.records[0]["name"] == "feature"


# ---------------------------------------------------------------------------
# Query: milestones
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_milestones(connector):
    route = respx.get(f"{_API}/projects/group%2Fproject/milestones")
    route.mock(return_value=httpx.Response(200, json=[{"id": 1, "title": "Sprint 1"}]))
    result = await connector.query(ConnectorQuery(resource="milestones", filters={"project": "group/project"}))
    assert len(result.records) == 1
    assert result.records[0]["title"] == "Sprint 1"


# ---------------------------------------------------------------------------
# Query: issue notes
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_issue_notes(connector):
    route = respx.get(f"{_API}/projects/group%2Fproject/issues/42/notes")
    route.mock(return_value=httpx.Response(200, json=[{"id": 101, "body": "Note"}]))
    result = await connector.query(
        ConnectorQuery(resource="issue_notes", filters={"project": "group/project", "iid": 42}),
    )
    assert len(result.records) == 1
    assert result.records[0]["body"] == "Note"


@respx.mock
async def test_query_issue_notes_sort(connector):
    route = respx.get(f"{_API}/projects/group%2Fproject/issues/42/notes")
    route.mock(return_value=httpx.Response(200, json=[]))
    await connector.query(ConnectorQuery(
        resource="issue_notes",
        filters={"project": "group/project", "iid": 42, "sort": "asc", "order_by": "created_at"},
    ))
    url = str(route.calls.last.request.url)
    assert "sort=asc" in url
    assert "order_by=created_at" in url


# ---------------------------------------------------------------------------
# Query: issue discussions
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_issue_discussions(connector):
    route = respx.get(f"{_API}/projects/group%2Fproject/issues/42/discussions")
    route.mock(return_value=httpx.Response(200, json=[{"id": "disc1", "notes": []}]))
    result = await connector.query(
        ConnectorQuery(resource="issue_discussions", filters={"project": "group/project", "iid": 42})
    )
    assert len(result.records) == 1


# ---------------------------------------------------------------------------
# Query: merge requests + single merge request
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_merge_requests(connector):
    route = respx.get(f"{_API}/projects/group%2Fproject/merge_requests")
    route.mock(return_value=httpx.Response(200, json=[{"id": 1, "iid": 5, "title": "MR"}]))
    result = await connector.query(ConnectorQuery(resource="merge_requests", filters={"project": "group/project"}))
    assert len(result.records) == 1


@respx.mock
async def test_query_merge_requests_filters(connector):
    route = respx.get(f"{_API}/projects/group%2Fproject/merge_requests")
    route.mock(return_value=httpx.Response(200, json=[]))
    await connector.query(ConnectorQuery(
        resource="merge_requests",
        filters={"project": "group/project", "state": "opened", "labels": "bug", "milestone": "Sprint 1"},
    ))
    url = str(route.calls.last.request.url)
    assert "state=opened" in url
    assert "labels=bug" in url
    assert "milestone=Sprint+1" in url or "milestone=Sprint%201" in url


@respx.mock
async def test_query_merge_request(connector):
    route = respx.get(f"{_API}/projects/group%2Fproject/merge_requests/5")
    route.mock(return_value=httpx.Response(200, json={"id": 1, "iid": 5, "title": "MR"}))
    result = await connector.query(
        ConnectorQuery(resource="merge_request", filters={"project": "group/project", "iid": 5}),
    )
    assert result.records[0]["iid"] == 5


# ---------------------------------------------------------------------------
# Query: branch / branches / tags
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_branch(connector):
    route = respx.get(f"{_API}/projects/group%2Fproject/repository/branches/main")
    route.mock(return_value=httpx.Response(200, json={"name": "main", "commit": {"id": "abc"}}))
    result = await connector.query(
        ConnectorQuery(resource="branch", filters={"project": "group/project", "name": "main"}),
    )
    assert result.records[0]["name"] == "main"


@respx.mock
async def test_query_branches(connector):
    route = respx.get(f"{_API}/projects/group%2Fproject/repository/branches")
    route.mock(return_value=httpx.Response(200, json=[{"name": "main"}]))
    result = await connector.query(ConnectorQuery(resource="branches", filters={"project": "group/project"}))
    assert len(result.records) == 1


@respx.mock
async def test_query_tags(connector):
    route = respx.get(f"{_API}/projects/group%2Fproject/repository/tags")
    route.mock(return_value=httpx.Response(200, json=[{"name": "v1.0"}]))
    result = await connector.query(ConnectorQuery(resource="tags", filters={"project": "group/project"}))
    assert len(result.records) == 1


# ---------------------------------------------------------------------------
# Query: pipelines / jobs
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_pipelines(connector):
    route = respx.get(f"{_API}/projects/group%2Fproject/pipelines")
    route.mock(return_value=httpx.Response(200, json=[{"id": 1, "ref": "main", "status": "success"}]))
    result = await connector.query(ConnectorQuery(resource="pipelines", filters={"project": "group/project"}))
    assert len(result.records) == 1
    assert result.records[0]["status"] == "success"


@respx.mock
async def test_query_jobs(connector):
    route = respx.get(f"{_API}/projects/group%2Fproject/pipelines/1/jobs")
    route.mock(return_value=httpx.Response(200, json=[{"id": 10, "name": "test", "status": "success"}]))
    result = await connector.query(
        ConnectorQuery(resource="jobs", filters={"project": "group/project", "pipeline_id": 1})
    )
    assert len(result.records) == 1
    assert result.records[0]["name"] == "test"


# ---------------------------------------------------------------------------
# Write: issue create
# ---------------------------------------------------------------------------


@respx.mock
async def test_write_issue(connector):
    route = respx.post(f"{_API}/projects/group%2Fproject/issues")
    route.mock(return_value=httpx.Response(200, json={"id": 100, "iid": 50, "title": "New bug", "state": "opened"}))
    result = await connector.write(ConnectorPayload(
        resource="issue",
        data={"project": "group/project", "title": "New bug", "description": "Details here"},
    ))
    assert result["iid"] == 50
    assert result["state"] == "opened"


@respx.mock
async def test_write_issue_with_labels(connector):
    route = respx.post(f"{_API}/projects/group%2Fproject/issues")
    route.mock(return_value=httpx.Response(200, json={"id": 101, "labels": ["bug"]}))
    await connector.write(ConnectorPayload(
        resource="issue",
        data={"project": "group/project", "title": "Bug", "labels": ["bug"], "milestone_id": 1, "assignee_ids": [42]},
    ))
    assert route.calls.last.request.content is not None


@respx.mock
async def test_write_issue_minimal(connector):
    route = respx.post(f"{_API}/projects/group%2Fproject/issues")
    route.mock(return_value=httpx.Response(200, json={"id": 102, "title": "Minimal"}))
    result = await connector.write(ConnectorPayload(
        resource="issue",
        data={"project": "group/project", "title": "Minimal"},
    ))
    assert result["title"] == "Minimal"


# ---------------------------------------------------------------------------
# Write: issue update
# ---------------------------------------------------------------------------


@respx.mock
async def test_write_issue_update_close(connector):
    route = respx.put(f"{_API}/projects/group%2Fproject/issues/42")
    route.mock(return_value=httpx.Response(200, json={"id": 100, "iid": 42, "state": "closed"}))
    result = await connector.write(ConnectorPayload(
        resource="issue_update",
        data={"project": "group/project", "iid": 42, "state_event": "close"},
    ))
    assert result["state"] == "closed"


@respx.mock
async def test_write_issue_update_reopen(connector):
    route = respx.put(f"{_API}/projects/group%2Fproject/issues/42")
    route.mock(return_value=httpx.Response(200, json={"id": 100, "state": "reopened"}))
    result = await connector.write(ConnectorPayload(
        resource="issue_update",
        data={"project": "group/project", "iid": 42, "state_event": "reopen"},
    ))
    assert result["state"] == "reopened"


@respx.mock
async def test_write_issue_update_title(connector):
    route = respx.put(f"{_API}/projects/group%2Fproject/issues/42")
    route.mock(return_value=httpx.Response(200, json={"id": 100, "title": "Updated"}))
    result = await connector.write(ConnectorPayload(
        resource="issue_update",
        data={"project": "group/project", "iid": 42, "title": "Updated"},
    ))
    assert result["title"] == "Updated"


# ---------------------------------------------------------------------------
# Write: issue note
# ---------------------------------------------------------------------------


@respx.mock
async def test_write_issue_note(connector):
    route = respx.post(f"{_API}/projects/group%2Fproject/issues/42/notes")
    route.mock(return_value=httpx.Response(200, json={"id": 200, "body": "Fixed"}))
    result = await connector.write(ConnectorPayload(
        resource="issue_note",
        data={"project": "group/project", "iid": 42, "body": "Fixed"},
    ))
    assert result["body"] == "Fixed"


# ---------------------------------------------------------------------------
# Write: issue label
# ---------------------------------------------------------------------------


@respx.mock
async def test_write_issue_label(connector):
    route = respx.put(f"{_API}/projects/group%2Fproject/issues/42")
    route.mock(return_value=httpx.Response(200, json={"id": 100, "labels": ["bug", "frontend"]}))
    result = await connector.write(ConnectorPayload(
        resource="issue_label",
        data={"project": "group/project", "iid": 42, "labels": ["bug", "frontend"]},
    ))
    assert "bug" in result["labels"]


# ---------------------------------------------------------------------------
# Write: label create
# ---------------------------------------------------------------------------


@respx.mock
async def test_write_label(connector):
    route = respx.post(f"{_API}/projects/group%2Fproject/labels")
    route.mock(return_value=httpx.Response(200, json={"id": 5, "name": "bug", "color": "#FF0000"}))
    result = await connector.write(ConnectorPayload(
        resource="label",
        data={"project": "group/project", "name": "bug", "color": "#FF0000", "description": "Bug label"},
    ))
    assert result["name"] == "bug"
    assert result["color"] == "#FF0000"


# ---------------------------------------------------------------------------
# Write: milestone create
# ---------------------------------------------------------------------------


@respx.mock
async def test_write_milestone(connector):
    route = respx.post(f"{_API}/projects/group%2Fproject/milestones")
    route.mock(return_value=httpx.Response(200, json={"id": 10, "title": "Sprint 1", "state": "active"}))
    result = await connector.write(ConnectorPayload(
        resource="milestone",
        data={"project": "group/project", "title": "Sprint 1", "description": "First sprint", "due_date": "2025-01-31"},
    ))
    assert result["title"] == "Sprint 1"


# ---------------------------------------------------------------------------
# Write: pipeline run
# ---------------------------------------------------------------------------


@respx.mock
async def test_write_pipeline_run(connector):
    route = respx.post(f"{_API}/projects/group%2Fproject/pipeline")
    route.mock(return_value=httpx.Response(200, json={"id": 99, "ref": "main", "status": "pending"}))
    result = await connector.write(ConnectorPayload(
        resource="pipeline_run",
        data={"project": "group/project", "ref": "main"},
    ))
    assert result["id"] == 99
    assert result["status"] == "pending"


@respx.mock
async def test_write_pipeline_run_with_variables(connector):
    route = respx.post(f"{_API}/projects/group%2Fproject/pipeline")
    route.mock(return_value=httpx.Response(200, json={"id": 100, "ref": "main"}))
    await connector.write(ConnectorPayload(
        resource="pipeline_run",
        data={
            "project": "group/project",
            "ref": "main",
            "variables": [{"key": "VAR", "value": "val"}],
        },
    ))


# ---------------------------------------------------------------------------
# Write: merge request (new alias)
# ---------------------------------------------------------------------------


@respx.mock
async def test_write_merge_request(connector):
    route = respx.post(f"{_API}/projects/group%2Fproject/merge_requests")
    route.mock(return_value=httpx.Response(200, json={"id": 50, "iid": 25, "title": "New MR", "state": "opened"}))
    result = await connector.write(ConnectorPayload(
        resource="merge_request",
        data={
            "project": "group/project",
            "title": "New MR",
            "source_branch": "feature",
            "target_branch": "main",
            "description": "Description",
        },
    ))
    assert result["iid"] == 25


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


async def test_unsupported_query_resource(connector):
    with pytest.raises(ValueError, match="Unsupported GitLab resource"):
        await connector.query(ConnectorQuery(resource="fork"))


async def test_unsupported_write_resource(connector):
    with pytest.raises(ValueError, match="Unsupported GitLab write resource"):
        await connector.write(ConnectorPayload(resource="fork", data={}))


@respx.mock
async def test_query_issues_api_error(connector):
    route = respx.get(f"{_API}/projects/group%2Fproject/issues")
    route.mock(return_value=httpx.Response(403, text="Forbidden"))
    with pytest.raises(httpx.HTTPStatusError):
        await connector.query(ConnectorQuery(resource="issues", filters={"project": "group/project"}))


@respx.mock
async def test_write_issue_api_error(connector):
    route = respx.post(f"{_API}/projects/group%2Fproject/issues")
    route.mock(return_value=httpx.Response(422, text="Validation failed"))
    with pytest.raises(httpx.HTTPStatusError):
        await connector.write(ConnectorPayload(
            resource="issue",
            data={"project": "group/project", "title": ""},
        ))
