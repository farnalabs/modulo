"""Unit tests for GitHubConnector — HTTP responses are mocked via httpx."""

import json

import httpx
import pytest
import respx

from modulo.connectors.base import ConnectorPayload, ConnectorQuery, ConnectorType
from modulo.connectors.github import GitHubConnector

TOKEN = "ghp_test_token"


@pytest.fixture()
def connector():
    return GitHubConnector(token=TOKEN)


@respx.mock
async def test_health_check_ok(connector):
    respx.get("https://api.github.com/user").mock(
        return_value=httpx.Response(200, json={"login": "octocat"}, headers={"X-OAuth-Scopes": "repo, read:org"}),
    )
    result = await connector.health_check()
    assert result.ok is True
    assert result.detail == "octocat"


@respx.mock
async def test_health_check_missing_scopes(connector):
    respx.get("https://api.github.com/user").mock(
        return_value=httpx.Response(200, json={"login": "octocat"}, headers={"X-OAuth-Scopes": "repo"}),
    )
    result = await connector.health_check()
    assert result.ok is False
    assert "Missing scopes" in result.detail
    assert "read:org" in result.detail


@respx.mock
async def test_health_check_fail(connector):
    respx.get("https://api.github.com/user").mock(return_value=httpx.Response(401, text="Unauthorized"))
    result = await connector.health_check()
    assert result.ok is False
    assert "401" in result.detail


@respx.mock
async def test_query_repos(connector):
    repos = [{"id": 1, "name": "repo-a"}, {"id": 2, "name": "repo-b"}]
    respx.get("https://api.github.com/user/repos").mock(return_value=httpx.Response(200, json=repos))
    result = await connector.query(ConnectorQuery(resource="repos"))
    assert len(result.records) == 2
    assert result.records[0]["name"] == "repo-a"


@respx.mock
async def test_query_file(connector):
    file_data = {"name": "README.md", "content": "SGVsbG8=", "sha": "abc123"}
    respx.get("https://api.github.com/repos/owner/repo/contents/README.md").mock(
        return_value=httpx.Response(200, json=file_data)
    )
    result = await connector.query(
        ConnectorQuery(
            resource="file",
            filters={"repo": "owner/repo", "path": "README.md", "ref": "main"},
        )
    )
    assert result.records[0]["name"] == "README.md"


@respx.mock
async def test_query_pulls(connector):
    prs = [{"number": 42, "title": "Fix bug"}]
    respx.get("https://api.github.com/repos/owner/repo/pulls").mock(return_value=httpx.Response(200, json=prs))
    result = await connector.query(ConnectorQuery(resource="pulls", filters={"repo": "owner/repo"}))
    assert result.records[0]["number"] == 42


@respx.mock
async def test_write_file(connector):
    response_body = {"content": {"sha": "def456"}, "commit": {"sha": "ghi789"}}
    respx.put("https://api.github.com/repos/owner/repo/contents/path/file.txt").mock(
        return_value=httpx.Response(200, json=response_body)
    )
    result = await connector.write(
        ConnectorPayload(
            resource="file",
            data={
                "repo": "owner/repo",
                "path": "path/file.txt",
                "content": "SGVsbG8=",
                "message": "Update file",
                "sha": "abc123",
            },
        )
    )
    assert result["commit"]["sha"] == "ghi789"


async def test_unsupported_query_resource(connector):
    with pytest.raises(ValueError, match="Unsupported GitHub resource"):
        await connector.query(ConnectorQuery(resource="unknown"))


async def test_unsupported_write_resource(connector):
    with pytest.raises(ValueError, match="Unsupported GitHub write resource"):
        await connector.write(ConnectorPayload(resource="branch", data={}))


def test_connector_type(connector):
    assert connector.connector_type == ConnectorType.GITHUB


# ---------------------------------------------------------------------------
# Issue operation tests — query and write via respx-mocked HTTP
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_issues(connector):
    issues = [
        {"number": 1, "title": "Bug found", "state": "open"},
        {"number": 2, "title": "Feature request", "state": "open"},
    ]
    respx.get("https://api.github.com/repos/owner/repo/issues").mock(return_value=httpx.Response(200, json=issues))
    result = await connector.query(ConnectorQuery(resource="issues", filters={"repo": "owner/repo"}))
    assert len(result.records) == 2
    assert result.records[0]["number"] == 1


