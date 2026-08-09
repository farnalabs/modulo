"""Structured error hierarchy tests for GitHubConnector.

Covers the machine-parseable error codes and the expired-token vs
insufficient-scope vs rate-limit vs network distinction that lets callers
branch on GitHub failures without parsing human-readable messages.
"""

import httpx
import pytest
import respx

from modulo.connectors.base import ConnectorQuery
from modulo.connectors.github import (
    GitHubAPIError,
    GitHubAuthError,
    GitHubConnector,
    GitHubError,
    GitHubNetworkError,
    GitHubNotFoundError,
    GitHubRateLimitError,
    _error_for_status,
)

TOKEN = "ghp_test_token"


@pytest.fixture
def connector():
    return GitHubConnector(token=TOKEN)


# ---------------------------------------------------------------------------
# Error class hierarchy
# ---------------------------------------------------------------------------


def test_error_hierarchy():
    """All GitHub errors derive from GitHubError and remain ValueError-compatible."""
    assert issubclass(GitHubError, ValueError)
    assert issubclass(GitHubAPIError, GitHubError)
    assert issubclass(GitHubRateLimitError, GitHubAPIError)
    assert issubclass(GitHubAuthError, GitHubAPIError)
    assert issubclass(GitHubNotFoundError, GitHubAPIError)
    assert issubclass(GitHubNetworkError, GitHubError)
    assert not issubclass(GitHubNetworkError, GitHubAPIError)


def test_default_error_codes():
    """Each error type exposes a stable machine-parseable default error_code."""
    assert GitHubError("x").error_code == "github_error"
    assert GitHubAPIError("x").error_code == "api_error"
    assert GitHubRateLimitError("x").error_code == "rate_limited"
    assert GitHubAuthError("x").error_code == "authentication_failed"
    assert GitHubNotFoundError("x").error_code == "not_found"
    assert GitHubNetworkError("x").error_code == "network_error"


def test_error_holds_status_code():
    """Typed errors record the originating HTTP status for programmatic use."""
    err = GitHubAuthError("x", status_code=403, error_code="insufficient_scope")
    assert err.status_code == 403
    assert err.error_code == "insufficient_scope"
    assert GitHubNetworkError("x").status_code is None


def test_error_for_status_mapping():
    """_error_for_status maps each status to the correct type + code."""
    assert isinstance(_error_for_status(429, "x"), GitHubRateLimitError)
    assert isinstance(_error_for_status(401, "x"), GitHubAuthError)
    assert isinstance(_error_for_status(403, "x"), GitHubAuthError)
    assert isinstance(_error_for_status(404, "x"), GitHubNotFoundError)
    assert isinstance(_error_for_status(500, "x"), GitHubAPIError)
    assert isinstance(_error_for_status(422, "x"), GitHubAPIError)

    assert _error_for_status(401, "x").error_code == "token_expired"
    assert _error_for_status(403, "x").error_code == "insufficient_scope"
    assert _error_for_status(429, "x").error_code == "rate_limited"
    assert _error_for_status(404, "x").error_code == "not_found"
    assert _error_for_status(500, "x").error_code == "api_error"


# ---------------------------------------------------------------------------
# _call_api raises typed errors on query/write paths
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_401_raises_auth_error(connector):
    """An expired/invalid token surfaces as GitHubAuthError with token_expired."""
    respx.get("https://api.github.com/user/repos").mock(return_value=httpx.Response(401, text="Bad credentials"))
    with pytest.raises(GitHubAuthError) as excinfo:
        await connector.query(ConnectorQuery(resource="repos"))
    assert excinfo.value.error_code == "token_expired"
    assert excinfo.value.status_code == 401


@respx.mock
async def test_query_403_raises_insufficient_scope(connector):
    """A permission denial surfaces as GitHubAuthError with insufficient_scope."""
    respx.get("https://api.github.com/user/repos").mock(return_value=httpx.Response(403, text="Forbidden"))
    with pytest.raises(GitHubAuthError) as excinfo:
        await connector.query(ConnectorQuery(resource="repos"))
    assert excinfo.value.error_code == "insufficient_scope"
    assert excinfo.value.status_code == 403


@respx.mock
async def test_query_404_raises_not_found(connector):
    """A missing resource surfaces as GitHubNotFoundError with not_found."""
    respx.get("https://api.github.com/user/repos").mock(return_value=httpx.Response(404, text="Not Found"))
    with pytest.raises(GitHubNotFoundError) as excinfo:
        await connector.query(ConnectorQuery(resource="repos"))
    assert excinfo.value.error_code == "not_found"
    assert excinfo.value.status_code == 404


@respx.mock
async def test_query_500_raises_api_error(connector):
    """A generic 5xx surfaces as GitHubAPIError with api_error."""
    respx.get("https://api.github.com/user/repos").mock(return_value=httpx.Response(500, text="Server Error"))
    with pytest.raises(GitHubAPIError) as excinfo:
        await connector.query(ConnectorQuery(resource="repos"))
    assert excinfo.value.error_code == "api_error"
    assert not isinstance(excinfo.value, GitHubAuthError)


