import importlib
import typing

from modulo.model_backends.base import (
    HealthResult,
    ModelBackendBase,
    openai_compatible_health_check,
)

_LAZY_MODULES: dict[str, str] = {
    "Ai21Backend": "modulo.model_backends.ai21",
    "AnthropicBackend": "modulo.model_backends.anthropic",
    "AzureOpenAIBackend": "modulo.model_backends.azure_openai",
    "BedrockBackend": "modulo.model_backends.bedrock",
    "CohereBackend": "modulo.model_backends.cohere",
    "DeepSeekBackend": "modulo.model_backends.deepseek",
    "FireworksBackend": "modulo.model_backends.fireworks",
    "GeminiBackend": "modulo.model_backends.gemini",
    "GrokBackend": "modulo.model_backends.grok",
    "GroqBackend": "modulo.model_backends.groq",
    "JanBackend": "modulo.model_backends.jan",
    "LLamaCppBackend": "modulo.model_backends.llamacpp",
    "LmStudioBackend": "modulo.model_backends.lm_studio",
    "LocalAIBackend": "modulo.model_backends.localai",
    "MistralBackend": "modulo.model_backends.mistral",
    "OllamaBackend": "modulo.model_backends.ollama",
    "OpenAIBackend": "modulo.model_backends.openai",
    "OpenCodeBackend": "modulo.model_backends.opencode",
    "OpenRouterBackend": "modulo.model_backends.openrouter",
    "PerplexityBackend": "modulo.model_backends.perplexity",
    "QwenBackend": "modulo.model_backends.qwen",
    "StubModelBackend": "modulo.model_backends.stub",
    "TgiBackend": "modulo.model_backends.tgi",
    "TogetherAIBackend": "modulo.model_backends.togetherai",
    "VertexAIBackend": "modulo.model_backends.vertexai",
    "VllmBackend": "modulo.model_backends.vllm",
    "WatsonXBackend": "modulo.model_backends.watsonx",
}


def __getattr__(name: str) -> typing.Any:
    module_name = _LAZY_MODULES.get(name)
    if module_name is not None:
        module = importlib.import_module(module_name)
        return getattr(module, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return [*globals().keys(), *_LAZY_MODULES.keys()]


__all__ = [
    "HealthResult",
    "ModelBackendBase",
    "openai_compatible_health_check",
    *_LAZY_MODULES.keys(),
]
