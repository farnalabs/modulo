"""Unit tests for MistralBackend adapter."""

from unittest.mock import AsyncMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from modulo.model_backends.base import ModelBackendBase
from modulo.model_backends.mistral import MISTRAL_BASE_URL, MistralBackend


@pytest.fixture()
def backend():
    with patch("modulo.model_backends.mistral.ChatMistralAI"):
        return MistralBackend(api_key="sk-test", model_id="mistral-large-latest")


def test_is_model_backend_base(backend):
    assert isinstance(backend, ModelBackendBase)


def test_backend_id_format(backend):
    assert backend.backend_id == "mistral/mistral-large-latest"


def test_repr(backend):
    r = repr(backend)
    assert "MistralBackend" in r
    assert "mistral-large-latest" in r


def test_base_url_constant():
    assert MISTRAL_BASE_URL == "https://api.mistral.ai/v1"


async def test_invoke_delegates_to_langchain(backend):
    reply = AIMessage(content="Hello from Mistral")
    backend._model.ainvoke = AsyncMock(return_value=reply)
    messages = [HumanMessage(content="hi")]
    result = await backend.invoke(messages)
    assert result.content == "Hello from Mistral"
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


@pytest.mark.parametrize(
    "model_id",
    [
        "open-mistral-7b",
        "mistral-small-latest",
        "mistral-medium-latest",
        "mistral-large-latest",
    ],
)
def test_supported_models(model_id):
    with patch("modulo.model_backends.mistral.ChatMistralAI"):
        backend = MistralBackend(api_key="sk-test", model_id=model_id)
        assert backend.backend_id == f"mistral/{model_id}"
