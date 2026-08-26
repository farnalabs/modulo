"""Unit tests for GitHubConnector — HTTP responses are mocked via httpx."""

import base64
import json
import time

import httpx
import pytest
import respx

from modulo.connectors.base import ConnectorPayload, ConnectorQuery, ConnectorType
from modulo.connectors.github import (
    GitHubConnector,
    _parse_rate_limit_reset,
    _rate_limit_detail,
    _rate_limit_metadata,
    _search_total,
)

TOKEN = "ghp_test_token"


@pytest.fixture
def connector():
    return GitHubConnector(token=TOKEN)


# ---------------------------------------------------------------------------
# Health check — parametrized across success, scope, error, non-JSON, API error
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "json_body", "text", "headers", "expected_ok", "expected_detail"),
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
    file_data = {"name": "README.md", "content": "SGVsbG8=", "sha": "abc123", "encoding": "base64"}
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
    assert result.records[0]["content"] == "Hello"


@respx.mock
async def test_query_pulls(connector):
    prs = [{"number": 42, "title": "Fix bug"}]
    respx.get("https://api.github.com/repos/owner/repo/pulls").mock(return_value=httpx.Response(200, json=prs))
    result = await connector.query(ConnectorQuery(resource="pulls", filters={"repo": "owner/repo"}))
    assert result.records[0]["number"] == 42


@respx.mock
async def test_write_file(connector):
    response_body = {"content": {"sha": "def456"}, "commit": {"sha": "ghi789"}}
    route = respx.put("https://api.github.com/repos/owner/repo/contents/path/file.txt").mock(
        return_value=httpx.Response(200, json=response_body)
    )
    result = await connector.write(
        ConnectorPayload(
            resource="file",
            data={
                "repo": "owner/repo",
                "path": "path/file.txt",
                "content": "Hello",
                "message": "Update file",
                "sha": "abc123",
            },
        )
    )
    assert result["commit"]["sha"] == "ghi789"
    sent = json.loads(route.calls.last.request.content)
    assert sent["content"] == "SGVsbG8="


# ---------------------------------------------------------------------------
# File content encoding — base64 decode on read, encode on write
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_file_decodes_multiline_base64_content(connector) -> None:
    raw = "line1\nline2\n"
    file_data = {"name": "notes.txt", "content": base64.b64encode(raw.encode()).decode(), "encoding": "base64"}
    respx.get("https://api.github.com/repos/owner/repo/contents/notes.txt").mock(
        return_value=httpx.Response(200, json=file_data)
    )
    result = await connector.query(ConnectorQuery(resource="file", filters={"repo": "owner/repo", "path": "notes.txt"}))
    assert result.records[0]["content"] == raw


@respx.mock
async def test_query_file_leaves_binary_content_encoded(connector) -> None:
    raw_bytes = b"\x89PNG\r\n\x1a\n"
    encoded = base64.b64encode(raw_bytes).decode()
    file_data = {"name": "img.png", "content": encoded, "encoding": "base64"}
    respx.get("https://api.github.com/repos/owner/repo/contents/img.png").mock(
        return_value=httpx.Response(200, json=file_data)
    )
    result = await connector.query(ConnectorQuery(resource="file", filters={"repo": "owner/repo", "path": "img.png"}))
    assert result.records[0]["content"] == encoded


@respx.mock
async def test_query_file_plain_text_untouched(connector) -> None:
    file_data = {"name": "LICENSE", "content": "plain text", "encoding": "none"}
    respx.get("https://api.github.com/repos/owner/repo/contents/LICENSE").mock(
        return_value=httpx.Response(200, json=file_data)
    )
    result = await connector.query(ConnectorQuery(resource="file", filters={"repo": "owner/repo", "path": "LICENSE"}))
    assert result.records[0]["content"] == "plain text"


@respx.mock
async def test_write_file_content_base64_passthrough(connector) -> None:
    response_body = {"content": {"sha": "def456"}, "commit": {"sha": "ghi789"}}
    route = respx.put("https://api.github.com/repos/owner/repo/contents/binary.bin").mock(
        return_value=httpx.Response(200, json=response_body)
    )
    result = await connector.write(
        ConnectorPayload(
            resource="file",
            data={
                "repo": "owner/repo",
                "path": "binary.bin",
                "content_base64": "AAECAw==",
                "message": "Add binary",
            },
        )
    )
    assert result["commit"]["sha"] == "ghi789"
    sent = json.loads(route.calls.last.request.content)
    assert sent["content"] == "AAECAw=="


@respx.mock
async def test_write_file_utf8_content_round_trips(connector) -> None:
    raw = "héllo wörld\n"
    route = respx.put("https://api.github.com/repos/owner/repo/contents/utf8.txt").mock(
        return_value=httpx.Response(200, json={"commit": {"sha": "abc"}})
    )
    await connector.write(
        ConnectorPayload(
            resource="file",
            data={"repo": "owner/repo", "path": "utf8.txt", "content": raw},
        )
    )
    sent = json.loads(route.calls.last.request.content)
    assert base64.b64decode(sent["content"]).decode("utf-8") == raw


async def test_write_file_missing_content_raises(connector) -> None:
    payload = ConnectorPayload(resource="file", data={"repo": "owner/repo", "path": "x"})
    with pytest.raises(ValueError, match=r"requires 'content' \(raw text\) or 'content_base64'"):
        await connector.write(payload)


@respx.mock
async def test_write_file_both_content_and_content_base64_raises(connector) -> None:
    payload = ConnectorPayload(
        resource="file",
        data={
            "repo": "owner/repo",
            "path": "x",
            "content": "raw",
            "content_base64": "cmF3",
        },
    )
    with pytest.raises(ValueError, match="exactly one of 'content' or 'content_base64'"):
        await connector.write(payload)


async def test_unsupported_query_resource(connector):
    query = ConnectorQuery(resource="unknown")
    with pytest.raises(ValueError, match="Unsupported GitHub resource"):
        await connector.query(query)


