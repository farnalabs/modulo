"""Unit tests for GiteaConnector — HTTP responses are mocked via httpx."""

import httpx
import pytest
import respx

from modulo.connectors.base import ConnectorPayload, ConnectorQuery, ConnectorType
from modulo.connectors.gitea import GiteaConnector

TOKEN = "gitea_test_token"
_API = "https://codeberg.org/api/v1"


@pytest.fixture()
def connector():
    return GiteaConnector(token=TOKEN)


@respx.mock
async def test_health_check_ok(connector):
    respx.get(f"{_API}/user").mock(return_value=httpx.Response(200, json={"login": "myuser"}))
    respx.get(f"{_API}/repos").mock(return_value=httpx.Response(200, json=[{"id": 1}]))
    result = await connector.health_check()
    assert result.ok is True
    assert result.detail == "myuser"


@respx.mock
async def test_health_check_missing_scopes(connector):
    respx.get(f"{_API}/user").mock(return_value=httpx.Response(200, json={"login": "myuser"}))
    respx.get(f"{_API}/repos").mock(return_value=httpx.Response(403, text="forbidden"))
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
async def test_query_repos(connector):
    repos = [{"id": 1, "name": "repo-a"}, {"id": 2, "name": "repo-b"}]
    respx.get(f"{_API}/user/repos").mock(return_value=httpx.Response(200, json=repos))
    result = await connector.query(ConnectorQuery(resource="repos"))
    assert len(result.records) == 2
    assert result.records[0]["name"] == "repo-a"


@respx.mock
async def test_query_file(connector):
    file_data = {"name": "README.md", "content": "SGVsbG8gV29ybGQ=", "encoding": "base64"}
    respx.get(f"{_API}/repos/owner/repo/contents/README.md").mock(
        return_value=httpx.Response(200, json=file_data)
    )
    result = await connector.query(
        ConnectorQuery(
            resource="file",
            filters={"repo": "owner/repo", "path": "README.md", "ref": "main"},
        )
    )
    assert result.records[0]["name"] == "README.md"
    assert result.records[0]["content"] == "Hello World"


@respx.mock
async def test_query_pulls(connector):
    prs = [{"number": 42, "title": "Fix bug"}]
    respx.get(f"{_API}/repos/owner/repo/pulls").mock(return_value=httpx.Response(200, json=prs))
    result = await connector.query(ConnectorQuery(resource="pulls", filters={"repo": "owner/repo"}))
    assert result.records[0]["number"] == 42


@respx.mock
async def test_query_issues(connector):
    issues = [{"id": 1, "title": "Bug report", "state": "open"}]
    respx.get(f"{_API}/repos/owner/repo/issues").mock(return_value=httpx.Response(200, json=issues))
    result = await connector.query(ConnectorQuery(resource="issues", filters={"repo": "owner/repo"}))
    assert result.records[0]["id"] == 1


@respx.mock
async def test_write_file(connector):
    response_body = {"content": {"sha": "def456"}, "commit": {"sha": "ghi789"}}
    respx.put(f"{_API}/repos/owner/repo/contents/path/file.txt").mock(
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


@respx.mock
async def test_write_pull_request(connector):
    pr_response = {"number": 42, "title": "Add feature", "state": "open"}
    respx.post(f"{_API}/repos/owner/repo/pulls").mock(
        return_value=httpx.Response(200, json=pr_response)
    )
    result = await connector.write(
        ConnectorPayload(
            resource="pull",
            data={
                "repo": "owner/repo",
                "title": "Add feature",
                "head": "feature-branch",
                "base": "main",
                "body": "Implements the feature",
            },
        )
    )
    assert result["number"] == 42
    assert result["title"] == "Add feature"


@respx.mock
async def test_write_issue(connector):
    issue_response = {"id": 100, "number": 10, "title": "Bug report", "state": "open"}
    respx.post(f"{_API}/repos/owner/repo/issues").mock(
        return_value=httpx.Response(200, json=issue_response)
    )
    result = await connector.write(
        ConnectorPayload(
            resource="issue",
            data={
                "repo": "owner/repo",
                "title": "Bug report",
                "body": "Something broke",
                "assignees": ["user1"],
                "labels": ["bug"],
            },
        )
    )
    assert result["id"] == 100
    assert result["title"] == "Bug report"


async def test_unsupported_query_resource(connector):
    with pytest.raises(ValueError, match="Unsupported Gitea resource"):
        await connector.query(ConnectorQuery(resource="unknown"))


async def test_unsupported_write_resource(connector):
    with pytest.raises(ValueError, match="Unsupported Gitea write resource"):
        await connector.write(ConnectorPayload(resource="branch", data={}))


def test_connector_type(connector):
    assert connector.connector_type == ConnectorType.GITEA


@respx.mock
async def test_custom_base_url():
    custom = GiteaConnector(token=TOKEN, base_url="https://gitea.example.com")
    _api = "https://gitea.example.com/api/v1"
    respx.get(f"{_api}/user").mock(return_value=httpx.Response(200, json={"login": "admin"}))
    respx.get(f"{_api}/repos").mock(return_value=httpx.Response(200, json=[]))
    result = await custom.health_check()
    assert result.ok is True
    assert result.detail == "admin"
