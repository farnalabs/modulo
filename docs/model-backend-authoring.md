# Model Backend Authoring Guide

Model backends wrap LLM providers behind Modulo's `ModelBackendBase` ABC.

## Architecture

```
ModelBackendBase (ABC)      ← modulo/model_backends/base.py
  ├── AnthropicBackend      ← modulo/model_backends/anthropic/
  ├── OpenAIBackend         ← modulo/model_backends/openai/
  ├── OllamaBackend         ← modulo/model_backends/ollama/
  └── YourBackend           ← your package
```

## ModelBackendBase interface

```python
class ModelBackendBase(ABC):
    """Abstract base for all model backends (real + stub)."""

    supports_tools: bool = False
    supports_native_structured_output: bool = False

    @property
    @abstractmethod
    def backend_id(self) -> str:
        """Stable identifier for this backend (e.g.
        'anthropic/claude-sonnet-4-6')."""

    @abstractmethod
    async def invoke(
        self,
        messages: list[BaseMessage],
        output_schema: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> BaseMessage:
        """Send a messages list and return a single response.

        When `output_schema` is supplied and `supports_native_structured_output`
        is True, the backend uses the provider's native structured-output
        decoding (e.g. OpenAI `response_format`); otherwise the schema is
        ignored and behaviour is unchanged."""

    @abstractmethod
    def stream(
        self,
        messages: list[BaseMessage],
        tools: list[dict[str, Any]] | None = None,
        output_schema: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[BaseMessage]:
        """Return an async iterator that yields token chunks."""
```

`supports_native_structured_output` is a per-instance flag, and the hub can
force it off: for the providers in `_NO_NATIVE_STRUCTURED_OUTPUT_PROVIDERS`
(`ollama`, `llamacpp`, `localai`, `tgi`, `vllm`) `_build_backend` sets it to
`False` after construction, because those servers either reject or silently
strip OpenAI-style `response_format` JSON schema (FAR-898). A backend in that
set therefore cannot opt in from its class body.

## Supported providers

The following backends ship built-in (each in `modulo.model_backends`):
Ai21, Anthropic, Azure OpenAI, Bedrock, Cohere, DeepSeek, Fireworks, Gemini,
Grok, Groq, Jan, LlamaCpp, LM Studio, LocalAI, Mistral, Ollama, OpenAI,
OpenCode, OpenRouter, Perplexity, Qwen, Stub, TGI, TogetherAI, Vertex AI,
vLLM, and WatsonX. Most share one OpenAI-compatible implementation
(`module.py`); unique providers like Anthropic, Gemini, or Bedrock implement
their own.

| Provider | Backend class | Package / entry point |
|----------|--------------|------------------------|
| Anthropic | `AnthropicBackend` | `modulo.model_backends.anthropic` |
| OpenAI | `OpenAIBackend` | `modulo.model_backends.openai` |
| Azure OpenAI | `AzureOpenAIBackend` | `modulo.model_backends.azure_openai` |
| Ollama | `OllamaBackend` | `modulo.model_backends.ollama` |
| Custom plugin | `YourBackend` | plugin group `modulo.model_backends.your_backend` |

> The per-provider classes in this table are the authoring surface, but most are
> not the objects the hub instantiates: `ModelBackendHub._build_backend`
> constructs `module.OpenAICompatibleBackend` directly for the OpenAI-compatible
> providers, special-casing only a few. Implement the documented ABC contract
> rather than assuming the hub calls your class.

## Implementation example

```python
from modulo.model_backends.base import ModelBackendBase


class MyCustomBackend(ModelBackendBase):
    def __init__(self, api_key: str, model_id: str, **kwargs):
        self._api_key = api_key
        self._model_id = model_id
        # Initialise your client here

    supports_tools: bool = True

    @property
    def backend_id(self) -> str:
        return f"custom/{self._model_id}"

    async def invoke(self, messages, **kwargs):
        # Call your provider's API and return a BaseMessage
        ...
        return BaseMessage(content=response_text)

    def stream(self, messages, tools=None, **kwargs):
        # Return an async iterator yielding BaseMessage chunks
        ...
        yield BaseMessage(content=token)
```

## Health checks

`ModelBackendBase.health_check()` has a default that verifies connectivity
with a minimal ping `invoke(..., max_tokens=1)`. Override it when a cheaper
or more accurate probe (such as `openai_compatible_health_check`, which GETs
`{base_url}/models`) is available. `health_check()` can be invoked directly via
`ModelBackendHub.health_check(backend_id)` where `backend_id` is a `uuid.UUID`
(the DB primary key, distinct from the `backend_id` string property on the
backend class); it is not run automatically at load time.

