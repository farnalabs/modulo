"""Unit tests for ModelBackendHub.initialise and backend construction.

Covers the secret-decryption / fallback-parsing error paths in
``initialise``, plus the provider dispatch in ``_build_backend`` (bedrock,
vertexai, custom stub, API-key required providers, OpenAI-compatible, azure,
watsonx, plugin registry, and unknown providers).
"""

from __future__ import annotations

import asyncio
import json
import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cryptography.fernet import Fernet

from modulo.core.model_backend_hub import (
    ModelBackendHub,
    _build_backend,
    _build_custom_stub_backend,
    _extract_fixture_map,
)


def _row(**attrs: Any) -> SimpleNamespace:
    defaults: dict[str, Any] = {
        "id": uuid.uuid4(),
        "provider": "ollama",
        "model_id": "llama3",
        "credentials_ciphertext": b"{}",
        "default_params": {},
    }
    defaults.update(attrs)
    return SimpleNamespace(**defaults)


def _secrets_backend(*, secret: str | None = "{}") -> AsyncMock:
    secrets = AsyncMock()
    secrets.get_secret = AsyncMock(return_value=secret)
    return secrets


class TestInitialiseValidation:
    async def test_rejects_none_instances(self) -> None:
        async with ModelBackendHub() as hub:
            with pytest.raises(ValueError, match="must not be None"):
                await hub.initialise(None, secrets_backend=AsyncMock())

    async def test_empty_instances_warns_and_registers_nothing(self, caplog) -> None:
        import logging

        async with ModelBackendHub() as hub:
            with caplog.at_level(logging.WARNING, logger="modulo.core.model_backend_hub"):
                await hub.initialise([], secrets_backend=AsyncMock())
            assert hub._backends == {}
            assert any("No backends were registered" in r.message for r in caplog.records)


class TestInitialiseSecretHandling:
    async def test_timeout_fetching_secret_skips_backend(self, caplog) -> None:
        import logging

        row = _row()
        secrets = AsyncMock()
        secrets.get_secret = AsyncMock(side_effect=TimeoutError("slow vault"))

        async with ModelBackendHub() as hub:
            with caplog.at_level(logging.WARNING, logger="modulo.core.model_backend_hub"):
                await hub.initialise([row], secrets_backend=secrets)
            assert row.id not in hub._backends
            assert any("Timeout fetching secret" in r.message for r in caplog.records)

    async def test_decrypts_credentials_ciphertext_fallback(self) -> None:
        key = Fernet.generate_key()
        encrypted = Fernet(key).encrypt(json.dumps({"api_key": "sk-decrypted"}).encode())
        row = _row(provider="openai", credentials_ciphertext=encrypted)
        secrets = AsyncMock()
        secrets.get_secret = AsyncMock(side_effect=KeyError("no backend secret"))

        with patch("modulo.settings.get_settings", return_value=SimpleNamespace(fernet_key=key.decode())):
            async with ModelBackendHub() as hub:
                await hub.initialise([row], secrets_backend=secrets)
                assert row.id in hub._backends

    async def test_failed_decrypt_raises_backend_decrypt_error(self) -> None:
        key = Fernet.generate_key()
        row = _row(credentials_ciphertext=b"definitely-not-fernet")
        secrets = AsyncMock()
        secrets.get_secret = AsyncMock(side_effect=KeyError("no backend secret"))

        with patch("modulo.settings.get_settings", return_value=SimpleNamespace(fernet_key=key.decode())):
            async with ModelBackendHub() as hub:
                await hub.initialise([row], secrets_backend=secrets)
                assert row.id not in hub._backends

    async def test_malformed_secret_json_skips_backend(self, caplog) -> None:
        import logging

        row = _row()
        secrets = _secrets_backend(secret="this is not json")

        async with ModelBackendHub() as hub:
            with caplog.at_level(logging.WARNING, logger="modulo.core.model_backend_hub"):
                await hub.initialise([row], secrets_backend=secrets)
            assert row.id not in hub._backends
            assert any("Malformed secret JSON" in r.message for r in caplog.records)

    async def test_non_object_secret_skips_backend(self, caplog) -> None:
        import logging

        row = _row()
        secrets = _secrets_backend(secret='"just a string"')

        async with ModelBackendHub() as hub:
            with caplog.at_level(logging.WARNING, logger="modulo.core.model_backend_hub"):
                await hub.initialise([row], secrets_backend=secrets)
            assert row.id not in hub._backends
            assert any("not a JSON object" in r.message for r in caplog.records)

    async def test_key_error_without_ciphertext_is_caught(self, caplog) -> None:
        """A missing backend secret with no ciphertext fallback must not crash
        the whole initialise pass — the row is skipped and logged."""
        import logging

        row = _row(credentials_ciphertext=b"")
        secrets = AsyncMock()
        secrets.get_secret = AsyncMock(side_effect=KeyError("no backend secret"))

        async with ModelBackendHub() as hub:
            with caplog.at_level(logging.WARNING, logger="modulo.core.model_backend_hub"):
                await hub.initialise([row], secrets_backend=secrets)
            assert hub._backends == {}
            assert any("Failed to initialise backend" in r.message for r in caplog.records)


