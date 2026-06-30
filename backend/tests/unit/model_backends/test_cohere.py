"""Unit tests for CohereBackend adapter."""

from unittest.mock import AsyncMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from modulo.model_backends.base import ModelBackendBase
from modulo.model_backends.cohere import COHERE_BASE_URL, CohereBackend


@pytest.fixture()
def backend():
    with patch("modulo.model_backends.cohere.ChatCohere"):
        return CohereBackend(api_key="test-key", model_id="command-r")


def test_is_model_backend_base(backend):
    assert isinstance(backend, ModelBackendBase)


def test_backend_id_format(backend):
    assert backend.backend_id == "cohere/command-r"


def test_repr(backend):
    r = repr(backend)
    assert "CohereBackend" in r
    assert "cohere/command-r" in r


def test_cohere_base_url_constant():
    assert COHERE_BASE_URL == "https://api.cohere.ai/v1"


def test_chat_cohere_uses_cohere_base_url():
    with patch("modulo.model_backends.cohere.ChatCohere") as mock_chat_cohere:
        CohereBackend(api_key="test-key", model_id="command-r")
        mock_chat_cohere.assert_called_once_with(
            model="command-r",
            api_key="test-key",
            base_url=COHERE_BASE_URL,
        )


async def test_invoke_delegates_to_langchain(backend):
    reply = AIMessage(content="Hello from Cohere")
    backend._model.ainvoke = AsyncMock(return_value=reply)
    messages = [HumanMessage(content="hi")]
    result = await backend.invoke(messages)
    assert result.content == "Hello from Cohere"
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
