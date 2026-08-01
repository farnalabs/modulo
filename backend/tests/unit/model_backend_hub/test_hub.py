"""Unit tests for ModelBackendHub lifecycle, health check, and rotation."""

import asyncio
import json
import subprocess
import sys
import uuid
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from cryptography.fernet import Fernet
from langchain_core.messages import AIMessage, BaseMessage

from modulo.core.model_backend_hub import (
    _ERROR_DETAIL_MAX_LENGTH,
    BackendNotFoundError,
    BackendUnavailableError,
    ModelBackendHub,
)
from modulo.core.secrets_backend import create_secrets_backend
from modulo.model_backends.base import HealthResult, ModelBackendBase

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_KEY = Fernet.generate_key().decode()


def test_import_does_not_load_provider_adapters() -> None:
    """Importing the registry must not initialise optional provider SDKs."""
    script = """
import json
import sys
import modulo.core.model_backend_hub

provider_modules = sorted(
    name
    for name in sys.modules
    if name.startswith("modulo.model_backends.")
    and name != "modulo.model_backends.base"
)
print(json.dumps(provider_modules))
"""
    result = subprocess.run(  # noqa: S603 — trusted input (sys.executable)
        [sys.executable, "-c", script],
        check=True,
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert json.loads(result.stdout) == []


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

    async def health_check(self) -> HealthResult:
        if self._fail:
            return HealthResult(ok=False, detail="Backend unavailable")
        return HealthResult(ok=True)

    async def invoke(self, messages: list[BaseMessage], **kwargs: Any) -> BaseMessage:
        if self._fail:
            raise RuntimeError("Backend unavailable")
        return AIMessage(content="ok")

    def stream(self, messages: list[BaseMessage], **kwargs: Any):  # type: ignore[return]
        async def _gen():
            yield AIMessage(content="ok")

        return _gen()


async def _slow_health_check() -> HealthResult:
    raise TimeoutError("took too long")


async def _broken_health_check() -> HealthResult:
    raise RuntimeError("x" * 501)


async def _cancelled_health_check() -> HealthResult:
    raise asyncio.CancelledError()


class _KWVFakeBackend(ModelBackendBase):
    """ModelBackendBase that accepts arbitrary constructor kwargs (for _build_backend)."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.args = args
        self.kwargs = kwargs

    @property
    def backend_id(self) -> str:
        return "fake"

    async def health_check(self) -> HealthResult:
        return HealthResult(ok=True)

    async def invoke(self, messages: list[BaseMessage], **kwargs: Any) -> BaseMessage:
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
    fallback_backend_ids: list[Any] | None = None


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


async def test_find_healthy_fallback_unregistered_logs_warning():
    """A configured fallback that was never registered logs a warning and is skipped."""
    hub = ModelBackendHub()
    primary = uuid.uuid4()
    unregistered = uuid.uuid4()
    hub.register(primary, _FakeBackend())
    hub.mark_unhealthy(primary)
    hub._fallbacks[primary] = [unregistered]

    with patch("modulo.core.model_backend_hub.logger.warning") as mock_warn, pytest.raises(BackendUnavailableError):
        await hub.get(primary)
    mock_warn.assert_called_once()
    args, _kwargs = mock_warn.call_args
    assert "not registered" in args[0]


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
    result = await hub.get_with_rotation(primary)
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
    result = await hub.get_with_rotation(primary)
    assert result.backend is hub._backends[fallback]
    assert result.rotated is True
    assert result.original_id == primary


async def test_get_with_rotation_raises_when_all_unhealthy():
    hub = ModelBackendHub()
    bid = uuid.uuid4()
    hub.register(bid, _FakeBackend())
    hub.mark_unhealthy(bid)
    with pytest.raises(BackendUnavailableError):
        await hub.get_with_rotation(bid)


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


async def test_initialise_wrong_key_skips_backend():
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
        await hub.initialise([mb], secrets_backend=backend)
    assert mb.id not in hub.backend_ids


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

        async def health_check(self) -> HealthResult:
            return HealthResult(ok=True)

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


async def test_initialise_plugin_fallback_not_registered_skips_backend():
    """When a provider is not built-in and not in the plugin registry, skip and continue."""
    mb = _FakeMB(
        id=uuid.uuid4(),
        provider="unknown_provider",
        model_id="m1",
        credentials_ciphertext=_encrypt({"api_key": "x"}),
    )
    backend = create_secrets_backend(fernet_key=_KEY, backend_name="fernet")
    with patch.object(backend, "get_secret", return_value='{"api_key": "x"}'):
        hub = ModelBackendHub()
        await hub.initialise([mb], secrets_backend=backend)
    assert mb.id not in hub.backend_ids


async def test_initialise_missing_api_key_skips_backend():
    mb = _FakeMB(
        id=uuid.uuid4(),
        provider="anthropic",
        model_id="claude-haiku-4-5",
        credentials_ciphertext=_encrypt({}),  # no api_key field
    )
    backend = create_secrets_backend(fernet_key=_KEY, backend_name="fernet")
    with patch.object(backend, "get_secret", return_value="{}"):
        hub = ModelBackendHub()
        await hub.initialise([mb], secrets_backend=backend)
    assert mb.id not in hub.backend_ids


async def test_get_with_rotation_raises_for_unregistered_id():
    """get_with_rotation raises BackendUnavailableError (not BackendNotFoundError)
    for unregistered IDs, consistent with the product map spec."""
    hub = ModelBackendHub()
    with pytest.raises(BackendUnavailableError):
        await hub.get_with_rotation(uuid.uuid4())


async def test_get_with_rotation_empty_hub_raises():
    """get_with_rotation on empty hub raises BackendUnavailableError."""
    hub = ModelBackendHub()
    with pytest.raises(BackendUnavailableError):
        await hub.get_with_rotation(uuid.uuid4())


async def test_initialise_self_referencing_fallback_does_not_crash():
    """Self-referencing fallback ID should not crash; the inner _fallbacks entry
    matches the backend's own ID, which get() skips because the self is unhealthy."""
    primary_id = uuid.uuid4()
    row = _FakeMB(
        id=primary_id,
        provider="ollama",
        model_id="llama3",
        credentials_ciphertext=_encrypt({"api_key": ""}),
    )
    secrets_backend = create_secrets_backend(fernet_key=_KEY, backend_name="fernet")
    with (
        patch.object(secrets_backend, "get_secret", return_value='{"api_key": ""}'),
        patch("modulo.model_backends.ollama.ChatOpenAI"),
    ):
        hub = ModelBackendHub()
        hub._fallbacks[primary_id] = [primary_id]
        await hub.initialise([row], secrets_backend=secrets_backend)
    assert primary_id in hub.backend_ids
    assert hub._fallbacks.get(primary_id) == [primary_id]


async def test_initialise_plugin_build_failure_skips_backend():
    """When registry.build_model_backend raises, the backend is skipped
    and initialise continues without propagating the error."""
    mb = _FakeMB(
        id=uuid.uuid4(),
        provider="my_custom_provider",
        model_id="custom-model",
        credentials_ciphertext=_encrypt({"api_key": "ck-test"}),
    )
    secrets_backend = create_secrets_backend(fernet_key=_KEY, backend_name="fernet")
    with (
        patch.object(secrets_backend, "get_secret", return_value='{"api_key": "ck-test"}'),
        patch("modulo.core.model_backend_hub.get_plugin_registry") as mock_reg,
    ):
        mock_reg.return_value.has_model_backend.return_value = True
        mock_reg.return_value.build_model_backend.side_effect = RuntimeError("Plugin crash")
        hub = ModelBackendHub()
        await hub.initialise([mb], secrets_backend=secrets_backend)
    assert mb.id not in hub.backend_ids


# ---------------------------------------------------------------------------
# Lifecycle edge cases
# ---------------------------------------------------------------------------


async def test_aexit_logs_and_clears_on_error():
    """__aexit__ with an active exception logs it and still clears the hub."""
    hub = ModelBackendHub()
    bid = uuid.uuid4()
    hub.register(bid, _FakeBackend())

    with pytest.raises(RuntimeError):
        async with hub:
            raise RuntimeError("boom")

    assert hub.backend_ids == frozenset()


async def test_register_overwrite_logs_warning():
    """Re-registering the same ID logs a warning and replaces the backend."""
    hub = ModelBackendHub()
    bid = uuid.uuid4()
    hub.register(bid, _FakeBackend(bid="first"))
    with patch("modulo.core.model_backend_hub.logger.warning") as mock_warn:
        hub.register(bid, _FakeBackend(bid="second"))
    mock_warn.assert_called_once()
    assert hub._backends[bid].backend_id == "second"


async def test_initialise_none_instances_raises():
    hub = ModelBackendHub()
    with pytest.raises(ValueError, match="instances must not be None"):
        await hub.initialise(None, secrets_backend=create_secrets_backend(fernet_key=_KEY, backend_name="fernet"))


# ---------------------------------------------------------------------------
# initialise error handling
# ---------------------------------------------------------------------------


async def test_initialise_secret_fetch_timeout_skips_backend():
    """A timeout fetching the secret for a backend skips it and continues."""
    mb = _FakeMB(
        id=uuid.uuid4(),
        provider="anthropic",
        model_id="claude-haiku-4-5",
        credentials_ciphertext=_encrypt({"api_key": "sk-ant-test"}),
    )
    secrets_backend = create_secrets_backend(fernet_key=_KEY, backend_name="fernet")
    with patch.object(secrets_backend, "get_secret", side_effect=TimeoutError("slow")):
        hub = ModelBackendHub()
        await hub.initialise([mb], secrets_backend=secrets_backend)
    assert mb.id not in hub.backend_ids


async def test_initialise_decrypt_failure_skips_backend():
    """A ciphertext that cannot be decrypted raises BackendDecryptError
    which is swallowed by initialise; the backend is not registered."""
    mb = _FakeMB(
        id=uuid.uuid4(),
        provider="anthropic",
        model_id="claude-haiku-4-5",
        credentials_ciphertext=Fernet(Fernet.generate_key().decode()).encrypt(b'{"api_key":"x"}'),
    )
    secrets_backend = create_secrets_backend(fernet_key=_KEY, backend_name="fernet")
    with patch.object(secrets_backend, "get_secret", side_effect=KeyError(str(mb.id))):
        hub = ModelBackendHub()
        await hub.initialise([mb], secrets_backend=secrets_backend)
    assert mb.id not in hub.backend_ids


async def test_initialise_keyerror_without_ciphertext_re_raises():
    """A KeyError with no fallback ciphertext is re-raised and swallowed;
    the backend is skipped rather than registered."""
    mb = _FakeMB(
        id=uuid.uuid4(),
        provider="anthropic",
        model_id="claude-haiku-4-5",
        credentials_ciphertext=b"",  # empty → falls through to bare `raise`
    )
    secrets_backend = create_secrets_backend(fernet_key=_KEY, backend_name="fernet")
    with patch.object(secrets_backend, "get_secret", side_effect=KeyError(str(mb.id))):
        hub = ModelBackendHub()
        await hub.initialise([mb], secrets_backend=secrets_backend)
    assert mb.id not in hub.backend_ids


async def test_initialise_malformed_secret_json_skips_backend():
    mb = _FakeMB(
        id=uuid.uuid4(),
        provider="anthropic",
        model_id="claude-haiku-4-5",
        credentials_ciphertext=_encrypt({"api_key": "sk-ant-test"}),
    )
    secrets_backend = create_secrets_backend(fernet_key=_KEY, backend_name="fernet")
    with patch.object(secrets_backend, "get_secret", return_value="{not json"):
        hub = ModelBackendHub()
        await hub.initialise([mb], secrets_backend=secrets_backend)
    assert mb.id not in hub.backend_ids


async def test_initialise_secret_not_object_skips_backend():
    mb = _FakeMB(
        id=uuid.uuid4(),
        provider="anthropic",
        model_id="claude-haiku-4-5",
        credentials_ciphertext=_encrypt({"api_key": "sk-ant-test"}),
    )
    secrets_backend = create_secrets_backend(fernet_key=_KEY, backend_name="fernet")
    with patch.object(secrets_backend, "get_secret", return_value="[1, 2, 3]"):
        hub = ModelBackendHub()
        await hub.initialise([mb], secrets_backend=secrets_backend)
    assert mb.id not in hub.backend_ids


async def test_initialise_non_iterable_fallback_ids_skips_backend():
    """Non-iterable fallback_backend_ids logs a warning and skips fallback registration."""
    mb = _FakeMB(
        id=uuid.uuid4(),
        provider="anthropic",
        model_id="claude-haiku-4-5",
        credentials_ciphertext=_encrypt({"api_key": "sk-ant-test"}),
        fallback_backend_ids="not-a-list",  # type: ignore[arg-type]
    )
    secrets_backend = create_secrets_backend(fernet_key=_KEY, backend_name="fernet")
    with patch.object(secrets_backend, "get_secret", return_value='{"api_key": "sk-ant-test"}'):
        hub = ModelBackendHub()
        await hub.initialise([mb], secrets_backend=secrets_backend)
    assert mb.id in hub.backend_ids
    assert mb.id not in hub._fallbacks


async def test_initialise_invalid_fallback_id_strings_skipped():
    """Invalid fallback UUID strings and wrong-typed IDs are skipped individually."""
    primary = uuid.uuid4()
    good_fallback = uuid.uuid4()
    mb = _FakeMB(
        id=primary,
        provider="anthropic",
        model_id="claude-haiku-4-5",
        credentials_ciphertext=_encrypt({"api_key": "sk-ant-test"}),
        fallback_backend_ids=["not-a-uuid", 12345, str(good_fallback)],
    )
    secrets_backend = create_secrets_backend(fernet_key=_KEY, backend_name="fernet")
    with patch.object(secrets_backend, "get_secret", return_value='{"api_key": "sk-ant-test"}'):
        hub = ModelBackendHub()
        await hub.initialise([mb], secrets_backend=secrets_backend)
    assert primary in hub.backend_ids
    assert hub._fallbacks[primary] == [good_fallback]


async def test_initialise_uuid_instance_fallback_id_appended():
    """A raw uuid.UUID fallback ID is appended without string parsing."""
    primary = uuid.uuid4()
    fallback = uuid.uuid4()
    mb = _FakeMB(
        id=primary,
        provider="anthropic",
        model_id="claude-haiku-4-5",
        credentials_ciphertext=_encrypt({"api_key": "sk-ant-test"}),
        fallback_backend_ids=[fallback],
    )
    secrets_backend = create_secrets_backend(fernet_key=_KEY, backend_name="fernet")
    with patch.object(secrets_backend, "get_secret", return_value='{"api_key": "sk-ant-test"}'):
        hub = ModelBackendHub()
        await hub.initialise([mb], secrets_backend=secrets_backend)
    assert hub._fallbacks[primary] == [fallback]


async def test_initialise_decrypt_ciphertext_success_path():
    """When get_secret raises KeyError but a decryptable ciphertext exists,
    the backend is built from the decrypted credentials."""
    key = Fernet.generate_key().decode()
    ciphertext = Fernet(key.encode()).encrypt(b'{"api_key": "sk-ciphertext"}')
    mb = _FakeMB(
        id=uuid.uuid4(),
        provider="anthropic",
        model_id="claude-haiku-4-5",
        credentials_ciphertext=ciphertext,
    )
    secrets_backend = create_secrets_backend(fernet_key=key, backend_name="fernet")
    settings = MagicMock()
    settings.fernet_key = key
    with (
        patch.object(secrets_backend, "get_secret", side_effect=KeyError(str(mb.id))),
        patch("modulo.settings.get_settings", return_value=settings),
        patch("modulo.model_backends.anthropic.ChatAnthropic"),
    ):
        hub = ModelBackendHub()
        await hub.initialise([mb], secrets_backend=secrets_backend)
    assert mb.id in hub.backend_ids


# ---------------------------------------------------------------------------
# Health check failure paths
# ---------------------------------------------------------------------------


async def test_health_check_timeout_marks_unhealthy():
    """A health check that exceeds the timeout marks the backend unhealthy."""
    hub = ModelBackendHub()
    bid = uuid.uuid4()
    slow = _FakeBackend()
    slow.health_check = _slow_health_check  # type: ignore[method-assign]
    hub.register(bid, slow)
    result = await hub.health_check(bid)
    assert result.ok is False
    assert "timed out" in result.detail
    with pytest.raises(BackendUnavailableError):
        await hub.get(bid)


async def test_health_check_exception_marks_unhealthy():
    """An exception from the backend's health check is captured and truncates detail."""
    hub = ModelBackendHub()
    bid = uuid.uuid4()
    broken = _FakeBackend()
    broken.health_check = _broken_health_check  # type: ignore[method-assign]
    hub.register(bid, broken)
    result = await hub.health_check(bid)
    assert result.ok is False
    assert result.detail == "x" * _ERROR_DETAIL_MAX_LENGTH
    assert len(result.detail) <= _ERROR_DETAIL_MAX_LENGTH
    with pytest.raises(BackendUnavailableError):
        await hub.get(bid)


async def test_health_check_cancelled_error_propagates():
    """asyncio.CancelledError from a health check propagates (never swallowed)."""
    hub = ModelBackendHub()
    bid = uuid.uuid4()
    cancelled = _FakeBackend()
    cancelled.health_check = _cancelled_health_check  # type: ignore[method-assign]
    hub.register(bid, cancelled)
    with pytest.raises(asyncio.CancelledError):
        await hub.health_check(bid)


# ---------------------------------------------------------------------------
# Rotation audit events
# ---------------------------------------------------------------------------


async def test_get_emits_failover_event_with_audit_logger():
    """get() with a healthy fallback emits a model_failover audit event."""
    hub = ModelBackendHub()
    primary = uuid.uuid4()
    fallback = uuid.uuid4()
    hub.register(primary, _FakeBackend())
    hub.register(fallback, _FakeBackend())
    hub.mark_unhealthy(primary)
    hub._fallbacks[primary] = [fallback]

    events: list[dict[str, Any]] = []

    async def _audit(event: dict[str, Any]) -> None:
        events.append(event)

    result = await hub.get(primary, audit_logger=_audit)
    assert result is hub._backends[fallback]
    assert events == [
        {
            "event_type": "model_failover",
            "primary_id": str(primary),
            "fallback_id": str(fallback),
        }
    ]


async def test_get_with_rotation_emits_failover_event():
    hub = ModelBackendHub()
    primary = uuid.uuid4()
    fallback = uuid.uuid4()
    hub.register(primary, _FakeBackend())
    hub.register(fallback, _FakeBackend())
    hub.mark_unhealthy(primary)
    hub._fallbacks[primary] = [fallback]

    events: list[dict[str, Any]] = []

    async def _audit(event: dict[str, Any]) -> None:
        events.append(event)

    result = await hub.get_with_rotation(primary, audit_logger=_audit)
    assert result.rotated is True
    assert result.backend is hub._backends[fallback]
    assert len(events) == 1


async def test_failover_event_audit_logger_failure_isolated():
    """A failing audit logger never breaks backend resolution."""
    hub = ModelBackendHub()
    primary = uuid.uuid4()
    fallback = uuid.uuid4()
    hub.register(primary, _FakeBackend())
    hub.register(fallback, _FakeBackend())
    hub.mark_unhealthy(primary)
    hub._fallbacks[primary] = [fallback]

    async def _bad_audit(event: dict[str, Any]) -> None:
        raise RuntimeError("audit logger down")

    with patch("modulo.core.model_backend_hub.logger.exception") as mock_log:
        result = await hub.get_with_rotation(primary, audit_logger=_bad_audit)
    mock_log.assert_called_once()
    assert result.backend is hub._backends[fallback]


async def test_failover_event_audit_logger_cancelled_propagates():
    """CancelledError from the audit logger must propagate (never swallowed)."""
    hub = ModelBackendHub()
    primary = uuid.uuid4()
    fallback = uuid.uuid4()
    hub.register(primary, _FakeBackend())
    hub.register(fallback, _FakeBackend())
    hub.mark_unhealthy(primary)
    hub._fallbacks[primary] = [fallback]

    async def _cancel_audit(event: dict[str, Any]) -> None:
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await hub.get(primary, audit_logger=_cancel_audit)


# ---------------------------------------------------------------------------
# mark_unhealthy edge cases
# ---------------------------------------------------------------------------


async def test_mark_unhealthy_unknown_backend_raises():
    hub = ModelBackendHub()
    with pytest.raises(BackendNotFoundError):
        hub.mark_unhealthy(uuid.uuid4())


# ---------------------------------------------------------------------------
# _build_backend provider validation
# ---------------------------------------------------------------------------


async def test_build_backend_bedrock_missing_credentials():
    from modulo.core.model_backend_hub import _build_backend

    with pytest.raises(ValueError, match="aws_access_key_id"):
        _build_backend("bedrock", "model", {}, {})


async def test_build_backend_bedrock_missing_secret():
    from modulo.core.model_backend_hub import _build_backend

    with pytest.raises(ValueError, match="aws_secret_access_key"):
        _build_backend("bedrock", "model", {"aws_access_key_id": "x"}, {})


async def test_build_backend_vertexai_missing_project():
    from modulo.core.model_backend_hub import _build_backend

    with pytest.raises(ValueError, match="project"):
        _build_backend("vertexai", "model", {}, {})


async def test_build_backend_api_key_provider_missing_key():
    from modulo.core.model_backend_hub import _build_backend

    with pytest.raises(ValueError, match="api_key"):
        _build_backend("cohere", "model", {}, {})


async def test_build_backend_azure_missing_endpoint():
    from modulo.core.model_backend_hub import _build_backend

    with pytest.raises(ValueError, match="azure_endpoint"):
        _build_backend("azure_openai", "model", {"api_key": "x"}, {})


async def test_build_backend_watsonx_missing_project_id():
    from modulo.core.model_backend_hub import _build_backend

    with pytest.raises(ValueError, match="project_id"):
        _build_backend("watsonx", "model", {"api_key": "x"}, {})


async def test_build_backend_bedrock_success():
    from modulo.core.model_backend_hub import _build_backend

    with patch("modulo.core.model_backend_hub._backend_class") as mock_cls:
        mock_cls.return_value = _KWVFakeBackend
        backend = _build_backend(
            "bedrock",
            "model",
            {"aws_access_key_id": "a", "aws_secret_access_key": "s"},
            {},
        )
    assert isinstance(backend, _KWVFakeBackend)
    mock_cls.assert_called_once_with("bedrock", "BedrockBackend")


async def test_build_backend_vertexai_success():
    from modulo.core.model_backend_hub import _build_backend

    with patch("modulo.core.model_backend_hub._backend_class") as mock_cls:
        mock_cls.return_value = _KWVFakeBackend
        backend = _build_backend("vertexai", "model", {"project": "proj"}, {})
    assert isinstance(backend, _KWVFakeBackend)
    mock_cls.assert_called_once_with("vertexai", "VertexAIBackend")


async def test_build_backend_azure_success():
    from modulo.core.model_backend_hub import _build_backend

    with patch("modulo.core.model_backend_hub._backend_class") as mock_cls:
        mock_cls.return_value = _KWVFakeBackend
        backend = _build_backend(
            "azure_openai",
            "model",
            {"api_key": "x", "azure_endpoint": "https://azure.invalid"},
            {},
        )
    assert isinstance(backend, _KWVFakeBackend)
    mock_cls.assert_called_once_with("azure_openai", "AzureOpenAIBackend")


async def test_build_backend_watsonx_success():
    from modulo.core.model_backend_hub import _build_backend

    with patch("modulo.core.model_backend_hub._backend_class") as mock_cls:
        mock_cls.return_value = _KWVFakeBackend
        backend = _build_backend("watsonx", "model", {"api_key": "x", "project_id": "proj"}, {})
    assert isinstance(backend, _KWVFakeBackend)
    mock_cls.assert_called_once_with("watsonx", "WatsonXBackend")


async def test_build_backend_openai_compatible_default_base_url():
    from modulo.core.model_backend_hub import _build_backend
    from modulo.model_backends.module import OpenAICompatibleBackend

    backend = _build_backend("ollama", "model", {"api_key": ""}, {})
    assert isinstance(backend, OpenAICompatibleBackend)
    assert backend.base_url == "http://localhost:11434/v1"


async def test_build_backend_openai_no_default_base_url():
    from modulo.core.model_backend_hub import _build_backend
    from modulo.model_backends.module import OpenAICompatibleBackend

    backend = _build_backend("openai", "model", {"api_key": "x"}, {})
    assert isinstance(backend, OpenAICompatibleBackend)
    assert backend.base_url is None


async def test_build_backend_plugin_missing_api_key_raises():
    from modulo.core.model_backend_hub import _build_backend
    from modulo.core.plugin_registry import PluginManifest, PluginRegistry

    reg = PluginRegistry()
    reg.register_model_backend(
        "plugin_no_key",
        lambda api_key, model_id, **kwargs: _FakeBackend(),
        PluginManifest(PLUGIN_ID="pkg-nokey", display_name="NoKey", description="", version="1"),
    )
    with (
        patch("modulo.core.model_backend_hub.get_plugin_registry", return_value=reg),
        pytest.raises(ValueError, match="api_key"),
    ):
        _build_backend("plugin_no_key", "model", {}, {})


async def test_build_backend_plugin_cancelled_propagates():
    from modulo.core.model_backend_hub import _build_backend
    from modulo.core.plugin_registry import PluginManifest, PluginRegistry

    def _cancelling_builder(api_key: str, model_id: str, **kwargs: Any) -> ModelBackendBase:
        raise asyncio.CancelledError()

    reg = PluginRegistry()
    reg.register_model_backend(
        "plugin_cancel",
        _cancelling_builder,
        PluginManifest(PLUGIN_ID="pkg-cancel", display_name="Cancel", description="", version="1"),
    )
    with (
        patch("modulo.core.model_backend_hub.get_plugin_registry", return_value=reg),
        pytest.raises(asyncio.CancelledError),
    ):
        _build_backend("plugin_cancel", "model", {"api_key": "x"}, {})


async def test_build_backend_unknown_provider():
    from modulo.core.model_backend_hub import _build_backend

    with pytest.raises(ValueError, match="Unknown model backend provider"):
        _build_backend("no_such_provider", "model", {"api_key": "x"}, {})