@respx.mock
async def test_query_single_issue(connector):
    issue = {"number": 42, "title": "Critical bug", "state": "open"}
    respx.get("https://api.github.com/repos/owner/repo/issues/42").mock(return_value=httpx.Response(200, json=issue))
    result = await connector.query(ConnectorQuery(resource="issue", filters={"repo": "owner/repo", "issue_number": 42}))
    assert result.records[0]["number"] == 42


@respx.mock
async def test_write_issue(connector):
    created = {"number": 100, "title": "New feature", "html_url": "https://github.com/owner/repo/issues/100"}
    respx.post("https://api.github.com/repos/owner/repo/issues").mock(return_value=httpx.Response(201, json=created))
    result = await connector.write(
        ConnectorPayload(resource="issue", data={"repo": "owner/repo", "title": "New feature", "body": "Details here"})
    )
    assert result["number"] == 100


@respx.mock
async def test_write_issue_comment(connector):
    comment = {"id": 1, "body": "Looking into this"}
    respx.post("https://api.github.com/repos/owner/repo/issues/42/comments").mock(
        return_value=httpx.Response(201, json=comment)
    )
    result = await connector.write(
        ConnectorPayload(
            resource="issue_comment", data={"repo": "owner/repo", "issue_number": 42, "body": "Looking into this"}
        )
    )
    assert result["id"] == 1


@respx.mock
async def test_write_issue_update(connector):
    updated = {"number": 42, "state": "closed", "title": "Fixed bug"}
    respx.patch("https://api.github.com/repos/owner/repo/issues/42").mock(
        return_value=httpx.Response(200, json=updated)
    )
    result = await connector.write(
        ConnectorPayload(resource="issue_update", data={"repo": "owner/repo", "issue_number": 42, "state": "closed"})
    )
    assert result["number"] == 42


@respx.mock
async def test_write_issue_label(connector):
    label_result = [{"id": 1, "name": "bug", "color": "d73a4a"}]
    respx.post("https://api.github.com/repos/owner/repo/issues/42/labels").mock(
        return_value=httpx.Response(200, json=label_result)
    )
    result = await connector.write(
        ConnectorPayload(resource="issue_label", data={"repo": "owner/repo", "issue_number": 42, "labels": ["bug"]})
    )
    assert result[0]["name"] == "bug"


@respx.mock
async def test_query_issues_missing_repo_filter(connector):
    with pytest.raises(ValueError, match="requires 'repo' filter"):
        await connector.query(ConnectorQuery(resource="issues"))


@respx.mock
async def test_write_issue_missing_title(connector):
    with pytest.raises(ValueError, match="requires 'title' in data"):
        await connector.write(ConnectorPayload(resource="issue", data={"repo": "owner/repo"}))


# ---------------------------------------------------------------------------
# Error path tests — HTTP errors on query/write operations
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_repos_http_error(connector):
    respx.get("https://api.github.com/user/repos").mock(return_value=httpx.Response(403, text="Forbidden"))
    with pytest.raises(ValueError, match="403"):
        await connector.query(ConnectorQuery(resource="repos"))


@respx.mock
async def test_query_file_http_error(connector):
    respx.get("https://api.github.com/repos/owner/repo/contents/missing.py").mock(
        return_value=httpx.Response(404, text="Not Found")
    )
    with pytest.raises(ValueError, match="404"):
        await connector.query(ConnectorQuery(resource="file", filters={"repo": "owner/repo", "path": "missing.py"}))


@respx.mock
async def test_query_pulls_http_error(connector):
    respx.get("https://api.github.com/repos/owner/repo/pulls").mock(
        return_value=httpx.Response(500, text="Server Error")
    )
    with pytest.raises(ValueError, match="500"):
        await connector.query(ConnectorQuery(resource="pulls", filters={"repo": "owner/repo"}))


@respx.mock
async def test_write_file_http_error(connector):
    respx.put("https://api.github.com/repos/owner/repo/contents/bad.txt").mock(
        return_value=httpx.Response(422, text="Unprocessable")
    )
    with pytest.raises(ValueError, match="422"):
        await connector.write(
            ConnectorPayload(
                resource="file",
                data={"repo": "owner/repo", "path": "bad.txt", "content": "data"},
            )
        )


