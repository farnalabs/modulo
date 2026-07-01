"""Unit tests for GroqBackend adapter."""

from unittest.mock import AsyncMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from modulo.model_backends.base import ModelBackendBase
from modulo.model_backends.groq import GROQ_BASE_URL, GroqBackend


@pytest.fixture()
def backend():
    with patch("modulo.model_backends.groq.ChatOpenAI"):
        return GroqBackend(
            api_key="test-key",
            model_id="llama3-70b-8192",
        )


def test_is_model_backend_base(backend):
    assert isinstance(backend, ModelBackendBase)


def test_backend_id_format(backend):
    assert backend.backend_id == "groq/llama3-70b-8192"


def test_repr(backend):
    r = repr(backend)
    assert "GroqBackend" in r
    assert "llama3-70b-8192" in r


def test_groq_base_url_constant():
    assert GROQ_BASE_URL == "https://api.groq.com/openai/v1"


def test_chat_openai_uses_groq_base_url():
    with patch("modulo.model_backends.groq.ChatOpenAI") as MockChatOpenAI:
        GroqBackend(api_key="test-key", model_id="llama3-70b-8192")
        MockChatOpenAI.assert_called_once_with(
            model="llama3-70b-8192",
            api_key="test-key",
            base_url=GROQ_BASE_URL,
        )


async def test_invoke_delegates_to_langchain(backend):
    reply = AIMessage(content="Hello from Groq")
    backend._model.ainvoke = AsyncMock(return_value=reply)
    messages = [HumanMessage(content="hi")]
    result = await backend.invoke(messages)
    assert result.content == "Hello from Groq"
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
