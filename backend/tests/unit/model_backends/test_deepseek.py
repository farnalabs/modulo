"""Unit tests for DeepSeekBackend adapter."""

from unittest.mock import ANY, patch

import pytest

from modulo.model_backends.deepseek import DeepSeekBackend


@pytest.fixture
def backend():
    with patch("modulo.model_backends.module.ChatOpenAI"):
        return DeepSeekBackend(api_key="sk-test", model_id="deepseek-chat")


def test_backend_id(backend):
    assert backend.backend_id == "deepseek/deepseek-chat"


def test_chat_openai_uses_deepseek_base_url():
    with patch("modulo.model_backends.module.ChatOpenAI") as mock_chat_openai:
        DeepSeekBackend(api_key="sk-test", model_id="deepseek-chat")
        mock_chat_openai.assert_called_once_with(
            model="deepseek-chat",
            api_key="sk-test",
            base_url="https://api.deepseek.com/v1",
            http_async_client=ANY,
        )


def test_api_key_placeholder_uses_provider_name():
    with patch("modulo.model_backends.module.ChatOpenAI") as mock:
        DeepSeekBackend(api_key=None, model_id="deepseek-chat")
    assert mock.call_args[1]["api_key"] == "deepseek"
