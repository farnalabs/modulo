"""Unit tests for ModelBackendHub lifecycle, health check, and rotation."""

import json
import uuid
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import patch

import pytest
from cryptography.fernet import Fernet
from langchain_core.messages import AIMessage, BaseMessage

from modulo.core.model_backend_hub import (
    BackendDecryptError,
    BackendNotFoundError,
    BackendUnavailableError,
    ModelBackendHub,
)
from modulo.core.secrets_backend import create_secrets_backend
from modulo.model_backends.base import ModelBackendBase

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_KEY = Fernet.generate_key().decode()


def _encrypt(payload: dict[str, Any]) -> bytes:
    return Fernet(_KEY.encode()).encrypt(json.dumps(payload).encode())


class _FakeBackend(ModelBackendBase):
    """Minimal ModelBackendBase for testing (never calls a real LLM)."""

    def __init__(self, bid: str = "test/model", fail: bool = False) -> None:
        self._bid = bid
        self._fail = fail

    @property
    def backend_id(self) -> str:
        return self._bid

    async def invoke(self, messages: list[BaseMessage], **kwargs: Any) -> BaseMessage:
        if self._fail:
            raise RuntimeError("Backend unavailable")
        return AIMessage(content="ok")

    def stream(self, messages: list[BaseMessage], **kwargs: Any):  # type: ignore[return]
        async def _gen():
            yield AIMessage(content="ok")

        return _gen()


@dataclass
class _FakeMB:
    """Mimics a ModelBackend ORM row."""

    id: uuid.UUID
    provider: str
    model_id: str
    credentials_ciphertext: bytes
    default_params: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Registration and lifecycle
# ---------------------------------------------------------------------------


async def test_register_and_get():
    hub = ModelBackendHub()
    bid = uuid.uuid4()
    backend = _FakeBackend()
    hub.register(bid, backend)
    got = await hub.get(bid)
    assert got is backend


async def test_get_unknown_raises():
    hub = ModelBackendHub()
    with pytest.raises(BackendNotFoundError) as exc_info:
        await hub.get(uuid.uuid4())
    assert exc_info.value.backend_id is not None


async def test_aexit_clears_backends():
    hub = ModelBackendHub()
    bid = uuid.uuid4()
    async with hub:
        hub.register(bid, _FakeBackend())
        result = await hub.get(bid)  # should not raise
        assert result is not None

    with pytest.raises(BackendNotFoundError):
        await hub.get(bid)


async def test_backend_ids_property():
    hub = ModelBackendHub()
    id1, id2 = uuid.uuid4(), uuid.uuid4()
    hub.register(id1, _FakeBackend())
    hub.register(id2, _FakeBackend())
    assert hub.backend_ids == frozenset({id1, id2})


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


async def test_health_check_pass():
    hub = ModelBackendHub()
    bid = uuid.uuid4()
    hub.register(bid, _FakeBackend())
    result = await hub.health_check(bid)
    assert result.ok is True


async def test_health_check_fail_marks_unhealthy():
    hub = ModelBackendHub()
    bid = uuid.uuid4()
    hub.register(bid, _FakeBackend(fail=True))
    result = await hub.health_check(bid)
    assert result.ok is False
    with pytest.raises(BackendUnavailableError):
        await hub.get(bid)


async def test_health_check_unknown_backend():
    hub = ModelBackendHub()
    result = await hub.health_check(uuid.uuid4())
    assert result.ok is False
    assert "not registered" in result.detail


async def test_get_raises_if_unhealthy():
    hub = ModelBackendHub()
    bid = uuid.uuid4()
    hub.register(bid, _FakeBackend())
    hub.mark_unhealthy(bid)
    with pytest.raises(BackendUnavailableError):
        await hub.get(bid)


async def test_mark_unhealthy_then_health_check_recovers():
    hub = ModelBackendHub()
    bid = uuid.uuid4()
    hub.register(bid, _FakeBackend())
    hub.mark_unhealthy(bid)
    await hub.health_check(bid)  # passes -> marks healthy
    result = await hub.get(bid)  # should not raise
    assert result is not None


