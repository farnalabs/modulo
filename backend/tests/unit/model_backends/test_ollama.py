"""Unit tests for OllamaBackend adapter."""

from unittest.mock import AsyncMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from modulo.model_backends.base import ModelBackendBase
from modulo.model_backends.ollama import DEFAULT_OLLAMA_BASE_URL, OllamaBackend


@pytest.fixture()
def backend():
    with patch("modulo.model_backends.ollama.ChatOpenAI"):
        return OllamaBackend(api_key=None, model_id="llama3")


def test_is_model_backend_base(backend):
    assert isinstance(backend, ModelBackendBase)


def test_backend_id(backend):
    assert backend.backend_id == "ollama/llama3"


def test_default_base_url(backend):
    assert backend.base_url == DEFAULT_OLLAMA_BASE_URL


def test_custom_base_url():
    with patch("modulo.model_backends.ollama.ChatOpenAI"):
        backend = OllamaBackend(api_key=None, model_id="llama3", base_url="http://ollama:11434/v1")
    assert backend.base_url == "http://ollama:11434/v1"


def test_api_key_placeholder():
    with patch("modulo.model_backends.ollama.ChatOpenAI") as mock:
        OllamaBackend(api_key=None, model_id="llama3")
    assert mock.call_args[1]["api_key"] == "ollama"


def test_api_key_empty_string_falls_back_to_placeholder():
    with patch("modulo.model_backends.ollama.ChatOpenAI") as mock:
        OllamaBackend(api_key="", model_id="llama3")
    assert mock.call_args[1]["api_key"] == "ollama"


def test_explicit_api_key_passed_through():
    with patch("modulo.model_backends.ollama.ChatOpenAI") as mock:
        OllamaBackend(api_key="sk-custom", model_id="llama3")
    assert mock.call_args[1]["api_key"] == "sk-custom"


def test_base_url_trailing_slash_stripped():
    with patch("modulo.model_backends.ollama.ChatOpenAI") as mock:
        OllamaBackend(api_key=None, model_id="llama3", base_url="http://ollama:11434/v1/")
    assert mock.call_args[1]["base_url"] == "http://ollama:11434/v1"


async def test_invoke_delegates_to_langchain(backend):
    reply = AIMessage(content="Hello from Ollama")
    backend._model.ainvoke = AsyncMock(return_value=reply)
    messages = [HumanMessage(content="hi")]
    result = await backend.invoke(messages)
    assert result.content == "Hello from Ollama"
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


def test_repr_does_not_leak_api_key(backend):
    assert "sk-" not in repr(backend)
    assert "ollama" in repr(backend)
