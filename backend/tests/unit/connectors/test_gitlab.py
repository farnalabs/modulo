"""Unit tests for GitLabConnector — HTTP responses are mocked via httpx."""

import json

import httpx
import pytest
import respx

from modulo.connectors.base import ConnectorPayload, ConnectorQuery, ConnectorType
from modulo.connectors.gitlab import GitLabConnector

TOKEN = "glpat_test_token"
_API = "https://gitlab.com/api/v4"
_TOKEN_INFO = "https://gitlab.com/oauth/token/info"
_SELF_TOKEN_INFO = "https://gitlab.example.com/oauth/token/info"
_FULL_SCOPES = {"scope": ["read_api", "write_repository", "api"]}


@pytest.fixture
def connector():
    return GitLabConnector(token=TOKEN)


@respx.mock
async def test_health_check_ok(connector):
    respx.get(f"{_API}/user").mock(return_value=httpx.Response(200, json={"username": "myuser"}))
    respx.get(f"{_API}/projects").mock(return_value=httpx.Response(200, json=[{"id": 1}]))
    respx.get(_TOKEN_INFO).mock(return_value=httpx.Response(200, json=_FULL_SCOPES))
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
    query = ConnectorQuery(resource="unknown")
    with pytest.raises(ValueError, match="Unsupported GitLab resource"):
        await connector.query(query)


async def test_unsupported_write_resource(connector):
    payload = ConnectorPayload(resource="branch", data={})
    with pytest.raises(ValueError, match="Unsupported GitLab write resource"):
        await connector.write(payload)


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
    query = ConnectorQuery(resource="file", filters={})
    with pytest.raises(ValueError, match="Missing required filter"):
        await connector.query(query)


@respx.mock
async def test_write_missing_project_data(connector):
    payload = ConnectorPayload(resource="file", data={})
    with pytest.raises(ValueError, match="Missing required filter"):
        await connector.write(payload)


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
    assert not result.metadata["rate_limit"]


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
async def test_self_hosted_base_url():
    """Self-hosted GitLab instances must be reachable via configurable base_url."""
    custom = GitLabConnector(token=TOKEN, base_url="https://gitlab.example.com/api/v4")
    respx.get("https://gitlab.example.com/api/v4/user").mock(
        return_value=httpx.Response(200, json={"username": "selfhosted"})
    )
    respx.get("https://gitlab.example.com/api/v4/projects").mock(return_value=httpx.Response(200, json=[{"id": 1}]))
    respx.get("https://gitlab.example.com/api/v4/version").mock(
        return_value=httpx.Response(200, json={"version": "17.5.0", "revision": "abc123"})
    )
    respx.get(_SELF_TOKEN_INFO).mock(return_value=httpx.Response(200, json=_FULL_SCOPES))
    result = await custom.health_check()
    assert result.ok is True
    assert result.detail == "selfhosted (GitLab 17.5.0)"


@respx.mock
async def test_self_hosted_base_url_trailing_slash():
    """base_url with a trailing slash must be normalised (rstrip)."""
    custom = GitLabConnector(token=TOKEN, base_url="https://gitlab.example.com/api/v4/")
    respx.get("https://gitlab.example.com/api/v4/user").mock(
        return_value=httpx.Response(200, json={"username": "selfhosted"})
    )
    respx.get("https://gitlab.example.com/api/v4/projects").mock(return_value=httpx.Response(200, json=[{"id": 1}]))
    respx.get("https://gitlab.example.com/api/v4/version").mock(
        return_value=httpx.Response(200, json={"version": "17.5.0", "revision": "abc123"})
    )
    respx.get(_SELF_TOKEN_INFO).mock(return_value=httpx.Response(200, json=_FULL_SCOPES))
    result = await custom.health_check()
    assert result.ok is True


@respx.mock
async def test_self_hosted_base_url_query_routes(connector):
    base_url = "https://gitlab.example.com/api/v4"
    self_hosted = GitLabConnector(token=TOKEN, base_url=base_url)
    respx.get(f"{base_url}/projects").mock(return_value=httpx.Response(200, json=[{"id": 1}]))
    result = await self_hosted.query(ConnectorQuery(resource="projects"))
    assert result.records[0]["id"] == 1


