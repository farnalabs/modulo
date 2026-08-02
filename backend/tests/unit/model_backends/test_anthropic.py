"""Unit tests for AnthropicBackend adapter."""

from unittest.mock import AsyncMock, patch

import pytest

from modulo.model_backends.anthropic import ANTHROPIC_BASE_URL, AnthropicBackend
from modulo.model_backends.base import HealthResult


@pytest.fixture()
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
