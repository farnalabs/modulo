"""Resilience tests for GitHubConnector — HTTP/network errors wrapped as ValueError."""
import httpx
import pytest
import respx

from modulo.connectors.base import ConnectorPayload, ConnectorQuery
from modulo.connectors.github import GitHubConnector

TOKEN = "ghp_test_token"


@pytest.fixture()
def connector():
    return GitHubConnector(token=TOKEN)


@respx.mock
async def test_query_repos_429_rate_limit(connector):
    """429 rate limit raises ValueError with status code."""
    respx.get("https://api.github.com/user/repos").mock(
        return_value=httpx.Response(429, text="Rate limit exceeded")
    )
    with pytest.raises(ValueError, match="429"):
        await connector.query(ConnectorQuery(resource="repos"))


@respx.mock
async def test_query_repos_500_error(connector):
    """500 server error raises ValueError with status code."""
    respx.get("https://api.github.com/user/repos").mock(
        return_value=httpx.Response(500, text="Server Error")
    )
    with pytest.raises(ValueError, match="500"):
        await connector.query(ConnectorQuery(resource="repos"))


@respx.mock
async def test_query_repos_connection_error(connector):
    """Connection error raises ValueError with descriptive message."""
    respx.get("https://api.github.com/user/repos").mock(
        side_effect=httpx.ConnectError("Connection refused")
    )
    with pytest.raises(ValueError, match="connection error"):
        await connector.query(ConnectorQuery(resource="repos"))


@respx.mock
async def test_query_repos_invalid_json(connector):
    """Invalid JSON response raises ValueError."""
    respx.get("https://api.github.com/user/repos").mock(
        return_value=httpx.Response(200, text="not-json")
    )
    with pytest.raises(ValueError, match="invalid JSON"):
        await connector.query(ConnectorQuery(resource="repos"))


@respx.mock
async def test_write_file_429_rate_limit(connector):
    """Write file with 429 raises ValueError with status code."""
    respx.put("https://api.github.com/repos/owner/repo/contents/test.txt").mock(
        return_value=httpx.Response(429, text="Rate limit exceeded")
    )
    with pytest.raises(ValueError, match="429"):
        await connector.write(
            ConnectorPayload(
                resource="file",
                data={"repo": "owner/repo", "path": "test.txt", "content": "data"},
            )
        )


@respx.mock
async def test_health_check_connection_error(connector):
    """Health check returns HealthResult(ok=False) on connection error."""
    respx.get("https://api.github.com/user").mock(
        side_effect=httpx.ConnectError("Connection refused")
    )
    result = await connector.health_check()
    assert result.ok is False
    assert "connection error" in result.detail.lower()


@respx.mock
async def test_health_check_invalid_json(connector):
    """Health check returns HealthResult(ok=False) on invalid JSON."""
    respx.get("https://api.github.com/user").mock(
        return_value=httpx.Response(200, text="not-json", headers={"X-OAuth-Scopes": "repo, read:org"}),
    )
    result = await connector.health_check()
    assert result.ok is False
    assert "invalid JSON" in result.detail
