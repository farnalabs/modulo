"""Unit tests for PerplexityBackend adapter."""

from unittest.mock import AsyncMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from modulo.model_backends.base import ModelBackendBase
from modulo.model_backends.perplexity import PERPLEXITY_BASE_URL, PerplexityBackend


@pytest.fixture()
def backend():
    with patch("modulo.model_backends.perplexity.ChatOpenAI"):
        return PerplexityBackend(
            api_key="test-key",
            model_id="llama-3.1-sonar-small-128k-online",
        )


def test_is_model_backend_base(backend):
    assert isinstance(backend, ModelBackendBase)


def test_backend_id_format(backend):
    assert backend.backend_id == "perplexity/llama-3.1-sonar-small-128k-online"


def test_repr(backend):
    r = repr(backend)
    assert "PerplexityBackend" in r
    assert "llama-3.1-sonar-small-128k-online" in r


def test_perplexity_base_url_constant():
    assert PERPLEXITY_BASE_URL == "https://api.perplexity.ai"


def test_chat_openai_uses_perplexity_base_url():
    with patch("modulo.model_backends.perplexity.ChatOpenAI") as MockChatOpenAI:
        PerplexityBackend(api_key="test-key", model_id="llama-3.1-sonar-small-128k-online")
        MockChatOpenAI.assert_called_once_with(
            model="llama-3.1-sonar-small-128k-online",
            api_key="test-key",
            base_url=PERPLEXITY_BASE_URL,
        )


async def test_invoke_delegates_to_langchain(backend):
    reply = AIMessage(content="Hello from Perplexity")
    backend._model.ainvoke = AsyncMock(return_value=reply)
    messages = [HumanMessage(content="hi")]
    result = await backend.invoke(messages)
    assert result.content == "Hello from Perplexity"
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
