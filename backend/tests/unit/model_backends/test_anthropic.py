"""Unit tests for AnthropicBackend adapter."""

from unittest.mock import AsyncMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from modulo.model_backends.anthropic import AnthropicBackend
from modulo.model_backends.base import ModelBackendBase


@pytest.fixture()
def backend():
    with patch("modulo.model_backends.anthropic.ChatAnthropic"):
        return AnthropicBackend(api_key="sk-ant-test", model_id="claude-haiku-4-5")


def test_is_model_backend_base(backend):
    assert isinstance(backend, ModelBackendBase)


def test_backend_id(backend):
    assert backend.backend_id == "anthropic/claude-haiku-4-5"


async def test_invoke_delegates_to_langchain(backend):
    reply = AIMessage(content="Hello from Claude")
    backend._model.ainvoke = AsyncMock(return_value=reply)
    messages = [HumanMessage(content="hi")]
    result = await backend.invoke(messages)
    assert result.content == "Hello from Claude"
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