class TestInitialiseFallbackParsing:
    async def test_accepts_uuid_fallback_ids(self) -> None:
        primary, fallback = uuid.uuid4(), uuid.uuid4()
        row = _row(id=primary, fallback_backend_ids=[fallback])

        async with ModelBackendHub() as hub:
            await hub.initialise([row], secrets_backend=_secrets_backend())
            assert hub._fallbacks.get(primary) == [fallback]

    async def test_accepts_string_fallback_ids(self) -> None:
        primary, fallback = uuid.uuid4(), uuid.uuid4()
        row = _row(id=primary, fallback_backend_ids=[str(fallback)])

        async with ModelBackendHub() as hub:
            await hub.initialise([row], secrets_backend=_secrets_backend())
            assert hub._fallbacks.get(primary) == [fallback]

    async def test_non_iterable_fallback_ids_keeps_backend_without_fallbacks(self, caplog) -> None:
        import logging

        row = _row(fallback_backend_ids="ollama")

        async with ModelBackendHub() as hub:
            with caplog.at_level(logging.WARNING, logger="modulo.core.model_backend_hub"):
                await hub.initialise([row], secrets_backend=_secrets_backend())
            assert row.id in hub._backends
            assert row.id not in hub._fallbacks
            assert any("Non-iterable fallback_backend_ids" in r.message for r in caplog.records)

    async def test_invalid_string_fallback_id_is_skipped(self, caplog) -> None:
        import logging

        primary = uuid.uuid4()
        row = _row(id=primary, fallback_backend_ids=["not-a-uuid"])

        async with ModelBackendHub() as hub:
            with caplog.at_level(logging.WARNING, logger="modulo.core.model_backend_hub"):
                await hub.initialise([row], secrets_backend=_secrets_backend())
            assert row.id in hub._backends
            assert primary not in hub._fallbacks
            assert any("Invalid fallback ID string" in r.message for r in caplog.records)

    async def test_unexpected_fallback_type_is_skipped(self, caplog) -> None:
        import logging

        primary = uuid.uuid4()
        row = _row(id=primary, fallback_backend_ids=[42])

        async with ModelBackendHub() as hub:
            with caplog.at_level(logging.WARNING, logger="modulo.core.model_backend_hub"):
                await hub.initialise([row], secrets_backend=_secrets_backend())
            assert row.id in hub._backends
            assert primary not in hub._fallbacks
            assert any("Unexpected fallback ID type" in r.message for r in caplog.records)


