"""Unit tests for FireworksBackend adapter."""

from unittest.mock import AsyncMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from modulo.model_backends.base import ModelBackendBase
from modulo.model_backends.fireworks import FIREWORKS_BASE_URL, FireworksBackend


@pytest.fixture()
def backend():
    with patch("modulo.model_backends.fireworks.ChatOpenAI"):
        return FireworksBackend(
            api_key="test-key",
            model_id="accounts/fireworks/models/llama-v3p1-8b",
        )


def test_is_model_backend_base(backend):
    assert isinstance(backend, ModelBackendBase)


def test_backend_id_format(backend):
    assert backend.backend_id == "fireworks/accounts/fireworks/models/llama-v3p1-8b"


def test_repr(backend):
    r = repr(backend)
    assert "FireworksBackend" in r
    assert "fireworks/accounts/fireworks/models/llama-v3p1-8b" in r


def test_fireworks_base_url_constant():
    assert FIREWORKS_BASE_URL == "https://api.fireworks.ai/inference/v1"


def test_chat_openai_uses_fireworks_base_url():
    with patch("modulo.model_backends.fireworks.ChatOpenAI") as MockChatOpenAI:
        FireworksBackend(api_key="test-key", model_id="accounts/fireworks/models/llama-v3p1-8b")
        MockChatOpenAI.assert_called_once_with(
            model="accounts/fireworks/models/llama-v3p1-8b",
            api_key="test-key",
            base_url=FIREWORKS_BASE_URL,
        )


async def test_invoke_delegates_to_langchain(backend):
    reply = AIMessage(content="Hello from FireworksAI")
    backend._model.ainvoke = AsyncMock(return_value=reply)
    messages = [HumanMessage(content="hi")]
    result = await backend.invoke(messages)
    assert result.content == "Hello from FireworksAI"
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
