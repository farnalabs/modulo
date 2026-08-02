"""Unit tests for GitLabConnector — HTTP responses are mocked via httpx."""

import json

import httpx
import pytest
import respx

from modulo.connectors.base import ConnectorPayload, ConnectorQuery, ConnectorType
from modulo.connectors.gitlab import GitLabConnector

TOKEN = "glpat_test_token"
_API = "https://gitlab.com/api/v4"


@pytest.fixture()
def connector():
    return GitLabConnector(token=TOKEN)


@respx.mock
async def test_health_check_ok(connector):
    respx.get(f"{_API}/user").mock(return_value=httpx.Response(200, json={"username": "myuser"}))
    respx.get(f"{_API}/projects").mock(return_value=httpx.Response(200, json=[{"id": 1}]))
    result = await connector.health_check()
    assert result.ok is True
    assert result.detail == "myuser"


@respx.mock
async def test_health_check_missing_scopes(connector):
    respx.get(f"{_API}/user").mock(return_value=httpx.Response(200, json={"username": "myuser"}))
    respx.get(f"{_API}/projects").mock(return_value=httpx.Response(403, text="forbidden"))
    result = await connector.health_check()
    assert result.ok is False
    assert "Missing scopes" in result.detail


@respx.mock
async def test_health_check_fail(connector):
    respx.get(f"{_API}/user").mock(return_value=httpx.Response(401, text="Unauthorized"))
    result = await connector.health_check()
    assert result.ok is False
    assert "401" in result.detail


@respx.mock
async def test_query_projects(connector):
    projects = [{"id": 1, "name": "proj-a"}, {"id": 2, "name": "proj-b"}]
    respx.get(f"{_API}/projects").mock(return_value=httpx.Response(200, json=projects))
    result = await connector.query(ConnectorQuery(resource="projects"))
    assert len(result.records) == 2
    assert result.records[0]["name"] == "proj-a"


@respx.mock
async def test_query_file(connector):
    file_data = {
        "file_name": "README.md",
        "content": "SGVsbG8gV29ybGQ=",
    }
    respx.get(f"{_API}/projects/group%2Fproject/repository/files/README.md").mock(
        return_value=httpx.Response(200, json=file_data)
    )
    result = await connector.query(
        ConnectorQuery(
            resource="file",
            filters={"project": "group/project", "path": "README.md", "ref": "main"},
        )
    )
    assert result.records[0]["content"] == "Hello World"


@respx.mock
async def test_query_mrs(connector):
    mrs = [{"id": 42, "title": "Fix bug"}]
    respx.get(f"{_API}/projects/group%2Fproject/merge_requests").mock(return_value=httpx.Response(200, json=mrs))
    result = await connector.query(ConnectorQuery(resource="mrs", filters={"project": "group/project"}))
    assert result.records[0]["id"] == 42


@respx.mock
async def test_write_file(connector):
    response_body = {"file_path": "src/main.py", "branch": "main"}
    route = respx.put(f"{_API}/projects/group%2Fproject/repository/files/src%2Fmain.py").mock(
        return_value=httpx.Response(200, json=response_body)
    )
    result = await connector.write(
        ConnectorPayload(
            resource="file",
            data={
                "project": "group/project",
                "path": "src/main.py",
                "content": "print('hello')",
                "message": "Update file",
            },
        )
    )
    assert result["file_path"] == "src/main.py"
    body = json.loads(route.calls.last.request.content)
    assert body["branch"] == "main"


@respx.mock
async def test_write_mr(connector):
    mr_response = {"id": 99, "web_url": "https://gitlab.com/group/project/-/merge_requests/99"}
    respx.post(f"{_API}/projects/group%2Fproject/merge_requests").mock(
        return_value=httpx.Response(200, json=mr_response)
    )
    result = await connector.write(
        ConnectorPayload(
            resource="mr",
            data={
                "project": "group/project",
                "title": "Add feature",
                "source_branch": "feature-branch",
                "target_branch": "main",
                "description": "Implements the feature",
            },
        )
    )
    assert result["id"] == 99


async def test_unsupported_query_resource(connector):
    with pytest.raises(ValueError, match="Unsupported GitLab resource"):
        await connector.query(ConnectorQuery(resource="unknown"))


async def test_unsupported_write_resource(connector):
    with pytest.raises(ValueError, match="Unsupported GitLab write resource"):
        await connector.write(ConnectorPayload(resource="branch", data={}))


@respx.mock
async def test_health_check_network_error(connector):
    respx.get(f"{_API}/user").mock(side_effect=httpx.ConnectError("Connection refused"))
    result = await connector.health_check()
    assert result.ok is False
    assert "Connection refused" in result.detail


