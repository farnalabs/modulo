"""Resilience unit tests for GitLabConnector — error wrapping for pipeline safety."""

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


@respx.mock
async def test_query_429_rate_limit_returns_value_error(connector):
    """HTTP 429 should be wrapped as ValueError, not raw HTTPStatusError."""
    respx.get(f"{_API}/projects/group%2Fproject/issues").mock(
        return_value=httpx.Response(429, text="Rate limit exceeded"),
    )
    with pytest.raises(ValueError, match="GitLab API HTTP 429"):
        await connector.query(ConnectorQuery(resource="issues", filters={"project": "group/project"}))


@respx.mock
async def test_query_500_error_returns_value_error(connector):
    """HTTP 500 should be wrapped as ValueError."""
    respx.get(f"{_API}/projects/group%2Fproject/issues").mock(
        return_value=httpx.Response(500, text="Internal Server Error"),
    )
    with pytest.raises(ValueError, match="GitLab API HTTP 500"):
        await connector.query(ConnectorQuery(resource="issues", filters={"project": "group/project"}))


@respx.mock
async def test_write_429_rate_limit_returns_value_error(connector):
    """HTTP 429 on write should be wrapped as ValueError."""
    respx.post(f"{_API}/projects/group%2Fproject/issues").mock(
        return_value=httpx.Response(429, text="Rate limit exceeded"),
    )
    with pytest.raises(ValueError, match="GitLab API HTTP 429"):
        await connector.write(
            ConnectorPayload(
                resource="issue",
                data={"project": "group/project", "title": "Test"},
            )
        )


@respx.mock
async def test_query_connection_error_returns_value_error(connector):
    """Connection error should be wrapped as ValueError."""
    respx.get(f"{_API}/projects/group%2Fproject/issues").mock(
        side_effect=httpx.ConnectError("Connection refused"),
    )
    with pytest.raises(ValueError, match="GitLab API connection error"):
        await connector.query(ConnectorQuery(resource="issues", filters={"project": "group/project"}))


@respx.mock
async def test_query_timeout_returns_value_error(connector):
    """Timeout should be wrapped as ValueError."""
    respx.get(f"{_API}/projects/group%2Fproject/issues").mock(
        side_effect=httpx.TimeoutException("Request timed out"),
    )
    with pytest.raises(ValueError, match="GitLab API connection error"):
        await connector.query(ConnectorQuery(resource="issues", filters={"project": "group/project"}))


@respx.mock
async def test_query_invalid_json_returns_value_error(connector):
    """Malformed JSON response should be wrapped as ValueError."""
    respx.get(f"{_API}/projects/group%2Fproject/issues").mock(
        return_value=httpx.Response(200, text="not-json"),
    )
    with pytest.raises(ValueError, match="GitLab API invalid response"):
        await connector.query(ConnectorQuery(resource="issues", filters={"project": "group/project"}))


@respx.mock
async def test_health_check_invalid_json(connector):
    """Health check should handle invalid JSON gracefully."""
    respx.get(f"{_API}/user").mock(
        return_value=httpx.Response(200, text="not-json"),
    )
    result = await connector.health_check()
    assert result.ok is False
    assert "Invalid JSON" in result.detail
