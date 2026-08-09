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
async def test_verify_scopes_fine_grained_pat_no_header(connector):
    """A valid token with no X-OAuth-Scopes header is a fine-grained PAT / app token.

    Its permissions are per-endpoint and enforced by GitHub, so classic OAuth
    scopes cannot be enumerated and no scope is provably missing.
    """
    respx.get("https://api.github.com/user").mock(
        return_value=httpx.Response(200, json={"login": "octocat"}),
    )
    missing = await connector.verify_scopes()
    assert missing == set()


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


@respx.mock
async def test_health_check_fine_grained_pat_passes(connector):
    """A fine-grained PAT / app token (no X-OAuth-Scopes header) passes health.

    The token is valid but its permissions are per-endpoint, so classic scopes
    cannot be enumerated — the health check succeeds with an informational note
    instead of a false missing-scopes failure.
    """
    respx.get("https://api.github.com/user").mock(
        return_value=httpx.Response(200, json={"login": "octocat"}),
    )
    result = await connector.health_check()
    assert result.ok is True
    assert "octocat" in result.detail
    assert "fine-grained" in result.detail
    assert "X-OAuth-Scopes" in result.detail


@respx.mock
async def test_health_check_classic_pat_empty_scopes_still_fails(connector):
    """A classic PAT with an empty X-OAuth-Scopes value reports missing scopes.

    The header being present means the token is scope-based, so an empty scope
    list is a genuine missing-scopes failure — distinct from a fine-grained PAT
    whose header is absent.
    """
    respx.get("https://api.github.com/user").mock(
        return_value=httpx.Response(200, json={"login": "octocat"}, headers={"X-OAuth-Scopes": ""}),
    )
    result = await connector.health_check()
    assert result.ok is False
    assert "missing_scope:repo" in result.detail
    assert "missing_scope:read:org" in result.detail
