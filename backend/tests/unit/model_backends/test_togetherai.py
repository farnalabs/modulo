"""Unit tests for TogetherAIBackend adapter."""

from unittest.mock import ANY, patch

import pytest

from modulo.model_backends.togetherai import TOGETHERAI_BASE_URL, TogetherAIBackend


@pytest.fixture
def backend():
    with patch("modulo.model_backends.module.ChatOpenAI"):
        return TogetherAIBackend(
            api_key="test-key",
            model_id="mistralai/Mixtral-8x7B-Instruct-v0.1",
        )


def test_togetherai_base_url_constant():
    assert TOGETHERAI_BASE_URL == "https://api.together.xyz/v1"


def test_chat_openai_uses_togetherai_base_url():
    with patch("modulo.model_backends.module.ChatOpenAI") as mock_chat_openai:
        TogetherAIBackend(api_key="test-key", model_id="mistralai/Mixtral-8x7B-Instruct-v0.1")
        mock_chat_openai.assert_called_once_with(
            model="mistralai/Mixtral-8x7B-Instruct-v0.1",
            api_key="test-key",
            base_url=TOGETHERAI_BASE_URL,
            http_async_client=ANY,
        )