class TestBuildBackend:
    def test_bedrock_missing_access_key_raises(self) -> None:
        with pytest.raises(ValueError, match="aws_access_key_id"):
            _build_backend("bedrock", "m", {"aws_secret_access_key": "s"}, {})

    def test_bedrock_missing_secret_key_raises(self) -> None:
        with pytest.raises(ValueError, match="aws_secret_access_key"):
            _build_backend("bedrock", "m", {"aws_access_key_id": "a"}, {})

    def test_bedrock_builds_backend(self) -> None:
        fake_class = MagicMock()
        fake_class.return_value = "bedrock-backend"
        with patch("modulo.core.model_backend_hub._backend_class", return_value=fake_class) as mock_bc:
            result = _build_backend(
                "bedrock",
                "model",
                {"aws_access_key_id": "AK", "aws_secret_access_key": "SK", "region": "eu-west-1"},
                {},
            )

        assert result == "bedrock-backend"
        mock_bc.assert_called_once_with("bedrock", "BedrockBackend")
        fake_class.assert_called_once_with(
            aws_access_key_id="AK",
            aws_secret_access_key="SK",
            model_id="model",
            region="eu-west-1",
        )

    def test_vertexai_missing_project_raises(self) -> None:
        with pytest.raises(ValueError, match="project"):
            _build_backend("vertexai", "m", {}, {})

    def test_vertexai_builds_backend(self) -> None:
        fake_class = MagicMock()
        fake_class.return_value = "vertex-backend"
        with patch("modulo.core.model_backend_hub._backend_class", return_value=fake_class) as mock_bc:
            result = _build_backend("vertexai", "m", {"project": "p"}, {})

        assert result == "vertex-backend"
        mock_bc.assert_called_once_with("vertexai", "VertexAIBackend")
        fake_class.assert_called_once_with(project="p", model_id="m", location="us-central-1")

    def test_custom_provider_builds_stub_backend(self) -> None:
        backend = _build_backend("custom", "stub", {}, {"fixture_map": {"hi": "yo"}})

        assert backend.backend_id == "custom/stub"
        assert backend._stub.fixture_map == {"hi": "yo"}

    def test_api_key_required_provider_missing_key_raises(self) -> None:
        with pytest.raises(ValueError, match="api_key"):
            _build_backend("mistral", "m", {}, {})

    def test_non_openai_compatible_provider_builds_backend(self) -> None:
        fake_class = MagicMock()
        fake_class.return_value = "anthropic-backend"
        with patch("modulo.core.model_backend_hub._backend_class", return_value=fake_class) as mock_bc:
            result = _build_backend("anthropic", "claude", {"api_key": "k"}, {})

        assert result == "anthropic-backend"
        mock_bc.assert_called_once_with("anthropic", "AnthropicBackend")
        fake_class.assert_called_once_with(api_key="k", model_id="claude")

    def test_openai_compatible_provider_builds_backend(self) -> None:
        backend = _build_backend("openai", "gpt-4o", {"api_key": "sk-x"}, {})

        assert backend.backend_id == "openai/gpt-4o"
        assert backend.base_url is None

    def test_openai_compatible_uses_configured_base_url(self) -> None:
        backend = _build_backend("ollama", "llama3", {"api_key": "k", "base_url": "http://localhost:11434/v1"}, {})

        assert backend.base_url == "http://localhost:11434/v1"

    def test_azure_missing_endpoint_raises(self) -> None:
        with pytest.raises(ValueError, match="azure_endpoint"):
            _build_backend("azure_openai", "m", {"api_key": "k"}, {})

    def test_azure_builds_backend(self) -> None:
        fake_class = MagicMock()
        fake_class.return_value = "azure-backend"
        with patch("modulo.core.model_backend_hub._backend_class", return_value=fake_class) as mock_bc:
            result = _build_backend(
                "azure_openai",
                "m",
                {"api_key": "k", "azure_endpoint": "https://x.openai.azure.com", "api_version": "2023-05-15"},
                {},
            )

        assert result == "azure-backend"
        mock_bc.assert_called_once_with("azure_openai", "AzureOpenAIBackend")
        fake_class.assert_called_once_with(
            api_key="k",
            model_id="m",
            azure_endpoint="https://x.openai.azure.com",
            api_version="2023-05-15",
        )

    def test_watsonx_missing_project_id_raises(self) -> None:
        with pytest.raises(ValueError, match="project_id"):
            _build_backend("watsonx", "m", {"api_key": "k"}, {})

    def test_watsonx_builds_backend(self) -> None:
        fake_class = MagicMock()
        fake_class.return_value = "watson-backend"
        with patch("modulo.core.model_backend_hub._backend_class", return_value=fake_class) as mock_bc:
            result = _build_backend("watsonx", "m", {"api_key": "k", "project_id": "p", "url": "https://custom"}, {})

        assert result == "watson-backend"
        mock_bc.assert_called_once_with("watsonx", "WatsonXBackend")
        fake_class.assert_called_once_with(api_key="k", model_id="m", project_id="p", url="https://custom")

    def test_plugin_provider_builds_backend(self) -> None:
        fake_registry = MagicMock()
        fake_registry.has_model_backend.return_value = True
        plugin_backend = MagicMock()
        fake_registry.build_model_backend.return_value = plugin_backend
        with patch("modulo.core.model_backend_hub.get_plugin_registry", return_value=fake_registry):
            result = _build_backend("myplugin", "m", {"api_key": "k"}, {"temperature": 0.5})

        assert result is plugin_backend
        fake_registry.build_model_backend.assert_called_once_with("myplugin", "m", "k", temperature=0.5)

    def test_plugin_provider_missing_api_key_raises(self) -> None:
        fake_registry = MagicMock()
        fake_registry.has_model_backend.return_value = True
        with (
            patch("modulo.core.model_backend_hub.get_plugin_registry", return_value=fake_registry),
            pytest.raises(ValueError, match="api_key"),
        ):
            _build_backend("myplugin", "m", {}, {})

    def test_plugin_build_failure_raises_value_error(self) -> None:
        fake_registry = MagicMock()
        fake_registry.has_model_backend.return_value = True
        fake_registry.build_model_backend.side_effect = RuntimeError("plugin boom")
        with (
            patch("modulo.core.model_backend_hub.get_plugin_registry", return_value=fake_registry),
            pytest.raises(ValueError, match="Failed to build plugin model backend"),
        ):
            _build_backend("myplugin", "m", {"api_key": "k"}, {})

    def test_plugin_build_cancellation_propagates(self) -> None:
        fake_registry = MagicMock()
        fake_registry.has_model_backend.return_value = True
        fake_registry.build_model_backend.side_effect = asyncio.CancelledError()
        with (
            patch("modulo.core.model_backend_hub.get_plugin_registry", return_value=fake_registry),
            pytest.raises(asyncio.CancelledError),
        ):
            _build_backend("myplugin", "m", {"api_key": "k"}, {})

    def test_unknown_provider_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown model backend provider"):
            _build_backend("nope", "m", {}, {})

    def test_backend_class_lazy_import(self) -> None:
        from modulo.core.model_backend_hub import _backend_class

        cls = _backend_class("stub", "StubModelBackend")
        assert cls.__name__ == "StubModelBackend"