async def test_unsupported_write_resource(connector):
    payload = ConnectorPayload(resource="branch", data={})
    with pytest.raises(ValueError, match="Unsupported GitHub write resource"):
        await connector.write(payload)


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
    ("resource", "data", "http_method", "url", "response_json", "assert_key", "assert_value"),
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
    ("resource", "filters", "match_text"),
    [
        ("issues", {}, "requires 'repo' filter"),
        ("file", {"repo": "owner/repo"}, "requires 'path' filter"),
        ("pulls", {}, "requires 'repo' filter"),
        ("pr_commits", {"repo": "owner/repo"}, "requires 'pull_number' filter"),
        ("pr_diff", {"repo": "owner/repo"}, "requires 'pull_number' filter"),
        ("search_issues", {}, "requires 'q' filter"),
    ],
)
async def test_query_missing_filters(connector, resource, filters, match_text):
    query = ConnectorQuery(resource=resource, filters=filters)
    with pytest.raises(ValueError, match=match_text):
        await connector.query(query)


@pytest.mark.parametrize(
    ("resource", "data", "match_text"),
    [
        ("issue", {"repo": "owner/repo"}, "requires 'title' in data"),
        ("file", {"repo": "owner/repo", "path": "x"}, r"requires 'content' \(raw text\) or 'content_base64'"),
        ("pr", {"repo": "owner/repo", "title": "PR", "head": "fix"}, "requires 'base' in data"),
        ("pr", {"repo": "owner/repo", "title": "No head"}, "requires 'head' in data"),
        ("pr_comment", {"repo": "owner/repo", "pull_number": "1"}, "requires 'body' in data"),
        ("pr_merge", {"repo": "owner/repo"}, "requires 'pull_number' in data"),
        ("pr_label", {"repo": "owner/repo", "pull_number": "1"}, "requires 'labels' in data"),
        ("issue_assign", {"repo": "owner/repo", "issue_number": "42"}, "requires 'assignees' in data"),
    ],
    ids=[
        "issue_missing_title",
        "file_missing_content",
        "pr_missing_base",
        "pr_missing_head",
        "pr_comment_missing_body",
        "pr_merge_missing_pull_number",
        "pr_label_missing_labels",
        "issue_assign_missing_assignees",
    ],
)
async def test_write_missing_data(connector, resource, data, match_text):
    payload = ConnectorPayload(resource=resource, data=data)
    with pytest.raises(ValueError, match=match_text):
        await connector.write(payload)


# ---------------------------------------------------------------------------
# Error path tests — HTTP errors on query/write operations
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("resource", "filters", "data", "url_method", "url_pattern", "status_code"),
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
        payload = ConnectorPayload(resource=resource, data=data)
        with pytest.raises(ValueError, match=str(status_code)):
            await connector.write(payload)
    else:
        query = ConnectorQuery(resource=resource, filters=filters)
        with pytest.raises(ValueError, match=str(status_code)):
            await connector.query(query)


@respx.mock
async def test_query_repos_passes_limit(connector):
    respx.get("https://api.github.com/user/repos?per_page=5").mock(return_value=httpx.Response(200, json=[]))
    result = await connector.query(ConnectorQuery(resource="repos", limit=5))
    assert result.total == 0


# ---------------------------------------------------------------------------
# PR write operations
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("resource", "data", "http_method", "url", "response_json", "assert_key", "assert_value", "sent_checks"),
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
    payload = ConnectorPayload(
        resource="pr",
        data={"repo": "owner/repo", "title": "Bad PR", "head": "fix", "base": "main"},
    )
    with pytest.raises(ValueError, match="422"):
        await connector.write(payload)


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


# ---------------------------------------------------------------------------
# PR diff and issue search queries
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_pr_diff(connector):
    diff = (
        "diff --git a/README.md b/README.md\nindex abc..def 100644\n--- a/README.md\n"
        "+++ b/README.md\n@@ -1 +1 @@\n-hello\n+world\n"
    )
    respx.get("https://api.github.com/repos/owner/repo/pulls/42").mock(
        return_value=httpx.Response(200, text=diff, headers={"Content-Type": "text/plain; charset=utf-8"})
    )
    result = await connector.query(
        ConnectorQuery(resource="pr_diff", filters={"repo": "owner/repo", "pull_number": "42"})
    )
    assert result.total == 1
    assert result.records[0]["diff"] == diff


@respx.mock
async def test_query_pr_diff_sends_diff_accept_header(connector):
    route = respx.get("https://api.github.com/repos/owner/repo/pulls/7").mock(
        return_value=httpx.Response(200, text="+patch")
    )
    await connector.query(ConnectorQuery(resource="pr_diff", filters={"repo": "owner/repo", "pull_number": "7"}))
    accept = route.calls.last.request.headers.get("Accept")
    assert accept == "application/vnd.github.v3.diff"


@respx.mock
async def test_query_search_issues(connector):
    body = {
        "total_count": 1,
        "items": [{"number": 9, "title": "Search hit", "html_url": "https://github.com/owner/repo/issues/9"}],
    }
    respx.get("https://api.github.com/search/issues?q=repo%3Aowner%2Frepo+is%3Aopen&per_page=100").mock(
        return_value=httpx.Response(200, json=body)
    )
    result = await connector.query(ConnectorQuery(resource="search_issues", filters={"q": "repo:owner/repo is:open"}))
    assert result.total == 1
    assert len(result.records) == 1
    assert result.records[0]["number"] == 9


@respx.mock
async def test_query_search_issues_empty(connector):
    respx.get("https://api.github.com/search/issues?q=nothing&per_page=100").mock(
        return_value=httpx.Response(200, json={"total_count": 0, "items": []})
    )
    result = await connector.query(ConnectorQuery(resource="search_issues", filters={"q": "nothing"}))
    assert result.total == 0
    assert not result.records


@respx.mock
async def test_query_search_issues_corrupt_total(connector):
    """A non-finite ``total_count`` must not poison the reported total."""
    respx.get("https://api.github.com/search/issues?q=bug&per_page=100").mock(
        return_value=httpx.Response(200, content=b'{"total_count": 1e999, "items": [{"number": 1}]}')
    )
    result = await connector.query(ConnectorQuery(resource="search_issues", filters={"q": "bug"}))
    assert len(result.records) == 1
    assert result.total == 0


