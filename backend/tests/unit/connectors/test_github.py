"""Unit tests for GitHubConnector — HTTP responses are mocked via httpx."""

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
# Error path tests — HTTP errors on query/write operations
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_repos_http_error(connector):
    respx.get("https://api.github.com/user/repos").mock(return_value=httpx.Response(403, text="Forbidden"))
    with pytest.raises(httpx.HTTPStatusError):
        await connector.query(ConnectorQuery(resource="repos"))


@respx.mock
async def test_query_file_http_error(connector):
    respx.get("https://api.github.com/repos/owner/repo/contents/missing.py").mock(
        return_value=httpx.Response(404, text="Not Found")
    )
    with pytest.raises(httpx.HTTPStatusError):
        await connector.query(
            ConnectorQuery(resource="file", filters={"repo": "owner/repo", "path": "missing.py"})
        )


@respx.mock
async def test_query_pulls_http_error(connector):
    respx.get("https://api.github.com/repos/owner/repo/pulls").mock(
        return_value=httpx.Response(500, text="Server Error")
    )
    with pytest.raises(httpx.HTTPStatusError):
        await connector.query(ConnectorQuery(resource="pulls", filters={"repo": "owner/repo"}))


@respx.mock
async def test_write_file_http_error(connector):
    respx.put("https://api.github.com/repos/owner/repo/contents/bad.txt").mock(
        return_value=httpx.Response(422, text="Unprocessable")
    )
    with pytest.raises(httpx.HTTPStatusError):
        await connector.write(
            ConnectorPayload(
                resource="file",
                data={"repo": "owner/repo", "path": "bad.txt", "content": "data"},
            )
        )


@respx.mock
async def test_query_file_missing_filters(connector):
    with pytest.raises(KeyError):
        await connector.query(ConnectorQuery(resource="file", filters={"repo": "owner/repo"}))


@respx.mock
async def test_query_pulls_missing_repo_filter(connector):
    with pytest.raises(KeyError):
        await connector.query(ConnectorQuery(resource="pulls"))


@respx.mock
async def test_write_file_missing_content(connector):
    with pytest.raises(KeyError):
        await connector.write(
            ConnectorPayload(resource="file", data={"repo": "owner/repo", "path": "x"})
        )


@respx.mock
async def test_health_check_non_json_response(connector):
    respx.get("https://api.github.com/user").mock(
        return_value=httpx.Response(200, text="not-json", headers={"X-OAuth-Scopes": "repo, read:org"})
    )
    with pytest.raises((ValueError, KeyError)):
        await connector.health_check()


@respx.mock
async def test_health_check_api_error(connector):
    respx.get("https://api.github.com/user").mock(return_value=httpx.Response(503, text="Service Unavailable"))
    result = await connector.health_check()
    assert result.ok is False
    assert "503" in result.detail


@respx.mock
async def test_query_repos_passes_limit(connector):
    respx.get("https://api.github.com/user/repos?per_page=5").mock(
        return_value=httpx.Response(200, json=[])
    )
    result = await connector.query(ConnectorQuery(resource="repos", limit=5))
    assert result.total == 0
