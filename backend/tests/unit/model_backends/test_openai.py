"""Unit tests for OpenAIBackend adapter."""

from unittest.mock import AsyncMock, patch

import httpx
import pytest
from langchain_core.messages import HumanMessage
from openai import InternalServerError

from modulo.model_backends.module import ProviderUnavailableError
from modulo.model_backends.openai import OpenAIBackend


@pytest.fixture
def backend():
    with patch("modulo.model_backends.module.ChatOpenAI"):
        return OpenAIBackend(api_key="sk-test", model_id="gpt-4o")


def test_backend_id():
    with patch("modulo.model_backends.module.ChatOpenAI"):
        backend = OpenAIBackend(api_key="sk-test", model_id="gpt-4o")
    assert backend.backend_id == "openai/gpt-4o"


def test_default_base_url_is_none(backend):
    assert backend.base_url is None


def test_custom_base_url():
    with patch("modulo.model_backends.module.ChatOpenAI"):
        backend = OpenAIBackend(api_key="sk-test", model_id="gpt-4o", base_url="https://api.example.com/v1")
    assert backend.base_url == "https://api.example.com/v1"


def test_api_key_placeholder_uses_provider_name():
    with patch("modulo.model_backends.module.ChatOpenAI") as mock:
        OpenAIBackend(api_key=None, model_id="gpt-4o")
    assert mock.call_args[1]["api_key"] == "openai"


def test_chat_openai_uses_default_params():
    with patch("modulo.model_backends.module.ChatOpenAI") as mock:
        OpenAIBackend(api_key="sk-test", model_id="gpt-4o", temperature=0.7, max_tokens=100)
    mock.assert_called_once_with(
        model="gpt-4o",
        api_key="sk-test",
        base_url=None,
        temperature=0.7,
        max_tokens=100,
    )


async def test_invoke_http_5xx_classified_as_provider_unavailable():
    """The hub builds the plain OpenAICompatibleBackend for the opencode
    provider; a gateway 5xx must surface as ProviderUnavailableError there
    too (upstream outage), not as a raw openai error."""
    with patch("modulo.model_backends.module.ChatOpenAI"):
        backend = OpenAIBackend(
            api_key="sk-test",
            model_id="opencode-go/deepseek-v4-flash",
            base_url="https://opencode.ai/zen/go/v1",
            provider="opencode",
        )
    request = httpx.Request("POST", "https://opencode.ai/zen/go/v1/chat/completions")
    backend._model.ainvoke = AsyncMock(
        side_effect=InternalServerError(
            message="boom",
            response=httpx.Response(500, request=request),
            body={"error": {"message": "boom"}},
        )
    )
    with pytest.raises(ProviderUnavailableError) as exc_info:
        await backend.invoke([HumanMessage(content="hi")])
    message = str(exc_info.value)
    assert "opencode/opencode-go/deepseek-v4-flash provider gateway" in message
    assert "HTTP 500" in message
    assert "upstream" in message
