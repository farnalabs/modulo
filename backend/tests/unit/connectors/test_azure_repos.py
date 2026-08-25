"""Unit tests for AzureReposConnector — HTTP responses are mocked via httpx."""

import base64

import httpx
import pytest
import respx

from modulo.connectors._safe_page import safe_paging_total as _paging_total
from modulo.connectors.azure_repos import AzureReposConnector
from modulo.connectors.base import ConnectorPayload, ConnectorQuery, ConnectorType

TOKEN = "azure_test_token"
ORG = "myorg"
_BASE = "https://dev.azure.com/myorg"
_PROFILE_URL = "https://app.vssps.visualstudio.com/_apis/profile/profiles/me"


@pytest.fixture
def connector():
    return AzureReposConnector(token=TOKEN, organization=ORG)


@respx.mock
async def test_health_check_ok(connector):
    respx.get(_PROFILE_URL, params={"api-version": "7.0"}).mock(
        return_value=httpx.Response(200, json={"displayName": "Duncan Tait"})
    )
    result = await connector.health_check()
    assert result.ok is True
    assert result.detail == "Duncan Tait"


@respx.mock
async def test_health_check_fail(connector):
    respx.get(_PROFILE_URL, params={"api-version": "7.0"}).mock(return_value=httpx.Response(401, text="Unauthorized"))
    result = await connector.health_check()
    assert result.ok is False
    assert "401" in result.detail


@respx.mock
async def test_query_repos(connector):
    repos = [{"id": "repo-1", "name": "frontend"}, {"id": "repo-2", "name": "backend"}]
    respx.get(f"{_BASE}/myproject/_apis/git/repositories", params={"api-version": "7.0"}).mock(
        return_value=httpx.Response(200, json={"value": repos, "count": 2})
    )
    result = await connector.query(ConnectorQuery(resource="repos", filters={"project": "myproject"}))
    assert len(result.records) == 2
    assert result.records[0]["name"] == "frontend"
    assert result.total == 2


@respx.mock
async def test_query_file(connector):
    file_content = "# Hello Azure Repos"
    respx.get(
        f"{_BASE}/myproject/_apis/git/repositories/myrepo/items",
        params={"path": "README.md", "versionDescriptor.version": "main", "api-version": "7.0"},
    ).mock(return_value=httpx.Response(200, text=file_content))
    result = await connector.query(
        ConnectorQuery(
            resource="file",
            filters={"project": "myproject", "repo": "myrepo", "path": "README.md", "ref": "main"},
        )
    )
    assert result.records[0]["content"] == "# Hello Azure Repos"
    assert result.records[0]["path"] == "README.md"
    assert result.records[0]["ref"] == "main"


@respx.mock
async def test_query_pulls(connector):
    prs = [{"pullRequestId": 42, "title": "Fix bug", "status": "active"}]
    respx.get(
        f"{_BASE}/myproject/_apis/git/repositories/myrepo/pullrequests",
        params={"searchCriteria.status": "active", "api-version": "7.0"},
    ).mock(return_value=httpx.Response(200, json={"value": prs, "count": 1}))
    result = await connector.query(ConnectorQuery(resource="pulls", filters={"project": "myproject", "repo": "myrepo"}))
    assert len(result.records) == 1
    assert result.records[0]["pullRequestId"] == 42


@respx.mock
async def test_query_commits(connector):
    commits = [{"commitId": "abc123", "comment": "Initial commit"}]
    respx.get(
        f"{_BASE}/myproject/_apis/git/repositories/myrepo/commits",
        params={"searchCriteria.itemVersion.version": "main", "api-version": "7.0"},
    ).mock(return_value=httpx.Response(200, json={"value": commits, "count": 1}))
    result = await connector.query(
        ConnectorQuery(resource="commits", filters={"project": "myproject", "repo": "myrepo"})
    )
    assert result.records[0]["commitId"] == "abc123"


