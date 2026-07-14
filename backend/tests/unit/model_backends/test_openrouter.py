"""Unit tests for OpenRouterBackend adapter."""

from unittest.mock import patch

import pytest

from modulo.model_backends.openrouter import OpenRouterBackend


@pytest.fixture()
def backend():
    with patch("modulo.model_backends.openrouter.ChatOpenAI"):
        return OpenRouterBackend(api_key="sk-test", model_id="gpt-4o")
