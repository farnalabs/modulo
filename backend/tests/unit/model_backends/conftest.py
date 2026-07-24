from unittest.mock import patch

import pytest


def _mock_backend(module_name, chat_class, backend_class, **kwargs):
    with patch(f"modulo.model_backends.{module_name}.{chat_class}"):
        return backend_class(**kwargs)


@pytest.fixture
def ai21_backend():
    from modulo.model_backends.ai21 import Ai21Backend
    return _mock_backend("ai21", "ChatOpenAI", Ai21Backend, api_key="test-key", model_id="jamba-1.5-mini")


@pytest.fixture
def anthropic_backend():
    from modulo.model_backends.anthropic import AnthropicBackend
    return _mock_backend("anthropic", "ChatAnthropic", AnthropicBackend, api_key="sk-ant-test", model_id="claude-haiku-4-5")


@pytest.fixture
def azure_openai_backend():
    from modulo.model_backends.azure_openai import AzureOpenAIBackend
    return _mock_backend("azure_openai", "ChatOpenAI", AzureOpenAIBackend, api_key="test-key", model_id="gpt-4-deployment", azure_endpoint="https://my-resource.openai.azure.com")


@pytest.fixture
def bedrock_backend():
    from modulo.model_backends.bedrock import BedrockBackend
    return _mock_backend("bedrock", "ChatBedrock", BedrockBackend, aws_access_key_id="AKIA123", aws_secret_access_key="secret123", model_id="us.anthropic.claude-sonnet-4-5-v2", region="us-east-1")


@pytest.fixture
def cohere_backend():
    from modulo.model_backends.cohere import CohereBackend
    return _mock_backend("cohere", "ChatCohere", CohereBackend, api_key="test-key", model_id="command-r")


@pytest.fixture
def deepseek_backend():
    from modulo.model_backends.deepseek import DeepSeekBackend
    return _mock_backend("deepseek", "ChatOpenAI", DeepSeekBackend, api_key="sk-test", model_id="deepseek-chat")


@pytest.fixture
def fireworks_backend():
    from modulo.model_backends.fireworks import FireworksBackend
    return _mock_backend("fireworks", "ChatOpenAI", FireworksBackend, api_key="test-key", model_id="accounts/fireworks/models/llama-v3p1-8b")


@pytest.fixture
def gemini_backend():
    from modulo.model_backends.gemini import GeminiBackend
    return _mock_backend("gemini", "ChatGoogleGenerativeAI", GeminiBackend, api_key="test-key", model_id="gemini-2.0-flash")


@pytest.fixture
def grok_backend():
    from modulo.model_backends.grok import GrokBackend
    return _mock_backend("grok", "ChatOpenAI", GrokBackend, api_key="sk-test", model_id="grok-2")


@pytest.fixture
def groq_backend():
    from modulo.model_backends.groq import GroqBackend
    return _mock_backend("groq", "ChatOpenAI", GroqBackend, api_key="test-key", model_id="llama3-70b-8192")