@respx.mock
async def test_query_file_missing_filters(connector):
    with pytest.raises(ValueError, match="requires 'path' filter"):
        await connector.query(ConnectorQuery(resource="file", filters={"repo": "owner/repo"}))


@respx.mock
async def test_query_pulls_missing_repo_filter(connector):
    with pytest.raises(ValueError, match="requires 'repo' filter"):
        await connector.query(ConnectorQuery(resource="pulls"))


@respx.mock
async def test_write_file_missing_content(connector):
    with pytest.raises(ValueError, match="requires 'content' in data"):
        await connector.write(ConnectorPayload(resource="file", data={"repo": "owner/repo", "path": "x"}))


@respx.mock
async def test_health_check_non_json_response(connector):
    respx.get("https://api.github.com/user").mock(
        return_value=httpx.Response(200, text="not-json", headers={"X-OAuth-Scopes": "repo, read:org"})
    )
    result = await connector.health_check()
    assert result.ok is False
    assert "invalid JSON" in result.detail


@respx.mock
async def test_health_check_api_error(connector):
    respx.get("https://api.github.com/user").mock(return_value=httpx.Response(503, text="Service Unavailable"))
    result = await connector.health_check()
    assert result.ok is False
    assert "503" in result.detail


@respx.mock
async def test_query_repos_passes_limit(connector):
    respx.get("https://api.github.com/user/repos?per_page=5").mock(return_value=httpx.Response(200, json=[]))
    result = await connector.query(ConnectorQuery(resource="repos", limit=5))
    assert result.total == 0


# ---------------------------------------------------------------------------
# PR write operations
# ---------------------------------------------------------------------------


@respx.mock
async def test_write_create_pr(connector):
    created = {"number": 1, "title": "My PR", "state": "open", "html_url": "https://github.com/owner/repo/pull/1"}
    route = respx.post("https://api.github.com/repos/owner/repo/pulls").mock(
        return_value=httpx.Response(201, json=created)
    )
    result = await connector.write(
        ConnectorPayload(
            resource="pr",
            data={
                "repo": "owner/repo",
                "title": "My PR",
                "head": "feature-branch",
                "base": "main",
                "body": "Description here",
                "draft": True,
            },
        )
    )
    assert result["number"] == 1
    sent = json.loads(route.calls.last.request.content)
    assert sent["head"] == "feature-branch"
    assert sent["base"] == "main"


@respx.mock
async def test_write_create_pr_minimal(connector):
    created = {"number": 2, "title": "Minimal PR", "state": "open"}
    route = respx.post("https://api.github.com/repos/owner/repo/pulls").mock(
        return_value=httpx.Response(201, json=created)
    )
    result = await connector.write(
        ConnectorPayload(
            resource="pr",
            data={"repo": "owner/repo", "title": "Minimal PR", "head": "fix", "base": "main"},
        )
    )
    assert result["number"] == 2
    sent = json.loads(route.calls.last.request.content)
    assert "draft" not in sent


@respx.mock
async def test_write_pr_comment(connector):
    comment = {"id": 1, "body": "Good catch", "path": "file.py", "position": 42}
    route = respx.post("https://api.github.com/repos/owner/repo/pulls/1/comments").mock(
        return_value=httpx.Response(201, json=comment)
    )
    result = await connector.write(
        ConnectorPayload(
            resource="pr_comment",
            data={"repo": "owner/repo", "pull_number": 1, "body": "Good catch"},
        )
    )
    assert result["id"] == 1
    sent = json.loads(route.calls.last.request.content)
    assert sent["body"] == "Good catch"


@respx.mock
async def test_write_pr_update(connector):
    updated = {"number": 1, "title": "Updated PR", "state": "closed"}
    route = respx.patch("https://api.github.com/repos/owner/repo/pulls/1").mock(
        return_value=httpx.Response(200, json=updated)
    )
    result = await connector.write(
        ConnectorPayload(
            resource="pr_update",
            data={"repo": "owner/repo", "pull_number": 1, "state": "closed", "title": "Updated PR"},
        )
    )
    assert result["state"] == "closed"
    sent = json.loads(route.calls.last.request.content)
    assert sent["state"] == "closed"
    assert sent["title"] == "Updated PR"


@respx.mock
async def test_write_pr_missing_head(connector):
    with pytest.raises(ValueError, match="requires 'head' in data"):
        await connector.write(
            ConnectorPayload(
                resource="pr",
                data={"repo": "owner/repo", "title": "No head"},
            )
        )


