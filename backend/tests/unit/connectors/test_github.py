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


# ---------------------------------------------------------------------------
# Health check — parametrized across success, scope, error, non-JSON, API error
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status,json_body,text,headers,expected_ok,expected_detail",
    [
        (200, {"login": "octocat"}, None, {"X-OAuth-Scopes": "repo, read:org"}, True, "octocat"),
        (200, {"login": "octocat"}, None, {"X-OAuth-Scopes": "repo"}, False, "Missing scopes"),
        (401, None, "Unauthorized", {}, False, "401"),
        (200, None, "not-json", {"X-OAuth-Scopes": "repo, read:org"}, False, "invalid JSON"),
        (503, None, "Service Unavailable", {}, False, "503"),
    ],
)
@respx.mock
async def test_health_check(connector, status, json_body, text, headers, expected_ok, expected_detail):
    respx.get("https://api.github.com/user").mock(
        return_value=httpx.Response(status, json=json_body, text=text, headers=headers),
    )
    result = await connector.health_check()
    assert result.ok is expected_ok
    assert expected_detail in result.detail


# ---------------------------------------------------------------------------
# Query resources
# ---------------------------------------------------------------------------


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
    result = await connector.query(
        ConnectorQuery(resource="issue", filters={"repo": "owner/repo", "issue_number": "42"})
    )
    assert result.records[0]["number"] == 42


@pytest.mark.parametrize(
    "resource,data,http_method,url,response_json,assert_key,assert_value",
    [
        (
            "issue",
            {"repo": "owner/repo", "title": "New feature", "body": "Details here"},
            "post",
            "https://api.github.com/repos/owner/repo/issues",
            {"number": 100},
            "number",
            100,
        ),
        (
            "issue_comment",
            {"repo": "owner/repo", "issue_number": "42", "body": "Looking into this"},
            "post",
            "https://api.github.com/repos/owner/repo/issues/42/comments",
            {"id": 1},
            "id",
            1,
        ),
        (
            "issue_update",
            {"repo": "owner/repo", "issue_number": "42", "state": "closed"},
            "patch",
            "https://api.github.com/repos/owner/repo/issues/42",
            {"number": 42},
            "number",
            42,
        ),
        (
            "issue_label",
            {"repo": "owner/repo", "issue_number": "42", "labels": ["bug"]},
            "post",
            "https://api.github.com/repos/owner/repo/issues/42/labels",
            [{"id": 1, "name": "bug"}],
            "[0].name",
            "bug",
        ),
    ],
)
@respx.mock
async def test_write_issue_operations(
    connector,
    resource,
    data,
    http_method,
    url,
    response_json,
    assert_key,
    assert_value,
):
    getattr(respx, http_method)(url).mock(
        return_value=httpx.Response(201 if http_method == "post" else 200, json=response_json),
    )
    result = await connector.write(ConnectorPayload(resource=resource, data=data))
    keys = assert_key.split(".")
    val = result
    for k in keys:
        k = int(k.strip("[]")) if k.strip("[]").isdigit() else k
        val = val[k]
    assert val == assert_value


# ---------------------------------------------------------------------------
# Missing filter/data validation — parametrized
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "resource,filters,match_text",
    [
        ("issues", {}, "requires 'repo' filter"),
        ("file", {"repo": "owner/repo"}, "requires 'path' filter"),
        ("pulls", {}, "requires 'repo' filter"),
        ("pr_commits", {"repo": "owner/repo"}, "requires 'pull_number' filter"),
    ],
)
async def test_query_missing_filters(connector, resource, filters, match_text):
    with pytest.raises(ValueError, match=match_text):
        await connector.query(ConnectorQuery(resource=resource, filters=filters))


@pytest.mark.parametrize(
    "resource,data,match_text",
    [
        ("issue", {"repo": "owner/repo"}, "requires 'title' in data"),
        ("file", {"repo": "owner/repo", "path": "x"}, "requires 'content' in data"),
        ("pr", {"repo": "owner/repo", "title": "PR", "head": "fix"}, "requires 'base' in data"),
        ("pr", {"repo": "owner/repo", "title": "No head"}, "requires 'head' in data"),
        ("pr_comment", {"repo": "owner/repo", "pull_number": "1"}, "requires 'body' in data"),
    ],
)
async def test_write_missing_data(connector, resource, data, match_text):
    with pytest.raises(ValueError, match=match_text):
        await connector.write(ConnectorPayload(resource=resource, data=data))