@respx.mock
async def test_query_search_issues_pagination(connector):
    body = {"total_count": 30, "items": [{"number": 1}]}
    link_header = '<https://api.github.com/search/issues?q=bug&page=2&per_page=10>; rel="next"'
    respx.get("https://api.github.com/search/issues?q=bug&per_page=10").mock(
        return_value=httpx.Response(200, json=body, headers={"Link": link_header})
    )
    result = await connector.query(ConnectorQuery(resource="search_issues", filters={"q": "bug"}, limit=10))
    assert result.next_cursor is not None
    assert "page=2" in result.next_cursor


@respx.mock
async def test_query_search_issues_passes_optional_params(connector):
    body = {"total_count": 1, "items": [{"number": 3, "state": "open"}]}
    respx.get("https://api.github.com/search/issues?q=bug&sort=created&order=asc&state=open&per_page=5").mock(
        return_value=httpx.Response(200, json=body)
    )
    result = await connector.query(
        ConnectorQuery(
            resource="search_issues",
            filters={"q": "bug", "sort": "created", "order": "asc", "state": "open"},
            limit=5,
        )
    )
    assert result.total == 1
    assert result.records[0]["number"] == 3


# ---------------------------------------------------------------------------
# PR merge / review request / labels and issue assign writes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("data", "expected_body"),
    [
        ({"repo": "owner/repo", "pull_number": "1"}, {}),
        (
            {
                "repo": "owner/repo",
                "pull_number": "1",
                "commit_title": "T",
                "commit_message": "M",
                "merge_method": "squash",
                "sha": "abc",
            },
            {"commit_title": "T", "commit_message": "M", "merge_method": "squash", "sha": "abc"},
        ),
    ],
)
@respx.mock
async def test_write_pr_merge(connector, data, expected_body):
    route = respx.put("https://api.github.com/repos/owner/repo/pulls/1/merge").mock(
        return_value=httpx.Response(200, json={"sha": "merged123", "merged": True})
    )
    result = await connector.write(ConnectorPayload(resource="pr_merge", data=data))
    assert result["merged"] is True
    sent = json.loads(route.calls.last.request.content)
    assert sent == expected_body


@respx.mock
async def test_write_pr_review_request_reviewers(connector):
    route = respx.post("https://api.github.com/repos/owner/repo/pulls/1/requested_reviewers").mock(
        return_value=httpx.Response(201, json={"number": 1, "requested_reviewers": [{"login": "alice"}]})
    )
    result = await connector.write(
        ConnectorPayload(
            resource="pr_review_request",
            data={"repo": "owner/repo", "pull_number": "1", "reviewers": ["alice", "bob"]},
        )
    )
    assert result["requested_reviewers"][0]["login"] == "alice"
    sent = json.loads(route.calls.last.request.content)
    assert sent == {"reviewers": ["alice", "bob"]}


@respx.mock
async def test_write_pr_review_request_team_reviewers(connector):
    route = respx.post("https://api.github.com/repos/owner/repo/pulls/1/requested_reviewers").mock(
        return_value=httpx.Response(201, json={"number": 1})
    )
    await connector.write(
        ConnectorPayload(
            resource="pr_review_request",
            data={"repo": "owner/repo", "pull_number": "1", "team_reviewers": ["eng-core"]},
        )
    )
    sent = json.loads(route.calls.last.request.content)
    assert sent == {"team_reviewers": ["eng-core"]}


@respx.mock
async def test_write_pr_review_request_both(connector):
    route = respx.post("https://api.github.com/repos/owner/repo/pulls/1/requested_reviewers").mock(
        return_value=httpx.Response(201, json={"number": 1})
    )
    await connector.write(
        ConnectorPayload(
            resource="pr_review_request",
            data={
                "repo": "owner/repo",
                "pull_number": "1",
                "reviewers": ["alice"],
                "team_reviewers": ["eng-core"],
            },
        )
    )
    sent = json.loads(route.calls.last.request.content)
    assert sent == {"reviewers": ["alice"], "team_reviewers": ["eng-core"]}


async def test_write_pr_review_request_missing_reviewers(connector):
    payload = ConnectorPayload(resource="pr_review_request", data={"repo": "owner/repo", "pull_number": "1"})
    with pytest.raises(ValueError, match="requires 'reviewers' or 'team_reviewers'"):
        await connector.write(payload)


@respx.mock
async def test_write_pr_label(connector):
    route = respx.post("https://api.github.com/repos/owner/repo/issues/1/labels").mock(
        return_value=httpx.Response(200, json=[{"id": 1, "name": "ready"}])
    )
    result = await connector.write(
        ConnectorPayload(
            resource="pr_label",
            data={"repo": "owner/repo", "pull_number": "1", "labels": ["ready", "review"]},
        )
    )
    assert result[0]["name"] == "ready"
    sent = json.loads(route.calls.last.request.content)
    assert sent == {"labels": ["ready", "review"]}


@respx.mock
async def test_write_issue_assign(connector):
    route = respx.post("https://api.github.com/repos/owner/repo/issues/42/assignees").mock(
        return_value=httpx.Response(201, json={"number": 42, "assignees": [{"login": "alice"}]})
    )
    result = await connector.write(
        ConnectorPayload(
            resource="issue_assign",
            data={"repo": "owner/repo", "issue_number": "42", "assignees": ["alice"]},
        )
    )
    assert result["assignees"][0]["login"] == "alice"
    sent = json.loads(route.calls.last.request.content)
    assert sent == {"assignees": ["alice"]}


