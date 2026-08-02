"""Unit tests for GeminiBackend adapter."""

from unittest.mock import AsyncMock, patch

import pytest

from modulo.model_backends.base import HealthResult
from modulo.model_backends.gemini import GEMINI_BASE_URL, GeminiBackend


@pytest.fixture()
def backend():
    with patch("modulo.model_backends.gemini.ChatGoogleGenerativeAI"):
        return GeminiBackend(api_key="test-key", model_id="gemini-2.0-flash")


def test_gemini_base_url_constant():
    assert GEMINI_BASE_URL == "https://generativelanguage.googleapis.com/v1beta"


def test_backend_id(backend):
    assert backend.backend_id == "gemini/gemini-2.0-flash"


def test_chat_google_genai_uses_default_params():
    with patch("modulo.model_backends.gemini.ChatGoogleGenerativeAI") as mock_chat:
        GeminiBackend(api_key="test-key", model_id="gemini-2.0-flash", temperature=0.2)
        mock_chat.assert_called_once_with(
            model="gemini-2.0-flash",
            api_key="test-key",
            temperature=0.2,
        )


async def test_health_check_uses_goog_api_key_header():
    with patch("modulo.model_backends.gemini.ChatGoogleGenerativeAI"):
        backend = GeminiBackend(api_key="test-key", model_id="gemini-2.0-flash")
    with patch(
        "modulo.model_backends.gemini.openai_compatible_health_check",
        new=AsyncMock(return_value=HealthResult(ok=True)),
    ) as mock_health:
        result = await backend.health_check()
    assert result.ok is True
    mock_health.assert_awaited_once_with(
        base_url=GEMINI_BASE_URL,
        api_key=None,
        extra_headers={"x-goog-api-key": "test-key"},
    )
