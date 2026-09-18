"""Unit tests for QwenBackend adapter."""

from unittest.mock import ANY, patch

import pytest

from modulo.model_backends.qwen import QwenBackend


@pytest.fixture
def backend():
    with patch("modulo.model_backends.module.ChatOpenAI"):
        return QwenBackend(api_key="sk-test", model_id="qwen-max")


def test_backend_id(backend):
    assert backend.backend_id == "qwen/qwen-max"


def test_chat_openai_uses_qwen_base_url():
    with patch("modulo.model_backends.module.ChatOpenAI") as mock_chat_openai:
        QwenBackend(api_key="sk-test", model_id="qwen-max")
        mock_chat_openai.assert_called_once_with(
            model="qwen-max",
            api_key="sk-test",
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            http_async_client=ANY,
        )


def test_api_key_placeholder_uses_provider_name():
    with patch("modulo.model_backends.module.ChatOpenAI") as mock:
        QwenBackend(api_key=None, model_id="qwen-max")
    assert mock.call_args[1]["api_key"] == "qwen"
