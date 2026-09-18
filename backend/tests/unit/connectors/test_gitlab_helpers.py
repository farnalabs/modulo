"""Unit tests for the extracted retry/error helpers in GitLabConnector."""

from __future__ import annotations

import httpx
import pytest

from modulo.connectors.gitlab import GitLabConnector


def _status_exc(status_code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://gitlab.example.com/api/v4/x")
    response = httpx.Response(status_code, request=request, json={"message": "nope"})
    return httpx.HTTPStatusError(f"status {status_code}", request=request, response=response)


def test_network_error_message_timeout():
    c = GitLabConnector(token="t")
    assert c._network_error_message(httpx.TimeoutException("x")) == "GitLab API timeout"


def test_network_error_message_connect_error():
    c = GitLabConnector(token="t")
    assert c._network_error_message(httpx.ConnectError("x")) == "GitLab API connection error"


def test_network_error_message_generic_http_error():
    c = GitLabConnector(token="t")
    msg = c._network_error_message(httpx.HTTPError("kaboom"))
    assert "GitLab API HTTP error" in msg


async def test_retry_status_error_retryable(monkeypatch):
    c = GitLabConnector(token="t")
    monkeypatch.setattr(c, "_sleep_delay", lambda *a, **k: 0)
    assert await c._retry_status_error(_status_exc(503), attempt=0) is True


async def test_retry_status_error_non_retryable_raises():
    c = GitLabConnector(token="t")
    with pytest.raises(ValueError, match="GitLab API HTTP 404"):
        await c._retry_status_error(_status_exc(404), attempt=0)


async def test_retry_status_error_exhausted_raises():
    c = GitLabConnector(token="t")
    with pytest.raises(ValueError, match="GitLab API HTTP 503"):
        await c._retry_status_error(_status_exc(503), attempt=5)


async def test_retry_network_error_retryable(monkeypatch):
    c = GitLabConnector(token="t")
    monkeypatch.setattr(c, "_jitter", lambda *a, **k: 0)
    assert await c._retry_network_error(httpx.TimeoutException("x"), attempt=0) is True


async def test_retry_network_error_exhausted_raises(monkeypatch):
    c = GitLabConnector(token="t")
    monkeypatch.setattr(c, "_jitter", lambda *a, **k: 0)
    with pytest.raises(ValueError, match="GitLab API timeout"):
        await c._retry_network_error(httpx.TimeoutException("x"), attempt=5)
