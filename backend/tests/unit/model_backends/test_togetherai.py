"""Unit tests for TogetherAIBackend adapter."""

from unittest.mock import AsyncMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from modulo.model_backends.base import ModelBackendBase
from modulo.model_backends.togetherai import TOGETHERAI_BASE_URL, TogetherAIBackend


@pytest.fixture()
def backend():
    with patch("modulo.model_backends.togetherai.ChatOpenAI"):
        return TogetherAIBackend(
            api_key="test-key",
            model_id="mistralai/Mixtral-8x7B-Instruct-v0.1",
        )


def test_is_model_backend_base(backend):
    assert isinstance(backend, ModelBackendBase)


def test_backend_id_format(backend):
    assert backend.backend_id == "togetherai/mistralai/Mixtral-8x7B-Instruct-v0.1"


def test_repr(backend):
    r = repr(backend)
    assert "TogetherAIBackend" in r
    assert "togetherai/mistralai/Mixtral-8x7B-Instruct-v0.1" in r


def test_togetherai_base_url_constant():
    assert TOGETHERAI_BASE_URL == "https://api.together.xyz/v1"


def test_chat_openai_uses_togetherai_base_url():
    with patch("modulo.model_backends.togetherai.ChatOpenAI") as mock_chat_openai:
        TogetherAIBackend(api_key="test-key", model_id="mistralai/Mixtral-8x7B-Instruct-v0.1")
        mock_chat_openai.assert_called_once_with(
            model="mistralai/Mixtral-8x7B-Instruct-v0.1",
            api_key="test-key",
            base_url=TOGETHERAI_BASE_URL,
        )


async def test_invoke_delegates_to_langchain(backend):
    reply = AIMessage(content="Hello from TogetherAI")
    backend._model.ainvoke = AsyncMock(return_value=reply)
    messages = [HumanMessage(content="hi")]
    result = await backend.invoke(messages)
    assert result.content == "Hello from TogetherAI"
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
