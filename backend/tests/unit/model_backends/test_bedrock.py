"""Unit tests for BedrockBackend adapter."""

from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from modulo.model_backends.base import ModelBackendBase
from modulo.model_backends.bedrock import BedrockBackend


@pytest.fixture()
def backend() -> BedrockBackend:
    with patch("modulo.model_backends.bedrock.ChatBedrock"):
        return BedrockBackend(
            aws_access_key_id="AKIA123",
            aws_secret_access_key="secret123",
            model_id="us.anthropic.claude-sonnet-4-5-v2",
            region="us-east-1",
        )


def test_is_model_backend_base(backend: BedrockBackend) -> None:
    assert isinstance(backend, ModelBackendBase)


def test_backend_id(backend: BedrockBackend) -> None:
    assert backend.backend_id == "bedrock/us.anthropic.claude-sonnet-4-5-v2"


def test_repr(backend: BedrockBackend) -> None:
    assert "BedrockBackend" in repr(backend)
    assert "claude-sonnet" in repr(backend)


def test_default_region() -> None:
    with patch("modulo.model_backends.bedrock.ChatBedrock") as mock_chat:
        BedrockBackend(
            aws_access_key_id="AKIA123",
            aws_secret_access_key="secret123",
            model_id="us.anthropic.claude-3-haiku-20240307",
        )
    mock_chat.assert_called_once()
    _call_kwargs = mock_chat.call_args.kwargs
    assert _call_kwargs["region_name"] == "us-east-1"


async def test_invoke_delegates_to_langchain(backend: BedrockBackend) -> None:
    reply = AIMessage(content="Hello from Bedrock")
    backend._model.ainvoke = AsyncMock(return_value=reply)
    messages = [HumanMessage(content="hi")]
    result = await backend.invoke(messages)
    assert result.content == "Hello from Bedrock"
    backend._model.ainvoke.assert_called_once_with(messages)


async def test_stream_yields_chunks(backend: BedrockBackend) -> None:
    chunk1 = AIMessage(content="chunk1")
    chunk2 = AIMessage(content="chunk2")

    async def _astream(*args: object, **kwargs: object) -> AsyncIterator[AIMessage]:
        for c in [chunk1, chunk2]:
            yield c

    backend._model.astream = _astream
    chunks = []
    async for chunk in backend.stream([HumanMessage(content="hi")]):
        chunks.append(chunk)
    assert [c.content for c in chunks] == ["chunk1", "chunk2"]


async def test_invoke_passes_kwargs(backend: BedrockBackend) -> None:
    reply = AIMessage(content="Hello")
    mock_invoke = AsyncMock(return_value=reply)
    backend._model.ainvoke = mock_invoke
    messages = [HumanMessage(content="hi")]
    await backend.invoke(messages, max_tokens=500, temperature=0.7)
    mock_invoke.assert_called_once_with(messages, max_tokens=500, temperature=0.7)
