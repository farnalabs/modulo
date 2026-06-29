"""Unit tests for Ai21Backend adapter."""

from unittest.mock import AsyncMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from modulo.model_backends.ai21 import AI21_BASE_URL, Ai21Backend
from modulo.model_backends.base import ModelBackendBase


@pytest.fixture()
def backend():
    with patch("modulo.model_backends.ai21.ChatOpenAI"):
        return Ai21Backend(api_key="test-key", model_id="jamba-1.5-mini")


def test_is_model_backend_base(backend):
    assert isinstance(backend, ModelBackendBase)


def test_backend_id_format(backend):
    assert backend.backend_id == "ai21/jamba-1.5-mini"


def test_repr(backend):
    r = repr(backend)
    assert "Ai21Backend" in r
    assert "ai21/jamba-1.5-mini" in r


def test_ai21_base_url_constant():
    assert AI21_BASE_URL == "https://api.ai21.com/studio/v1"


def test_chat_openai_uses_ai21_base_url():
    with patch("modulo.model_backends.ai21.ChatOpenAI") as MockChatOpenAI:
        Ai21Backend(api_key="test-key", model_id="jamba-1.5-mini")
        MockChatOpenAI.assert_called_once_with(
            model="jamba-1.5-mini",
            api_key="test-key",
            base_url=AI21_BASE_URL,
        )


async def test_invoke_delegates_to_langchain(backend):
    reply = AIMessage(content="Hello from AI21")
    backend._model.ainvoke = AsyncMock(return_value=reply)
    messages = [HumanMessage(content="hi")]
    result = await backend.invoke(messages)
    assert result.content == "Hello from AI21"
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