# ---------------------------------------------------------------------------
# Recursive tree listing — query("tree")
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_tree_recursive(connector):
    """tree resolves ref to a commit SHA and lists the full recursive tree."""
    tree_entries = [
        {"path": "README.md", "mode": "100644", "type": "blob", "sha": "aaa", "size": 10},
        {"path": "src", "mode": "040000", "type": "tree", "sha": "bbb", "size": 0},
        {"path": "src/main.py", "mode": "100644", "type": "blob", "sha": "ccc", "size": 42},
    ]
    respx.get("https://api.github.com/repos/owner/repo/commits/main").mock(
        return_value=httpx.Response(200, json={"sha": "abc123", "commit": {"message": "init"}})
    )
    tree_route = respx.get("https://api.github.com/repos/owner/repo/git/trees/abc123").mock(
        return_value=httpx.Response(200, json={"sha": "abc123", "truncated": False, "tree": tree_entries})
    )
    result = await connector.query(ConnectorQuery(resource="tree", filters={"repo": "owner/repo"}))
    assert len(result.records) == 3
    assert result.records[0]["path"] == "README.md"
    assert result.records[2]["type"] == "blob"
    assert tree_route.calls.last.request.url.params.get("recursive") == "1"


@respx.mock
async def test_query_tree_non_recursive(connector):
    """recursive: false skips the recursive param (top-level listing only)."""
    respx.get("https://api.github.com/repos/owner/repo/commits/main").mock(
        return_value=httpx.Response(200, json={"sha": "abc123"})
    )
    tree_route = respx.get("https://api.github.com/repos/owner/repo/git/trees/abc123").mock(
        return_value=httpx.Response(200, json={"tree": [{"path": "src", "type": "tree", "sha": "bbb"}]})
    )
    result = await connector.query(ConnectorQuery(resource="tree", filters={"repo": "owner/repo", "recursive": False}))
    assert len(result.records) == 1
    assert tree_route.calls.last.request.url.params.get("recursive") is None


@respx.mock
async def test_query_tree_custom_ref(connector):
    """A custom ref is forwarded to the commits resolution call."""
    commit_route = respx.get("https://api.github.com/repos/owner/repo/commits/develop").mock(
        return_value=httpx.Response(200, json={"sha": "dev123"})
    )
    respx.get("https://api.github.com/repos/owner/repo/git/trees/dev123").mock(
        return_value=httpx.Response(200, json={"tree": [{"path": "a.txt", "type": "blob", "sha": "s"}]})
    )
    result = await connector.query(ConnectorQuery(resource="tree", filters={"repo": "owner/repo", "ref": "develop"}))
    assert result.records[0]["path"] == "a.txt"
    assert "commits/develop" in str(commit_route.calls.last.request.url)


@respx.mock
async def test_query_tree_path_filter(connector):
    """A path filter narrows the returned entries to that directory (locally)."""
    tree_entries = [
        {"path": "README.md", "type": "blob", "sha": "aaa"},
        {"path": "docs", "type": "tree", "sha": "bbb"},
        {"path": "docs/guide.md", "type": "blob", "sha": "ccc"},
        {"path": "docs/api.md", "type": "blob", "sha": "ddd"},
        {"path": "src/main.py", "type": "blob", "sha": "eee"},
    ]
    respx.get("https://api.github.com/repos/owner/repo/commits/main").mock(
        return_value=httpx.Response(200, json={"sha": "abc123"})
    )
    respx.get("https://api.github.com/repos/owner/repo/git/trees/abc123").mock(
        return_value=httpx.Response(200, json={"tree": tree_entries})
    )
    result = await connector.query(ConnectorQuery(resource="tree", filters={"repo": "owner/repo", "path": "docs"}))
    assert [r["path"] for r in result.records] == ["docs/guide.md", "docs/api.md"]


async def test_query_tree_missing_repo(connector):
    query = ConnectorQuery(resource="tree", filters={})
    with pytest.raises(ValueError, match="requires 'repo' filter"):
        await connector.query(query)


@respx.mock
async def test_query_tree_unresolvable_ref(connector):
    """A commit response without a SHA raises a descriptive ValueError."""
    respx.get("https://api.github.com/repos/owner/repo/commits/main").mock(
        return_value=httpx.Response(200, json={"message": "no commit"})
    )
    query = ConnectorQuery(resource="tree", filters={"repo": "owner/repo"})
    with pytest.raises(ValueError, match="could not resolve ref"):
        await connector.query(query)


# ---------------------------------------------------------------------------
# Batch file operations — write("commit") via the Git Database API
# ---------------------------------------------------------------------------


def _mock_commit_flow(repo="owner/repo", base_sha="base123", blob_shas=("blob1", "blob2")):
    """Register the commits/blobs/trees/commits/refs endpoints for a batch commit."""
    respx.get(f"https://api.github.com/repos/{repo}/commits/main").mock(
        return_value=httpx.Response(200, json={"sha": base_sha})
    )
    blob_route = respx.post(f"https://api.github.com/repos/{repo}/git/blobs")
    blob_route.mock(side_effect=[httpx.Response(201, json={"sha": sha}) for sha in blob_shas])
    tree_route = respx.post(f"https://api.github.com/repos/{repo}/git/trees").mock(
        return_value=httpx.Response(201, json={"sha": "tree123"})
    )
    commit_route = respx.post(f"https://api.github.com/repos/{repo}/git/commits").mock(
        return_value=httpx.Response(201, json={"sha": "commit123"})
    )
    ref_route = respx.patch(f"https://api.github.com/repos/{repo}/git/refs/refs/heads/main").mock(
        return_value=httpx.Response(200, json={"ref": "refs/heads/main", "object": {"sha": "commit123"}})
    )
    return blob_route, tree_route, commit_route, ref_route


@respx.mock
async def test_write_commit_create_multiple_files(connector):
    """A batch create commit flows blobs -> tree -> commit -> ref update."""
    _, tree_route, commit_route, ref_route = _mock_commit_flow()
    result = await connector.write(
        ConnectorPayload(
            resource="commit",
            data={
                "repo": "owner/repo",
                "message": "Add two files",
                "actions": [
                    {"action": "create", "path": "a.txt", "content": "hello"},
                    {"action": "create", "path": "b.txt", "content": "world"},
                ],
            },
        )
    )
    assert result["ref"] == "refs/heads/main"
    assert result["object"]["sha"] == "commit123"

    tree_sent = json.loads(tree_route.calls.last.request.content)
    assert tree_sent["base_tree"] == "base123"
    assert tree_sent["tree"] == [
        {"path": "a.txt", "mode": "100644", "type": "blob", "sha": "blob1"},
        {"path": "b.txt", "mode": "100644", "type": "blob", "sha": "blob2"},
    ]

    commit_sent = json.loads(commit_route.calls.last.request.content)
    assert commit_sent["message"] == "Add two files"
    assert commit_sent["tree"] == "tree123"
    assert commit_sent["parents"] == ["base123"]

    ref_sent = json.loads(ref_route.calls.last.request.content)
    assert ref_sent == {"sha": "commit123", "force": False}