@respx.mock
async def test_default_base_url_unchanged(connector):
    """Default connector still targets the hosted GitLab endpoint."""
    respx.get(f"{_API}/user").mock(return_value=httpx.Response(200, json={"username": "myuser"}))
    respx.get(f"{_API}/projects").mock(return_value=httpx.Response(200, json=[{"id": 1}]))
    respx.get(_TOKEN_INFO).mock(return_value=httpx.Response(200, json=_FULL_SCOPES))
    result = await connector.health_check()
    assert result.ok is True


def test_default_base_url_is_gitlab_com(connector):
    assert connector._base_url == _API


@respx.mock
async def test_write_file_delete(connector):
    """DELETE /repository/files/{path} with branch, sha, and commit_message."""
    route = respx.delete(f"{_API}/projects/group%2Fproject/repository/files/src%2Fold.py").mock(
        return_value=httpx.Response(200, json={"file_path": "src/old.py", "branch": "main"})
    )
    result = await connector.write(
        ConnectorPayload(
            resource="file_delete",
            data={
                "project": "group/project",
                "path": "src/old.py",
                "ref": "main",
                "sha": "abc123",
                "message": "Remove file",
            },
        )
    )
    assert result["file_path"] == "src/old.py"
    assert route.calls.last.request.method == "DELETE"
    assert route.calls.last.request.url.params.get("branch") == "main"
    assert json.loads(route.calls.last.request.content) == {"commit_message": "Remove file"}


@respx.mock
async def test_write_file_delete_defaults_ref(connector):
    route = respx.delete(f"{_API}/projects/group%2Fproject/repository/files/README.md").mock(
        return_value=httpx.Response(200, json={"file_path": "README.md", "branch": "main"})
    )
    result = await connector.write(
        ConnectorPayload(resource="file_delete", data={"project": "group/project", "path": "README.md"})
    )
    assert result["file_path"] == "README.md"
    assert route.calls.last.request.url.params.get("branch") == "main"


@respx.mock
async def test_write_file_delete_missing_project(connector):
    payload = ConnectorPayload(resource="file_delete", data={"path": "x"})
    with pytest.raises(ValueError, match="Missing required filter"):
        await connector.write(payload)
    payload = ConnectorPayload(resource="file_delete", data={"project": "g/p"})
    with pytest.raises(ValueError, match="Missing required filter"):
        await connector.write(payload)


