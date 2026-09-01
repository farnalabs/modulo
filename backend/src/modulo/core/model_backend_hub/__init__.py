"""ModelBackendHub — run-scoped registry with health check and rotation.

Usage:
    hub = ModelBackendHub()
    async with hub:
        await hub.initialise(model_backend_rows, secrets_backend=secrets_backend)
        backend = await hub.get(backend_id)
        reply = await backend.invoke(messages)
    # After __aexit__: all backend references discarded, API keys gone.
"""

import asyncio
import importlib
import json
import logging
import sys
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Self, cast

from langchain_core.messages import BaseMessage

from modulo.core.plugin_registry import get_plugin_registry
from modulo.core.secrets_backend import SecretsBackend
from modulo.model_backends.base import HealthResult, ModelBackendBase

logger = logging.getLogger(__name__)

_HEALTH_CHECK_TIMEOUT: float = 10.0
_SECRET_FETCH_TIMEOUT: float = 10.0
_ERROR_DETAIL_MAX_LENGTH: int = 500
_LOCALHOST_V1_URL: str = "http://localhost:8080/v1"


@dataclass
class RotatedResult:
    backend: ModelBackendBase
    rotated: bool
    original_id: uuid.UUID | None = None
    used_fallback_id: uuid.UUID | None = None


class BackendNotFoundError(Exception):
    """Raised when hub.get() is called with an unregistered backend ID."""

    def __init__(self, backend_id: uuid.UUID) -> None:
        super().__init__(f"Backend {backend_id} not found")
        self.backend_id = backend_id


class BackendUnavailableError(Exception):
    """Raised when the requested backend (and all fallbacks) are unhealthy."""

    def __init__(self, backend_id: uuid.UUID) -> None:
        super().__init__(f"No healthy backend available; requested {backend_id}")
        self.backend_id = backend_id


class BackendDecryptError(ValueError):
    """Raised when credentials cannot be decrypted."""

    def __init__(self, backend_id: uuid.UUID) -> None:
        super().__init__(f"Failed to decrypt credentials for model backend {backend_id}")
        self.backend_id = backend_id


