"""Unit tests for VertexAIBackend adapter."""

from unittest.mock import AsyncMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from modulo.model_backends.base import ModelBackendBase
from modulo.model_backends.vertexai import VERTEXAI_DEFAULT_LOCATION, VertexAIBackend


@pytest.fixture()
def backend():
    with patch("modulo.model_backends.vertexai.ChatVertexAI"):
        return VertexAIBackend(project="my-project", model_id="gemini-2.0-flash-001")


def test_is_model_backend_base(backend):
    assert isinstance(backend, ModelBackendBase)


def test_backend_id_format(backend):
    assert backend.backend_id == "vertexai/gemini-2.0-flash-001"


def test_repr(backend):
    r = repr(backend)
    assert "VertexAIBackend" in r
    assert "vertexai/gemini-2.0-flash-001" in r


def test_default_location_constant():
    assert VERTEXAI_DEFAULT_LOCATION == "us-central1"


async def test_invoke_delegates_to_langchain(backend):
    reply = AIMessage(content="Hello from Vertex AI")
    backend._model.ainvoke = AsyncMock(return_value=reply)
    messages = [HumanMessage(content="hi")]
    result = await backend.invoke(messages)
    assert result.content == "Hello from Vertex AI"
    backend._model.ainvoke.assert_called_once_with(messages)


async def test_stream_yields_chunks(backend):
    chunk1 = AIMessage(content="chunk1")
    chunk2 = AIMessage(content="chunk2")

    async def _astream(*args, **kwargs):
        for c in [chunk1, chunk2]:
            yield c

    backend._model.astream = _astream
    chunks = []
    async for chunk in backend.stream([HumanMessage(content="hi")]):
        chunks.append(chunk)
    assert [c.content for c in chunks] == ["chunk1", "chunk2"]


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
