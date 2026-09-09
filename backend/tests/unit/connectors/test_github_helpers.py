"""Unit tests for the extracted network-error helper in GitHubConnector."""

from __future__ import annotations

import httpx

from modulo.connectors.github import GitHubConnector


def test_network_error_kind_timeout():
    c = GitHubConnector(token="t")
    message, code = c._network_error_kind(httpx.TimeoutException("boom"))
    assert message == "GitHub API timeout"
    assert code == "network_timeout"


def test_network_error_kind_connect_error():
    c = GitHubConnector(token="t")
    message, code = c._network_error_kind(httpx.ConnectError("boom"))
    assert message == "GitHub API connection error"
    assert code == "network_connection"