@respx.mock
async def test_write_pr_http_error(connector):
    respx.post("https://api.github.com/repos/owner/repo/pulls").mock(
        return_value=httpx.Response(422, text="Unprocessable")
    )
    with pytest.raises(ValueError, match="422"):
        await connector.write(
            ConnectorPayload(
                resource="pr",
                data={"repo": "owner/repo", "title": "Bad PR", "head": "fix", "base": "main"},
            )
        )


# ---------------------------------------------------------------------------
# PR query operations
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_pr_commits(connector):
    commits = [{"sha": "abc123", "commit": {"message": "Fix bug"}}]
    respx.get("https://api.github.com/repos/owner/repo/pulls/1/commits").mock(
        return_value=httpx.Response(200, json=commits)
    )
    result = await connector.query(
        ConnectorQuery(resource="pr_commits", filters={"repo": "owner/repo", "pull_number": 1})
    )
    assert len(result.records) == 1
    assert result.records[0]["sha"] == "abc123"


@respx.mock
async def test_query_pr_files(connector):
    files = [{"filename": "README.md", "status": "modified", "additions": 1, "deletions": 0}]
    respx.get("https://api.github.com/repos/owner/repo/pulls/1/files").mock(
        return_value=httpx.Response(200, json=files)
    )
    result = await connector.query(
        ConnectorQuery(resource="pr_files", filters={"repo": "owner/repo", "pull_number": 1})
    )
    assert len(result.records) == 1
    assert result.records[0]["filename"] == "README.md"


# ---------------------------------------------------------------------------
# Configurable base URL
# ---------------------------------------------------------------------------


@respx.mock
async def test_custom_base_url():
    ghe_connector = GitHubConnector(token=TOKEN, base_url="https://github.internal.example.com/api/v3")
    respx.get("https://github.internal.example.com/api/v3/user").mock(
        return_value=httpx.Response(200, json={"login": "ghe-user"}, headers={"X-OAuth-Scopes": "repo, read:org"}),
    )
    result = await ghe_connector.health_check()
    assert result.ok is True
    assert result.detail == "ghe-user"


# ---------------------------------------------------------------------------
# Pagination — Link header parsing
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_repos_pagination_cursor(connector):
    repos = [{"id": 1, "name": "repo-a"}]
    link_header = '<https://api.github.com/user/repos?page=2&per_page=5>; rel="next", <https://api.github.com/user/repos?page=1&per_page=5>; rel="first"'
    respx.get("https://api.github.com/user/repos?per_page=5").mock(
        return_value=httpx.Response(200, json=repos, headers={"Link": link_header})
    )
    result = await connector.query(ConnectorQuery(resource="repos", limit=5))
    assert result.next_cursor == "https://api.github.com/user/repos?page=2&per_page=5"
    assert len(result.records) == 1


@respx.mock
async def test_query_pulls_pagination_cursor(connector):
    prs = [{"number": 1, "title": "PR 1"}]
    link_header = '<https://api.github.com/repos/owner/repo/pulls?page=2>; rel="next"'
    respx.get("https://api.github.com/repos/owner/repo/pulls?state=open&per_page=10").mock(
        return_value=httpx.Response(200, json=prs, headers={"Link": link_header})
    )
    result = await connector.query(ConnectorQuery(resource="pulls", filters={"repo": "owner/repo"}, limit=10))
    assert result.next_cursor is not None
    assert "page=2" in result.next_cursor


# ---------------------------------------------------------------------------
# Missing filter error paths for new operations
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_pr_commits_missing_pull_number(connector):
    with pytest.raises(ValueError, match="requires 'pull_number' filter"):
        await connector.query(ConnectorQuery(resource="pr_commits", filters={"repo": "owner/repo"}))


@respx.mock
async def test_write_pr_missing_base(connector):
    with pytest.raises(ValueError, match="requires 'base' in data"):
        await connector.write(
            ConnectorPayload(
                resource="pr",
                data={"repo": "owner/repo", "title": "PR", "head": "fix"},
            )
        )


@respx.mock
async def test_write_pr_comment_missing_body(connector):
    with pytest.raises(ValueError, match="requires 'body' in data"):
        await connector.write(
            ConnectorPayload(
                resource="pr_comment",
                data={"repo": "owner/repo", "pull_number": 1},
            )
        )