class TestExtractFixtureMap:
    def test_prefers_default_params_fixture_map(self) -> None:
        result = _extract_fixture_map({"fixture_map": {"a": "1"}}, {"fixture_map": {"b": "2"}})
        assert result == {"b": "2"}

    def test_falls_back_to_creds_fixture_map(self) -> None:
        result = _extract_fixture_map({"fixture_map": {"a": "1"}}, {})
        assert result == {"a": "1"}

    def test_returns_empty_when_no_fixture_map(self) -> None:
        assert _extract_fixture_map({}, {}) == {}

    def test_ignores_non_dict_fixture_map(self) -> None:
        assert _extract_fixture_map({"fixture_map": "nope"}, {}) == {}


class TestBuildCustomStubBackend:
    def test_backend_id_and_fixtures(self) -> None:
        backend = _build_custom_stub_backend({"hello": "world"})
        assert backend.backend_id == "custom/stub"
        assert backend._stub.fixture_map == {"hello": "world"}

    async def test_invoke_stream_and_health_check(self) -> None:
        from langchain_core.messages import HumanMessage

        backend = _build_custom_stub_backend({"hello": "world"})

        reply = await backend.invoke([HumanMessage(content="hello")])
        assert reply.content == "world"

        chunks = [chunk async for chunk in backend.stream([HumanMessage(content="hello")])]
        assert chunks

        health = await backend.health_check()
        assert health.ok is True
