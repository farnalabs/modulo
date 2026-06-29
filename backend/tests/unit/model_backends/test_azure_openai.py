"""Unit tests for AzureOpenAIBackend adapter."""

from unittest.mock import AsyncMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from modulo.model_backends.azure_openai import AzureOpenAIBackend
from modulo.model_backends.base import ModelBackendBase


@pytest.fixture()
def backend():
    with patch("modulo.model_backends.azure_openai.ChatOpenAI"):
        return AzureOpenAIBackend(
            api_key="test-key",
            model_id="gpt-4-deployment",
            azure_endpoint="https://my-resource.openai.azure.com",
        )


def test_is_model_backend_base(backend):
    assert isinstance(backend, ModelBackendBase)


def test_backend_id_format(backend):
    assert backend.backend_id == "azure_openai/gpt-4-deployment"


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


def test_repr(backend):
    r = repr(backend)
    assert "AzureOpenAIBackend" in r
    assert "gpt-4-deployment" in r
    assert "my-resource" in r


async def test_invoke_delegates_to_langchain(backend):
    reply = AIMessage(content="Hello from Azure")
    backend._model.ainvoke = AsyncMock(return_value=reply)
    messages = [HumanMessage(content="hi")]
    result = await backend.invoke(messages)
    assert result.content == "Hello from Azure"
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