@respx.mock
async def test_query_exhausted_429_raises_rate_limit_error(connector, monkeypatch):
    """Exhausted 429 (after retries) surfaces as GitHubRateLimitError."""

    async def no_sleep(delay: float) -> None:
        pass

    monkeypatch.setattr("modulo.connectors.github.asyncio.sleep", no_sleep)
    route = respx.get("https://api.github.com/user/repos")
    route.mock(return_value=httpx.Response(429, text="Rate limit"))
    with pytest.raises(GitHubRateLimitError) as excinfo:
        await connector.query(ConnectorQuery(resource="repos"))
    assert excinfo.value.error_code == "rate_limited"
    assert route.call_count == 4


@respx.mock
async def test_query_timeout_raises_network_error(connector, monkeypatch):
    """Timeout surfaces as GitHubNetworkError with network_timeout."""

    async def no_sleep(delay: float) -> None:
        pass

    monkeypatch.setattr("modulo.connectors.github.asyncio.sleep", no_sleep)
    respx.get("https://api.github.com/user/repos").mock(side_effect=httpx.TimeoutException("Timed out"))
    with pytest.raises(GitHubNetworkError) as excinfo:
        await connector.query(ConnectorQuery(resource="repos"))
    assert excinfo.value.error_code == "network_timeout"


@respx.mock
async def test_query_connect_error_raises_network_error(connector, monkeypatch):
    """Connection failure surfaces as GitHubNetworkError with network_connection."""

    async def no_sleep(delay: float) -> None:
        pass

    monkeypatch.setattr("modulo.connectors.github.asyncio.sleep", no_sleep)
    respx.get("https://api.github.com/user/repos").mock(side_effect=httpx.ConnectError("Connection refused"))
    with pytest.raises(GitHubNetworkError) as excinfo:
        await connector.query(ConnectorQuery(resource="repos"))
    assert excinfo.value.error_code == "network_connection"


@respx.mock
async def test_query_invalid_json_raises_api_error(connector):
    """A malformed response surfaces as GitHubAPIError with invalid_response."""
    respx.get("https://api.github.com/user/repos").mock(return_value=httpx.Response(200, text="not-json"))
    with pytest.raises(GitHubAPIError) as excinfo:
        await connector.query(ConnectorQuery(resource="repos"))
    assert excinfo.value.error_code == "invalid_response"


# ---------------------------------------------------------------------------
# health_check distinguishes failure modes
# ---------------------------------------------------------------------------


@respx.mock
async def test_health_check_expired_token(connector):
    """401 reports an invalid/expired token explicitly."""
    respx.get("https://api.github.com/user").mock(return_value=httpx.Response(401, text="Bad credentials"))
    result = await connector.health_check()
    assert result.ok is False
    assert "Invalid or expired GitHub token (HTTP 401)" in result.detail


@respx.mock
async def test_health_check_missing_permission(connector):
    """403 reports a token that lacks the required permission."""
    respx.get("https://api.github.com/user").mock(return_value=httpx.Response(403, text="Forbidden"))
    result = await connector.health_check()
    assert result.ok is False
    assert "Missing scopes" in result.detail
    assert "HTTP 403" in result.detail


@respx.mock
async def test_health_check_rate_limited(connector, monkeypatch):
    """Exhausted 429 reports the rate-limit failure distinctly."""

    async def no_sleep(delay: float) -> None:
        pass

    monkeypatch.setattr("modulo.connectors.github.asyncio.sleep", no_sleep)
    respx.get("https://api.github.com/user").mock(return_value=httpx.Response(429, text="Rate limit"))
    result = await connector.health_check()
    assert result.ok is False
    assert "rate limit" in result.detail.lower()


@respx.mock
async def test_health_check_network_error(connector):
    """A connection failure reports a network error, not an auth error."""
    respx.get("https://api.github.com/user").mock(side_effect=httpx.ConnectError("Connection refused"))
    result = await connector.health_check()
    assert result.ok is False
    assert "network error" in result.detail.lower()


@respx.mock
async def test_health_check_missing_scope_codes(connector):
    """Missing scopes are reported as machine-parseable missing_scope:<name> codes."""
    respx.get("https://api.github.com/user").mock(
        return_value=httpx.Response(200, json={"login": "octocat"}, headers={"X-OAuth-Scopes": "read:org"}),
    )
    result = await connector.health_check()
    assert result.ok is False
    assert "missing_scope:repo" in result.detail
    assert "Required:" in result.detail


@respx.mock
async def test_health_check_ok(connector):
    """A valid token with all scopes passes with the login in detail."""
    respx.get("https://api.github.com/user").mock(
        return_value=httpx.Response(200, json={"login": "octocat"}, headers={"X-OAuth-Scopes": "repo, read:org"}),
    )
    result = await connector.health_check()
    assert result.ok is True
    assert result.detail == "octocat"