```python
from langchain_core.messages import HumanMessage

from modulo.model_backends.base import HealthResult


async def health_check(self) -> HealthResult:
    try:
        await self.invoke([HumanMessage(content="ping")], max_tokens=1)
        return HealthResult(ok=True, detail="")
    except Exception as exc:  # noqa: BLE001
        return HealthResult(ok=False, detail=str(exc))
```

## Gateway-outage classification

`ModelBackendBase` carries one error-classification contract that a concrete
backend's `invoke()` / `stream()` should route SDK failures through. It exists
because the run error mapper derives a run's `error_code` from the *exception
type name*, so an unclassified upstream 5xx is indistinguishable from a bad API
key:

```python
class ModelBackendBase(ABC):
    _status_error_types: ClassVar[tuple[type[Exception], ...]] = ()

    def _gateway_error_context(self) -> str:
        """Provider-specific context appended to an outage message."""

    def _classify_gateway_error(self, exc: Exception) -> Exception:
        """HTTP 4xx/429 pass through; 5xx and connection failures become
        ProviderUnavailableError."""
```

Set `_status_error_types` to your SDK's status-error class (for example
`AnthropicBackend` declares `(AnthropicStatusError,)`) and wrap the provider
call:

```python
try:
    response = await self._client.messages.create(...)
except Exception as exc:  # noqa: BLE001
    raise self._classify_gateway_error(exc) from exc
```

`HTTP 4xx` and `429` are re-raised unchanged because they are actionable as-is;
`5xx` and connection failures become `ProviderUnavailableError`, which keeps a
gateway outage distinct from a genuinely bad credential. Override
`_gateway_error_context()` to append the endpoint host to the message.

## Custom endpoint (`base_url`)

An operator-supplied endpoint lives in the credential dict
(`creds["base_url"]`), never in a model parameter (FAR-2046).
`core/model_backend_hub/` exports `supports_custom_base_url(provider)` as the
single source of truth for which providers honour one, and the API rejects a
`base_url` for any other provider with
`base_url_not_supported_for_provider` instead of storing a value it would
ignore.

Where the value lands depends on the provider:

- The OpenAI-compatible path forwards it as the client `base_url`.
- The adapters that expose it under a provider-specific constructor argument
  are `anthropic` (`anthropic_api_url`), `cohere` and `gemini` (`base_url`) and
  `mistral` (`endpoint`); `_CUSTOM_ENDPOINT_KWARGS` is the only place that
  mapping lives.
- Providers with their own endpoint semantics (`azure_openai`, `bedrock`,
  `vertexai`, `watsonx`) cannot consume it. A stored value for one of them is
  logged and dropped, never applied to the wrong host; a missing
  `_CUSTOM_ENDPOINT_KWARGS` entry for a new adapter fails loudly with
  `KeyError`.

A `base_url` left in `default_params` is the pre-FAR-2046 location and is still
honoured, but it is popped from the splat before construction so it can never
collide with an adapter's explicit endpoint argument.

## Configuration

Model backends are configured via the ModelBackend entity in the database.
Sensitive fields (API keys) are encrypted with Fernet and never stored in
plaintext. The `ModelBackendHub` decrypts credentials at initialisation/load time; health checks do not re-decrypt.

## Registration

Via entry points in `pyproject.toml` (the entry point must reference a
**builder function**, not the class directly; see [Plugin API](./plugin-api.md)):

```toml
[project.entry-points."modulo.model_backends"]
my_backend = "my_package.backend:build_my_backend"
```

```python
# my_package/backend.py
from modulo.model_backends.base import ModelBackendBase


def build_my_backend(api_key: str, model_id: str, **params) -> ModelBackendBase:
    return MyCustomBackend(api_key=api_key, model_id=model_id, **params)
```

Or programmatically:

```python
from modulo.core.plugin_registry import PluginManifest, get_plugin_registry
from my_package.backend import MyCustomBackend

manifest = PluginManifest(
    PLUGIN_ID="my_backend",
    display_name="My Backend",
    description="Custom model backend",
    version="0.1.0",
)

registry = get_plugin_registry()
registry.register_model_backend("my-provider", build_my_backend, manifest)
```

> **Pick a provider id other than `"custom"`.** `ModelBackendHub._build_backend`
> intercepts `provider == "custom"` before it ever consults the plugin registry
> and returns a stub backend, so a plugin registered under `"custom"` is dead
> code at run time. Any other id reaches the plugin fallback and the registry
> lookup.

`register_model_backend(provider, builder, manifest)` always takes a
`PluginManifest`; pass the plugin's manifest object so it can track plugin
health and capabilities.