# ---------------------------------------------------------------------------
# Rotation
# ---------------------------------------------------------------------------


async def test_get_with_rotation_returns_primary_when_healthy():
    hub = ModelBackendHub()
    primary = uuid.uuid4()
    fallback = uuid.uuid4()
    hub.register(primary, _FakeBackend())
    hub.register(fallback, _FakeBackend())
    result = hub.get_with_rotation(primary)
    assert result.backend is hub._backends[primary]
    assert result.rotated is False
    assert result.original_id == primary


async def test_get_with_rotation_falls_back_when_primary_unhealthy():
    hub = ModelBackendHub()
    primary = uuid.uuid4()
    fallback = uuid.uuid4()
    hub.register(primary, _FakeBackend())
    hub.register(fallback, _FakeBackend())
    hub.mark_unhealthy(primary)
    result = hub.get_with_rotation(primary)
    assert result.backend is hub._backends[fallback]
    assert result.rotated is True
    assert result.original_id == primary


async def test_get_with_rotation_raises_when_all_unhealthy():
    hub = ModelBackendHub()
    bid = uuid.uuid4()
    hub.register(bid, _FakeBackend())
    hub.mark_unhealthy(bid)
    with pytest.raises(BackendUnavailableError):
        hub.get_with_rotation(bid)


# ---------------------------------------------------------------------------
# Initialise with encrypted credentials
# ---------------------------------------------------------------------------


async def test_initialise_anthropic():
    mb = _FakeMB(
        id=uuid.uuid4(),
        provider="anthropic",
        model_id="claude-haiku-4-5",
        credentials_ciphertext=_encrypt({"api_key": "sk-ant-test"}),
    )
    backend = create_secrets_backend(fernet_key=_KEY, backend_name="fernet")
    with patch.object(backend, "get_secret", return_value='{"api_key": "sk-ant-test"}'):
        hub = ModelBackendHub()
        with patch("modulo.model_backends.anthropic.ChatAnthropic"):
            await hub.initialise([mb], secrets_backend=backend)
    assert mb.id in hub.backend_ids


async def test_initialise_openai():
    mb = _FakeMB(
        id=uuid.uuid4(),
        provider="openai",
        model_id="gpt-4o",
        credentials_ciphertext=_encrypt({"api_key": "sk-test"}),
    )
    backend = create_secrets_backend(fernet_key=_KEY, backend_name="fernet")
    with patch.object(backend, "get_secret", return_value='{"api_key": "sk-test"}'):
        hub = ModelBackendHub()
        with patch("modulo.model_backends.openai.ChatOpenAI"):
            await hub.initialise([mb], secrets_backend=backend)
    assert mb.id in hub.backend_ids


async def test_initialise_wrong_key_raises():
    other_key = Fernet.generate_key().decode()
    mb = _FakeMB(
        id=uuid.uuid4(),
        provider="anthropic",
        model_id="claude-haiku-4-5",
        credentials_ciphertext=Fernet(other_key.encode()).encrypt(b'{"api_key":"x"}'),
    )
    backend = create_secrets_backend(fernet_key=other_key, backend_name="fernet")
    with patch.object(backend, "get_secret", side_effect=KeyError(str(mb.id))):
        hub = ModelBackendHub()
        with pytest.raises(BackendDecryptError) as exc_info:
            await hub.initialise([mb], secrets_backend=backend)
    assert exc_info.value.backend_id == mb.id


async def test_initialise_ollama():
    mb = _FakeMB(
        id=uuid.uuid4(),
        provider="ollama",
        model_id="llama3",
        credentials_ciphertext=_encrypt({"api_key": "", "base_url": "http://localhost:11434/v1"}),
    )
    backend = create_secrets_backend(fernet_key=_KEY, backend_name="fernet")
    with patch.object(backend, "get_secret", return_value='{"api_key": "", "base_url": "http://localhost:11434/v1"}'):
        hub = ModelBackendHub()
        with patch("modulo.model_backends.ollama.ChatOpenAI"):
            await hub.initialise([mb], secrets_backend=backend)
    assert mb.id in hub.backend_ids


