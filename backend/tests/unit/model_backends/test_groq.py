"""Unit tests for GroqBackend adapter."""

from unittest.mock import ANY, patch

import pytest

from modulo.model_backends.groq import GROQ_BASE_URL, GroqBackend


@pytest.fixture
def backend():
    with patch("modulo.model_backends.module.ChatOpenAI"):
        return GroqBackend(
            api_key="test-key",
            model_id="llama3-70b-8192",
        )


def test_groq_base_url_constant():
    assert GROQ_BASE_URL == "https://api.groq.com/openai/v1"


def test_chat_openai_uses_groq_base_url():
    with patch("modulo.model_backends.module.ChatOpenAI") as mock_chat_openai:
        GroqBackend(api_key="test-key", model_id="llama3-70b-8192")
        mock_chat_openai.assert_called_once_with(
            model="llama3-70b-8192",
            api_key="test-key",
            base_url=GROQ_BASE_URL,
            http_async_client=ANY,
        )
