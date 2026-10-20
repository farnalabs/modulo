"""Focused branch coverage for the S3776 connector decompositions.

Covers the defensive branches split out of the Azure Pipelines run parser, the
Jira transport-error mapper and the SharePoint page-parameter builder.
"""

from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from modulo.connectors.azure_pipelines import AzurePipelinesConnector
from modulo.connectors.base import ConnectorQuery
from modulo.connectors.github import GitHubConnector
from modulo.connectors.jira import _network_error_message
from modulo.connectors.sharepoint import SharePointConnector


def _azure_connector() -> AzurePipelinesConnector:
    return AzurePipelinesConnector(token="pat", organization="org", project="proj")


def test_parse_run_ignores_non_dict_repositories() -> None:
    run = _azure_connector()._parse_run({"id": 7, "resources": {"repositories": []}})

    assert not run.branch
    assert not run.commit_sha


def test_jira_network_error_message_maps_connection_errors() -> None:
    assert _network_error_message(httpx.ConnectError("boom")) == "Jira API connection error"


def test_jira_network_error_message_maps_timeouts() -> None:
    assert _network_error_message(httpx.TimeoutException("slow")) == "Jira API timeout"


def test_sharepoint_page_params_without_limit_or_cursor() -> None:
    params = SharePointConnector._page_params(ConnectorQuery(resource="sites", limit=0))

    assert params == {}


async def test_github_retryable_status_error_returns_retry_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connector = GitHubConnector(token="ghp_test_token")
    request = httpx.Request("GET", "https://api.github.com/user/repos")
    response = httpx.Response(429, request=request)
    error = httpx.HTTPStatusError("rate limited", request=request, response=response)

    async def _attempt_request(*_args: Any, **_kwargs: Any) -> httpx.Response:
        raise error

    sleep = AsyncMock()
    monkeypatch.setattr(connector, "_attempt_request", _attempt_request)
    monkeypatch.setattr("asyncio.sleep", sleep)

    outcome, retry_error = await connector._request_attempt("GET", "/user/repos", 0, {})

    assert outcome is None
    assert retry_error is error
    sleep.assert_awaited_once()
