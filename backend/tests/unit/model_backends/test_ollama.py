"""Unit tests for OllamaBackend adapter."""

from unittest.mock import patch

import pytest

from modulo.model_backends.ollama import DEFAULT_OLLAMA_BASE_URL, OllamaBackend


@pytest.fixture()
def backend():
    with patch("modulo.model_backends.ollama.ChatOpenAI"):
        return OllamaBackend(api_key=None, model_id="llama3")


def test_default_base_url(backend):
    assert backend.base_url == DEFAULT_OLLAMA_BASE_URL


def test_custom_base_url():
    with patch("modulo.model_backends.ollama.ChatOpenAI"):
        backend = OllamaBackend(api_key=None, model_id="llama3", base_url="http://ollama:11434/v1")
    assert backend.base_url == "http://ollama:11434/v1"


def test_api_key_placeholder():
    with patch("modulo.model_backends.ollama.ChatOpenAI") as mock:
        OllamaBackend(api_key=None, model_id="llama3")
    assert mock.call_args[1]["api_key"] == "ollama"


def test_api_key_empty_string_falls_back_to_placeholder():
    with patch("modulo.model_backends.ollama.ChatOpenAI") as mock:
        OllamaBackend(api_key="", model_id="llama3")
    assert mock.call_args[1]["api_key"] == "ollama"


def test_explicit_api_key_passed_through():
    with patch("modulo.model_backends.ollama.ChatOpenAI") as mock:
        OllamaBackend(api_key="sk-custom", model_id="llama3")
    assert mock.call_args[1]["api_key"] == "sk-custom"


def test_base_url_trailing_slash_stripped():
    with patch("modulo.model_backends.ollama.ChatOpenAI") as mock:
        OllamaBackend(api_key=None, model_id="llama3", base_url="http://ollama:11434/v1/")
    assert mock.call_args[1]["base_url"] == "http://ollama:11434/v1"
