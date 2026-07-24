"""Unit tests for AzureOpenAIBackend adapter."""

from unittest.mock import patch

import pytest

from modulo.model_backends.azure_openai import AzureOpenAIBackend


@pytest.fixture()
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
