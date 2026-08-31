"""Unit tests for OpenRouterBackend adapter."""

from unittest.mock import ANY, patch

import pytest

from modulo.model_backends.openrouter import OpenRouterBackend


@pytest.fixture
def backend():
    with patch("modulo.model_backends.module.ChatOpenAI"):
        return OpenRouterBackend(api_key="sk-test", model_id="gpt-4o")


def test_backend_id(backend):
    assert backend.backend_id == "openrouter/gpt-4o"


def test_chat_openai_uses_openrouter_base_url():
    with patch("modulo.model_backends.module.ChatOpenAI") as mock_chat_openai:
        OpenRouterBackend(api_key="sk-test", model_id="gpt-4o")
        mock_chat_openai.assert_called_once_with(
            model="gpt-4o",
            api_key="sk-test",
            base_url="https://openrouter.ai/api/v1",
            http_async_client=ANY,
        )


def test_api_key_placeholder_uses_provider_name():
    with patch("modulo.model_backends.module.ChatOpenAI") as mock:
        OpenRouterBackend(api_key=None, model_id="gpt-4o")
    assert mock.call_args[1]["api_key"] == "openrouter"
