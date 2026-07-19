"""Unit tests for WatsonXBackend adapter."""

from unittest.mock import patch

import pytest

from modulo.model_backends.watsonx import WATSONX_BASE_URL, WatsonXBackend


@pytest.fixture()
def backend():
    with patch("modulo.model_backends.watsonx.ChatWatsonx"):
        return WatsonXBackend(
            api_key="test-api-key",
            model_id="meta-llama/llama-3-70b-instruct",
            project_id="test-project-id",
        )


def test_base_url_constant():
    assert WATSONX_BASE_URL == "https://us-south.ml.cloud.ibm.com"


def test_constructor_with_custom_url():
    with patch("modulo.model_backends.watsonx.ChatWatsonx"):
        backend = WatsonXBackend(
            api_key="key",
            model_id="ibm/granite-13b-instruct",
            project_id="proj",
            url="https://eu-de.ml.cloud.ibm.com",
        )
        assert backend.backend_id == "watsonx/ibm/granite-13b-instruct"


@pytest.mark.parametrize(
    "model_id",
    [
        "meta-llama/llama-3-70b-instruct",
        "meta-llama/llama-3-8b-instruct",
        "mistralai/mixtral-8x7b-instruct-v01",
        "ibm/granite-13b-chat-v2",
    ],
)
def test_supported_models(model_id):
    with patch("modulo.model_backends.watsonx.ChatWatsonx"):
        backend = WatsonXBackend(
            api_key="key",
            model_id=model_id,
            project_id="proj",
        )
        assert backend.backend_id == f"watsonx/{model_id}"
