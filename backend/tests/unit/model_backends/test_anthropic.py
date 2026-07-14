"""Unit tests for AnthropicBackend adapter."""

from unittest.mock import patch

import pytest

from modulo.model_backends.anthropic import AnthropicBackend


@pytest.fixture()
def backend():
    with patch("modulo.model_backends.anthropic.ChatAnthropic"):
        return AnthropicBackend(api_key="sk-ant-test", model_id="claude-haiku-4-5")
