"""Unit tests for FireworksBackend adapter."""

from unittest.mock import ANY, patch

import pytest

from modulo.model_backends.fireworks import FIREWORKS_BASE_URL, FireworksBackend


@pytest.fixture
def backend():
    with patch("modulo.model_backends.module.ChatOpenAI"):
        return FireworksBackend(
            api_key="test-key",
            model_id="accounts/fireworks/models/llama-v3p1-8b",
        )


def test_fireworks_base_url_constant():
    assert FIREWORKS_BASE_URL == "https://api.fireworks.ai/inference/v1"


def test_chat_openai_uses_fireworks_base_url():
    with patch("modulo.model_backends.module.ChatOpenAI") as mock_chat_openai:
        FireworksBackend(api_key="test-key", model_id="accounts/fireworks/models/llama-v3p1-8b")
        mock_chat_openai.assert_called_once_with(
            model="accounts/fireworks/models/llama-v3p1-8b",
            api_key="test-key",
            base_url=FIREWORKS_BASE_URL,
            http_async_client=ANY,
        )
