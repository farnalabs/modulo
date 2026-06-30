"""Unit tests for WatsonXBackend adapter."""

from unittest.mock import AsyncMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from modulo.model_backends.base import ModelBackendBase
from modulo.model_backends.watsonx import WATSONX_BASE_URL, WatsonXBackend


@pytest.fixture()
def backend():
    with patch("modulo.model_backends.watsonx.ChatWatsonx"):
        return WatsonXBackend(
            api_key="test-api-key",
            model_id="meta-llama/llama-3-70b-instruct",
            project_id="test-project-id",
        )


def test_is_model_backend_base(backend):
    assert isinstance(backend, ModelBackendBase)


def test_backend_id_format(backend):
    assert backend.backend_id == "watsonx/meta-llama/llama-3-70b-instruct"


def test_repr(backend):
    r = repr(backend)
    assert "WatsonXBackend" in r
    assert "meta-llama/llama-3-70b-instruct" in r


def test_base_url_constant():
    assert WATSONX_BASE_URL == "https://us-south.ml.cloud.ibm.com"


async def test_invoke_delegates_to_langchain(backend):
    reply = AIMessage(content="Hello from WatsonX")
    backend._model.ainvoke = AsyncMock(return_value=reply)
    messages = [HumanMessage(content="hi")]
    result = await backend.invoke(messages)
    assert result.content == "Hello from WatsonX"
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


def test_constructor_with_custom_url():
    with patch("modulo.model_backends.watsonx.ChatWatsonx"):
        backend = WatsonXBackend(
            api_key="key",
            model_id="ibm/granite-13b-instruct",
            project_id="proj",
            url="https://eu-de.ml.cloud.ibm.com",
        )
        assert backend.backend_id == "watsonx/ibm/granite-13b-instruct"


@pytest.mark.parametrize(
    "model_id",
    [
        "meta-llama/llama-3-70b-instruct",
        "meta-llama/llama-3-8b-instruct",
        "mistralai/mixtral-8x7b-instruct-v01",
        "ibm/granite-13b-chat-v2",
    ],
)
def test_supported_models(model_id):
    with patch("modulo.model_backends.watsonx.ChatWatsonx"):
        backend = WatsonXBackend(
            api_key="key",
            model_id=model_id,
            project_id="proj",
        )
        assert backend.backend_id == f"watsonx/{model_id}"
