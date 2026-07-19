"""Unit tests for GeminiBackend adapter."""

from unittest.mock import patch

import pytest

from modulo.model_backends.gemini import GeminiBackend


@pytest.fixture()
def backend():
    with patch("modulo.model_backends.gemini.ChatGoogleGenerativeAI"):
        return GeminiBackend(api_key="test-key", model_id="gemini-2.0-flash")
