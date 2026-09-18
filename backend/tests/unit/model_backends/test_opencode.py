"""Unit tests for the opencode provider backend and gateway error classification.

The opencode provider is OpenAI-compatible against the external zen gateway
(``https://opencode.ai/zen/go/v1``). This suite proves that a gateway 5xx or
connection failure surfaces as ``ProviderUnavailableError`` (an upstream
outage, not a bad key) while a genuine 4xx auth error still surfaces as
``openai.AuthenticationError``, and that the success path passes through.
"""

from unittest.mock import ANY, AsyncMock, patch

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage
from openai import APIConnectionError, AuthenticationError, InternalServerError

from modulo.model_backends.opencode import OpenCodeBackend, ProviderUnavailableError

_CHAT_URL = "https://opencode.ai/zen/go/v1/chat/completions"


def _request() -> httpx.Request:
    return httpx.Request("POST", _CHAT_URL)


@pytest.fixture
def backend():
    with patch("modulo.model_backends.module.ChatOpenAI"):
        return OpenCodeBackend(api_key="sk-test", model_id="opencode-go/deepseek-v4-flash")


def _internal_server_error() -> InternalServerError:
    response = httpx.Response(500, request=_request())
    return InternalServerError(
        message='{"error":{"message":"upstream gateway failure"}}',
        response=response,
        body={"error": {"message": "upstream gateway failure"}},
    )


def test_backend_id(backend):
    assert backend.backend_id == "opencode/opencode-go/deepseek-v4-flash"


def test_constructor_uses_zen_gateway_base_url():
    with patch("modulo.model_backends.module.ChatOpenAI") as mock_chat:
        OpenCodeBackend(api_key="sk-test", model_id="opencode-go/deepseek-v4-flash")
    mock_chat.assert_called_once_with(
        model="opencode-go/deepseek-v4-flash",
        api_key="sk-test",
        base_url="https://opencode.ai/zen/go/v1",
        http_async_client=ANY,
    )


def test_constructor_api_key_placeholder_uses_provider_name():
    with patch("modulo.model_backends.module.ChatOpenAI") as mock:
        OpenCodeBackend(api_key=None, model_id="opencode-go/deepseek-v4-flash")
    assert mock.call_args[1]["api_key"] == "opencode"


async def test_invoke_success_passes_through(backend):
    reply = AIMessage(content="hello from opencode")
    backend._model.ainvoke = AsyncMock(return_value=reply)
    result = await backend.invoke([HumanMessage(content="hi")])
    assert result.content == "hello from opencode"
    backend._model.ainvoke.assert_called_once_with([HumanMessage(content="hi")])


async def test_invoke_http_5xx_raises_provider_unavailable(backend):
    backend._model.ainvoke = AsyncMock(side_effect=_internal_server_error())
    with pytest.raises(ProviderUnavailableError) as exc_info:
        await backend.invoke([HumanMessage(content="hi")])
    message = str(exc_info.value)
    assert "opencode/opencode-go/deepseek-v4-flash provider gateway" in message
    assert "https://opencode.ai/zen/go/v1" in message
    assert "HTTP 500" in message
    assert "upstream" in message


async def test_invoke_connection_failure_raises_provider_unavailable(backend):
    backend._model.ainvoke = AsyncMock(side_effect=APIConnectionError(message="Connection error.", request=_request()))
    with pytest.raises(ProviderUnavailableError) as exc_info:
        await backend.invoke([HumanMessage(content="hi")])
    assert "connection failure" in str(exc_info.value)


async def test_invoke_auth_error_passes_through(backend):
    response = httpx.Response(401, request=_request())
    backend._model.ainvoke = AsyncMock(
        side_effect=AuthenticationError(
            message="Incorrect API key",
            response=response,
            body={"error": {"message": "Incorrect API key"}},
        )
    )
    with pytest.raises(AuthenticationError):
        await backend.invoke([HumanMessage(content="hi")])


async def test_stream_http_5xx_raises_provider_unavailable(backend):
    async def _fail(*args, **kwargs):
        raise _internal_server_error()
        yield  # pragma: no cover

    backend._model.astream = _fail
    with pytest.raises(ProviderUnavailableError) as exc_info:
        [c async for c in backend.stream([HumanMessage(content="hi")])]
    assert "HTTP 500" in str(exc_info.value)


async def test_stream_success_yields_chunks(backend):
    async def _astream(*args, **kwargs):
        yield AIMessage(content="chunk1")
        yield AIMessage(content="chunk2")

    backend._model.astream = _astream
    chunks = [c async for c in backend.stream([HumanMessage(content="hi")])]
    assert [c.content for c in chunks] == ["chunk1", "chunk2"]