@respx.mock
async def test_health_check_timeout(connector):
    respx.get(f"{_API}/user").mock(side_effect=httpx.TimeoutException("Request timed out"))
    result = await connector.health_check()
    assert result.ok is False
    assert "timed out" in result.detail.lower() or "Timeout" in result.detail


@respx.mock
async def test_query_missing_project_filter(connector):
    with pytest.raises(ValueError, match="Missing required filter"):
        await connector.query(ConnectorQuery(resource="file", filters={}))


@respx.mock
async def test_write_missing_project_data(connector):
    with pytest.raises(ValueError, match="Missing required filter"):
        await connector.write(ConnectorPayload(resource="file", data={}))


def test_connector_type(connector):
    assert connector.connector_type == ConnectorType.GITLAB


@respx.mock
async def test_query_projects_next_cursor(connector):
    respx.get(f"{_API}/projects").mock(
        return_value=httpx.Response(
            200,
            json=[{"id": 1}, {"id": 2}],
            headers={"X-Next-Page": "2"},
        )
    )
    result = await connector.query(ConnectorQuery(resource="projects"))
    assert result.next_cursor == "2"


@respx.mock
async def test_query_projects_no_next_page(connector):
    respx.get(f"{_API}/projects").mock(return_value=httpx.Response(200, json=[{"id": 1}], headers={"X-Next-Page": "0"}))
    result = await connector.query(ConnectorQuery(resource="projects"))
    assert result.next_cursor is None


@respx.mock
async def test_query_projects_passes_cursor_as_page(connector):
    route = respx.get(f"{_API}/projects").mock(
        return_value=httpx.Response(200, json=[{"id": 1}], headers={"X-Next-Page": "0"})
    )
    await connector.query(ConnectorQuery(resource="projects", cursor="3"))
    assert route.calls.last.request.url.params.get("page") == "3"


@respx.mock
async def test_query_mrs_next_cursor(connector):
    respx.get(f"{_API}/projects/group%2Fproject/merge_requests").mock(
        return_value=httpx.Response(
            200,
            json=[{"id": 42}],
            headers={"X-Next-Page": "4"},
        )
    )
    result = await connector.query(ConnectorQuery(resource="mrs", filters={"project": "group/project"}))
    assert result.next_cursor == "4"


@respx.mock
async def test_query_issues_next_cursor(connector):
    respx.get(f"{_API}/projects/group%2Fproject/issues").mock(
        return_value=httpx.Response(
            200,
            json=[{"id": 7}],
            headers={"X-Next-Page": "2"},
        )
    )
    result = await connector.query(ConnectorQuery(resource="issues", filters={"project": "group/project"}))
    assert result.next_cursor == "2"


@respx.mock
async def test_query_pipelines_next_cursor(connector):
    respx.get(f"{_API}/projects/group%2Fproject/pipelines").mock(
        return_value=httpx.Response(
            200,
            json=[{"id": 9}],
            headers={"X-Next-Page": "3"},
        )
    )
    result = await connector.query(ConnectorQuery(resource="pipelines", filters={"project": "group/project"}))
    assert result.next_cursor == "3"


@respx.mock
async def test_query_invalid_cursor_raises(connector):
    with pytest.raises(ValueError, match="Invalid GitLab pagination cursor"):
        await connector.query(ConnectorQuery(resource="projects", cursor="abc"))


@respx.mock
async def test_query_single_resource_no_next_cursor(connector):
    respx.get(f"{_API}/projects/group%2Fproject/repository/files/README.md").mock(
        return_value=httpx.Response(
            200,
            json={"content": "SGVsbG8="},
            headers={"X-Next-Page": "2"},
        )
    )
    result = await connector.query(
        ConnectorQuery(
            resource="file",
            filters={"project": "group/project", "path": "README.md"},
        )
    )
    assert result.next_cursor is None


@respx.mock
async def test_self_hosted_base_url():
    """Self-hosted GitLab instances must be reachable via configurable base_url."""
    custom = GitLabConnector(token=TOKEN, base_url="https://gitlab.example.com/api/v4")
    respx.get("https://gitlab.example.com/api/v4/user").mock(
        return_value=httpx.Response(200, json={"username": "selfhosted"})
    )
    respx.get("https://gitlab.example.com/api/v4/projects").mock(return_value=httpx.Response(200, json=[{"id": 1}]))
    result = await custom.health_check()
    assert result.ok is True
    assert result.detail == "selfhosted"