@respx.mock
async def test_write_file_delete_error_response(connector):
    respx.delete(f"{_API}/projects/group%2Fproject/repository/files/README.md").mock(
        return_value=httpx.Response(400, text='{"message": "branch is missing"}')
    )
    payload = ConnectorPayload(resource="file_delete", data={"project": "group/project", "path": "README.md"})
    with pytest.raises(ValueError, match="GitLab API HTTP 400"):
        await connector.write(payload)


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
    """PUT /merge_requests/{iid}/merge with squash option."""
    route = respx.put(f"{_API}/projects/group%2Fproject/merge_requests/7/merge").mock(
        return_value=httpx.Response(200, json={"id": 99, "state": "merged"})
    )
    result = await connector.write(
        ConnectorPayload(
            resource="mr_merge",
            data={"project": "group/project", "iid": "7", "squash": True},
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
    payload = ConnectorPayload(resource="mr_merge", data={"project": "group/project"})
    with pytest.raises(ValueError, match="Missing required filter"):
        await connector.write(payload)


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
async def test_query_mr_changes(connector):
    """GET /merge_requests/{iid}/changes returns the MR diff and changed files."""
    changes_response = {
        "id": 50,
        "iid": 5,
        "title": "Fix bug",
        "changes": [
            {"old_path": "src/old.py", "new_path": "src/new.py", "new_file": False, "diff": "@@ -1 +1 @@"},
            {"old_path": "README.md", "new_path": "README.md", "new_file": True, "diff": "@@ -0,0 +1 @@"},
        ],
    }
    respx.get(f"{_API}/projects/group%2Fproject/merge_requests/5/changes").mock(
        return_value=httpx.Response(200, json=changes_response)
    )
    result = await connector.query(
        ConnectorQuery(resource="mr_changes", filters={"project": "group/project", "iid": "5"})
    )
    assert len(result.records) == 1
    changes = result.records[0]["changes"]
    assert len(changes) == 2
    assert changes[0]["old_path"] == "src/old.py"
    assert result.records[0]["title"] == "Fix bug"


@respx.mock
async def test_query_mr_changes_missing_iid(connector):
    with pytest.raises(ValueError, match="Missing required filter"):
        await connector.query(ConnectorQuery(resource="mr_changes", filters={"project": "group/project"}))


@respx.mock
async def test_query_file_path_traversal_blocked(connector):
    """A path with a '..' segment must be rejected before any request is sent."""
    with pytest.raises(ValueError, match="path traversal"):
        await connector.query(
            ConnectorQuery(resource="file", filters={"project": "group/project", "path": "../secret.txt"})
        )


@respx.mock
async def test_write_file_path_traversal_blocked(connector):
    with pytest.raises(ValueError, match="path traversal"):
        await connector.write(
            ConnectorPayload(
                resource="file",
                data={"project": "group/project", "path": "src/../../etc/passwd", "content": "x"},
            )
        )


@respx.mock
async def test_write_file_delete_path_traversal_blocked(connector):
    with pytest.raises(ValueError, match="path traversal"):
        await connector.write(
            ConnectorPayload(resource="file_delete", data={"project": "group/project", "path": "../../evil.txt"})
        )


@respx.mock
async def test_write_file_absolute_path_rejected(connector):
    """Absolute paths must be rejected — repository files are relative."""
    with pytest.raises(ValueError, match="must be relative"):
        await connector.write(
            ConnectorPayload(
                resource="file",
                data={"project": "group/project", "path": "/etc/passwd", "content": "x"},
            )
        )


@respx.mock
async def test_query_file_nested_relative_path_allowed(connector):
    """Nested relative paths remain valid."""
    respx.get(f"{_API}/projects/group%2Fproject/repository/files/src%2Fmain.py").mock(
        return_value=httpx.Response(200, json={"file_name": "main.py", "content": "cHJpbnQoJ2hpJyk="})
    )
    result = await connector.query(
        ConnectorQuery(resource="file", filters={"project": "group/project", "path": "src/main.py"})
    )
    assert result.records[0]["content"] == "print('hi')"


@respx.mock
async def test_query_tree(connector):
    """Repository tree listing returns entries with name/type/path."""
    entries = [
        {"id": "a1", "name": "README.md", "type": "blob", "path": "README.md"},
        {"id": "a2", "name": "src", "type": "tree", "path": "src"},
    ]
    route = respx.get(f"{_API}/projects/group%2Fproject/repository/tree").mock(
        return_value=httpx.Response(200, json=entries)
    )
    result = await connector.query(ConnectorQuery(resource="tree", filters={"project": "group/project"}))
    assert len(result.records) == 2
    assert result.records[0]["type"] == "blob"
    assert route.calls.last.request.url.params.get("recursive") is None


@respx.mock
async def test_query_tree_recursive_with_path_and_ref(connector):
    """recursive + path + ref filters forwarded to the tree endpoint."""
    route = respx.get(f"{_API}/projects/group%2Fproject/repository/tree").mock(
        return_value=httpx.Response(200, json=[{"name": "main.py", "type": "blob", "path": "src/main.py"}])
    )
    result = await connector.query(
        ConnectorQuery(
            resource="tree",
            filters={"project": "group/project", "path": "src", "ref": "dev", "recursive": True},
        )
    )
    assert result.records[0]["path"] == "src/main.py"
    url = route.calls.last.request.url
    assert url.params.get("recursive") == "true"
    assert url.params.get("path") == "src"
    assert url.params.get("ref") == "dev"


@respx.mock
async def test_query_tree_next_cursor(connector):
    respx.get(f"{_API}/projects/group%2Fproject/repository/tree").mock(
        return_value=httpx.Response(
            200,
            json=[{"name": "a", "type": "blob", "path": "a"}],
            headers={"X-Next-Page": "2"},
        )
    )
    result = await connector.query(ConnectorQuery(resource="tree", filters={"project": "group/project"}))
    assert result.next_cursor == "2"


@respx.mock
async def test_query_tree_path_traversal_blocked(connector):
    with pytest.raises(ValueError, match="path traversal"):
        await connector.query(ConnectorQuery(resource="tree", filters={"project": "group/project", "path": "../src"}))


@respx.mock
async def test_query_tree_missing_project(connector):
    with pytest.raises(ValueError, match="Missing required filter"):
        await connector.query(ConnectorQuery(resource="tree", filters={}))


@respx.mock
async def test_write_files_batch_commit(connector):
    """Batch file ops go through the Commits API as one atomic commit."""
    commit_response = {"id": "abc123", "short_id": "abc123", "title": "Update via Modulo"}
    route = respx.post(f"{_API}/projects/group%2Fproject/repository/commits").mock(
        return_value=httpx.Response(201, json=commit_response)
    )
    result = await connector.write(
        ConnectorPayload(
            resource="files",
            data={
                "project": "group/project",
                "actions": [
                    {"action": "create", "file_path": "src/a.py", "content": "print(1)"},
                    {"action": "update", "file_path": "src/b.py", "content": "print(2)"},
                    {"action": "delete", "file_path": "src/old.py"},
                ],
            },
        )
    )
    assert result["id"] == "abc123"
    body = json.loads(route.calls.last.request.content)
    assert body["branch"] == "main"
    assert body["commit_message"] == "Update via Modulo"
    assert body["actions"] == [
        {"action": "create", "file_path": "src/a.py", "content": "print(1)"},
        {"action": "update", "file_path": "src/b.py", "content": "print(2)"},
        {"action": "delete", "file_path": "src/old.py"},
    ]


@respx.mock
async def test_write_files_custom_branch_message(connector):
    route = respx.post(f"{_API}/projects/group%2Fproject/repository/commits").mock(
        return_value=httpx.Response(201, json={"id": "x"})
    )
    await connector.write(
        ConnectorPayload(
            resource="files",
            data={
                "project": "group/project",
                "ref": "feature",
                "message": "Bulk change",
                "actions": [{"action": "create", "file_path": "docs/a.md", "content": "hi"}],
            },
        )
    )
    body = json.loads(route.calls.last.request.content)
    assert body["branch"] == "feature"
    assert body["commit_message"] == "Bulk change"


@respx.mock
async def test_write_files_move_action(connector):
    route = respx.post(f"{_API}/projects/group%2Fproject/repository/commits").mock(
        return_value=httpx.Response(201, json={"id": "y"})
    )
    await connector.write(
        ConnectorPayload(
            resource="files",
            data={
                "project": "group/project",
                "actions": [{"action": "move", "file_path": "src/new.py", "previous_path": "src/old.py"}],
            },
        )
    )
    body = json.loads(route.calls.last.request.content)
    assert body["actions"] == [
        {"action": "move", "file_path": "src/new.py", "previous_path": "src/old.py"},
    ]


@respx.mock
async def test_write_files_empty_actions(connector):
    with pytest.raises(ValueError, match="non-empty 'actions'"):
        await connector.write(ConnectorPayload(resource="files", data={"project": "group/project", "actions": []}))
    with pytest.raises(ValueError, match="Missing required filter"):
        await connector.write(ConnectorPayload(resource="files", data={"project": "group/project"}))


@respx.mock
async def test_write_files_invalid_action_type(connector):
    with pytest.raises(ValueError, match="must be one of"):
        await connector.write(
            ConnectorPayload(
                resource="files",
                data={"project": "group/project", "actions": [{"action": "explode", "file_path": "a"}]},
            )
        )


@respx.mock
async def test_write_files_missing_file_path(connector):
    with pytest.raises(ValueError, match="requires 'file_path'"):
        await connector.write(
            ConnectorPayload(resource="files", data={"project": "group/project", "actions": [{"action": "create"}]})
        )


@respx.mock
async def test_write_files_move_requires_previous_path(connector):
    with pytest.raises(ValueError, match="requires 'previous_path'"):
        await connector.write(
            ConnectorPayload(
                resource="files",
                data={"project": "group/project", "actions": [{"action": "move", "file_path": "new.py"}]},
            )
        )


@respx.mock
async def test_write_files_path_traversal_blocked(connector):
    with pytest.raises(ValueError, match="path traversal"):
        await connector.write(
            ConnectorPayload(
                resource="files",
                data={"project": "group/project", "actions": [{"action": "create", "file_path": "../evil.txt"}]},
            )
        )


@respx.mock
async def test_write_files_move_previous_path_traversal_blocked(connector):
    with pytest.raises(ValueError, match="path traversal"):
        await connector.write(
            ConnectorPayload(
                resource="files",
                data={
                    "project": "group/project",
                    "actions": [{"action": "move", "file_path": "ok.py", "previous_path": "../evil.py"}],
                },
            )
        )


@respx.mock
async def test_write_mr_approval_request(connector):
    """POST /approval_rules creates a rule requesting approval from specific users."""
    rule_response = {"id": 3, "name": "Requested approvers", "rule_type": "approval", "approvals_required": 1}
    route = respx.post(f"{_API}/projects/group%2Fproject/merge_requests/7/approval_rules").mock(
        return_value=httpx.Response(201, json=rule_response)
    )
    result = await connector.write(
        ConnectorPayload(
            resource="mr_approval_request",
            data={"project": "group/project", "iid": "7", "user_ids": [10, 11]},
        )
    )
    assert result["id"] == 3
    body = json.loads(route.calls.last.request.content)
    assert body["rule_type"] == "approval"
    assert body["user_ids"] == [10, 11]
    assert body["approvals_required"] == 1


@respx.mock
async def test_write_mr_approval_request_by_email(connector):
    route = respx.post(f"{_API}/projects/group%2Fproject/merge_requests/7/approval_rules").mock(
        return_value=httpx.Response(201, json={"id": 4, "name": "Requested approvers"})
    )
    result = await connector.write(
        ConnectorPayload(
            resource="mr_approval_request",
            data={
                "project": "group/project",
                "iid": "7",
                "user_emails": ["alice@example.com"],
                "name": "Review team",
                "approvals_required": 2,
            },
        )
    )
    assert result["id"] == 4
    body = json.loads(route.calls.last.request.content)
    assert body["user_emails"] == ["alice@example.com"]
    assert body["name"] == "Review team"
    assert body["approvals_required"] == 2


@respx.mock
async def test_write_mr_approval_request_requires_users(connector):
    with pytest.raises(ValueError, match="requires 'user_ids' and/or 'user_emails'"):
        await connector.write(
            ConnectorPayload(resource="mr_approval_request", data={"project": "group/project", "iid": "7"})
        )


@respx.mock
async def test_write_mr_approval_request_missing_project(connector):
    with pytest.raises(ValueError, match="Missing required filter"):
        await connector.write(ConnectorPayload(resource="mr_approval_request", data={"iid": "7", "user_ids": [1]}))


@respx.mock
async def test_health_check_reports_missing_write_repository_scope(connector):
    """A token without write_repository/api scopes must fail health with the scopes listed."""
    respx.get(f"{_API}/user").mock(return_value=httpx.Response(200, json={"username": "myuser"}))
    respx.get(f"{_API}/projects").mock(return_value=httpx.Response(200, json=[{"id": 1}]))
    respx.get(_TOKEN_INFO).mock(return_value=httpx.Response(200, json={"scope": ["read_api"]}))
    result = await connector.health_check()
    assert result.ok is False
    assert "write_repository" in result.detail
    assert "api" in result.detail
    assert "Missing scopes" in result.detail


@respx.mock
async def test_health_check_reports_missing_api_scope(connector):
    """read_api + write_repository without api must fail health (issue/MR writes need api)."""
    respx.get(f"{_API}/user").mock(return_value=httpx.Response(200, json={"username": "myuser"}))
    respx.get(f"{_API}/projects").mock(return_value=httpx.Response(200, json=[{"id": 1}]))
    respx.get(_TOKEN_INFO).mock(return_value=httpx.Response(200, json={"scope": ["read_api", "write_repository"]}))
    result = await connector.health_check()
    assert result.ok is False
    assert result.detail.startswith("Missing scopes: api")
    assert "write_repository" not in result.detail.split("Required:")[0]


@respx.mock
async def test_health_check_api_scope_satisfies_all_required(connector):
    """Declaring api alone satisfies read_api + write_repository (superset)."""
    respx.get(f"{_API}/user").mock(return_value=httpx.Response(200, json={"username": "myuser"}))
    respx.get(f"{_API}/projects").mock(return_value=httpx.Response(200, json=[{"id": 1}]))
    respx.get(_TOKEN_INFO).mock(return_value=httpx.Response(200, json={"scope": ["api"]}))
    result = await connector.health_check()
    assert result.ok is True
    assert result.detail == "myuser"


@respx.mock
async def test_health_check_token_info_unavailable_non_fatal(connector):
    """A 404 from /oauth/token/info (old self-hosted) must not fail health."""
    respx.get(f"{_API}/user").mock(return_value=httpx.Response(200, json={"username": "myuser"}))
    respx.get(f"{_API}/projects").mock(return_value=httpx.Response(200, json=[{"id": 1}]))
    respx.get(_TOKEN_INFO).mock(return_value=httpx.Response(404, text="Not Found"))
    result = await connector.health_check()
    assert result.ok is True
    assert result.detail == "myuser"


@respx.mock
async def test_health_check_token_info_network_error_non_fatal(connector):
    """A network error on /oauth/token/info must not fail health (best-effort probe)."""
    respx.get(f"{_API}/user").mock(return_value=httpx.Response(200, json={"username": "myuser"}))
    respx.get(f"{_API}/projects").mock(return_value=httpx.Response(200, json=[{"id": 1}]))
    respx.get(_TOKEN_INFO).mock(side_effect=httpx.ConnectError("Connection refused"))
    result = await connector.health_check()
    assert result.ok is True
    assert result.detail == "myuser"


@respx.mock
async def test_health_check_token_info_invalid_body_non_fatal(connector):
    """A non-object/invalid JSON body from /oauth/token/info must not fail health."""
    respx.get(f"{_API}/user").mock(return_value=httpx.Response(200, json={"username": "myuser"}))
    respx.get(f"{_API}/projects").mock(return_value=httpx.Response(200, json=[{"id": 1}]))
    respx.get(_TOKEN_INFO).mock(return_value=httpx.Response(200, text="not json"))
    result = await connector.health_check()
    assert result.ok is True
    assert result.detail == "myuser"


@respx.mock
async def test_health_check_token_info_empty_scopes_non_fatal(connector):
    """An empty scope list from /oauth/token/info is treated as unknown, not failing."""
    respx.get(f"{_API}/user").mock(return_value=httpx.Response(200, json={"username": "myuser"}))
    respx.get(f"{_API}/projects").mock(return_value=httpx.Response(200, json=[{"id": 1}]))
    respx.get(_TOKEN_INFO).mock(return_value=httpx.Response(200, json={"scope": []}))
    result = await connector.health_check()
    assert result.ok is True


@respx.mock
async def test_health_check_self_hosted_scope_probe_uses_instance_root(connector):
    """Self-hosted scope probe must hit the instance root, outside /api/v4."""
    custom = GitLabConnector(token=TOKEN, base_url="https://gitlab.example.com/api/v4")
    respx.get("https://gitlab.example.com/api/v4/user").mock(
        return_value=httpx.Response(200, json={"username": "selfhosted"})
    )
    respx.get("https://gitlab.example.com/api/v4/projects").mock(return_value=httpx.Response(200, json=[{"id": 1}]))
    respx.get("https://gitlab.example.com/api/v4/version").mock(
        return_value=httpx.Response(200, json={"version": "17.5.0"})
    )
    route = respx.get(_SELF_TOKEN_INFO).mock(return_value=httpx.Response(200, json={"scope": ["read_api"]}))
    result = await custom.health_check()
    assert route.called
    assert result.ok is False
    assert "write_repository" in result.detail


@respx.mock
async def test_write_file_blocked_without_write_repository_scope(connector):
    """A read_api-only token must be blocked before a repository-file write reaches the API."""
    respx.get(_TOKEN_INFO).mock(return_value=httpx.Response(200, json={"scope": ["read_api"]}))
    write_route = respx.put(f"{_API}/projects/group%2Fproject/repository/files/src%2Fmain.py")
    with pytest.raises(ValueError, match="requires scope 'write_repository'"):
        await connector.write(
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
    assert not write_route.called


@respx.mock
async def test_write_file_blocked_error_lists_declared_scopes(connector):
    """The scope error must surface exactly which scopes the token declares."""
    respx.get(_TOKEN_INFO).mock(return_value=httpx.Response(200, json={"scope": ["read_api"]}))
    with pytest.raises(ValueError, match="token declares: read_api"):
        await connector.write(
            ConnectorPayload(
                resource="file",
                data={"project": "group/project", "path": "src/main.py", "content": "x", "message": "m"},
            )
        )


@respx.mock
async def test_write_mr_blocked_without_api_scope(connector):
    """write_repository without api must not permit MR creation (MRs need the api scope)."""
    respx.get(_TOKEN_INFO).mock(return_value=httpx.Response(200, json={"scope": ["read_api", "write_repository"]}))
    mr_route = respx.post(f"{_API}/projects/group%2Fproject/merge_requests")
    with pytest.raises(ValueError, match="requires scope 'api'"):
        await connector.write(
            ConnectorPayload(
                resource="mr",
                data={"project": "group/project", "title": "Add feature", "source_branch": "feature-branch"},
            )
        )
    assert not mr_route.called


@respx.mock
async def test_write_file_allowed_with_write_repository_scope(connector):
    """write_repository satisfies repository-file writes without the api scope."""
    respx.get(_TOKEN_INFO).mock(return_value=httpx.Response(200, json={"scope": ["read_api", "write_repository"]}))
    response_body = {"file_path": "src/main.py", "branch": "main"}
    write_route = respx.put(f"{_API}/projects/group%2Fproject/repository/files/src%2Fmain.py").mock(
        return_value=httpx.Response(200, json=response_body)
    )
    result = await connector.write(
        ConnectorPayload(
            resource="file",
            data={"project": "group/project", "path": "src/main.py", "content": "print('hello')", "message": "m"},
        )
    )
    assert write_route.called
    assert result["file_path"] == "src/main.py"


@respx.mock
async def test_write_allowed_with_api_scope(connector):
    """The api scope satisfies both repository-file and non-file writes (superset)."""
    respx.get(_TOKEN_INFO).mock(return_value=httpx.Response(200, json={"scope": ["api"]}))
    response_body = {"id": 99, "web_url": "https://gitlab.com/group/project/-/merge_requests/99"}
    mr_route = respx.post(f"{_API}/projects/group%2Fproject/merge_requests").mock(
        return_value=httpx.Response(200, json=response_body)
    )
    result = await connector.write(
        ConnectorPayload(
            resource="mr",
            data={"project": "group/project", "title": "Add feature", "source_branch": "feature-branch"},
        )
    )
    assert mr_route.called
    assert result["id"] == 99


@respx.mock
async def test_write_proceeds_when_scopes_unavailable(connector):
    """An unavailable token-info endpoint (old self-hosted) must not block writes."""
    respx.get(_TOKEN_INFO).mock(return_value=httpx.Response(404, text="Not Found"))
    response_body = {"file_path": "src/main.py", "branch": "main"}
    write_route = respx.put(f"{_API}/projects/group%2Fproject/repository/files/src%2Fmain.py").mock(
        return_value=httpx.Response(200, json=response_body)
    )
    result = await connector.write(
        ConnectorPayload(
            resource="file",
            data={"project": "group/project", "path": "src/main.py", "content": "print('hello')", "message": "m"},
        )
    )
    assert write_route.called
    assert result["file_path"] == "src/main.py"


@respx.mock
async def test_write_proceeds_when_token_info_network_error(connector):
    """A network error probing scopes must degrade to allow, not block the write."""
    respx.get(_TOKEN_INFO).mock(side_effect=httpx.ConnectError("Connection refused"))
    write_route = respx.put(f"{_API}/projects/group%2Fproject/repository/files/src%2Fmain.py").mock(
        return_value=httpx.Response(200, json={"file_path": "src/main.py", "branch": "main"})
    )
    await connector.write(
        ConnectorPayload(
            resource="file",
            data={"project": "group/project", "path": "src/main.py", "content": "x", "message": "m"},
        )
    )
    assert write_route.called


@respx.mock
async def test_verify_write_scopes_reports_missing(connector):
    """verify_write_scopes returns exactly the scopes a resource lacks."""
    respx.get(_TOKEN_INFO).mock(return_value=httpx.Response(200, json={"scope": ["read_api"]}))
    assert await connector.verify_write_scopes("file") == frozenset({"write_repository"})
    assert await connector.verify_write_scopes("issue") == frozenset({"api"})
    assert not await connector.verify_write_scopes("unknown_resource")


@respx.mock
async def test_verify_write_scopes_empty_when_satisfied_or_unknown(connector):
    """verify_write_scopes returns empty when the token has the scope or it can't be probed."""
    respx.get(_TOKEN_INFO).mock(return_value=httpx.Response(200, json={"scope": ["api"]}))
    assert not await connector.verify_write_scopes("file")
    assert not await connector.verify_write_scopes("mr_merge")
    respx.get(_TOKEN_INFO).mock(return_value=httpx.Response(404, text="Not Found"))
    assert not await connector.verify_write_scopes("file")


@respx.mock
async def test_write_scope_cache_avoids_reprobe(connector):
    """Declared scopes are cached so consecutive writes don't re-hit token-info."""
    token_info_route = respx.get(_TOKEN_INFO).mock(return_value=httpx.Response(200, json={"scope": ["api"]}))
    write_route = respx.put(f"{_API}/projects/group%2Fproject/repository/files/src%2Fmain.py").mock(
        return_value=httpx.Response(200, json={"file_path": "src/main.py", "branch": "main"})
    )
    payload = ConnectorPayload(
        resource="file",
        data={"project": "group/project", "path": "src/main.py", "content": "x", "message": "m"},
    )
    await connector.write(payload)
    await connector.write(payload)
    assert write_route.called
    assert len(token_info_route.calls) == 1


@respx.mock
async def test_health_check_warms_write_scope_cache(connector):
    """A successful health check caches scopes so writes verify without re-probing."""
    respx.get(f"{_API}/user").mock(return_value=httpx.Response(200, json={"username": "myuser"}))
    respx.get(f"{_API}/projects").mock(return_value=httpx.Response(200, json=[{"id": 1}]))
    token_info_route = respx.get(_TOKEN_INFO).mock(return_value=httpx.Response(200, json={"scope": ["api"]}))
    result = await connector.health_check()
    assert result.ok is True
    write_route = respx.put(f"{_API}/projects/group%2Fproject/repository/files/src%2Fmain.py").mock(
        return_value=httpx.Response(200, json={"file_path": "src/main.py", "branch": "main"})
    )
    await connector.write(
        ConnectorPayload(
            resource="file",
            data={"project": "group/project", "path": "src/main.py", "content": "x", "message": "m"},
        )
    )
    assert write_route.called
    assert len(token_info_route.calls) == 1


def test_constructor_rejects_missing_token():
    """Construction with a None token fails fast instead of at first API call."""
    with pytest.raises(ValueError, match="non-empty token"):
        GitLabConnector(token=None)  # type: ignore[arg-type]


def test_constructor_rejects_empty_token():
    with pytest.raises(ValueError, match="non-empty token"):
        GitLabConnector(token="")


def test_constructor_rejects_whitespace_token():
    with pytest.raises(ValueError, match="non-empty token"):
        GitLabConnector(token="   ")


@respx.mock
async def test_query_file_numeric_project_id_coerced(connector):
    """A numeric project ID is coerced to a string and routed correctly."""
    route = respx.get(f"{_API}/projects/123/repository/files/README.md").mock(
        return_value=httpx.Response(200, json={"file_name": "README.md", "content": "SGVsbG8="})
    )
    result = await connector.query(
        ConnectorQuery(resource="file", filters={"project": 123, "path": "README.md", "ref": "main"})
    )
    assert result.records[0]["content"] == "Hello"
    assert route.called


@respx.mock
async def test_query_file_none_project_raises_descriptive_error(connector):
    """A None project filter fails gracefully instead of crashing in quote()."""
    with pytest.raises(ValueError, match="project ID or path"):
        await connector.query(ConnectorQuery(resource="file", filters={"project": None, "path": "README.md"}))


@respx.mock
async def test_query_file_empty_project_raises_descriptive_error(connector):
    with pytest.raises(ValueError, match="non-empty project"):
        await connector.query(ConnectorQuery(resource="file", filters={"project": "", "path": "README.md"}))


@respx.mock
async def test_query_file_whitespace_project_raises_descriptive_error(connector):
    with pytest.raises(ValueError, match="non-empty project"):
        await connector.query(ConnectorQuery(resource="file", filters={"project": "  ", "path": "README.md"}))


@respx.mock
async def test_query_file_bool_project_raises_descriptive_error(connector):
    with pytest.raises(ValueError, match="project ID or path"):
        await connector.query(ConnectorQuery(resource="file", filters={"project": True, "path": "README.md"}))


@respx.mock
async def test_write_file_none_project_raises_descriptive_error(connector):
    with pytest.raises(ValueError, match="project ID or path"):
        await connector.write(
            ConnectorPayload(resource="file", data={"project": None, "path": "src/main.py", "content": "x"})
        )
