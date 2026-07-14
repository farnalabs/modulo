"""Unit tests for VertexAIBackend adapter."""

from unittest.mock import patch

import pytest

from modulo.model_backends.vertexai import VERTEXAI_DEFAULT_LOCATION, VertexAIBackend


@pytest.fixture()
def backend():
    with patch("modulo.model_backends.vertexai.ChatVertexAI"):
        return VertexAIBackend(project="my-project", model_id="gemini-2.0-flash-001")


def test_default_location_constant():
    assert VERTEXAI_DEFAULT_LOCATION == "us-central1"


def test_constructor_with_custom_location():
    with patch("modulo.model_backends.vertexai.ChatVertexAI"):
        backend = VertexAIBackend(
            project="other-project",
            model_id="claude-3-sonnet@20240229",
            location="europe-west4",
        )
        assert backend.backend_id == "vertexai/claude-3-sonnet@20240229"


def test_constructor_passes_default_params():
    with patch("modulo.model_backends.vertexai.ChatVertexAI") as mock_chat:
        VertexAIBackend(
            project="p",
            model_id="gemini-2.0-flash-001",
            temperature=0.7,
            max_tokens=4096,
        )
        mock_chat.assert_called_once_with(
            model_name="gemini-2.0-flash-001",
            project="p",
            location="us-central1",
            temperature=0.7,
            max_tokens=4096,
        )


@pytest.mark.parametrize(
    "model_id",
    [
        "gemini-2.0-flash-001",
        "gemini-2.5-pro-001",
        "claude-3-sonnet@20240229",
        "meta/llama-3.1-405b-instruct-maas",
    ],
)
def test_supported_models(model_id):
    with patch("modulo.model_backends.vertexai.ChatVertexAI"):
        backend = VertexAIBackend(project="p", model_id=model_id)
        assert backend.backend_id == f"vertexai/{model_id}"