@respx.mock
async def test_self_hosted_base_url_trailing_slash():
    """base_url with a trailing slash must be normalised (rstrip)."""
    custom = GitLabConnector(token=TOKEN, base_url="https://gitlab.example.com/api/v4/")
    respx.get("https://gitlab.example.com/api/v4/user").mock(
        return_value=httpx.Response(200, json={"username": "selfhosted"})
    )
    respx.get("https://gitlab.example.com/api/v4/projects").mock(return_value=httpx.Response(200, json=[{"id": 1}]))
    result = await custom.health_check()
    assert result.ok is True


@respx.mock
async def test_default_base_url_unchanged(connector):
    """Default connector still targets the hosted GitLab endpoint."""
    respx.get(f"{_API}/user").mock(return_value=httpx.Response(200, json={"username": "myuser"}))
    respx.get(f"{_API}/projects").mock(return_value=httpx.Response(200, json=[{"id": 1}]))
    result = await connector.health_check()
    assert result.ok is True


@respx.mock
async def test_query_projects_rate_limit_metadata(connector):
    respx.get(f"{_API}/projects").mock(
        return_value=httpx.Response(
            200,
            json=[{"id": 1}],
            headers={
                "RateLimit-Limit": "600",
                "RateLimit-Remaining": "599",
                "RateLimit-Observed": "1",
                "RateLimit-Reset": "60",
                "RateLimit-ResetTime": "2026-08-02T10:40:00Z",
            },
        )
    )
    result = await connector.query(ConnectorQuery(resource="projects"))
    assert result.metadata["rate_limit"]["RateLimit-Limit"] == "600"
    assert result.metadata["rate_limit"]["RateLimit-Remaining"] == "599"
    assert result.metadata["rate_limit"]["RateLimit-Observed"] == "1"
    assert result.metadata["rate_limit"]["RateLimit-Reset"] == "60"
    assert result.metadata["rate_limit"]["RateLimit-ResetTime"] == "2026-08-02T10:40:00Z"


@respx.mock
async def test_query_no_rate_limit_headers_returns_empty(connector):
    respx.get(f"{_API}/projects").mock(return_value=httpx.Response(200, json=[{"id": 1}]))
    result = await connector.query(ConnectorQuery(resource="projects"))
    assert result.metadata["rate_limit"] == {}


@respx.mock
async def test_single_resource_metadata_rate_limit(connector):
    respx.get(f"{_API}/projects/group%2Fproject/repository/files/README.md").mock(
        return_value=httpx.Response(
            200,
            json={"content": "SGVsbG8="},
            headers={"RateLimit-Remaining": "42"},
        )
    )
    result = await connector.query(
        ConnectorQuery(resource="file", filters={"project": "group/project", "path": "README.md"})
    )
    assert result.metadata["rate_limit"]["RateLimit-Remaining"] == "42"


@respx.mock
async def test_self_hosted_base_url_query_routes(connector):
    base_url = "https://gitlab.example.com/api/v4"
    self_hosted = GitLabConnector(token=TOKEN, base_url=base_url)
    respx.get(f"{base_url}/projects").mock(return_value=httpx.Response(200, json=[{"id": 1}]))
    result = await self_hosted.query(ConnectorQuery(resource="projects"))
    assert result.records[0]["id"] == 1


def test_default_base_url_is_gitlab_com(connector):
    assert connector._base_url == _API

@respx.mock
async def test_write_file_delete(connector):
    """DELETE /repository/files/{path} returns status deleted on 204."""
    route = respx.delete(f"{_API}/projects/group%2Fproject/repository/files/src%2Fmain.py").mock(
        return_value=httpx.Response(204, text="")
    )
    result = await connector.write(
        ConnectorPayload(
            resource="file_delete",
            data={"project": "group/project", "path": "src/main.py", "ref": "main", "message": "Remove file"},
        )
    )
    assert result == {"status": "deleted"}
    assert route.calls.last.request.method == "DELETE"
    assert route.calls.last.request.url.params.get("branch") == "main"
    assert route.calls.last.request.url.params.get("ref") is None


@respx.mock
async def test_write_file_delete_with_sha_returns_body(connector):
    """DELETE with branch and sha params returns parsed body when present."""
    respx.delete(f"{_API}/projects/group%2Fproject/repository/files/src%2Fold.py").mock(
        return_value=httpx.Response(200, json={"file_path": "src/old.py", "branch": "main"})
    )
    result = await connector.write(
        ConnectorPayload(
            resource="file_delete",
            data={"project": "group/project", "path": "src/old.py", "branch": "main", "sha": "abc123"},
        )
    )
    assert result["file_path"] == "src/old.py"


