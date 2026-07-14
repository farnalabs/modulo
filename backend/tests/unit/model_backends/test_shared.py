"""Parametrized tests shared across all model backends.

These replace the boilerplate tests that were previously duplicated
in every individual backend test file.
"""

from unittest.mock import AsyncMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from modulo.model_backends.base import ModelBackendBase


def _build(module_path, class_name, patch_target, kwargs):
    with patch(patch_target):
        import importlib

        mod = importlib.import_module(module_path)
        cls = getattr(mod, class_name)
        return cls(**kwargs)


BACKENDS = [
    pytest.param(
        "modulo.model_backends.ollama",
        "OllamaBackend",
        "modulo.model_backends.ollama.ChatOpenAI",
        {"api_key": None, "model_id": "llama3"},
        "ollama/llama3",
        id="ollama",
    ),
    pytest.param(
        "modulo.model_backends.vllm",
        "VllmBackend",
        "modulo.model_backends.vllm.ChatOpenAI",
        {"api_key": None, "model_id": "vllm-model"},
        "vllm/vllm-model",
        id="vllm",
    ),
    pytest.param(
        "modulo.model_backends.llamacpp",
        "LLamaCppBackend",
        "modulo.model_backends.llamacpp.ChatOpenAI",
        {"api_key": None, "model_id": "llama-model"},
        "llamacpp/llama-model",
        id="llamacpp",
    ),
    pytest.param(
        "modulo.model_backends.jan",
        "JanBackend",
        "modulo.model_backends.jan.ChatOpenAI",
        {"api_key": None, "model_id": "jan-model"},
        "jan/jan-model",
        id="jan",
    ),
    pytest.param(
        "modulo.model_backends.lm_studio",
        "LmStudioBackend",
        "modulo.model_backends.lm_studio.ChatOpenAI",
        {"api_key": None, "model_id": "lm-studio-model"},
        "lm_studio/lm-studio-model",
        id="lm_studio",
    ),
    pytest.param(
        "modulo.model_backends.localai",
        "LocalAIBackend",
        "modulo.model_backends.localai.ChatOpenAI",
        {"api_key": None, "model_id": "localai-model"},
        "localai/localai-model",
        id="localai",
    ),
    pytest.param(
        "modulo.model_backends.tgi",
        "TgiBackend",
        "modulo.model_backends.tgi.ChatOpenAI",
        {"api_key": None, "model_id": "tgi-model"},
        "tgi/tgi-model",
        id="tgi",
    ),
    pytest.param(
        "modulo.model_backends.openai",
        "OpenAIBackend",
        "modulo.model_backends.openai.ChatOpenAI",
        {"api_key": "sk-test", "model_id": "gpt-4o"},
        "openai/gpt-4o",
        id="openai",
    ),
    pytest.param(
        "modulo.model_backends.anthropic",
        "AnthropicBackend",
        "modulo.model_backends.anthropic.ChatAnthropic",
        {"api_key": "sk-ant-test", "model_id": "claude-haiku-4-5"},
        "anthropic/claude-haiku-4-5",
        id="anthropic",
    ),
    pytest.param(
        "modulo.model_backends.gemini",
        "GeminiBackend",
        "modulo.model_backends.gemini.ChatGoogleGenerativeAI",
        {"api_key": "test-key", "model_id": "gemini-2.0-flash"},
        "gemini/gemini-2.0-flash",
        id="gemini",
    ),
    pytest.param(
        "modulo.model_backends.cohere",
        "CohereBackend",
        "modulo.model_backends.cohere.ChatCohere",
        {"api_key": "test-key", "model_id": "command-r"},
        "cohere/command-r",
        id="cohere",
    ),
    pytest.param(
        "modulo.model_backends.mistral",
        "MistralBackend",
        "modulo.model_backends.mistral.ChatMistralAI",
        {"api_key": "sk-test", "model_id": "mistral-large"},
        "mistral/mistral-large",
        id="mistral",
    ),
    pytest.param(
        "modulo.model_backends.deepseek",
        "DeepSeekBackend",
        "modulo.model_backends.deepseek.ChatOpenAI",
        {"api_key": "sk-test", "model_id": "deepseek-chat"},
        "deepseek/deepseek-chat",
        id="deepseek",
    ),
    pytest.param(
        "modulo.model_backends.grok",
        "GrokBackend",
        "modulo.model_backends.grok.ChatOpenAI",
        {"api_key": "sk-test", "model_id": "grok-2"},
        "grok/grok-2",
        id="grok",
    ),
    pytest.param(
        "modulo.model_backends.groq",
        "GroqBackend",
        "modulo.model_backends.groq.ChatOpenAI",
        {"api_key": "sk-test", "model_id": "llama-3.1-70b"},
        "groq/llama-3.1-70b",
        id="groq",
    ),
    pytest.param(
        "modulo.model_backends.qwen",
        "QwenBackend",
        "modulo.model_backends.qwen.ChatOpenAI",
        {"api_key": "sk-test", "model_id": "qwen-max"},
        "qwen/qwen-max",
        id="qwen",
    ),
    pytest.param(
        "modulo.model_backends.togetherai",
        "TogetherAIBackend",
        "modulo.model_backends.togetherai.ChatOpenAI",
        {"api_key": "sk-test", "model_id": "mistral-7b"},
        "togetherai/mistral-7b",
        id="togetherai",
    ),
    pytest.param(
        "modulo.model_backends.openrouter",
        "OpenRouterBackend",
        "modulo.model_backends.openrouter.ChatOpenAI",
        {"api_key": "sk-test", "model_id": "gpt-4o"},
        "openrouter/gpt-4o",
        id="openrouter",
    ),
    pytest.param(
        "modulo.model_backends.perplexity",
        "PerplexityBackend",
        "modulo.model_backends.perplexity.ChatOpenAI",
        {"api_key": "sk-test", "model_id": "llama-3-sonar"},
        "perplexity/llama-3-sonar",
        id="perplexity",
    ),
    pytest.param(
        "modulo.model_backends.fireworks",
        "FireworksBackend",
        "modulo.model_backends.fireworks.ChatOpenAI",
        {"api_key": "sk-test", "model_id": "llama-v3"},
        "fireworks/llama-v3",
        id="fireworks",
    ),
    pytest.param(
        "modulo.model_backends.ai21",
        "Ai21Backend",
        "modulo.model_backends.ai21.ChatOpenAI",
        {"api_key": "sk-test", "model_id": "jamba-1.5"},
        "ai21/jamba-1.5",
        id="ai21",
    ),
    pytest.param(
        "modulo.model_backends.opencode",
        "OpenCodeBackend",
        "modulo.model_backends.opencode.ChatOpenAI",
        {"api_key": "sk-test", "model_id": "deepseek-chat"},
        "opencode/deepseek-chat",
        id="opencode",
    ),
    pytest.param(
        "modulo.model_backends.bedrock",
        "BedrockBackend",
        "modulo.model_backends.bedrock.ChatBedrock",
        {
            "aws_access_key_id": "AKIA123",
            "aws_secret_access_key": "secret123",
            "model_id": "us.anthropic.claude-sonnet-4-5-v2",
            "region": "us-east-1",
        },
        "bedrock/us.anthropic.claude-sonnet-4-5-v2",
        id="bedrock",
    ),
    pytest.param(
        "modulo.model_backends.vertexai",
        "VertexAIBackend",
        "modulo.model_backends.vertexai.ChatVertexAI",
        {"project": "my-project", "model_id": "gemini-2.0-flash-001"},
        "vertexai/gemini-2.0-flash-001",
        id="vertexai",
    ),
    pytest.param(
        "modulo.model_backends.watsonx",
        "WatsonXBackend",
        "modulo.model_backends.watsonx.ChatWatsonx",
        {"api_key": "test-api-key", "model_id": "meta-llama/llama-3-70b-instruct", "project_id": "test-project-id"},
        "watsonx/meta-llama/llama-3-70b-instruct",
        id="watsonx",
    ),
    pytest.param(
        "modulo.model_backends.azure_openai",
        "AzureOpenAIBackend",
        "modulo.model_backends.azure_openai.ChatOpenAI",
        {
            "api_key": "test-key",
            "model_id": "gpt-4-deployment",
            "azure_endpoint": "https://my-resource.openai.azure.com",
        },
        "azure_openai/gpt-4-deployment",
        id="azure_openai",
    ),
]


