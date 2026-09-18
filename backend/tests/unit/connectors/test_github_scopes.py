"""Unit tests for GitHubConnector scope verification — HTTP mocked via httpx."""

import httpx
import pytest
import respx

from modulo.connectors.github import (
    REQUIRED_FINE_GRAINED_PERMISSIONS,
    REQUIRED_SCOPES,
    GitHubAuthError,
    GitHubConnector,
    GitHubNetworkError,
    is_fine_grained_pat,
)

TOKEN = "ghp_test_token"
FINE_GRAINED_TOKEN = "github_pat_11AA22BB33CC44DD55"


@pytest.fixture
def connector():
    return GitHubConnector(token=TOKEN)


@pytest.fixture
def fine_grained_connector():
    return GitHubConnector(token=FINE_GRAINED_TOKEN)


def test_is_fine_grained_pat_prefix() -> None:
    assert is_fine_grained_pat("github_pat_abc123") is True
    assert is_fine_grained_pat(FINE_GRAINED_TOKEN) is True
    assert is_fine_grained_pat("ghp_abc123") is False
    assert is_fine_grained_pat(TOKEN) is False
    assert is_fine_grained_pat("") is False


@respx.mock
async def test_fine_grained_verify_scopes_passes_without_scopes_header(fine_grained_connector):
    """A fine-grained PAT must not fail on the classic X-OAuth-Scopes check.

    GitHub never returns X-OAuth-Scopes for fine-grained tokens, so the required
    set is the PRD §7.11 permissions and an absent permission header means the
    API remains the enforcement point (fail-open).
    """
    respx.get("https://api.github.com/user").mock(
        return_value=httpx.Response(200, json={"login": "octocat"}),
    )
    missing = await fine_grained_connector.verify_scopes()
    assert missing == set()


@respx.mock
async def test_fine_grained_verify_scopes_all_permissions_present(fine_grained_connector):
    respx.get("https://api.github.com/user").mock(
        return_value=httpx.Response(
            200,
            json={"login": "octocat"},
            headers={"X-Accepted-GitHub-Permissions": "contents:read, contents:write, pull_requests:write"},
        ),
    )
    missing = await fine_grained_connector.verify_scopes()
    assert missing == set()


@respx.mock
async def test_fine_grained_verify_scopes_reports_missing_permissions(fine_grained_connector):
    respx.get("https://api.github.com/user").mock(
        return_value=httpx.Response(
            200,
            json={"login": "octocat"},
            headers={"X-Accepted-GitHub-Permissions": "contents:read"},
        ),
    )
    missing = await fine_grained_connector.verify_scopes()
    assert missing == REQUIRED_FINE_GRAINED_PERMISSIONS - {"contents:read"}


@respx.mock
async def test_fine_grained_verify_scopes_header_ignores_classic_scopes(fine_grained_connector):
    """Classic scopes in X-OAuth-Scopes are meaningless for fine-grained tokens.

    Even if a (nonstandard) X-OAuth-Scopes header appears, fine-grained tokens
    are only verified against the PRD §7.11 fine-grained permissions.
    """
    respx.get("https://api.github.com/user").mock(
        return_value=httpx.Response(
            200,
            json={"login": "octocat"},
            headers={"X-OAuth-Scopes": "repo, read:org"},
        ),
    )
    missing = await fine_grained_connector.verify_scopes()
    assert missing == set()


@respx.mock
async def test_fine_grained_health_check_ok(fine_grained_connector):
    respx.get("https://api.github.com/user").mock(
        return_value=httpx.Response(200, json={"login": "octocat"}),
    )
    result = await fine_grained_connector.health_check()
    assert result.ok is True
    assert result.detail == "octocat"


@respx.mock
async def test_fine_grained_health_check_reports_missing_permissions(fine_grained_connector):
    respx.get("https://api.github.com/user").mock(
        return_value=httpx.Response(
            200,
            json={"login": "octocat"},
            headers={"X-Accepted-GitHub-Permissions": "contents:read"},
        ),
    )
    result = await fine_grained_connector.health_check()
    assert result.ok is False
    assert "missing_scope:contents:write" in result.detail
    assert "missing_scope:pull_requests:write" in result.detail
    assert "contents:read" in result.detail


@respx.mock
async def test_classic_pat_still_requires_classic_scopes(connector):
    """Classic tokens keep the classic OAuth-scope check unchanged."""
    respx.get("https://api.github.com/user").mock(
        return_value=httpx.Response(200, json={"login": "octocat"}, headers={"X-OAuth-Scopes": "read:org"}),
    )
    missing = await connector.verify_scopes()
    assert missing == {"repo"}


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
