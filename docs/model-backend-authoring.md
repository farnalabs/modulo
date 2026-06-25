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

    @property
    @abstractmethod
    def backend_id(self) -> str:
        """Unique identifier (e.g. 'anthropic', 'openai/gpt-4')."""

    @abstractmethod
    async def invoke(self, messages: list[BaseMessage]) -> BaseMessage:
        """Send a messages list and return a single response."""

    @abstractmethod
    async def stream(
        self, messages: list[BaseMessage]
    ) -> AsyncIterator[str]:
        """Stream response tokens."""
```

## Supported providers

| Provider | Backend class | Package | Entry point |
|----------|--------------|---------|-------------|
| Anthropic | `AnthropicBackend` | Built-in | `modulo.model_backends.anthropic` |
| OpenAI | `OpenAIBackend` | Built-in | `modulo.model_backends.openai` |
| Azure OpenAI | `AzureOpenAIBackend` | Built-in | `modulo.model_backends.azure_openai` |
| Ollama | `OllamaBackend` | Built-in | `modulo.model_backends.ollama` |
| Custom | `YourBackend` | Plugin | `modulo.model_backends.your_backend` |

## Implementation example

```python
from modulo.model_backends.base import ModelBackendBase

class MyCustomBackend(ModelBackendBase):
    def __init__(self, api_key: str, model_id: str, **kwargs):
        self._api_key = api_key
        self._model_id = model_id
        # Initialise your client here

    @property
    def backend_id(self) -> str:
        return f"custom/{self._model_id}"

    async def invoke(self, messages):
        # Call your provider's API and return a response
        ...
        return AIMessage(content=response_text)

    async def stream(self, messages):
        async for token in self._provider.stream(messages):
            yield token
```

## Health checks

Every backend must implement `health_check()` which the `ModelBackendHub` calls:

```python
async def health_check(self) -> HealthResult:
    try:
        await self._provider.simple_ping()
        return HealthResult(ok=True)
    except Exception as e:
        return HealthResult(ok=False, detail=str(e))
```

## Configuration

Model backends are configured via the ModelBackend entity in the database.
Sensitive fields (API keys) are encrypted with Fernet and never stored in
plaintext. The `ModelBackendHub` decrypts credentials at health-check time.

## Registration

Via entry points in `pyproject.toml`:

```toml
[project.entry-points."modulo.model_backends"]
my_backend = "my_package.backend:MyCustomBackend"
```

Or programmatically:

```python
from modulo.core.plugin_registry import get_plugin_registry
registry = get_plugin_registry()
registry.register_model_backend("custom", MyCustomBackend)
```