@respx.mock
async def test_write_commit_update(connector):
    """The update action creates a blob and adds a tree entry, like create."""
    _, tree_route, _, _ = _mock_commit_flow()
    await connector.write(
        ConnectorPayload(
            resource="commit",
            data={
                "repo": "owner/repo",
                "actions": [{"action": "update", "path": "a.txt", "content": "changed"}],
            },
        )
    )
    tree_sent = json.loads(tree_route.calls.last.request.content)
    assert tree_sent["tree"] == [{"path": "a.txt", "mode": "100644", "type": "blob", "sha": "blob1"}]


@respx.mock
async def test_write_commit_delete(connector):
    """A delete action adds a tree entry with a null SHA (no blob created)."""
    respx.get("https://api.github.com/repos/owner/repo/commits/main").mock(
        return_value=httpx.Response(200, json={"sha": "base123"})
    )
    tree_route = respx.post("https://api.github.com/repos/owner/repo/git/trees").mock(
        return_value=httpx.Response(201, json={"sha": "tree123"})
    )
    respx.post("https://api.github.com/repos/owner/repo/git/commits").mock(
        return_value=httpx.Response(201, json={"sha": "commit123"})
    )
    respx.patch("https://api.github.com/repos/owner/repo/git/refs/refs/heads/main").mock(
        return_value=httpx.Response(200, json={"ref": "refs/heads/main"})
    )
    result = await connector.write(
        ConnectorPayload(
            resource="commit",
            data={"repo": "owner/repo", "actions": [{"action": "delete", "path": "old.txt"}]},
        )
    )
    assert result["ref"] == "refs/heads/main"
    tree_sent = json.loads(tree_route.calls.last.request.content)
    assert tree_sent["tree"] == [{"path": "old.txt", "mode": "100644", "type": "blob", "sha": None}]


@respx.mock
async def test_write_commit_move_reads_old_content(connector):
    """A move deletes the old path and blobs the old file's content at the new path."""
    respx.get("https://api.github.com/repos/owner/repo/commits/main").mock(
        return_value=httpx.Response(200, json={"sha": "base123"})
    )
    old_content = base64.b64encode(b"moved content").decode()
    respx.get("https://api.github.com/repos/owner/repo/contents/old.txt?ref=main").mock(
        return_value=httpx.Response(200, json={"content": old_content, "encoding": "base64"})
    )
    blob_route = respx.post("https://api.github.com/repos/owner/repo/git/blobs").mock(
        return_value=httpx.Response(201, json={"sha": "blob1"})
    )
    tree_route = respx.post("https://api.github.com/repos/owner/repo/git/trees").mock(
        return_value=httpx.Response(201, json={"sha": "tree123"})
    )
    respx.post("https://api.github.com/repos/owner/repo/git/commits").mock(
        return_value=httpx.Response(201, json={"sha": "commit123"})
    )
    respx.patch("https://api.github.com/repos/owner/repo/git/refs/refs/heads/main").mock(
        return_value=httpx.Response(200, json={"ref": "refs/heads/main"})
    )
    await connector.write(
        ConnectorPayload(
            resource="commit",
            data={
                "repo": "owner/repo",
                "actions": [{"action": "move", "path": "new.txt", "previous_path": "old.txt"}],
            },
        )
    )
    blob_sent = json.loads(blob_route.calls.last.request.content)
    assert blob_sent["content"] == "moved content"
    tree_sent = json.loads(tree_route.calls.last.request.content)
    assert tree_sent["tree"] == [
        {"path": "old.txt", "mode": "100644", "type": "blob", "sha": None},
        {"path": "new.txt", "mode": "100644", "type": "blob", "sha": "blob1"},
    ]


@respx.mock
async def test_write_commit_custom_ref_and_message(connector):
    """A custom ref is resolved and the ref update targets refs/heads/<ref>."""
    respx.get("https://api.github.com/repos/owner/repo/commits/develop").mock(
        return_value=httpx.Response(200, json={"sha": "dev123"})
    )
    respx.post("https://api.github.com/repos/owner/repo/git/blobs").mock(
        return_value=httpx.Response(201, json={"sha": "blob1"})
    )
    respx.post("https://api.github.com/repos/owner/repo/git/trees").mock(
        return_value=httpx.Response(201, json={"sha": "tree123"})
    )
    respx.post("https://api.github.com/repos/owner/repo/git/commits").mock(
        return_value=httpx.Response(201, json={"sha": "commit123"})
    )
    ref_route = respx.patch("https://api.github.com/repos/owner/repo/git/refs/refs/heads/develop").mock(
        return_value=httpx.Response(200, json={"ref": "refs/heads/develop"})
    )
    await connector.write(
        ConnectorPayload(
            resource="commit",
            data={
                "repo": "owner/repo",
                "ref": "develop",
                "message": "Custom message",
                "actions": [{"action": "create", "path": "a.txt", "content": "x"}],
            },
        )
    )
    assert "commits/develop" in respx.calls[0].request.url.path
    assert ref_route.calls.last.request.url.path.endswith("/refs/heads/develop")


