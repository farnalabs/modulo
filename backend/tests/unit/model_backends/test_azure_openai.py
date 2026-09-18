"""Unit tests for AzureOpenAIBackend adapter."""

from unittest.mock import AsyncMock, patch

import pytest

from modulo.model_backends.azure_openai import AzureOpenAIBackend
from modulo.model_backends.base import HealthResult


@pytest.fixture
def backend():
    with patch("modulo.model_backends.azure_openai.ChatOpenAI"):
        return AzureOpenAIBackend(
            api_key="test-key",
            model_id="gpt-4-deployment",
            azure_endpoint="https://my-resource.openai.azure.com",
        )


def test_default_api_version(backend):
    assert backend.api_version == "2024-10-01-preview"


def test_custom_api_version():
    with patch("modulo.model_backends.azure_openai.ChatOpenAI"):
        backend = AzureOpenAIBackend(
            api_key="test-key",
            model_id="gpt-4",
            azure_endpoint="https://my-resource.openai.azure.com",
            api_version="2023-05-15",
        )
    assert backend.api_version == "2023-05-15"


def test_azure_endpoint_strips_trailing_slash():
    with patch("modulo.model_backends.azure_openai.ChatOpenAI"):
        backend = AzureOpenAIBackend(
            api_key="test-key",
            model_id="gpt-4",
            azure_endpoint="https://my-resource.openai.azure.com/",
        )
    assert backend.azure_endpoint == "https://my-resource.openai.azure.com"


async def test_health_check_uses_api_key_header():
    with patch("modulo.model_backends.azure_openai.ChatOpenAI"):
        backend = AzureOpenAIBackend(
            api_key="test-key",
            model_id="gpt-4-deployment",
            azure_endpoint="https://my-resource.openai.azure.com/",
        )
    with patch(
        "modulo.model_backends.azure_openai.openai_compatible_health_check",
        new=AsyncMock(return_value=HealthResult(ok=True)),
    ) as mock_health:
        result = await backend.health_check()
    assert result.ok is True
    mock_health.assert_awaited_once_with(
        base_url="https://my-resource.openai.azure.com",
        api_key=None,
        extra_headers={"api-key": "test-key"},
    )
