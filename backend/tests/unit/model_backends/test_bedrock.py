"""Unit tests for BedrockBackend adapter."""

from unittest.mock import AsyncMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage

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


async def test_invoke_passes_kwargs(backend: BedrockBackend) -> None:
    reply = AIMessage(content="Hello")
    mock_invoke = AsyncMock(return_value=reply)
    backend._model.ainvoke = mock_invoke
    messages = [HumanMessage(content="hi")]
    await backend.invoke(messages, max_tokens=500, temperature=0.7)
    mock_invoke.assert_called_once_with(messages, max_tokens=500, temperature=0.7)
