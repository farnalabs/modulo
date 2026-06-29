"""Unit tests for GrokBackend adapter."""

from unittest.mock import AsyncMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from modulo.model_backends.base import ModelBackendBase
from modulo.model_backends.grok import GrokBackend


@pytest.fixture()
def backend():
    with patch("modulo.model_backends.grok.ChatOpenAI"):
        return GrokBackend(api_key="sk-test", model_id="grok-3")


def test_is_model_backend_base(backend):
    assert isinstance(backend, ModelBackendBase)


def test_backend_id_format(backend):
    assert backend.backend_id == "grok/grok-3"


def test_repr(backend):
    r = repr(backend)
    assert "GrokBackend" in r
    assert "grok-3" in r


async def test_invoke_delegates_to_langchain(backend):
    reply = AIMessage(content="Hello from Grok")
    backend._model.ainvoke = AsyncMock(return_value=reply)
    messages = [HumanMessage(content="hi")]
    result = await backend.invoke(messages)
    assert result.content == "Hello from Grok"
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