def _unpack_params(params):
    # Convert list of pytest.param objects to a list of tuples for parametrization
    result = []
    for p in params:
        if isinstance(p, pytest.mark.structures.ParameterSet):
            result.append(p)
        else:
            result.append(pytest.param(*p, id=p[4]))
    return result


class TestSharedBackendContracts:
    @pytest.mark.parametrize(
        "module_path,class_name,patch_target,kwargs,expected_id",
        BACKENDS,
    )
    def test_is_model_backend_base(self, module_path, class_name, patch_target, kwargs, expected_id):
        backend = _build(module_path, class_name, patch_target, kwargs)
        assert isinstance(backend, ModelBackendBase)

    @pytest.mark.parametrize(
        "module_path,class_name,patch_target,kwargs,expected_id",
        BACKENDS,
    )
    def test_backend_id(self, module_path, class_name, patch_target, kwargs, expected_id):
        backend = _build(module_path, class_name, patch_target, kwargs)
        assert backend.backend_id == expected_id

    @pytest.mark.parametrize(
        "module_path,class_name,patch_target,kwargs,expected_id",
        BACKENDS,
    )
    async def test_invoke_delegates_to_langchain(
        self,
        module_path,
        class_name,
        patch_target,
        kwargs,
        expected_id,
    ):
        backend = _build(module_path, class_name, patch_target, kwargs)
        reply = AIMessage(content=f"Hello from {class_name}")
        backend._model.ainvoke = AsyncMock(return_value=reply)
        messages = [HumanMessage(content="hi")]
        result = await backend.invoke(messages)
        assert result.content == f"Hello from {class_name}"
        backend._model.ainvoke.assert_called_once_with(messages)

    @pytest.mark.parametrize(
        "module_path,class_name,patch_target,kwargs,expected_id",
        BACKENDS,
    )
    async def test_stream_yields_chunks(
        self,
        module_path,
        class_name,
        patch_target,
        kwargs,
        expected_id,
    ):
        backend = _build(module_path, class_name, patch_target, kwargs)
        chunk1 = AIMessage(content="chunk1")
        chunk2 = AIMessage(content="chunk2")

        async def _astream(*args, **kwargs):
            for c in [chunk1, chunk2]:
                yield c

        backend._model.astream = _astream
        chunks = []
        async for chunk in backend.stream([HumanMessage(content="hi")]):
            chunks.append(chunk)
        assert [c.content for c in chunks] == ["chunk1", "chunk2"]

    @pytest.mark.parametrize(
        "module_path,class_name,patch_target,kwargs,expected_id",
        BACKENDS,
    )
    def test_repr_content(self, module_path, class_name, patch_target, kwargs, expected_id):
        backend = _build(module_path, class_name, patch_target, kwargs)
        r = repr(backend)
        assert class_name in r
        assert expected_id in r

    @pytest.mark.parametrize(
        "module_path,class_name,patch_target,kwargs,expected_id",
        BACKENDS,
    )
    def test_repr_does_not_leak_api_key(
        self,
        module_path,
        class_name,
        patch_target,
        kwargs,
        expected_id,
    ):
        backend = _build(module_path, class_name, patch_target, kwargs)
        assert "sk-" not in repr(backend)

    @pytest.mark.parametrize(
        "module_path,class_name,patch_target,kwargs,expected_id",
        BACKENDS,
    )
    async def test_invoke_passes_kwargs(
        self,
        module_path,
        class_name,
        patch_target,
        kwargs,
        expected_id,
    ):
        backend = _build(module_path, class_name, patch_target, kwargs)
        reply = AIMessage(content="Hello")
        mock_invoke = AsyncMock(return_value=reply)
        backend._model.ainvoke = mock_invoke
        messages = [HumanMessage(content="hi")]
        await backend.invoke(messages, max_tokens=500, temperature=0.7)
        mock_invoke.assert_called_once_with(messages, max_tokens=500, temperature=0.7)
