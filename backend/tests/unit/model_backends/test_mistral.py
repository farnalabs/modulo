"""Unit tests for MistralBackend adapter."""

from unittest.mock import AsyncMock, patch

import pytest

from modulo.model_backends.base import HealthResult
from modulo.model_backends.mistral import MISTRAL_BASE_URL, MistralBackend


@pytest.fixture
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


async def test_health_check_uses_mistral_base_url():
    with patch("modulo.model_backends.mistral.ChatMistralAI"):
        backend = MistralBackend(api_key="sk-test", model_id="mistral-large-latest")
    with patch(
        "modulo.model_backends.mistral.openai_compatible_health_check",
        new=AsyncMock(return_value=HealthResult(ok=True)),
    ) as mock_health:
        result = await backend.health_check()
    assert result.ok is True
    mock_health.assert_awaited_once_with(
        base_url=MISTRAL_BASE_URL,
        api_key="sk-test",
    )