@respx.mock
async def test_query_pulls_with_state(connector):
    prs = [{"pullRequestId": 7, "title": "Old PR", "status": "completed"}]
    respx.get(
        f"{_BASE}/myproject/_apis/git/repositories/myrepo/pullrequests",
        params={"searchCriteria.status": "completed", "api-version": "7.0"},
    ).mock(return_value=httpx.Response(200, json={"value": prs, "count": 1}))
    result = await connector.query(
        ConnectorQuery(
            resource="pulls",
            filters={"project": "myproject", "repo": "myrepo", "state": "completed"},
        )
    )
    assert result.records[0]["pullRequestId"] == 7


@respx.mock
async def test_query_commits_with_branch(connector):
    commits = [{"commitId": "def456", "comment": "Fix"}]
    respx.get(
        f"{_BASE}/myproject/_apis/git/repositories/myrepo/commits",
        params={"searchCriteria.itemVersion.version": "develop", "api-version": "7.0"},
    ).mock(return_value=httpx.Response(200, json={"value": commits, "count": 1}))
    result = await connector.query(
        ConnectorQuery(
            resource="commits",
            filters={"project": "myproject", "repo": "myrepo", "branch": "develop"},
        )
    )
    assert result.records[0]["commitId"] == "def456"


@respx.mock
async def test_write_file(connector):
    respx.get(
        f"{_BASE}/myproject/_apis/git/repositories/myrepo/refs",
        params={"filter": "heads/main", "api-version": "7.0"},
    ).mock(return_value=httpx.Response(200, json={"value": [{"objectId": "oldoid123"}]}))
    push_response = {"pushId": 1, "commitIds": ["newcommit456"]}
    respx.post(
        f"{_BASE}/myproject/_apis/git/repositories/myrepo/pushes",
        params={"api-version": "7.0"},
    ).mock(return_value=httpx.Response(200, json=push_response))
    result = await connector.write(
        ConnectorPayload(
            resource="file",
            data={
                "project": "myproject",
                "repo": "myrepo",
                "path": "src/main.py",
                "content": "print('hello')",
                "message": "Add main.py",
                "branch": "main",
            },
        )
    )
    assert result["pushId"] == 1
    assert result["commitIds"] == ["newcommit456"]


@respx.mock
async def test_write_file_branch_not_found(connector):
    respx.get(
        f"{_BASE}/myproject/_apis/git/repositories/myrepo/refs",
        params={"filter": "heads/nonexistent", "api-version": "7.0"},
    ).mock(return_value=httpx.Response(200, json={"value": []}))
    with pytest.raises(ValueError, match="not found"):
        await connector.write(
            ConnectorPayload(
                resource="file",
                data={
                    "project": "myproject",
                    "repo": "myrepo",
                    "path": "src/main.py",
                    "content": "print('x')",
                    "branch": "nonexistent",
                },
            )
        )


@respx.mock
async def test_write_pull(connector):
    pr_response = {"pullRequestId": 99, "title": "Add feature", "status": "active"}
    respx.post(
        f"{_BASE}/myproject/_apis/git/repositories/myrepo/pullrequests",
        params={"api-version": "7.0"},
    ).mock(return_value=httpx.Response(200, json=pr_response))
    result = await connector.write(
        ConnectorPayload(
            resource="pull",
            data={
                "project": "myproject",
                "repo": "myrepo",
                "title": "Add feature",
                "source_branch": "feature-branch",
                "target_branch": "main",
                "description": "Implements the feature",
            },
        )
    )
    assert result["pullRequestId"] == 99


@respx.mock
async def test_write_pull_without_description(connector):
    pr_response = {"pullRequestId": 100, "title": "Quick fix"}
    respx.post(
        f"{_BASE}/myproject/_apis/git/repositories/myrepo/pullrequests",
        params={"api-version": "7.0"},
    ).mock(return_value=httpx.Response(200, json=pr_response))
    result = await connector.write(
        ConnectorPayload(
            resource="pull",
            data={
                "project": "myproject",
                "repo": "myrepo",
                "title": "Quick fix",
                "source_branch": "hotfix",
            },
        )
    )
    assert result["pullRequestId"] == 100