@respx.mock
async def test_write_commit_full_git_ref_passthrough(connector):
    """An already-qualified refs/... ref is passed through unchanged."""
    respx.get("https://api.github.com/repos/owner/repo/commits/refs/heads/feature").mock(
        return_value=httpx.Response(200, json={"sha": "feat123"})
    )
    respx.post("https://api.github.com/repos/owner/repo/git/blobs").mock(
        return_value=httpx.Response(201, json={"sha": "blob1"})
    )
    respx.post("https://api.github.com/repos/owner/repo/git/trees").mock(
        return_value=httpx.Response(201, json={"sha": "tree123"})
    )
    respx.post("https://api.github.com/repos/owner/repo/git/commits").mock(
        return_value=httpx.Response(201, json={"sha": "commit123"})
    )
    ref_route = respx.patch("https://api.github.com/repos/owner/repo/git/refs/refs/heads/feature").mock(
        return_value=httpx.Response(200, json={"ref": "refs/heads/feature"})
    )
    await connector.write(
        ConnectorPayload(
            resource="commit",
            data={
                "repo": "owner/repo",
                "ref": "refs/heads/feature",
                "actions": [{"action": "create", "path": "a.txt", "content": "x"}],
            },
        )
    )
    assert ref_route.calls.last.request.url.path.endswith("/refs/heads/feature")


@respx.mock
async def test_write_files_alias(connector):
    """write("files") behaves identically to write("commit")."""
    _, tree_route, _, _ = _mock_commit_flow()
    result = await connector.write(
        ConnectorPayload(
            resource="files",
            data={"repo": "owner/repo", "actions": [{"action": "create", "path": "a.txt", "content": "hi"}]},
        )
    )
    assert result["object"]["sha"] == "commit123"
    assert json.loads(tree_route.calls.last.request.content)["tree"][0]["path"] == "a.txt"


@respx.mock
async def test_write_commit_branch_alias(connector):
    """The 'branch' fallback is used when 'ref' is absent."""
    respx.get("https://api.github.com/repos/owner/repo/commits/develop").mock(
        return_value=httpx.Response(200, json={"sha": "dev123"})
    )
    respx.post("https://api.github.com/repos/owner/repo/git/blobs").mock(
        return_value=httpx.Response(201, json={"sha": "blob1"})
    )
    respx.post("https://api.github.com/repos/owner/repo/git/trees").mock(
        return_value=httpx.Response(201, json={"sha": "tree123"})
    )
    respx.post("https://api.github.com/repos/owner/repo/git/commits").mock(
        return_value=httpx.Response(201, json={"sha": "commit123"})
    )
    ref_route = respx.patch("https://api.github.com/repos/owner/repo/git/refs/refs/heads/develop").mock(
        return_value=httpx.Response(200, json={"ref": "refs/heads/develop"})
    )
    await connector.write(
        ConnectorPayload(
            resource="commit",
            data={
                "repo": "owner/repo",
                "branch": "develop",
                "actions": [{"action": "create", "path": "a.txt", "content": "x"}],
            },
        )
    )
    assert "commits/develop" in respx.calls[0].request.url.path
    assert ref_route.calls.last.request.url.path.endswith("/refs/heads/develop")


@respx.mock
async def test_write_commit_duplicate_path_rejected(connector):
    """Two actions targeting the same path in one batch fail fast, before any network call."""
    payload = ConnectorPayload(
        resource="commit",
        data={
            "repo": "owner/repo",
            "actions": [
                {"action": "create", "path": "a.txt", "content": "one"},
                {"action": "update", "path": "a.txt", "content": "two"},
            ],
        },
    )
    with pytest.raises(ValueError, match="targeted more than once"):
        await connector.write(payload)
    assert not respx.calls


@respx.mock
async def test_write_commit_move_same_path_rejected(connector):
    """A move whose previous_path equals its path is ambiguous and rejected."""
    payload = ConnectorPayload(
        resource="commit",
        data={
            "repo": "owner/repo",
            "actions": [{"action": "move", "path": "a.txt", "previous_path": "a.txt"}],
        },
    )
    with pytest.raises(ValueError, match="targeted more than once"):
        await connector.write(payload)
    assert not respx.calls


@respx.mock
async def test_write_commit_move_binary_file_raises_descriptive_error(connector):
    """Moving a non-UTF-8 binary file surfaces a descriptive ValueError, not a raw UnicodeDecodeError."""
    respx.get("https://api.github.com/repos/owner/repo/commits/main").mock(
        return_value=httpx.Response(200, json={"sha": "base123"})
    )
    binary_b64 = base64.b64encode(b"\x80\x81binary\xff").decode()
    respx.get("https://api.github.com/repos/owner/repo/contents/old.bin?ref=main").mock(
        return_value=httpx.Response(200, json={"content": binary_b64, "encoding": "base64"})
    )
    blob_route = respx.post("https://api.github.com/repos/owner/repo/git/blobs").mock(
        return_value=httpx.Response(201, json={"sha": "blob1"})
    )
    payload = ConnectorPayload(
        resource="commit",
        data={
            "repo": "owner/repo",
            "actions": [{"action": "move", "path": "new.txt", "previous_path": "old.bin"}],
        },
    )
    with pytest.raises(ValueError, match="not decodable UTF-8 text"):
        await connector.write(payload)
    assert not blob_route.calls


async def test_write_commit_missing_repo(connector):
    with pytest.raises(ValueError, match="requires 'repo' in data"):
        await connector.write(ConnectorPayload(resource="commit", data={}))


async def test_write_commit_missing_actions(connector):
    with pytest.raises(ValueError, match="non-empty 'actions' list"):
        await connector.write(ConnectorPayload(resource="commit", data={"repo": "owner/repo"}))


async def test_write_commit_empty_actions(connector):
    with pytest.raises(ValueError, match="non-empty 'actions' list"):
        await connector.write(ConnectorPayload(resource="commit", data={"repo": "owner/repo", "actions": []}))


async def test_write_commit_invalid_action(connector):
    with pytest.raises(ValueError, match="must be one of"):
        await connector.write(
            ConnectorPayload(
                resource="commit",
                data={"repo": "owner/repo", "actions": [{"action": "chmod", "path": "a.txt"}]},
            )
        )


async def test_write_commit_missing_path(connector):
    with pytest.raises(ValueError, match="requires 'path'"):
        await connector.write(
            ConnectorPayload(
                resource="commit",
                data={"repo": "owner/repo", "actions": [{"action": "create", "content": "x"}]},
            )
        )