class ModelBackendHub:
    """Registry of model backends; manages decryption, health checks, and rotation.

    Not thread-safe. Each run gets its own hub instance.
    """

    def __init__(self) -> None:
        self._backends: dict[uuid.UUID, ModelBackendBase] = {}
        self._healthy: dict[uuid.UUID, bool] = {}
        self._fallbacks: dict[uuid.UUID, list[uuid.UUID]] = {}

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, exc_type: object, exc_val: object, _exc_tb: object) -> None:
        if exc_type is not None:
            logger.error("ModelBackendHub exiting due to error: %s", exc_val, exc_info=sys.exc_info())
        self._backends.clear()
        self._healthy.clear()
        self._fallbacks.clear()

    def register(self, backend_id: uuid.UUID, backend: ModelBackendBase) -> None:
        """Register a pre-built backend (e.g. StubModelBackend adapter in tests)."""
        if backend_id in self._backends:
            logger.warning("Overwriting already registered backend %s", backend_id)
        self._backends[backend_id] = backend
        self._healthy[backend_id] = True

    async def initialise(self, instances: Sequence[Any], secrets_backend: SecretsBackend) -> None:
        """Decrypt API keys and register backends. Call once at run start.

        `instances` must be `ModelBackend` ORM rows (or duck-typed equivalents with
        `.id`, `.provider`, `.model_id`, `.default_params`).
        """
        if instances is None:
            raise ValueError("instances must not be None")

        backends_to_register: list[tuple[uuid.UUID, ModelBackendBase]] = []
        fallback_map: dict[uuid.UUID, list[uuid.UUID]] = {}

        for mb in instances:
            try:
                try:
                    raw_str = await asyncio.wait_for(
                        secrets_backend.get_secret(str(mb.id)),
                        timeout=_SECRET_FETCH_TIMEOUT,
                    )
                except TimeoutError:
                    logger.warning("Timeout fetching secret for backend %s", mb.id)
                    continue
                except KeyError:
                    ciphertext = getattr(mb, "credentials_ciphertext", None)
                    if ciphertext and isinstance(ciphertext, bytes) and ciphertext != b"":
                        try:
                            from cryptography.fernet import Fernet

                            from modulo.settings import get_settings

                            _settings = get_settings()
                            f = Fernet(_settings.fernet_key.encode())
                            plaintext = f.decrypt(ciphertext)
                            raw_str = json.dumps({"api_key": plaintext.decode()})
                        except Exception:
                            logger.warning(
                                "Failed to decrypt credentials_ciphertext for backend %s", mb.id, exc_info=True
                            )
                            raise BackendDecryptError(mb.id) from None
                    else:
                        raise
                try:
                    raw_creds: Any = json.loads(raw_str)
                except json.JSONDecodeError as exc:
                    logger.warning("Malformed secret JSON for backend %s: %s", mb.id, exc)
                    continue
                if not isinstance(raw_creds, dict):
                    logger.warning("Secret for backend %s is not a JSON object", mb.id)
                    continue
                creds: dict[str, Any] = raw_creds
                backend = _build_backend(mb.provider, mb.model_id, creds, mb.default_params or {})
                backends_to_register.append((mb.id, backend))

                raw_fallback_ids = getattr(mb, "fallback_backend_ids", None)
                if raw_fallback_ids is not None:
                    if not isinstance(raw_fallback_ids, list | tuple):
                        logger.warning(
                            "Non-iterable fallback_backend_ids for backend %s: %r",
                            mb.id,
                            raw_fallback_ids,
                        )
                        continue
                    parsed: list[uuid.UUID] = []
                    for fid in raw_fallback_ids:
                        if isinstance(fid, str):
                            try:
                                parsed.append(uuid.UUID(fid))
                            except ValueError as exc:
                                logger.warning(
                                    "Invalid fallback ID string %r for backend %s: %s",
                                    fid,
                                    mb.id,
                                    exc,
                                )
                                continue
                        elif isinstance(fid, uuid.UUID):
                            parsed.append(fid)
                        else:
                            logger.warning(
                                "Unexpected fallback ID type %r for backend %s",
                                type(fid).__name__,
                                mb.id,
                            )
                            continue
                    if parsed:
                        fallback_map[mb.id] = parsed
            except (AttributeError, TypeError, ValueError, KeyError, BackendDecryptError):
                logger.exception("Failed to initialise backend %s", mb.id)
                continue

        if not backends_to_register:
            logger.warning("No backends were registered during initialise — all instances failed or none provided")

        for backend_id, backend in backends_to_register:
            self.register(backend_id, backend)

        self._fallbacks.update(fallback_map)

    def _find_healthy_fallback(self, backend_id: uuid.UUID) -> uuid.UUID | None:
        """Return the first healthy fallback ID, or None if none are healthy."""
        for fallback_id in self._fallbacks.get(backend_id, []):
            if fallback_id not in self._backends:
                logger.warning("Fallback %s for backend %s is not registered", fallback_id, backend_id)
                continue
            if self._healthy.get(fallback_id, False):
                return fallback_id
        return None

    async def _emit_failover_event(
        self,
        audit_logger: Callable[[dict[str, Any]], Awaitable[None]] | None,
        primary_id: uuid.UUID,
        fallback_id: uuid.UUID,
    ) -> None:
        """Emit a ``model_failover`` audit event, isolating logger failures.

        A failing audit logger must never break backend resolution — the failure
        is logged and the rotation proceeds.
        """
        if audit_logger is None:
            return
        try:
            await audit_logger(
                {
                    "event_type": "model_failover",
                    "primary_id": str(primary_id),
                    "fallback_id": str(fallback_id),
                }
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Audit logger failed during failover for backend %s", primary_id)

    async def get(
        self,
        backend_id: uuid.UUID,
        *,
        audit_logger: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> ModelBackendBase:
        """Return the backend, trying fallbacks if the primary is unhealthy.

        Raises BackendNotFoundError if the backend is not registered.
        Raises BackendUnavailableError if no backend (primary or fallback) is healthy.
        If *audit_logger* is provided and a fallback is used, the logger is called
        with a dict containing event_type, primary_id, fallback_id.
        """
        if backend_id not in self._backends:
            raise BackendNotFoundError(backend_id)
        if self._healthy.get(backend_id, False):
            return self._backends[backend_id]

        fallback_id = self._find_healthy_fallback(backend_id)
        if fallback_id is not None:
            await self._emit_failover_event(audit_logger, backend_id, fallback_id)
            return self._backends[fallback_id]

        raise BackendUnavailableError(backend_id)

    async def get_with_rotation(
        self,
        backend_id: uuid.UUID,
        *,
        audit_logger: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> RotatedResult:
        """Return the requested backend if healthy; else rotate through fallbacks.

        Uses the configured ``fallback_backend_ids`` in order. Falls back to
        scanning all registered backends if no configured fallback is healthy.

        Returns a RotatedResult so the caller can detect when a fallback was used.
        Raises BackendUnavailableError if the backend is not registered.
        Raises BackendUnavailableError if no backend (primary or fallback) is healthy.
        If *audit_logger* is provided and a fallback is used, the logger is called
        with a dict containing event_type, primary_id, fallback_id.
        """
        if backend_id not in self._backends:
            raise BackendUnavailableError(backend_id)
        if self._healthy.get(backend_id, False):
            return RotatedResult(
                backend=self._backends[backend_id],
                rotated=False,
                original_id=backend_id,
            )
        fallback_id = self._find_healthy_fallback(backend_id)
        if fallback_id is not None:
            await self._emit_failover_event(audit_logger, backend_id, fallback_id)
            return RotatedResult(
                backend=self._backends[fallback_id],
                rotated=True,
                original_id=backend_id,
                used_fallback_id=fallback_id,
            )
        logger.warning(
            "No configured fallback healthy for backend %s; scanning all registered backends",
            backend_id,
        )
        for oid, backend in self._backends.items():
            if self._healthy.get(oid, False):
                await self._emit_failover_event(audit_logger, backend_id, oid)
                return RotatedResult(
                    backend=backend,
                    rotated=True,
                    original_id=backend_id,
                    used_fallback_id=oid,
                )
        raise BackendUnavailableError(backend_id)

    async def health_check(self, backend_id: uuid.UUID) -> HealthResult:
        """Check backend health via the backend's own lightweight health check."""
        if backend_id not in self._backends:
            return HealthResult(ok=False, detail="Backend not registered")
        backend = self._backends[backend_id]
        try:
            result = await asyncio.wait_for(
                backend.health_check(),
                timeout=_HEALTH_CHECK_TIMEOUT,
            )
            self._healthy[backend_id] = result.ok
            return result
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            self._healthy[backend_id] = False
            return HealthResult(ok=False, detail="Health check timed out")
        except Exception as exc:
            self._healthy[backend_id] = False
            return HealthResult(ok=False, detail=str(exc)[:_ERROR_DETAIL_MAX_LENGTH])

    def mark_unhealthy(self, backend_id: uuid.UUID) -> None:
        """Explicitly mark a backend as unhealthy (e.g. after a node-level error)."""
        if backend_id not in self._backends:
            raise BackendNotFoundError(backend_id)
        self._healthy[backend_id] = False

    @property
    def backend_ids(self) -> frozenset[uuid.UUID]:
        return frozenset(self._backends)


_NON_OPENAI_COMPATIBLE: dict[str, str] = {
    "anthropic": "AnthropicBackend",
    "cohere": "CohereBackend",
    "gemini": "GeminiBackend",
    "mistral": "MistralBackend",
}

_OPENAI_COMPATIBLE_BACKENDS: dict[str, str | None] = {
    "ai21": "https://api.ai21.com/studio/v1",
    "deepseek": "https://api.deepseek.com/v1",
    "fireworks": "https://api.fireworks.ai/inference/v1",
    "grok": "https://api.x.ai/v1",
    "groq": "https://api.groq.com/openai/v1",
    "jan": "http://localhost:1337/v1",
    "llamacpp": _LOCALHOST_V1_URL,
    "lm_studio": "http://localhost:1234/v1",
    "localai": _LOCALHOST_V1_URL,
    "ollama": "http://localhost:11434/v1",
    "openai": None,
    "opencode": "https://opencode.ai/zen/go/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "perplexity": "https://api.perplexity.ai",
    "qwen": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "tgi": _LOCALHOST_V1_URL,
    "togetherai": "https://api.together.xyz/v1",
    "vllm": "http://localhost:8000/v1",
}


def _backend_class(provider: str, class_name: str) -> Callable[..., ModelBackendBase]:
    """Import a provider adapter only when that provider is configured."""
    module = importlib.import_module(f"modulo.model_backends.{provider}")
    return cast("Callable[..., ModelBackendBase]", getattr(module, class_name))


_API_KEY_REQUIRED_PROVIDERS: frozenset[str] = frozenset(
    {
        "anthropic",
        "azure_openai",
        "cohere",
        "gemini",
        "mistral",
        "watsonx",
    }
)


def _extract_fixture_map(
    creds: dict[str, Any],
    default_params: dict[str, Any],
) -> dict[str, str]:
    """Return the stub fixture_map from default_params or creds, never raising."""
    for source in (default_params, creds):
        raw = source.get("fixture_map")
        if isinstance(raw, dict):
            return {str(key): str(value) for key, value in raw.items()}
    return {}


def _build_custom_stub_backend(fixture_map: dict[str, str]) -> ModelBackendBase:
    """Build a hub-compatible async stub backend for provider='custom'.

    StubModelBackend inherits BaseChatModel's synchronous ``invoke()``, which is
    incompatible with the hub's ``await backend.invoke()`` contract (node_runner
    awaits the result). This wrapper adapts it to ModelBackendBase, mirroring the
    _StubAdapter pattern used throughout the test suite. The lazy import preserves
    module-level import isolation (see test_import_does_not_load_provider_adapters).
    """
    from modulo.model_backends.stub.backend import StubModelBackend

    class _CustomStubBackend(ModelBackendBase):
        def __init__(self, fixture_map: Mapping[str, str] | None = None, **kwargs: Any) -> None:
            del kwargs
            self._stub = StubModelBackend(fixture_map)

        async def invoke(self, messages: list[BaseMessage], **kwargs: Any) -> BaseMessage:
            return await self._stub.ainvoke(messages, **kwargs)

        def stream(
            self,
            messages: list[BaseMessage],
            tools: list[dict[str, Any]] | None = None,
            **kwargs: Any,
        ) -> Any:
            return self._stub.astream(messages, tools=tools, **kwargs)

        async def health_check(self) -> HealthResult:
            return await self._stub.health_check()

        @property
        def backend_id(self) -> str:
            return "custom/stub"

    return _CustomStubBackend(fixture_map)


def _build_backend(
    provider: str,
    model_id: str,
    creds: dict[str, Any],
    default_params: dict[str, Any],
) -> ModelBackendBase:
    if provider == "bedrock":
        if "aws_access_key_id" not in creds:
            raise ValueError("Missing 'aws_access_key_id' in credentials for provider 'bedrock'")
        if "aws_secret_access_key" not in creds:
            raise ValueError("Missing 'aws_secret_access_key' in credentials for provider 'bedrock'")
        return _backend_class("bedrock", "BedrockBackend")(
            aws_access_key_id=creds["aws_access_key_id"],
            aws_secret_access_key=creds["aws_secret_access_key"],
            model_id=model_id,
            region=creds.get("region", "us-east-1"),
            **default_params,
        )
    if provider == "vertexai":
        if "project" not in creds:
            raise ValueError("Missing 'project' in credentials for provider 'vertexai'")
        return _backend_class("vertexai", "VertexAIBackend")(
            project=creds["project"],
            model_id=model_id,
            location=creds.get("location", "us-central-1"),
            **default_params,
        )
    if provider == "custom":
        return _build_custom_stub_backend(_extract_fixture_map(creds, default_params))
    if provider in _API_KEY_REQUIRED_PROVIDERS and "api_key" not in creds:
        raise ValueError(f"Missing 'api_key' in credentials for provider {provider!r}")

    class_name = _NON_OPENAI_COMPATIBLE.get(provider)
    if class_name is not None:
        return _backend_class(provider, class_name)(api_key=creds["api_key"], model_id=model_id, **default_params)

    default_base_url = _OPENAI_COMPATIBLE_BACKENDS.get(provider)
    if default_base_url is not None or provider == "openai":
        base_url = creds.get("base_url", default_base_url) if default_base_url is not None else None
        from modulo.model_backends.module import OpenAICompatibleBackend

        return OpenAICompatibleBackend(
            api_key=creds.get("api_key"),
            model_id=model_id,
            base_url=base_url,
            provider=provider,
            **default_params,
        )

    if provider == "azure_openai":
        azure_endpoint = creds.get("azure_endpoint", "")
        if not azure_endpoint:
            raise ValueError("Missing 'azure_endpoint' in credentials for provider 'azure_openai'")
        api_version = creds.get("api_version", "2024-10-01-preview")
        return _backend_class("azure_openai", "AzureOpenAIBackend")(
            api_key=creds["api_key"],
            model_id=model_id,
            azure_endpoint=azure_endpoint,
            api_version=api_version,
            **default_params,
        )

    if provider == "watsonx":
        if "project_id" not in creds:
            raise ValueError("Missing 'project_id' in credentials for provider 'watsonx'")
        return _backend_class("watsonx", "WatsonXBackend")(
            api_key=creds["api_key"],
            model_id=model_id,
            project_id=creds["project_id"],
            url=creds.get("url", "https://us-south.ml.cloud.ibm.com"),
            **default_params,
        )

    registry = get_plugin_registry()
    if registry.has_model_backend(provider):
        api_key = creds.get("api_key")
        if not api_key:
            raise ValueError(f"Missing 'api_key' in credentials for provider {provider!r}")
        try:
            return registry.build_model_backend(provider, model_id, api_key, **default_params)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise ValueError(f"Failed to build plugin model backend for provider {provider!r}: {exc}") from exc
    raise ValueError(f"Unknown model backend provider: {provider!r}")
