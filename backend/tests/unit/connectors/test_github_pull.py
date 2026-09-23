"""Unit tests for GitHubConnector ``pull`` — the single-PR read (FAR-737).

The ``pull`` resource powers the Run Detail PR badge enrichment: one GET
``/repos/{owner_repo}/pulls/{number}`` returning the PR object (title,
state, merged, html_url). HTTP is mocked via respx, mirroring
``test_github_issues.py``.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from modulo.connectors.base import ConnectorQuery
from modulo.connectors.github import GitHubConnector, GitHubNotFoundError

TOKEN = "ghp_test_token"


@pytest.fixture
def connector():
    return GitHubConnector(token=TOKEN)


@respx.mock
async def test_query_pull_returns_pr_object(connector):
    pr = {
        "number": 42,
        "title": "Fix the widget",
        "state": "open",
        "merged": False,
        "html_url": "https://github.com/acme/repo/pull/42",
    }
    route = respx.get("https://api.github.com/repos/acme/repo/pulls/42").mock(return_value=httpx.Response(200, json=pr))
    result = await connector.query(ConnectorQuery(resource="pull", filters={"repo": "acme/repo", "pull_number": "42"}))
    assert route.called
    assert len(result.records) == 1
    record = result.records[0]
    assert record["title"] == "Fix the widget"
    assert record["state"] == "open"
    assert record["merged"] is False
    assert record["html_url"] == "https://github.com/acme/repo/pull/42"


@respx.mock
async def test_query_pull_merged_closed_pr(connector):
    pr = {"number": 7, "title": "Done", "state": "closed", "merged": True}
    respx.get("https://api.github.com/repos/acme/repo/pulls/7").mock(return_value=httpx.Response(200, json=pr))
    result = await connector.query(ConnectorQuery(resource="pull", filters={"repo": "acme/repo", "pull_number": "7"}))
    assert result.records[0]["merged"] is True
    assert result.records[0]["state"] == "closed"


@respx.mock
async def test_query_pull_missing_repo_filter(connector):
    with pytest.raises(ValueError, match="requires 'repo' filter"):
        await connector.query(ConnectorQuery(resource="pull", filters={"pull_number": "42"}))


@respx.mock
async def test_query_pull_missing_pull_number_filter(connector):
    with pytest.raises(ValueError, match="requires 'pull_number' filter"):
        await connector.query(ConnectorQuery(resource="pull", filters={"repo": "acme/repo"}))


@respx.mock
async def test_query_pull_non_string_pull_number_rejected(connector):
    with pytest.raises(ValueError, match="must be a string"):
        await connector.query(ConnectorQuery(resource="pull", filters={"repo": "acme/repo", "pull_number": 42}))


@respx.mock
async def test_query_pull_404_raises(connector):
    respx.get("https://api.github.com/repos/acme/repo/pulls/404").mock(
        return_value=httpx.Response(404, json={"message": "Not Found"})
    )
    with pytest.raises(GitHubNotFoundError):
        await connector.query(ConnectorQuery(resource="pull", filters={"repo": "acme/repo", "pull_number": "404"}))