async def test_write_commit_path_traversal_blocked(connector):
    with pytest.raises(ValueError, match="path traversal"):
        await connector.write(
            ConnectorPayload(
                resource="commit",
                data={"repo": "owner/repo", "actions": [{"action": "create", "path": "../evil.txt", "content": "x"}]},
            )
        )


async def test_write_commit_move_missing_previous_path(connector):
    with pytest.raises(ValueError, match="requires 'previous_path'"):
        await connector.write(
            ConnectorPayload(
                resource="commit",
                data={"repo": "owner/repo", "actions": [{"action": "move", "path": "new.txt"}]},
            )
        )


async def test_write_commit_move_previous_path_traversal_blocked(connector):
    with pytest.raises(ValueError, match="path traversal"):
        await connector.write(
            ConnectorPayload(
                resource="commit",
                data={
                    "repo": "owner/repo",
                    "actions": [{"action": "move", "path": "new.txt", "previous_path": "../old.txt"}],
                },
            )
        )


async def test_write_commit_create_missing_content(connector):
    with pytest.raises(ValueError, match="requires string 'content'"):
        await connector.write(
            ConnectorPayload(
                resource="commit",
                data={"repo": "owner/repo", "actions": [{"action": "create", "path": "a.txt"}]},
            )
        )


@respx.mock
async def test_write_commit_unresolvable_ref(connector):
    """A ref that cannot be resolved to a commit SHA raises a descriptive error."""
    respx.get("https://api.github.com/repos/owner/repo/commits/main").mock(
        return_value=httpx.Response(200, json={"message": "not found"})
    )
    with pytest.raises(ValueError, match="could not resolve ref"):
        await connector.write(
            ConnectorPayload(
                resource="commit",
                data={"repo": "owner/repo", "actions": [{"action": "create", "path": "a.txt", "content": "x"}]},
            )
        )


@respx.mock
async def test_write_commit_http_error_propagates(connector):
    """An API error on blob creation surfaces as a ValueError with the status."""
    respx.get("https://api.github.com/repos/owner/repo/commits/main").mock(
        return_value=httpx.Response(200, json={"sha": "base123"})
    )
    respx.post("https://api.github.com/repos/owner/repo/git/blobs").mock(
        return_value=httpx.Response(422, text="Unprocessable")
    )
    with pytest.raises(ValueError, match="422"):
        await connector.write(
            ConnectorPayload(
                resource="commit",
                data={"repo": "owner/repo", "actions": [{"action": "create", "path": "a.txt", "content": "x"}]},
            )
        )


# ---------------------------------------------------------------------------
# Path traversal protection
# ---------------------------------------------------------------------------


async def test_query_file_path_traversal_blocked(connector):
    with pytest.raises(ValueError, match="path traversal"):
        await connector.query(ConnectorQuery(resource="file", filters={"repo": "owner/repo", "path": "../etc/passwd"}))


async def test_query_file_absolute_path_blocked(connector):
    with pytest.raises(ValueError, match="must be relative"):
        await connector.query(ConnectorQuery(resource="file", filters={"repo": "owner/repo", "path": "/etc/passwd"}))


async def test_query_tree_path_traversal_blocked(connector):
    with pytest.raises(ValueError, match="path traversal"):
        await connector.query(ConnectorQuery(resource="tree", filters={"repo": "owner/repo", "path": "src/../secrets"}))


async def test_write_file_path_traversal_blocked(connector):
    with pytest.raises(ValueError, match="path traversal"):
        await connector.write(
            ConnectorPayload(
                resource="file",
                data={"repo": "owner/repo", "path": "../secret.txt", "content": "SGVsbG8="},
            )
        )


async def test_write_file_absolute_path_blocked(connector):
    with pytest.raises(ValueError, match="must be relative"):
        await connector.write(
            ConnectorPayload(
                resource="file",
                data={"repo": "owner/repo", "path": "/tmp/secret.txt", "content": "SGVsbG8="},
            )
        )


# ---------------------------------------------------------------------------
# Rate-limit budget awareness — X-RateLimit-* header inspection + metadata
# ---------------------------------------------------------------------------

RATE_LIMIT_HEADERS = {
    "X-RateLimit-Limit": "5000",
    "X-RateLimit-Remaining": "4999",
    "X-RateLimit-Used": "1",
    "X-RateLimit-Reset": str(int(time.time()) + 60),
    "X-RateLimit-Resource": "core",
}


def test_search_total_coerces_corrupt_values() -> None:
    assert _search_total({"total_count": 25}) == 25
    assert _search_total({"total_count": "25"}) == 25
    assert _search_total({"total_count": 1e999}) == 0
    assert _search_total({"total_count": float("nan")}) == 0
    assert _search_total({"total_count": True}) == 0
    assert _search_total({"total_count": "garbage"}) == 0
    assert _search_total({}) is None


def test_parse_rate_limit_reset_future() -> None:
    response = httpx.Response(200, headers={"X-RateLimit-Reset": str(int(time.time()) + 60)})
    delay = _parse_rate_limit_reset(response)
    assert delay is not None
    assert 0 < delay <= 60


def test_parse_rate_limit_reset_past_returns_none() -> None:
    response = httpx.Response(429, headers={"X-RateLimit-Reset": str(int(time.time()) - 60)})
    assert _parse_rate_limit_reset(response) is None


def test_parse_rate_limit_reset_missing_or_invalid_returns_none() -> None:
    assert _parse_rate_limit_reset(httpx.Response(429)) is None
    assert _parse_rate_limit_reset(httpx.Response(429, headers={"X-RateLimit-Reset": "not-a-number"})) is None


def test_rate_limit_metadata_present_headers() -> None:
    response = httpx.Response(200, headers=RATE_LIMIT_HEADERS)
    meta = _rate_limit_metadata(response)
    assert meta["X-RateLimit-Limit"] == "5000"
    assert meta["X-RateLimit-Remaining"] == "4999"
    assert meta["X-RateLimit-Reset"] == RATE_LIMIT_HEADERS["X-RateLimit-Reset"]


