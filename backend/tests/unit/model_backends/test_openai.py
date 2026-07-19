"""Unit tests for OpenAIBackend adapter."""

from unittest.mock import patch

import pytest

from modulo.model_backends.openai import OpenAIBackend


@pytest.fixture()
def backend():
    with patch("modulo.model_backends.openai.ChatOpenAI"):
        return OpenAIBackend(api_key="sk-test", model_id="gpt-4o")