async def test_initialise_ollama_defaults_base_url():
    mb = _FakeMB(
        id=uuid.uuid4(),
        provider="ollama",
        model_id="llama3",
        credentials_ciphertext=_encrypt({"api_key": ""}),
    )
    backend = create_secrets_backend(fernet_key=_KEY, backend_name="fernet")
    with patch.object(backend, "get_secret", return_value='{"api_key": ""}'):
        hub = ModelBackendHub()
        with patch("modulo.model_backends.ollama.ChatOpenAI"):
            await hub.initialise([mb], secrets_backend=backend)
    assert mb.id in hub.backend_ids


async def test_initialise_plugin_fallback_backend():
    """When a provider is not built-in, hub falls back to the plugin registry."""
    from modulo.core.plugin_registry import PluginManifest, PluginRegistry

    class _PluginBackend(ModelBackendBase):
        def __init__(self, api_key: str = "", model_id: str = "", **kwargs: Any) -> None:
            self._key = api_key
            self._mid = model_id

        @property
        def backend_id(self) -> str:
            return f"plugin/{self._mid}"

        async def invoke(self, messages: list, **kwargs: Any) -> Any:
            from langchain_core.messages import AIMessage

            return AIMessage(content="plugin reply")

        def stream(self, messages: list, **kwargs: Any) -> Any:
            async def _gen():
                from langchain_core.messages import AIMessageChunk

                yield AIMessageChunk(content="plugin ")

            return _gen()

    def _plugin_builder(api_key: str, model_id: str, **kwargs: Any) -> ModelBackendBase:
        return _PluginBackend(api_key=api_key, model_id=model_id, **kwargs)

    reg = PluginRegistry()
    reg.register_model_backend(
        "my_custom_provider",
        _plugin_builder,
        PluginManifest(PLUGIN_ID="pkg-mb", display_name="MB Demo", description="", version="1"),
    )

    mb = _FakeMB(
        id=uuid.uuid4(),
        provider="my_custom_provider",
        model_id="custom-model",
        credentials_ciphertext=_encrypt({"api_key": "ck-test"}),
    )
    backend = create_secrets_backend(fernet_key=_KEY, backend_name="fernet")
    with (
        patch.object(backend, "get_secret", return_value='{"api_key": "ck-test"}'),
        patch("modulo.core.model_backend_hub.get_plugin_registry", return_value=reg),
    ):
        hub = ModelBackendHub()
        await hub.initialise([mb], secrets_backend=backend)

    assert mb.id in hub.backend_ids
    backend_obj = hub._backends[mb.id]
    assert isinstance(backend_obj, _PluginBackend)


async def test_initialise_plugin_fallback_not_registered_raises():
    """When a provider is not built-in and not in the plugin registry, raise ValueError."""
    mb = _FakeMB(
        id=uuid.uuid4(),
        provider="unknown_provider",
        model_id="m1",
        credentials_ciphertext=_encrypt({"api_key": "x"}),
    )
    backend = create_secrets_backend(fernet_key=_KEY, backend_name="fernet")
    with patch.object(backend, "get_secret", return_value='{"api_key": "x"}'):
        hub = ModelBackendHub()
        with pytest.raises(ValueError, match="Unknown model backend provider"):
            await hub.initialise([mb], secrets_backend=backend)


async def test_initialise_missing_api_key_raises():
    mb = _FakeMB(
        id=uuid.uuid4(),
        provider="anthropic",
        model_id="claude-haiku-4-5",
        credentials_ciphertext=_encrypt({}),  # no api_key field
    )
    backend = create_secrets_backend(fernet_key=_KEY, backend_name="fernet")
    with patch.object(backend, "get_secret", return_value="{}"):
        hub = ModelBackendHub()
        with pytest.raises(ValueError, match="api_key"):
            await hub.initialise([mb], secrets_backend=backend)


async def test_get_with_rotation_raises_for_unregistered_id():
    hub = ModelBackendHub()
    with pytest.raises(BackendNotFoundError):
        hub.get_with_rotation(uuid.uuid4())
