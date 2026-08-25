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
    """Abstract base for all model backend implementations."""

    supports_tools: bool = False

    @property
    @abstractmethod
    def backend_id(self) -> str:
        """Stable identifier for this backend (e.g.
        'anthropic/claude-sonnet-4-6')."""

    @abstractmethod
    async def invoke(
        self,
        messages: list[BaseMessage],
        **kwargs: Any,
    ) -> BaseMessage:
        """Send a messages list and return a single response."""

    @abstractmethod
    def stream(
        self,
        messages: list[BaseMessage],
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[BaseMessage]:
        """Return an async iterator that yields token chunks."""
```

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
| Custom | `YourBackend` | plugin `modulo.model_backends.your_backend` |

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
        # Call your provider's API and return a response
        ...
        return AIMessage(content=response_text)

    def stream(self, messages, tools=None, **kwargs):
        # Return an async iterator yielding BaseMessage chunks
        ...
        yield AIMessage(content=token)
```

## Health checks

`ModelBackendBase.health_check()` has a default that verifies connectivity
with a minimal ping `invoke(..., max_tokens=1)`. Override it when a cheaper
or more accurate probe (such as `openai_compatible_health_check`, which GETs
`{base_url}/models`) is available. `health_check()` can be invoked directly via
`ModelBackendHub.health_check(backend_id)`; it is not run automatically at load
time.

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

## Configuration

Model backends are configured via the ModelBackend entity in the database.
Sensitive fields (API keys) are encrypted with Fernet and never stored in
plaintext. The `ModelBackendHub` decrypts credentials at initialisation/load time; health checks do not re-decrypt.

## Registration

Via entry points in `pyproject.toml`:

```toml
[project.entry-points."modulo.model_backends"]
my_backend = "my_package.backend:MyCustomBackend"
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
registry.register_model_backend("custom", MyCustomBackend, manifest)
```

`register_model_backend(provider, builder, manifest)` always takes a
`PluginManifest`; pass the plugin's manifest object so it can track plugin
health and capabilities.
