"""Unit tests for MistralBackend adapter."""

from unittest.mock import patch

import pytest

from modulo.model_backends.mistral import MISTRAL_BASE_URL, MistralBackend


@pytest.fixture()
def backend():
    with patch("modulo.model_backends.mistral.ChatMistralAI"):
        return MistralBackend(api_key="sk-test", model_id="mistral-large-latest")


def test_base_url_constant():
    assert MISTRAL_BASE_URL == "https://api.mistral.ai/v1"


@pytest.mark.parametrize(
    "model_id",
    [
        "open-mistral-7b",
        "mistral-small-latest",
        "mistral-medium-latest",
        "mistral-large-latest",
    ],
)
def test_supported_models(model_id):
    with patch("modulo.model_backends.mistral.ChatMistralAI"):
        backend = MistralBackend(api_key="sk-test", model_id=model_id)
        assert backend.backend_id == f"mistral/{model_id}"
