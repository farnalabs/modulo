"""Unit tests for AnthropicBackend adapter."""

from unittest.mock import AsyncMock, patch

import httpx2
import pytest
from anthropic import APIConnectionError, APIStatusError
from langchain_core.messages import HumanMessage

from modulo.model_backends.anthropic import ANTHROPIC_BASE_URL, AnthropicBackend
from modulo.model_backends.base import HealthResult, ProviderUnavailableError

_MESSAGES_URL = "https://api.anthropic.com/v1/messages"


def _request() -> httpx2.Request:
    return httpx2.Request("POST", _MESSAGES_URL)


@pytest.fixture
def backend():
    with patch("modulo.model_backends.anthropic.ChatAnthropic"):
        return AnthropicBackend(api_key="sk-ant-test", model_id="claude-haiku-4-5")


def test_anthropic_base_url_constant():
    assert ANTHROPIC_BASE_URL == "https://api.anthropic.com"


def test_backend_id(backend):
    assert backend.backend_id == "anthropic/claude-haiku-4-5"


def test_chat_anthropic_uses_default_params():
    with patch("modulo.model_backends.anthropic.ChatAnthropic") as mock_chat:
        AnthropicBackend(api_key="sk-ant-test", model_id="claude-haiku-4-5", max_tokens=1024)
        mock_chat.assert_called_once_with(
            model="claude-haiku-4-5",
            api_key="sk-ant-test",
            max_tokens=1024,
        )


async def test_health_check_uses_anthropic_headers():
    with patch("modulo.model_backends.anthropic.ChatAnthropic"):
        backend = AnthropicBackend(api_key="sk-ant-test", model_id="claude-haiku-4-5")
    with patch(
        "modulo.model_backends.anthropic.openai_compatible_health_check",
        new=AsyncMock(return_value=HealthResult(ok=True)),
    ) as mock_health:
        result = await backend.health_check()
    assert result.ok is True
    mock_health.assert_awaited_once_with(
        base_url=ANTHROPIC_BASE_URL,
        api_key=None,
        extra_headers={"x-api-key": "sk-ant-test", "anthropic-version": "2023-06-01"},
    )


async def test_invoke_http_5xx_raises_provider_unavailable(backend):
    """A gateway 5xx is an upstream outage, not a bad key."""
    backend._model.ainvoke = AsyncMock(
        side_effect=APIStatusError(
            message="boom",
            response=httpx2.Response(503, request=_request()),
            body={"error": {"message": "boom"}},
        )
    )
    with pytest.raises(ProviderUnavailableError) as exc_info:
        await backend.invoke([HumanMessage(content="hi")])
    message = str(exc_info.value)
    assert "anthropic/claude-haiku-4-5 provider gateway" in message
    assert "HTTP 503" in message
    assert "upstream" in message


async def test_invoke_http_4xx_passes_through(backend):
    """A 4xx (e.g. 429) is actionable as-is and must not be re-wrapped."""
    backend._model.ainvoke = AsyncMock(
        side_effect=APIStatusError(
            message="rate limited",
            response=httpx2.Response(429, request=_request()),
            body={"error": {"message": "rate limited"}},
        )
    )
    with pytest.raises(APIStatusError) as exc_info:
        await backend.invoke([HumanMessage(content="hi")])
    assert exc_info.value.status_code == 429


async def test_invoke_connection_failure_raises_provider_unavailable(backend):
    backend._model.ainvoke = AsyncMock(side_effect=APIConnectionError(message="Connection error.", request=_request()))
    with pytest.raises(ProviderUnavailableError) as exc_info:
        await backend.invoke([HumanMessage(content="hi")])
    assert "connection failure" in str(exc_info.value)