def test_rate_limit_metadata_absent_headers() -> None:
    response = httpx.Response(200)
    assert not _rate_limit_metadata(response)


def test_rate_limit_detail_summarises_quota() -> None:
    response = httpx.Response(429, headers=RATE_LIMIT_HEADERS)
    detail = _rate_limit_detail(response)
    assert "X-RateLimit-Limit=5000" in detail
    assert "X-RateLimit-Remaining=4999" in detail
    assert "X-RateLimit-Reset" in detail


def test_retry_delay_prefers_reset_then_retry_after_then_backoff() -> None:
    connector = GitHubConnector(token=TOKEN)
    future_reset = str(int(time.time()) + 90)
    reset_delay = connector._retry_delay(httpx.Response(429, headers={"X-RateLimit-Reset": future_reset}), 0)
    assert 0 < reset_delay <= 90
    assert connector._retry_delay(httpx.Response(429, headers={"Retry-After": "5"}), 2) == 5.0
    assert connector._retry_delay(httpx.Response(503), 0) == 1.0
    assert connector._retry_delay(httpx.Response(503), 2) == 4.0
    assert connector._retry_delay(httpx.Response(503), 5) == 30.0


def test_retry_delay_reset_past_falls_back_to_retry_after() -> None:
    connector = GitHubConnector(token=TOKEN)
    elapsed = httpx.Response(429, headers={"X-RateLimit-Reset": str(int(time.time()) - 30), "Retry-After": "3"})
    assert connector._retry_delay(elapsed, 0) == 3.0


@respx.mock
async def test_query_result_exposes_rate_limit_metadata(connector):
    repos = [{"id": 1, "name": "repo-a"}]
    respx.get("https://api.github.com/user/repos").mock(
        return_value=httpx.Response(200, json=repos, headers=RATE_LIMIT_HEADERS)
    )
    result = await connector.query(ConnectorQuery(resource="repos", limit=5))
    assert result.records[0]["name"] == "repo-a"
    meta = result.metadata.get("rate_limit", {})
    assert meta["X-RateLimit-Remaining"] == "4999"
    assert meta["X-RateLimit-Limit"] == "5000"


@respx.mock
async def test_query_result_rate_limit_metadata_absent_when_no_headers(connector):
    respx.get("https://api.github.com/user/repos").mock(return_value=httpx.Response(200, json=[]))
    result = await connector.query(ConnectorQuery(resource="repos", limit=5))
    assert not result.metadata.get("rate_limit", {})


@respx.mock
async def test_single_resource_rate_limit_metadata(connector):
    """Single-record queries also expose the rate-limit budget."""
    respx.get("https://api.github.com/repos/owner/repo/issues/1").mock(
        return_value=httpx.Response(200, json={"id": 1}, headers=RATE_LIMIT_HEADERS)
    )
    result = await connector.query(
        ConnectorQuery(resource="issue", filters={"repo": "owner/repo", "issue_number": "1"})
    )
    assert result.metadata["rate_limit"]["X-RateLimit-Remaining"] == "4999"


@respx.mock
async def test_query_tree_rate_limit_metadata(connector):
    """The Git Trees API response's quota headers flow through to the result."""
    respx.get("https://api.github.com/repos/owner/repo/commits/main").mock(
        return_value=httpx.Response(200, json={"sha": "abc123"})
    )
    respx.get("https://api.github.com/repos/owner/repo/git/trees/abc123").mock(
        return_value=httpx.Response(200, json={"tree": [{"path": "README.md"}]}, headers=RATE_LIMIT_HEADERS)
    )
    result = await connector.query(ConnectorQuery(resource="tree", filters={"repo": "owner/repo"}))
    assert result.metadata["rate_limit"]["X-RateLimit-Limit"] == "5000"


@respx.mock
async def test_query_rate_limit_resource(connector):
    body = {
        "resources": {
            "core": {"limit": 5000, "remaining": 4999, "reset": int(time.time()) + 60, "used": 1, "resource": "core"},
            "search": {"limit": 30, "remaining": 30, "reset": int(time.time()) + 60, "used": 0, "resource": "search"},
        }
    }
    respx.get("https://api.github.com/rate_limit").mock(
        return_value=httpx.Response(200, json=body, headers=RATE_LIMIT_HEADERS)
    )
    result = await connector.query(ConnectorQuery(resource="rate_limit"))
    assert len(result.records) == 1
    assert result.records[0]["core"]["remaining"] == 4999
    assert result.records[0]["search"]["remaining"] == 30
    assert result.metadata["rate_limit"]["X-RateLimit-Remaining"] == "4999"


@respx.mock
async def test_query_rate_limit_resource_missing_resources(connector):
    respx.get("https://api.github.com/rate_limit").mock(return_value=httpx.Response(200, json={}))
    result = await connector.query(ConnectorQuery(resource="rate_limit"))
    assert result.records == [{}]


@respx.mock
async def test_exhausted_429_reports_quota_detail(connector):
    route = respx.get("https://api.github.com/user/repos")
    route.mock(
        return_value=httpx.Response(
            429,
            text="API rate limit exceeded",
            headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(int(time.time()) + 600)},
        )
    )
    connector._sleep_delay = lambda response, attempt: 0
    with pytest.raises(ValueError, match="quota") as exc_info:
        await connector.query(ConnectorQuery(resource="repos"))
    assert "X-RateLimit-Remaining=0" in str(exc_info.value)
    assert route.call_count == 4


@respx.mock
async def test_429_retry_then_success_respects_reset_window(connector):
    route = respx.get("https://api.github.com/user/repos")
    route.mock(
        side_effect=[
            httpx.Response(429, text="Rate limit", headers={"X-RateLimit-Reset": str(int(time.time()) + 1)}),
            httpx.Response(200, json=[{"id": 1}], headers=RATE_LIMIT_HEADERS),
        ]
    )
    connector._sleep_delay = lambda response, attempt: 0
    result = await connector.query(ConnectorQuery(resource="repos"))
    assert len(result.records) == 1
    assert route.call_count == 2
    assert result.metadata["rate_limit"]["X-RateLimit-Remaining"] == "4999"
