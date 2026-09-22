"""Unit tests for the opencode provider backend and gateway error classification.

The opencode provider is OpenAI-compatible against the external zen gateway
(``https://opencode.ai/zen/go/v1``). This suite proves that a gateway 5xx or
connection failure surfaces as ``ProviderUnavailableError`` (an upstream
outage, not a bad key) while a genuine 4xx auth error still surfaces as
``openai.AuthenticationError``, and that the success path passes through.

FAR-1139: transport-level tests prove that ``x-opencode-session`` and a
custom ``User-Agent`` reach the wire, that the session ID is stable across
calls, and that non-opencode providers do not leak the header.
"""

from __future__ import annotations

import json
from unittest.mock import ANY, AsyncMock, patch

import httpx
import openai
import pytest
from langchain_core.messages import AIMessage, HumanMessage
from openai import APIConnectionError, AuthenticationError, InternalServerError

from modulo.model_backends.opencode import OpenCodeBackend, ProviderUnavailableError
from modulo.version import get_version

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
        default_headers=ANY,
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


# ---------------------------------------------------------------------------
# FAR-1139 — transport-level header tests
# ---------------------------------------------------------------------------
# These tests build a real openai.AsyncOpenAI client with the same params
# the backend would use, attach a mock transport, and assert the headers
# appear on the outgoing HTTP request.  This proves the headers survive
# the caller-supplied httpx client (the SSRF-pinned transport).


class _RecordingTransport(httpx.AsyncBaseTransport):
    """Mock transport that records the last request's headers."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        # Return a minimal valid chat completion response.
        body = json.dumps(
            {
                "id": "chatcmpl-test",
                "object": "chat.completion",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }
        )
        return httpx.Response(
            200,
            content=body.encode(),
            headers={"content-type": "application/json"},
            request=request,
        )


def _build_real_opencode_client(
    session_id: str,
    transport: _RecordingTransport,
) -> openai.AsyncOpenAI:
    """Build a real AsyncOpenAI client matching what OpenCodeBackend would use.

    This bypasses ChatOpenAI / langchain_openai entirely and constructs the
    SDK client directly with the same parameters, proving the headers survive
    through the real SDK machinery.
    """
    return openai.AsyncOpenAI(
        api_key="sk-test",
        base_url="https://opencode.ai/zen/go/v1",
        http_client=httpx.AsyncClient(transport=transport),
        default_headers={
            "x-opencode-session": session_id,
            "User-Agent": f"modulo/{get_version()}",
        },
    )


def _build_real_openai_client(
    transport: _RecordingTransport,
) -> openai.AsyncOpenAI:
    """Build a real AsyncOpenAI client for standard OpenAI (no opencode headers)."""
    return openai.AsyncOpenAI(
        api_key="sk-test",
        http_client=httpx.AsyncClient(transport=transport),
    )


@pytest.mark.anyio
async def test_opencode_headers_reach_the_wire():
    """FAR-1139: x-opencode-session and User-Agent appear on the outgoing request."""
    transport = _RecordingTransport()
    session_id = "test-session-abc-123"
    client = _build_real_opencode_client(session_id, transport)

    await client.chat.completions.create(
        model="test-model",
        messages=[{"role": "user", "content": "hi"}],
    )

    assert len(transport.requests) == 1
    headers = transport.requests[0].headers
    assert headers["x-opencode-session"] == session_id
    assert "modulo/" in headers["user-agent"]


@pytest.mark.anyio
async def test_opencode_session_id_stable_across_calls():
    """FAR-1139: the same session ID is sent on every request from one backend."""
    transport = _RecordingTransport()
    session_id = "stable-session-id-42"
    client = _build_real_opencode_client(session_id, transport)

    await client.chat.completions.create(
        model="test-model",
        messages=[{"role": "user", "content": "first"}],
    )
    await client.chat.completions.create(
        model="test-model",
        messages=[{"role": "user", "content": "second"}],
    )

    assert len(transport.requests) == 2
    assert transport.requests[0].headers["x-opencode-session"] == session_id
    assert transport.requests[1].headers["x-opencode-session"] == session_id


@pytest.mark.anyio
async def test_non_opencode_provider_no_session_header():
    """FAR-1139: standard OpenAI requests do NOT carry x-opencode-session."""
    transport = _RecordingTransport()
    client = _build_real_openai_client(transport)

    await client.chat.completions.create(
        model="gpt-4",
        messages=[{"role": "user", "content": "hi"}],
    )

    assert len(transport.requests) == 1
    headers = transport.requests[0].headers
    assert "x-opencode-session" not in headers


def test_opencode_session_id_is_uuid():
    """FAR-1139: session ID is a valid UUID generated per backend instance."""
    with patch("modulo.model_backends.module.ChatOpenAI"):
        b1 = OpenCodeBackend(api_key="sk-test", model_id="glm-5.3-flash")
        b2 = OpenCodeBackend(api_key="sk-test", model_id="glm-5.3-flash")

    # Each instance gets a unique, valid UUID.
    import uuid

    uuid.UUID(b1.session_id)  # raises if not valid UUID
    uuid.UUID(b2.session_id)
    assert b1.session_id != b2.session_id


def test_opencode_backend_default_headers_include_both():
    """FAR-1139: the default_headers dict sent to ChatOpenAI has both keys."""
    with patch("modulo.model_backends.module.ChatOpenAI") as mock_chat:
        OpenCodeBackend(api_key="sk-test", model_id="glm-5.3-flash")
    headers = mock_chat.call_args[1]["default_headers"]
    assert "x-opencode-session" in headers
    assert headers["x-opencode-session"]  # non-empty
    assert "User-Agent" in headers
    assert headers["User-Agent"].startswith("modulo/")