async def test_unsupported_query_resource(connector):
    with pytest.raises(ValueError, match="Unsupported Azure Repos resource"):
        await connector.query(ConnectorQuery(resource="unknown"))


async def test_unsupported_write_resource(connector):
    with pytest.raises(ValueError, match="Unsupported Azure Repos write resource"):
        await connector.write(ConnectorPayload(resource="branch", data={}))


def test_connector_type(connector):
    assert connector.connector_type == ConnectorType.AZURE_REPOS


@respx.mock
async def test_query_repos_corrupt_total(connector):
    """A non-finite ``count`` must not poison the reported total."""
    respx.get(f"{_BASE}/myproject/_apis/git/repositories", params={"api-version": "7.0"}).mock(
        return_value=httpx.Response(200, content=b'{"value": [{"id": "repo-1"}], "count": 1e999}')
    )
    result = await connector.query(ConnectorQuery(resource="repos", filters={"project": "myproject"}))
    assert len(result.records) == 1
    assert result.total == 0


@respx.mock
async def test_query_repos_corrupt_body_no_crash(connector):
    """A corrupt/hostile response returning a non-dict body must not crash the
    connector — it falls back to an empty page with no total."""
    respx.get(f"{_BASE}/myproject/_apis/git/repositories", params={"api-version": "7.0"}).mock(
        return_value=httpx.Response(200, json=["garbage"])
    )
    result = await connector.query(ConnectorQuery(resource="repos", filters={"project": "myproject"}))
    assert not result.records
    assert result.total is None


@respx.mock
async def test_query_repos_non_list_value_no_crash(connector):
    """A corrupt body placing a non-list in ``value`` must fall back to an
    empty page instead of returning a bare string as the records list."""
    respx.get(f"{_BASE}/myproject/_apis/git/repositories", params={"api-version": "7.0"}).mock(
        return_value=httpx.Response(200, json={"value": "not-a-list", "count": 2})
    )
    result = await connector.query(ConnectorQuery(resource="repos", filters={"project": "myproject"}))
    assert not result.records
    assert result.total == 2


@respx.mock
async def test_health_check_corrupt_body_no_crash(connector):
    """A corrupt/hostile profile response with a non-dict body must not crash
    health_check — it reports success with an empty display name."""
    respx.get(_PROFILE_URL, params={"api-version": "7.0"}).mock(return_value=httpx.Response(200, json=["garbage"]))
    result = await connector.health_check()
    assert result.ok is True
    assert not result.detail


@respx.mock
async def test_query_commits_corrupt_body_no_crash(connector):
    respx.get(
        f"{_BASE}/myproject/_apis/git/repositories/myrepo/commits",
        params={"searchCriteria.itemVersion.version": "main", "api-version": "7.0"},
    ).mock(return_value=httpx.Response(200, json=["garbage"]))
    result = await connector.query(
        ConnectorQuery(resource="commits", filters={"project": "myproject", "repo": "myrepo"})
    )
    assert not result.records
    assert result.total is None


def test_paging_total() -> None:
    assert _paging_total({"count": 25}, "count") == 25
    assert _paging_total({"count": "25"}, "count") == 25
    assert _paging_total({"count": 1e999}, "count") == 0
    assert _paging_total({"count": float("nan")}, "count") == 0
    assert _paging_total({"count": True}, "count") == 0
    assert _paging_total({"count": "garbage"}, "count") == 0
    assert _paging_total({}, "count") is None
    assert _paging_total(["garbage"], "count") is None


def test_auth_header_format():
    c = AzureReposConnector(token="pat123", organization="testorg")
    auth = c._headers()["Authorization"]
    assert "Basic" in auth
    expected = base64.b64encode(b":pat123").decode()
    assert auth == f"Basic {expected}"
