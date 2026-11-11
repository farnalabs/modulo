import importlib
import typing

from farna.model_backends.base import (
    HealthResult,
    ModelBackendBase,
    openai_compatible_health_check,
)

_LAZY_MODULES: dict[str, str] = {
    "Ai21Backend": "farna.model_backends.ai21",
    "AnthropicBackend": "farna.model_backends.anthropic",
    "AzureOpenAIBackend": "farna.model_backends.azure_openai",
    "BedrockBackend": "farna.model_backends.bedrock",
    "CohereBackend": "farna.model_backends.cohere",
    "DeepSeekBackend": "farna.model_backends.deepseek",
    "FireworksBackend": "farna.model_backends.fireworks",
    "GeminiBackend": "farna.model_backends.gemini",
    "GrokBackend": "farna.model_backends.grok",
    "GroqBackend": "farna.model_backends.groq",
    "JanBackend": "farna.model_backends.jan",
    "LLamaCppBackend": "farna.model_backends.llamacpp",
    "LmStudioBackend": "farna.model_backends.lm_studio",
    "LocalAIBackend": "farna.model_backends.localai",
    "MistralBackend": "farna.model_backends.mistral",
    "OllamaBackend": "farna.model_backends.ollama",
    "OpenAIBackend": "farna.model_backends.openai",
    "OpenCodeBackend": "farna.model_backends.opencode",
    "OpenRouterBackend": "farna.model_backends.openrouter",
    "PerplexityBackend": "farna.model_backends.perplexity",
    "QwenBackend": "farna.model_backends.qwen",
    "StubModelBackend": "farna.model_backends.stub",
    "TgiBackend": "farna.model_backends.tgi",
    "TogetherAIBackend": "farna.model_backends.togetherai",
    "VertexAIBackend": "farna.model_backends.vertexai",
    "VllmBackend": "farna.model_backends.vllm",
    "WatsonXBackend": "farna.model_backends.watsonx",
}


def __getattr__(name: str) -> typing.Any:
    module_name = _LAZY_MODULES.get(name)
    if module_name is not None:
        module = importlib.import_module(module_name)
        return getattr(module, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return [*globals().keys(), *_LAZY_MODULES.keys()]


_LAZY_BACKEND_NAMES = list(_LAZY_MODULES)

__all__ = [
    "HealthResult",
    "ModelBackendBase",
    "openai_compatible_health_check",
    *_LAZY_BACKEND_NAMES,
]
