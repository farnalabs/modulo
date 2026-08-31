"""Unit tests for Ai21Backend adapter."""

from unittest.mock import ANY, patch

import pytest

from modulo.model_backends.ai21 import AI21_BASE_URL, Ai21Backend


@pytest.fixture
def backend():
    with patch("modulo.model_backends.module.ChatOpenAI"):
        return Ai21Backend(api_key="test-key", model_id="jamba-1.5-mini")


def test_ai21_base_url_constant():
    assert AI21_BASE_URL == "https://api.ai21.com/studio/v1"


def test_chat_openai_uses_ai21_base_url():
    with patch("modulo.model_backends.module.ChatOpenAI") as mock_chat_openai:
        Ai21Backend(api_key="test-key", model_id="jamba-1.5-mini")
        mock_chat_openai.assert_called_once_with(
            model="jamba-1.5-mini",
            api_key="test-key",
            base_url=AI21_BASE_URL,
            http_async_client=ANY,
        )
