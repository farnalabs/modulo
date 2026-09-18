"""Unit tests for PerplexityBackend adapter."""

from unittest.mock import ANY, patch

import pytest

from modulo.model_backends.perplexity import PERPLEXITY_BASE_URL, PerplexityBackend


@pytest.fixture
def backend():
    with patch("modulo.model_backends.module.ChatOpenAI"):
        return PerplexityBackend(
            api_key="test-key",
            model_id="llama-3.1-sonar-small-128k-online",
        )


def test_perplexity_base_url_constant():
    assert PERPLEXITY_BASE_URL == "https://api.perplexity.ai"


def test_chat_openai_uses_perplexity_base_url():
    with patch("modulo.model_backends.module.ChatOpenAI") as mock_chat_openai:
        PerplexityBackend(api_key="test-key", model_id="llama-3.1-sonar-small-128k-online")
        mock_chat_openai.assert_called_once_with(
            model="llama-3.1-sonar-small-128k-online",
            api_key="test-key",
            base_url=PERPLEXITY_BASE_URL,
            http_async_client=ANY,
        )
