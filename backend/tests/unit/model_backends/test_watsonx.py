"""Unit tests for WatsonXBackend adapter."""

from unittest.mock import patch

from farna.model_backends.watsonx import WATSONX_BASE_URL, WatsonXBackend


def test_base_url_constant():
    assert WATSONX_BASE_URL == "https://us-south.ml.cloud.ibm.com"


def test_constructor_with_custom_url():
    with patch("farna.model_backends.watsonx.ChatWatsonx"):
        backend = WatsonXBackend(
            api_key="key",
            model_id="ibm/granite-13b-instruct",
            project_id="proj",
            url="https://eu-de.ml.cloud.ibm.com",
        )
        assert backend.backend_id == "watsonx/ibm/granite-13b-instruct"
