"""Unit tests for the shared model backend base logic.

Covers ``openai_compatible_health_check`` and ``ModelBackendBase.health_check``,
which are the only shared paths used by every backend but previously had no
direct unit coverage.
"""

import asyncio
from contextlib import contextmanager
from datetime import UTC, datetime
from unittest.mock import patch

import httpx
import pytest
from langchain_core.messages import AIMessage

from modulo.model_backends.base import (
    HEALTH_DETAIL_MAX_LENGTH,
    ModelBackendBase,
    _parse_server_time,
    openai_compatible_health_check,
)


@contextmanager
def _patched_http_client(handler):
    """Patch ``httpx.AsyncClient`` so ``.get`` dispatches to ``handler``."""
    with patch("httpx.AsyncClient") as mock_client:
        mock_client.return_value.__aenter__.return_value.get = handler
        yield


class _StubBackend(ModelBackendBase):
    """Concrete backend used to exercise the default health_check behaviour."""

    backend_id = "stub/health-check"

    def __init__(self) -> None:
        self._invoke_impl = None

    async def invoke(self, messages, **kwargs):
        if self._invoke_impl is None:
            raise NotImplementedError("invoke not configured")
        return self._invoke_impl()

    def stream(self, messages, tools=None, **kwargs):
        raise NotImplementedError


class TestOpenAICompatibleHealthCheck:
    async def test_success_with_bearer_auth(self):
        async def _handler(url, **kwargs):
            assert url == "https://api.example.com/v1/models"
            assert kwargs["headers"]["Authorization"] == "Bearer sk-test"
            return httpx.Response(200)

        with _patched_http_client(_handler):
            result = await openai_compatible_health_check(
                base_url="https://api.example.com/v1/",
                api_key="sk-test",
            )
        assert result.ok is True
        assert not result.detail

    async def test_success_with_extra_headers(self):
        captured = {}

        async def _handler(url, **kwargs):
            captured["headers"] = kwargs.get("headers", {})
            return httpx.Response(200)

        with _patched_http_client(_handler):
            result = await openai_compatible_health_check(
                base_url="https://api.example.com/v1",
                api_key=None,
                extra_headers={"x-api-key": "secret-key", "x-custom": "value"},
            )
        assert result.ok is True
        assert captured["headers"]["x-api-key"] == "secret-key"
        assert captured["headers"]["x-custom"] == "value"
        assert "Authorization" not in captured["headers"]

    async def test_non_success_response_returns_detail(self):
        async def _handler(url, **kwargs):
            return httpx.Response(401, text="Unauthorized credentials")

        with _patched_http_client(_handler):
            result = await openai_compatible_health_check(
                base_url="https://api.example.com/v1",
                api_key="sk-test",
            )
        assert result.ok is False
        assert result.detail == "Unauthorized credentials"

    async def test_timeout_returns_timed_out_detail(self):
        def _handler(url, **kwargs):
            raise httpx.TimeoutException("timed out")

        with _patched_http_client(_handler):
            result = await openai_compatible_health_check(
                base_url="https://api.example.com/v1",
                api_key="sk-test",
            )
        assert result.ok is False
        assert result.detail == "Health check timed out"

    async def test_http_error_returns_exception_detail(self):
        def _handler(url, **kwargs):
            raise httpx.ConnectError("connection refused")

        with _patched_http_client(_handler):
            result = await openai_compatible_health_check(
                base_url="https://api.example.com/v1",
                api_key="sk-test",
            )
        assert result.ok is False
        assert "connection refused" in result.detail

    async def test_long_error_detail_is_truncated(self):
        long_detail = "x" * (HEALTH_DETAIL_MAX_LENGTH + 100)

        async def _handler(request, **kwargs):
            return httpx.Response(500, text=long_detail)

        with _patched_http_client(_handler):
            result = await openai_compatible_health_check(
                base_url="https://api.example.com/v1",
                api_key="sk-test",
            )
        assert result.ok is False
        assert len(result.detail) == HEALTH_DETAIL_MAX_LENGTH

    async def test_invalid_endpoint_value_error_is_a_connection_failure(self):
        """An unbuildable endpoint is a connection failure, not an uncaught error."""
        with patch(
            "modulo.model_backends.base.pinned_async_client_sync",
            side_effect=ValueError("invalid base url"),
        ):
            result = await openai_compatible_health_check(
                base_url="https://api.example.com/v1",
                api_key="sk-test",
            )
        assert result.ok is False
        assert result.error_kind == "connection"
        assert result.detail == "invalid base url"
        assert result.status_code is None

    async def test_trailing_slash_is_stripped_from_url(self):
        requested_urls = []

        async def _handler(url, **kwargs):
            requested_urls.append(url)
            return httpx.Response(200)

        with _patched_http_client(_handler):
            await openai_compatible_health_check(
                base_url="https://api.example.com/v1///",
                api_key=None,
            )
        assert requested_urls == ["https://api.example.com/v1/models"]


class TestParseServerTime:
    """The clock-drift reading is a bonus: a bad header never fails the probe."""

    def test_malformed_date_header_is_ignored(self):
        assert _parse_server_time(httpx.Headers({"date": "not-a-date"})) is None

    def test_naive_date_header_is_treated_as_utc(self):
        parsed = _parse_server_time(httpx.Headers({"date": "Tue, 03 Nov 2026 10:00:00"}))
        assert parsed == datetime(2026, 11, 3, 10, 0, tzinfo=UTC)

    def test_absent_date_header_is_none(self):
        assert _parse_server_time(httpx.Headers({})) is None


class TestModelBackendBaseHealthCheck:
    async def test_success_when_invoke_succeeds(self):
        backend = _StubBackend()
        backend._invoke_impl = lambda: AIMessage(content="pong")
        result = await backend.health_check()
        assert result.ok is True
        assert not result.detail

    async def test_timeout_returns_timed_out_detail(self):
        backend = _StubBackend()

        async def _slow(*args, **kwargs):
            raise TimeoutError("timed out")

        backend.invoke = _slow
        result = await backend.health_check()
        assert result.ok is False
        assert result.detail == "Health check timed out"

    async def test_generic_exception_returns_detail(self):
        backend = _StubBackend()

        async def _boom(*args, **kwargs):
            raise RuntimeError("connection reset")

        backend.invoke = _boom
        result = await backend.health_check()
        assert result.ok is False
        assert result.detail == "connection reset"

    async def test_cancelled_error_is_propagated(self):
        backend = _StubBackend()

        async def _cancel(*args, **kwargs):
            raise asyncio.CancelledError

        backend.invoke = _cancel
        with pytest.raises(asyncio.CancelledError):
            await backend.health_check()

    async def test_exception_detail_is_truncated(self):
        backend = _StubBackend()

        async def _boom(*args, **kwargs):
            raise RuntimeError("x" * (HEALTH_DETAIL_MAX_LENGTH + 100))

        backend.invoke = _boom
        result = await backend.health_check()
        assert result.ok is False
        assert len(result.detail) == HEALTH_DETAIL_MAX_LENGTH
