"""Unit tests for GitHubConnector scope verification — HTTP mocked via httpx."""

import httpx
import pytest
import respx

from modulo.connectors.github import REQUIRED_SCOPES, GitHubConnector

TOKEN = "ghp_test_token"


@pytest.fixture()
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
    respx.get("https://api.github.com/user").mock(return_value=httpx.Response(401, text="Unauthorized"))
    with pytest.raises(ValueError, match="Cannot verify scopes: GitHub API HTTP 401"):
        await connector.verify_scopes()


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
