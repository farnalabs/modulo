"""Unit tests for CohereBackend adapter."""

from unittest.mock import patch

import pytest

from modulo.model_backends.cohere import COHERE_BASE_URL, CohereBackend


@pytest.fixture()
def backend():
    with patch("modulo.model_backends.cohere.ChatCohere"):
        return CohereBackend(api_key="test-key", model_id="command-r")


def test_cohere_base_url_constant():
    assert COHERE_BASE_URL == "https://api.cohere.ai/v1"


def test_chat_cohere_uses_cohere_base_url():
    with patch("modulo.model_backends.cohere.ChatCohere") as mock_chat_cohere:
        CohereBackend(api_key="test-key", model_id="command-r")
        mock_chat_cohere.assert_called_once_with(
            model="command-r",
            api_key="test-key",
            base_url=COHERE_BASE_URL,
        )