@respx.mock
async def test_write_file_delete_defaults_ref(connector):
    route = respx.delete(f"{_API}/projects/group%2Fproject/repository/files/README.md").mock(
        return_value=httpx.Response(204, text="")
    )
    result = await connector.write(
        ConnectorPayload(resource="file_delete", data={"project": "group/project", "path": "README.md"})
    )
    assert result == {"status": "deleted"}
    assert route.calls.last.request.url.params.get("branch") == "main"


@respx.mock
async def test_write_file_delete_missing_project(connector):
    with pytest.raises(ValueError, match="Missing required filter"):
        await connector.write(ConnectorPayload(resource="file_delete", data={"path": "x"}))
    with pytest.raises(ValueError, match="Missing required filter"):
        await connector.write(ConnectorPayload(resource="file_delete", data={"project": "g/p"}))


@respx.mock
async def test_write_file_delete_error_response(connector):
    respx.delete(f"{_API}/projects/group%2Fproject/repository/files/README.md").mock(
        return_value=httpx.Response(400, text='{"message": "branch is missing"}')
    )
    with pytest.raises(ValueError, match="GitLab API HTTP 400"):
        await connector.write(
            ConnectorPayload(resource="file_delete", data={"project": "group/project", "path": "README.md"})
        )


@respx.mock
async def test_write_mr_note(connector):
    note_response = {"id": 123, "body": "Looks good"}
    route = respx.post(f"{_API}/projects/group%2Fproject/merge_requests/5/notes").mock(
        return_value=httpx.Response(200, json=note_response)
    )
    result = await connector.write(
        ConnectorPayload(
            resource="mr_note",
            data={"project": "group/project", "iid": "5", "body": "Looks good"},
        )
    )
    assert result["id"] == 123
    assert json.loads(route.calls.last.request.content) == {"body": "Looks good"}


@respx.mock
async def test_write_mr_merge(connector):
    merge_response = {"id": 5, "state": "merged"}
    route = respx.put(f"{_API}/projects/group%2Fproject/merge_requests/5/merge").mock(
        return_value=httpx.Response(200, json=merge_response)
    )
    result = await connector.write(
        ConnectorPayload(
            resource="mr_merge",
            data={"project": "group/project", "iid": "5", "squash": True},
        )
    )
    assert result["state"] == "merged"
    assert json.loads(route.calls.last.request.content) == {"squash": True}


@respx.mock
async def test_write_mr_approve(connector):
    """POST /merge_requests/{iid}/approve."""
    respx.post(f"{_API}/projects/group%2Fproject/merge_requests/7/approve").mock(
        return_value=httpx.Response(200, json={"approved": True})
    )
    result = await connector.write(
        ConnectorPayload(
            resource="mr_approve",
            data={"project": "group/project", "iid": "7"},
        )
    )
    assert result["approved"] is True


@respx.mock
async def test_write_mr_comment(connector):
    """POST /merge_requests/{iid}/notes with body."""
    respx.post(f"{_API}/projects/group%2Fproject/merge_requests/7/notes").mock(
        return_value=httpx.Response(200, json={"id": 500, "body": "LGTM"})
    )
    result = await connector.write(
        ConnectorPayload(
            resource="mr_comment",
            data={"project": "group/project", "iid": "7", "body": "LGTM"},
        )
    )
    assert result["id"] == 500


@respx.mock
async def test_write_mr_labels(connector):
    labels_response = {"id": 5, "labels": ["review", "backend"]}
    route = respx.put(f"{_API}/projects/group%2Fproject/merge_requests/5").mock(
        return_value=httpx.Response(200, json=labels_response)
    )
    result = await connector.write(
        ConnectorPayload(
            resource="mr_labels",
            data={"project": "group/project", "iid": "5", "labels": ["review", "backend"]},
        )
    )
    assert result["labels"] == ["review", "backend"]
    assert json.loads(route.calls.last.request.content) == {"labels": ["review", "backend"]}


@respx.mock
async def test_write_file_delete_missing_branch_defaults_main(connector):
    """file_delete without branch defaults to main."""
    respx.delete(f"{_API}/projects/group%2Fproject/repository/files/README.md").mock(
        return_value=httpx.Response(200, json={"file_path": "README.md", "branch": "main"})
    )
    await connector.write(
        ConnectorPayload(resource="file_delete", data={"project": "group/project", "path": "README.md"})
    )
    request = respx.calls.last.request
    assert request.url.params.get("branch") == "main"


@respx.mock
async def test_write_mr_merge_missing_iid(connector):
    with pytest.raises(ValueError, match="Missing required filter"):
        await connector.write(ConnectorPayload(resource="mr_merge", data={"project": "group/project"}))