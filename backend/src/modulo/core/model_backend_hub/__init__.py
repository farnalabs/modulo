"""ModelBackendHub — run-scoped registry with health check and rotation.

Usage:
    hub = ModelBackendHub()
    async with hub:
        await hub.initialise(model_backend_rows, secrets_backend=secrets_backend)
        backend = await hub.get(backend_id)
        reply = await backend.invoke(messages)
    # After __aexit__: all backend references discarded, API keys gone.
"""

import json
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

from modulo.core.plugin_registry import get_plugin_registry
from modulo.core.secrets_backend import SecretsBackend
from modulo.model_backends.ai21 import Ai21Backend
from modulo.model_backends.anthropic import AnthropicBackend
from modulo.model_backends.azure_openai import AzureOpenAIBackend
from modulo.model_backends.base import HealthResult, ModelBackendBase
from modulo.model_backends.bedrock import BedrockBackend
from modulo.model_backends.cohere import CohereBackend
from modulo.model_backends.deepseek import DeepSeekBackend
from modulo.model_backends.fireworks import FireworksBackend
from modulo.model_backends.gemini import GeminiBackend
from modulo.model_backends.grok import GrokBackend
from modulo.model_backends.groq import GroqBackend
from modulo.model_backends.jan import JanBackend
from modulo.model_backends.llamacpp import LLamaCppBackend
from modulo.model_backends.lm_studio import LmStudioBackend
from modulo.model_backends.localai import LocalAIBackend
from modulo.model_backends.mistral import MistralBackend
from modulo.model_backends.ollama import OllamaBackend
from modulo.model_backends.openai import OpenAIBackend
from modulo.model_backends.openrouter import OpenRouterBackend
from modulo.model_backends.perplexity import PerplexityBackend
from modulo.model_backends.qwen import QwenBackend
from modulo.model_backends.tgi import TgiBackend
from modulo.model_backends.togetherai import TogetherAIBackend
from modulo.model_backends.vertexai import VertexAIBackend
from modulo.model_backends.vllm import VllmBackend
from modulo.model_backends.watsonx import WatsonXBackend


@dataclass
class RotatedResult:
    backend: ModelBackendBase
    rotated: bool
    original_id: uuid.UUID | None = None
    used_fallback_id: uuid.UUID | None = None


class BackendNotFoundError(KeyError):
    """Raised when hub.get() is called with an unregistered backend ID."""

    def __init__(self, backend_id: uuid.UUID) -> None:
        super().__init__(str(backend_id))
        self.backend_id = backend_id


