"""Unit tests for GrokBackend adapter."""

from unittest.mock import patch

import pytest

from modulo.model_backends.grok import GrokBackend


@pytest.fixture()
def backend():
    with patch("modulo.model_backends.grok.ChatOpenAI"):
        return GrokBackend(api_key="sk-test", model_id="grok-2")
