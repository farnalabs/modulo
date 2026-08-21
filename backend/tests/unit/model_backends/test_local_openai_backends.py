"""Unit tests for the local OpenAI-compatible backend adapters.

Covers jan, llamacpp, lm_studio, localai, ollama, tgi, and vllm. These
backends share the OpenAICompatibleBackend behaviour — a default local
base URL, an api_key that falls back to the provider name, and trailing
slash stripping — so the boilerplate is collapsed into a single
parametrised matrix instead of seven near-identical files.
"""

from unittest.mock import patch

import pytest

from modulo.model_backends.jan import DEFAULT_JAN_BASE_URL, JanBackend
from modulo.model_backends.llamacpp import DEFAULT_LLAMACPP_BASE_URL, LLamaCppBackend
from modulo.model_backends.lm_studio import DEFAULT_LM_STUDIO_BASE_URL, LmStudioBackend
from modulo.model_backends.localai import DEFAULT_LOCALAI_BASE_URL, LocalAIBackend
from modulo.model_backends.ollama import DEFAULT_OLLAMA_BASE_URL, OllamaBackend
from modulo.model_backends.tgi import DEFAULT_TGI_BASE_URL, TgiBackend
from modulo.model_backends.vllm import DEFAULT_VLLM_BASE_URL, VllmBackend


def _build(backend_cls, model_id, **kwargs):
    with patch("modulo.model_backends.module.ChatOpenAI"):
        return backend_cls(api_key=None, model_id=model_id, **kwargs)


_LOCAL_BACKENDS = [
    pytest.param(
        JanBackend,
        DEFAULT_JAN_BASE_URL,
        "http://jan:1337/v1",
        "jan",
        "jan-model",
        "sk-custom",
        id="jan",
    ),
    pytest.param(
        LLamaCppBackend,
        DEFAULT_LLAMACPP_BASE_URL,
        "http://llamacpp:8080/v1",
        "llamacpp",
        "llama-model",
        "sk-custom",
        id="llamacpp",
    ),
    pytest.param(
        LmStudioBackend,
        DEFAULT_LM_STUDIO_BASE_URL,
        "http://lm-studio:1234/v1",
        "lm_studio",
        "lm-studio-model",
        "sk-custom",
        id="lm_studio",
    ),
    pytest.param(
        LocalAIBackend,
        DEFAULT_LOCALAI_BASE_URL,
        "http://localai:8080/v1",
        "localai",
        "gpt-4",
        "sk-custom",
        id="localai",
    ),
    pytest.param(
        OllamaBackend,
        DEFAULT_OLLAMA_BASE_URL,
        "http://ollama:11434/v1",
        "ollama",
        "llama3",
        "sk-custom",
        id="ollama",
    ),
    pytest.param(
        TgiBackend,
        DEFAULT_TGI_BASE_URL,
        "http://tgi:8080/v1",
        "tgi",
        "mistral",
        "sk-custom",
        id="tgi",
    ),
    pytest.param(
        VllmBackend,
        DEFAULT_VLLM_BASE_URL,
        "http://vllm:8000/v1",
        "vllm",
        "llama3",
        "sk-vllm-key",
        id="vllm",
    ),
]


@pytest.mark.parametrize(
    ("backend_cls", "default_base_url", "custom_base_url", "provider", "model_id", "explicit_key"),
    _LOCAL_BACKENDS,
)
class TestLocalOpenAICompatibleBackends:
    def test_default_base_url(
        self,
        backend_cls,
        default_base_url,
        custom_base_url,
        provider,
        model_id,
        explicit_key,
    ):
        backend = _build(backend_cls, model_id)
        assert backend.base_url == default_base_url

    def test_custom_base_url(
        self,
        backend_cls,
        default_base_url,
        custom_base_url,
        provider,
        model_id,
        explicit_key,
    ):
        backend = _build(backend_cls, model_id, base_url=custom_base_url)
        assert backend.base_url == custom_base_url

    def test_api_key_placeholder(
        self,
        backend_cls,
        default_base_url,
        custom_base_url,
        provider,
        model_id,
        explicit_key,
    ):
        with patch("modulo.model_backends.module.ChatOpenAI") as mock:
            backend_cls(api_key=None, model_id=model_id)
        assert mock.call_args[1]["api_key"] == provider

    def test_api_key_empty_string_falls_back_to_placeholder(
        self,
        backend_cls,
        default_base_url,
        custom_base_url,
        provider,
        model_id,
        explicit_key,
    ):
        with patch("modulo.model_backends.module.ChatOpenAI") as mock:
            backend_cls(api_key="", model_id=model_id)
        assert mock.call_args[1]["api_key"] == provider

    def test_explicit_api_key_passed_through(
        self,
        backend_cls,
        default_base_url,
        custom_base_url,
        provider,
        model_id,
        explicit_key,
    ):
        with patch("modulo.model_backends.module.ChatOpenAI") as mock:
            backend_cls(api_key=explicit_key, model_id=model_id)
        assert mock.call_args[1]["api_key"] == explicit_key

    def test_base_url_trailing_slash_stripped(
        self,
        backend_cls,
        default_base_url,
        custom_base_url,
        provider,
        model_id,
        explicit_key,
    ):
        with patch("modulo.model_backends.module.ChatOpenAI") as mock:
            backend_cls(api_key=None, model_id=model_id, base_url=f"{custom_base_url}/")
        assert mock.call_args[1]["base_url"] == custom_base_url
