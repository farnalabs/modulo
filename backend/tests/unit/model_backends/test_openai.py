"""Unit tests for OpenAIBackend adapter."""

from unittest.mock import AsyncMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from modulo.model_backends.base import ModelBackendBase
from modulo.model_backends.openai import OpenAIBackend


@pytest.fixture()
def backend():
    with patch("modulo.model_backends.openai.ChatOpenAI"):
        return OpenAIBackend(api_key="sk-test", model_id="gpt-4o")


def test_is_model_backend_base(backend):
    assert isinstance(backend, ModelBackendBase)


def test_backend_id(backend):
    assert backend.backend_id == "openai/gpt-4o"


async def test_invoke_delegates_to_langchain(backend):
    reply = AIMessage(content="Hello from GPT")
    backend._model.ainvoke = AsyncMock(return_value=reply)
    messages = [HumanMessage(content="hi")]
    result = await backend.invoke(messages)
    assert result.content == "Hello from GPT"
    backend._model.ainvoke.assert_called_once_with(messages)


async def test_stream_yields_chunks(backend):
    chunk1 = AIMessage(content="a")
    chunk2 = AIMessage(content="b")

    async def _astream(*args, **kwargs):
        for c in [chunk1, chunk2]:
            yield c

    backend._model.astream = _astream
    chunks = []
    async for chunk in backend.stream([HumanMessage(content="hi")]):
        chunks.append(chunk)
    assert [c.content for c in chunks] == ["a", "b"]