class BackendUnavailableError(RuntimeError):
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

    async def __aenter__(self) -> "ModelBackendHub":
        return self

    async def __aexit__(self, *_: object) -> None:
        self._backends.clear()
        self._healthy.clear()
        self._fallbacks.clear()

    def register(self, backend_id: uuid.UUID, backend: ModelBackendBase) -> None:
        """Register a pre-built backend (e.g. StubModelBackend adapter in tests)."""
        self._backends[backend_id] = backend
        self._healthy[backend_id] = True

    async def initialise(self, instances: Sequence[Any], secrets_backend: SecretsBackend) -> None:
        """Decrypt API keys and register backends. Call once at run start.

        `instances` must be `ModelBackend` ORM rows (or duck-typed equivalents with
        `.id`, `.provider`, `.model_id`, `.credentials_ciphertext`, `.default_params`).
        """
        for mb in instances:
            try:
                raw_str = await secrets_backend.get_secret(str(mb.id))
            except KeyError as exc:
                raise BackendDecryptError(mb.id) from exc
            creds: dict[str, Any] = json.loads(raw_str)
            backend = _build_backend(mb.provider, mb.model_id, creds, mb.default_params or {})
            self.register(mb.id, backend)
            fallback_ids = getattr(mb, "fallback_backend_ids", None)
            if fallback_ids:
                self._fallbacks[mb.id] = [uuid.UUID(fid) if isinstance(fid, str) else fid for fid in fallback_ids]
            del raw_str, creds

    async def get(
        self,
        backend_id: uuid.UUID,
        *,
        audit_logger: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> ModelBackendBase:
        """Return the backend, trying fallbacks if the primary is unhealthy.

        Raises BackendUnavailableError if no backend (primary or fallback) is healthy.
        If *audit_logger* is provided and a fallback is used, the logger is called
        with a dict containing event_type, primary_id, fallback_id, provider info.
        """
        if backend_id not in self._backends:
            raise BackendNotFoundError(backend_id)
        if self._healthy.get(backend_id, False):
            return self._backends[backend_id]

        fallbacks = self._fallbacks.get(backend_id, [])
        for fallback_id in fallbacks:
            if fallback_id not in self._backends:
                continue
            if self._healthy.get(fallback_id, False):
                if audit_logger is not None:
                    await audit_logger(
                        {
                            "event_type": "model_failover",
                            "primary_id": str(backend_id),
                            "fallback_id": str(fallback_id),
                        }
                    )
                return self._backends[fallback_id]

        raise BackendUnavailableError(backend_id)

    def get_with_rotation(self, backend_id: uuid.UUID) -> RotatedResult:
        """Return the requested backend if healthy; else rotate through fallbacks.

        Uses the configured ``fallback_backend_ids`` in order. Falls back to
        scanning all registered backends if no fallbacks are configured.

        Returns a RotatedResult so the caller can detect when a fallback was used.
        """
        if backend_id not in self._backends:
            raise BackendNotFoundError(backend_id)
        if self._healthy.get(backend_id, False) and backend_id in self._backends:
            return RotatedResult(
                backend=self._backends[backend_id],
                rotated=False,
                original_id=backend_id,
            )
        fallbacks = self._fallbacks.get(backend_id, [])
        for fallback_id in fallbacks:
            if fallback_id not in self._backends:
                continue
            if self._healthy.get(fallback_id, False):
                return RotatedResult(
                    backend=self._backends[fallback_id],
                    rotated=True,
                    original_id=backend_id,
                    used_fallback_id=fallback_id,
                )
        for oid, backend in self._backends.items():
            if self._healthy.get(oid, False):
                return RotatedResult(
                    backend=backend,
                    rotated=True,
                    original_id=backend_id,
                    used_fallback_id=oid,
                )
        raise BackendUnavailableError(backend_id)

    async def health_check(self, backend_id: uuid.UUID) -> HealthResult:
        """Check backend health; delegates to the backend's own health check."""
        if backend_id not in self._backends:
            return HealthResult(ok=False, detail="Backend not registered")
        backend = self._backends[backend_id]
        result = await backend.health_check()
        self._healthy[backend_id] = result.ok
        return result

    def mark_unhealthy(self, backend_id: uuid.UUID) -> None:
        """Explicitly mark a backend as unhealthy (e.g. after a node-level error)."""
        self._healthy[backend_id] = False

    @property
    def backend_ids(self) -> frozenset[uuid.UUID]:
        return frozenset(self._backends)


def _build_backend(
    provider: str,
    model_id: str,
    creds: dict[str, Any],
    default_params: dict[str, Any],
) -> ModelBackendBase:
    if provider == "bedrock":
        return BedrockBackend(
            aws_access_key_id=creds["aws_access_key_id"],
            aws_secret_access_key=creds["aws_secret_access_key"],
            model_id=model_id,
            region=creds.get("region", "us-east-1"),
            **default_params,
        )
    if provider == "vertexai":
        if "project" not in creds:
            raise ValueError(f"Missing 'project' in credentials for provider 'vertexai'. Got keys: {sorted(creds)}")
        return VertexAIBackend(
            project=creds["project"],
            model_id=model_id,
            location=creds.get("location", "us-central-1"),
            **default_params,
        )
    if "api_key" not in creds:
        raise ValueError(f"Missing 'api_key' in credentials for provider {provider!r}")
    match provider:
        case "ai21":
            return Ai21Backend(api_key=creds["api_key"], model_id=model_id, **default_params)
        case "anthropic":
            return AnthropicBackend(api_key=creds["api_key"], model_id=model_id, **default_params)
        case "cohere":
            return CohereBackend(api_key=creds["api_key"], model_id=model_id, **default_params)
        case "azure_openai":
            azure_endpoint = creds.get("azure_endpoint", "")
            if not azure_endpoint:
                raise ValueError("Missing 'azure_endpoint' in credentials for provider 'azure_openai'")
            api_version = creds.get("api_version", "2024-10-01-preview")
            return AzureOpenAIBackend(
                api_key=creds["api_key"],
                model_id=model_id,
                azure_endpoint=azure_endpoint,
                api_version=api_version,
                **default_params,
            )
        case "openai":
            return OpenAIBackend(api_key=creds["api_key"], model_id=model_id, **default_params)
        case "lm_studio":
            base_url = creds.get("base_url", "http://localhost:1234/v1")
            return LmStudioBackend(
                api_key=creds.get("api_key", ""),
                model_id=model_id,
                base_url=base_url,
                **default_params,
            )
        case "localai":
            base_url = creds.get("base_url", "http://localhost:8080/v1")
            return LocalAIBackend(
                api_key=creds.get("api_key", ""),
                model_id=model_id,
                base_url=base_url,
                **default_params,
            )
        case "mistral":
            return MistralBackend(api_key=creds["api_key"], model_id=model_id, **default_params)
        case "ollama":
            base_url = creds.get("base_url", "http://localhost:11434/v1")
            return OllamaBackend(
                api_key=creds.get("api_key", ""),
                model_id=model_id,
                base_url=base_url,
                **default_params,
            )
        case "tgi":
            base_url = creds.get("base_url", "http://localhost:8080/v1")
            return TgiBackend(
                api_key=creds.get("api_key", ""),
                model_id=model_id,
                base_url=base_url,
                **default_params,
            )
        case "togetherai":
            return TogetherAIBackend(api_key=creds["api_key"], model_id=model_id, **default_params)
        case "deepseek":
            return DeepSeekBackend(api_key=creds["api_key"], model_id=model_id, **default_params)
        case "gemini":
            return GeminiBackend(api_key=creds["api_key"], model_id=model_id, **default_params)
        case "grok":
            return GrokBackend(api_key=creds["api_key"], model_id=model_id, **default_params)
        case "jan":
            base_url = creds.get("base_url", "http://localhost:1337/v1")
            return JanBackend(
                api_key=creds.get("api_key", ""),
                model_id=model_id,
                base_url=base_url,
                **default_params,
            )
        case "llamacpp":
            base_url = creds.get("base_url", "http://localhost:8080/v1")
            return LLamaCppBackend(
                api_key=creds.get("api_key", ""),
                model_id=model_id,
                base_url=base_url,
                **default_params,
            )
        case "fireworks":
            return FireworksBackend(api_key=creds["api_key"], model_id=model_id, **default_params)
        case "groq":
            return GroqBackend(api_key=creds["api_key"], model_id=model_id, **default_params)
        case "perplexity":
            return PerplexityBackend(api_key=creds["api_key"], model_id=model_id, **default_params)
        case "qwen":
            return QwenBackend(api_key=creds["api_key"], model_id=model_id, **default_params)
        case "vllm":
            base_url = creds.get("base_url", "http://localhost:8000/v1")
            return VllmBackend(
                api_key=creds.get("api_key", ""),
                model_id=model_id,
                base_url=base_url,
                **default_params,
            )
        case "watsonx":
            if "project_id" not in creds:
                raise ValueError(f"Missing 'project_id' in credentials for provider 'watsonx'. Got keys: {sorted(creds)}")
            return WatsonXBackend(
                api_key=creds["api_key"],
                model_id=model_id,
                project_id=creds["project_id"],
                url=creds.get("url", "https://us-south.ml.cloud.ibm.com"),
                **default_params,
            )
        case _:
            registry = get_plugin_registry()
            if registry.has_model_backend(provider):
                api_key = creds.get("api_key")
                if not api_key:
                    raise ValueError(
                        f"Missing 'api_key' in credentials for provider {provider!r}. Got keys: {sorted(creds)}"
                    )
                return registry.build_model_backend(provider, model_id, api_key, **default_params)
            raise ValueError(f"Unknown model backend provider: {provider!r}")
