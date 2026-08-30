"""Unit tests for ModelBackendHub._build_backend provider routing.

The provider adapter constructors are replaced with dummy classes so the tests
stay hermetic (many of them would otherwise attempt network calls at init).
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest
from langchain_core.messages import HumanMessage

from modulo.core.model_backend_hub import (
    _backend_class,
    _build_backend,
    _build_custom_stub_backend,
)
from modulo.model_backends.base import HealthResult


class _DummyBackend:
    """Records constructor kwargs so tests can assert what the hub passes."""

    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs


# ---------------------------------------------------------------------------
# bedrock
# ---------------------------------------------------------------------------


def test_build_backend_bedrock_missing_access_key() -> None:
    with pytest.raises(ValueError, match="aws_access_key_id"):
        _build_backend("bedrock", "model", {"aws_secret_access_key": "y"}, {})


def test_build_backend_bedrock_missing_secret_key() -> None:
    with pytest.raises(ValueError, match="aws_secret_access_key"):
        _build_backend("bedrock", "model", {"aws_access_key_id": "x"}, {})


def test_build_backend_bedrock_routes_with_region_default() -> None:
    with patch("modulo.core.model_backend_hub._backend_class") as klass:
        klass.side_effect = lambda *_a, **_k: _DummyBackend
        result = _build_backend(
            "bedrock",
            "teacher-1",
            {"aws_access_key_id": "x", "aws_secret_access_key": "y"},
            {"temperature": 0.1},
        )
    assert isinstance(result, _DummyBackend)
    assert result.kwargs == {
        "aws_access_key_id": "x",
        "aws_secret_access_key": "y",
        "model_id": "teacher-1",
        "region": "us-east-1",
        "temperature": 0.1,
    }


def test_build_backend_bedrock_uses_configured_region() -> None:
    with patch("modulo.core.model_backend_hub._backend_class") as klass:
        klass.side_effect = lambda *_a, **_k: _DummyBackend
        result = _build_backend(
            "bedrock",
            "teacher-1",
            {"aws_access_key_id": "x", "aws_secret_access_key": "y", "region": "eu-west-1"},
            {},
        )
    assert result.kwargs["region"] == "eu-west-1"


# ---------------------------------------------------------------------------
# vertexai
# ---------------------------------------------------------------------------


def test_build_backend_vertexai_missing_project() -> None:
    with pytest.raises(ValueError, match="project"):
        _build_backend("vertexai", "model", {"api_key": "x"}, {})


def test_build_backend_vertexai_routes_with_location_default() -> None:
    with patch("modulo.core.model_backend_hub._backend_class") as klass:
        klass.side_effect = lambda *_a, **_k: _DummyBackend
        result = _build_backend("vertexai", "chirp", {"project": "my-proj"}, {})
    assert result.kwargs == {
        "project": "my-proj",
        "model_id": "chirp",
        "location": "us-central-1",
    }


# ---------------------------------------------------------------------------
# API-key-required providers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("provider", ["anthropic", "azure_openai", "cohere", "gemini", "mistral", "watsonx"])
def test_build_backend_api_key_required_providers_require_api_key(provider: str) -> None:
    with pytest.raises(ValueError, match="api_key"):
        _build_backend(provider, "model", {}, {})


def test_build_backend_non_openai_provider_passes_api_key_and_model() -> None:
    with patch("modulo.core.model_backend_hub._backend_class") as klass:
        klass.side_effect = lambda *_a, **_k: _DummyBackend
        result = _build_backend("anthropic", "claude-3", {"api_key": "sk-a"}, {"max_tokens": 512})
    assert result.kwargs == {"api_key": "sk-a", "model_id": "claude-3", "max_tokens": 512}


# ---------------------------------------------------------------------------
# OpenAI-compatible providers
# ---------------------------------------------------------------------------


def test_build_backend_openai_compatible_uses_provider_default_base_url() -> None:
    with patch("modulo.model_backends.module.OpenAICompatibleBackend") as klass:
        klass.side_effect = _DummyBackend
        result = _build_backend("groq", "llama3", {"api_key": "k"}, {})
    assert result.kwargs == {
        "api_key": "k",
        "model_id": "llama3",
        "base_url": "https://api.groq.com/openai/v1",
        "provider": "groq",
    }


def test_build_backend_openai_uses_no_default_base_url() -> None:
    with patch("modulo.model_backends.module.OpenAICompatibleBackend") as klass:
        klass.side_effect = _DummyBackend
        result = _build_backend("openai", "gpt-4o", {"api_key": "k"}, {})
    assert result.kwargs["base_url"] is None
    assert result.kwargs["provider"] == "openai"


def test_build_backend_openai_compatible_accepts_custom_base_url() -> None:
    with patch("modulo.model_backends.module.OpenAICompatibleBackend") as klass:
        klass.side_effect = _DummyBackend
        result = _build_backend(
            "ollama",
            "llama3",
            {"api_key": "k", "base_url": "http://ollama.internal:11434/v1"},
            {},
        )
    assert result.kwargs["base_url"] == "http://ollama.internal:11434/v1"


# ---------------------------------------------------------------------------
# azure_openai
# ---------------------------------------------------------------------------


def test_build_backend_azure_openai_missing_endpoint() -> None:
    with pytest.raises(ValueError, match="azure_endpoint"):
        _build_backend("azure_openai", "gpt-4", {"api_key": "k"}, {})


def test_build_backend_azure_openai_routes() -> None:
    with patch("modulo.core.model_backend_hub._backend_class") as klass:
        klass.side_effect = lambda *_a, **_k: _DummyBackend
        result = _build_backend(
            "azure_openai",
            "gpt-4",
            {"api_key": "k", "azure_endpoint": "https://e.openai.azure.com", "api_version": "2025-01-01"},
            {"temperature": 0},
        )
    assert result.kwargs == {
        "api_key": "k",
        "model_id": "gpt-4",
        "azure_endpoint": "https://e.openai.azure.com",
        "api_version": "2025-01-01",
        "temperature": 0,
    }


# ---------------------------------------------------------------------------
# watsonx
# ---------------------------------------------------------------------------


def test_build_backend_watsonx_missing_project_id() -> None:
    with pytest.raises(ValueError, match="project_id"):
        _build_backend("watsonx", "model", {"api_key": "k"}, {})


def test_build_backend_watsonx_routes() -> None:
    with patch("modulo.core.model_backend_hub._backend_class") as klass:
        klass.side_effect = lambda *_a, **_k: _DummyBackend
        result = _build_backend(
            "watsonx",
            "granite",
            {"api_key": "k", "project_id": "pid"},
            {},
        )
    assert result.kwargs == {
        "api_key": "k",
        "model_id": "granite",
        "project_id": "pid",
        "url": "https://us-south.ml.cloud.ibm.com",
    }


# ---------------------------------------------------------------------------
# plugin registry providers
# ---------------------------------------------------------------------------


def test_build_backend_plugin_provider_missing_api_key() -> None:
    registry = MagicMock()
    registry.has_model_backend.return_value = True
    with (
        patch("modulo.core.model_backend_hub.get_plugin_registry", return_value=registry),
        pytest.raises(ValueError, match="api_key"),
    ):
        _build_backend("my-plugin", "model", {}, {})


def test_build_backend_plugin_provider_routes() -> None:
    registry = MagicMock()
    registry.has_model_backend.return_value = True
    registry.build_model_backend.return_value = _DummyBackend()
    with patch("modulo.core.model_backend_hub.get_plugin_registry", return_value=registry):
        result = _build_backend("my-plugin", "model", {"api_key": "k"}, {"max_tokens": 1})
    assert isinstance(result, _DummyBackend)
    registry.build_model_backend.assert_called_once_with("my-plugin", "model", "k", max_tokens=1)


def test_build_backend_plugin_provider_build_failure_wrapped() -> None:
    registry = MagicMock()
    registry.has_model_backend.return_value = True
    registry.build_model_backend.side_effect = RuntimeError("plugin exploded")
    with (
        patch("modulo.core.model_backend_hub.get_plugin_registry", return_value=registry),
        pytest.raises(ValueError, match="Failed to build plugin model backend"),
    ):
        _build_backend("my-plugin", "model", {"api_key": "k"}, {})


def test_build_backend_plugin_provider_cancellation_propagates() -> None:
    registry = MagicMock()
    registry.has_model_backend.return_value = True
    registry.build_model_backend.side_effect = asyncio.CancelledError()
    with (
        patch("modulo.core.model_backend_hub.get_plugin_registry", return_value=registry),
        pytest.raises(asyncio.CancelledError),
    ):
        _build_backend("my-plugin", "model", {"api_key": "k"}, {})


def test_build_backend_unknown_provider() -> None:
    with pytest.raises(ValueError, match="Unknown model backend provider"):
        _build_backend("definitely-not-real", "model", {}, {})


# ---------------------------------------------------------------------------
# custom — hub-compatible stub wrapper
# ---------------------------------------------------------------------------


def test_build_backend_custom_returns_stub_wrapper() -> None:
    result = _build_backend(
        "custom",
        "stub",
        {"fixture_map": {"ping": "pong"}},
        {},
    )
    assert result.backend_id == "custom/stub"
    assert result._stub.fixture_map == {"ping": "pong"}


@pytest.mark.anyio
async def test_custom_stub_invoke_returns_fixture_response() -> None:
    backend = _build_custom_stub_backend({"ping": "pong"})
    reply = await backend.invoke([HumanMessage(content="ping")])
    assert reply.content == "pong"


@pytest.mark.anyio
async def test_custom_stub_health_check_ok() -> None:
    backend = _build_custom_stub_backend({})
    result = await backend.health_check()
    assert isinstance(result, HealthResult)
    assert result.ok is True


@pytest.mark.anyio
async def test_custom_stub_stream_yields_fixture_response() -> None:
    backend = _build_custom_stub_backend({"ping": "pong"})
    chunks = [chunk async for chunk in backend.stream([HumanMessage(content="ping")])]
    assert [c.content for c in chunks] == ["pong"]


# ---------------------------------------------------------------------------
# _backend_class — late import of provider adapter
# ---------------------------------------------------------------------------


def test_backend_class_imports_provider_module() -> None:
    # `custom` provider has no backend adapter module — the raw import helper
    # proves it imports lazily by name and fails for non-existent adapters.
    with pytest.raises(ModuleNotFoundError):
        _backend_class("custom", "NopeBackend")