# ---------------------------------------------------------------------------
# Error path tests — HTTP errors on query/write operations
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "resource,filters,data,url_method,url_pattern,status_code",
    [
        ("repos", {}, None, "get", "https://api.github.com/user/repos", 403),
        (
            "file",
            {"repo": "owner/repo", "path": "missing.py"},
            None,
            "get",
            "https://api.github.com/repos/owner/repo/contents/missing.py",
            404,
        ),
        ("pulls", {"repo": "owner/repo"}, None, "get", "https://api.github.com/repos/owner/repo/pulls", 500),
        (
            "file",
            None,
            {"repo": "owner/repo", "path": "bad.txt", "content": "data"},
            "put",
            "https://api.github.com/repos/owner/repo/contents/bad.txt",
            422,
        ),
    ],
)
@respx.mock
async def test_http_error(connector, resource, filters, data, url_method, url_pattern, status_code):
    getattr(respx, url_method)(url_pattern).mock(return_value=httpx.Response(status_code, text="Error"))
    if data:
        with pytest.raises(ValueError, match=str(status_code)):
            await connector.write(ConnectorPayload(resource=resource, data=data))
    else:
        with pytest.raises(ValueError, match=str(status_code)):
            await connector.query(ConnectorQuery(resource=resource, filters=filters))


@respx.mock
async def test_query_repos_passes_limit(connector):
    respx.get("https://api.github.com/user/repos?per_page=5").mock(return_value=httpx.Response(200, json=[]))
    result = await connector.query(ConnectorQuery(resource="repos", limit=5))
    assert result.total == 0


# ---------------------------------------------------------------------------
# PR write operations
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "resource,data,http_method,url,response_json,assert_key,assert_value,sent_checks",
    [
        (
            "pr",
            {
                "repo": "owner/repo",
                "title": "My PR",
                "head": "feature-branch",
                "base": "main",
                "body": "Description here",
                "draft": True,
            },
            "post",
            "https://api.github.com/repos/owner/repo/pulls",
            {"number": 1},
            "number",
            1,
            [("head", "feature-branch"), ("base", "main")],
        ),
        (
            "pr",
            {"repo": "owner/repo", "title": "Minimal PR", "head": "fix", "base": "main"},
            "post",
            "https://api.github.com/repos/owner/repo/pulls",
            {"number": 2},
            "number",
            2,
            [("draft", None)],
        ),
        (
            "pr_comment",
            {"repo": "owner/repo", "pull_number": "1", "body": "Good catch"},
            "post",
            "https://api.github.com/repos/owner/repo/pulls/1/comments",
            {"id": 1},
            "id",
            1,
            [("body", "Good catch")],
        ),
        (
            "pr_update",
            {"repo": "owner/repo", "pull_number": "1", "state": "closed", "title": "Updated PR"},
            "patch",
            "https://api.github.com/repos/owner/repo/pulls/1",
            {"number": 1, "state": "closed", "title": "Updated PR"},
            "state",
            "closed",
            [("state", "closed"), ("title", "Updated PR")],
        ),
    ],
)
@respx.mock
async def test_write_pr_operations(
    connector,
    resource,
    data,
    http_method,
    url,
    response_json,
    assert_key,
    assert_value,
    sent_checks,
):
    route = getattr(respx, http_method)(url).mock(
        return_value=httpx.Response(201 if http_method == "post" else 200, json=response_json),
    )
    result = await connector.write(ConnectorPayload(resource=resource, data=data))
    keys = assert_key.split(".")
    val = result
    for k in keys:
        val = val[k]
    assert val == assert_value
    sent = json.loads(route.calls.last.request.content)
    for key, expected in sent_checks:
        if expected is None:
            assert key not in sent
        else:
            assert sent[key] == expected


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
        ConnectorQuery(resource="pr_commits", filters={"repo": "owner/repo", "pull_number": "1"})
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
        ConnectorQuery(resource="pr_files", filters={"repo": "owner/repo", "pull_number": "1"})
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
    link_header = (
        '<https://api.github.com/user/repos?page=2&per_page=5>; rel="next", '
        '<https://api.github.com/user/repos?page=1&per_page=5>; rel="first"'
    )
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
