"""Unit tests for DeepSeekBackend adapter."""

from unittest.mock import patch

import pytest

from modulo.model_backends.deepseek import DeepSeekBackend


@pytest.fixture()
def backend():
    with patch("modulo.model_backends.deepseek.ChatOpenAI"):
        return DeepSeekBackend(api_key="sk-test", model_id="deepseek-chat")
