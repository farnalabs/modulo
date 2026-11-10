"""Unit tests for VertexAIBackend adapter."""

from unittest.mock import patch

from farna.model_backends.vertexai import VERTEXAI_DEFAULT_LOCATION, VertexAIBackend


def test_default_location_constant():
    assert VERTEXAI_DEFAULT_LOCATION == "us-central1"


def test_constructor_with_custom_location():
    with patch("farna.model_backends.vertexai.ChatVertexAI"):
        backend = VertexAIBackend(
            project="other-project",
            model_id="claude-3-sonnet@20240229",
            location="europe-west4",
        )
        assert backend.backend_id == "vertexai/claude-3-sonnet@20240229"


def test_constructor_passes_default_params():
    with patch("farna.model_backends.vertexai.ChatVertexAI") as mock_chat:
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
