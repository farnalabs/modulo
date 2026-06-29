"""Unit tests for OpenRouterBackend adapter."""

from unittest.mock import AsyncMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from modulo.model_backends.base import ModelBackendBase
from modulo.model_backends.openrouter import OpenRouterBackend


@pytest.fixture()
def backend():
    with patch("modulo.model_backends.openrouter.ChatOpenAI"):
        return OpenRouterBackend(api_key="test-key", model_id="o3-mini")


def test_is_model_backend_base(backend):
    assert isinstance(backend, ModelBackendBase)


def test_backend_id_format(backend):
    assert backend.backend_id == "openrouter/o3-mini"


def test_repr(backend):
    r = repr(backend)
    assert "OpenRouterBackend" in r
    assert "openrouter/o3-mini" in r


async def test_invoke_delegates_to_langchain(backend):
    reply = AIMessage(content="Hello from OpenRouter")
    backend._model.ainvoke = AsyncMock(return_value=reply)
    messages = [HumanMessage(content="hi")]
    result = await backend.invoke(messages)
    assert result.content == "Hello from OpenRouter"
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
