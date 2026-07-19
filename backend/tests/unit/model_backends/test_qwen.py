"""Unit tests for QwenBackend adapter."""

from unittest.mock import patch

import pytest

from modulo.model_backends.qwen import QwenBackend


@pytest.fixture()
def backend():
    with patch("modulo.model_backends.qwen.ChatOpenAI"):
        return QwenBackend(api_key="sk-test", model_id="qwen-max")
