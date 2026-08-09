"""Unit tests for GitHubConnector scope verification — HTTP mocked via httpx."""

import httpx
import pytest
import respx

from modulo.connectors.github import REQUIRED_SCOPES, GitHubAuthError, GitHubConnector, GitHubNetworkError

TOKEN = "ghp_test_token"


@pytest.fixture
def connector():
    return GitHubConnector(token=TOKEN)


@respx.mock
async def test_verify_scopes_all_present(connector):
    respx.get("https://api.github.com/user").mock(
        return_value=httpx.Response(200, json={"login": "octocat"}, headers={"X-OAuth-Scopes": "repo, read:org"}),
    )
    missing = await connector.verify_scopes()
    assert missing == set()


@respx.mock
async def test_verify_scopes_missing_read_org(connector):
    respx.get("https://api.github.com/user").mock(
        return_value=httpx.Response(200, json={"login": "octocat"}, headers={"X-OAuth-Scopes": "repo"}),
    )
    missing = await connector.verify_scopes()
    assert missing == {"read:org"}


@respx.mock
async def test_verify_scopes_missing_both(connector):
    respx.get("https://api.github.com/user").mock(
        return_value=httpx.Response(200, json={"login": "octocat"}, headers={"X-OAuth-Scopes": ""}),
    )
    missing = await connector.verify_scopes()
    assert missing == REQUIRED_SCOPES


@respx.mock
async def test_verify_scopes_no_header(connector):
    respx.get("https://api.github.com/user").mock(
        return_value=httpx.Response(200, json={"login": "octocat"}),
    )
    missing = await connector.verify_scopes()
    assert missing == REQUIRED_SCOPES


@respx.mock
async def test_verify_scopes_extra_scopes(connector):
    respx.get("https://api.github.com/user").mock(
        return_value=httpx.Response(
            200, json={"login": "octocat"}, headers={"X-OAuth-Scopes": "repo, read:org, workflow, gist"}
        ),
    )
    missing = await connector.verify_scopes()
    assert missing == set()


@respx.mock
async def test_verify_scopes_api_failure(connector):
    """A 401 from /user raises a typed GitHubAuthError with code token_expired."""
    respx.get("https://api.github.com/user").mock(return_value=httpx.Response(401, text="Unauthorized"))
    with pytest.raises(GitHubAuthError, match="GitHub API HTTP 401") as excinfo:
        await connector.verify_scopes()
    assert excinfo.value.error_code == "token_expired"
    assert excinfo.value.status_code == 401


@respx.mock
async def test_verify_scopes_403_insufficient_scope(connector):
    """A 403 from /user raises a GitHubAuthError with code insufficient_scope."""
    respx.get("https://api.github.com/user").mock(return_value=httpx.Response(403, text="Forbidden"))
    with pytest.raises(GitHubAuthError, match="GitHub API HTTP 403") as excinfo:
        await connector.verify_scopes()
    assert excinfo.value.error_code == "insufficient_scope"
    assert excinfo.value.status_code == 403


@respx.mock
async def test_verify_scopes_network_error(connector):
    """A connection failure raises a GitHubNetworkError (not an auth error)."""
    respx.get("https://api.github.com/user").mock(side_effect=httpx.ConnectError("Connection refused"))
    with pytest.raises(GitHubNetworkError, match="connection error"):
        await connector.verify_scopes()
    assert issubclass(GitHubNetworkError, ValueError)


@respx.mock
async def test_health_check_includes_scope_status(connector):
    respx.get("https://api.github.com/user").mock(
        return_value=httpx.Response(200, json={"login": "octocat"}, headers={"X-OAuth-Scopes": "repo, read:org"}),
    )
    result = await connector.health_check()
    assert result.ok is True
    assert result.detail == "octocat"


@respx.mock
async def test_health_check_reports_missing_scopes(connector):
    respx.get("https://api.github.com/user").mock(
        return_value=httpx.Response(200, json={"login": "octocat"}, headers={"X-OAuth-Scopes": "read:org"}),
    )
    result = await connector.health_check()
    assert result.ok is False
    assert "repo" in result.detail
    assert "Required:" in result.detail
