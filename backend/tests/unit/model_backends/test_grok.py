"""Unit tests for GrokBackend adapter."""

from unittest.mock import ANY, patch

import pytest

from modulo.model_backends.grok import GrokBackend


@pytest.fixture
def backend():
    with patch("modulo.model_backends.module.ChatOpenAI"):
        return GrokBackend(api_key="sk-test", model_id="grok-2")


def test_backend_id(backend):
    assert backend.backend_id == "grok/grok-2"


def test_chat_openai_uses_grok_base_url():
    with patch("modulo.model_backends.module.ChatOpenAI") as mock_chat_openai:
        GrokBackend(api_key="sk-test", model_id="grok-2")
        mock_chat_openai.assert_called_once_with(
            model="grok-2",
            api_key="sk-test",
            base_url="https://api.x.ai/v1",
            http_async_client=ANY,
        )


def test_api_key_placeholder_uses_provider_name():
    with patch("modulo.model_backends.module.ChatOpenAI") as mock:
        GrokBackend(api_key=None, model_id="grok-2")
    assert mock.call_args[1]["api_key"] == "grok"
