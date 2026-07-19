"""Unit tests for LLamaCppBackend adapter."""

from unittest.mock import patch

import pytest

from modulo.model_backends.llamacpp import DEFAULT_LLAMACPP_BASE_URL, LLamaCppBackend


@pytest.fixture()
def backend():
    with patch("modulo.model_backends.llamacpp.ChatOpenAI"):
        return LLamaCppBackend(api_key=None, model_id="llama-model")


def test_default_base_url(backend):
    assert backend.base_url == DEFAULT_LLAMACPP_BASE_URL


def test_custom_base_url():
    with patch("modulo.model_backends.llamacpp.ChatOpenAI"):
        backend = LLamaCppBackend(api_key=None, model_id="llama-model", base_url="http://llamacpp:8080/v1")
    assert backend.base_url == "http://llamacpp:8080/v1"


def test_api_key_placeholder():
    with patch("modulo.model_backends.llamacpp.ChatOpenAI") as mock:
        LLamaCppBackend(api_key=None, model_id="llama-model")
    assert mock.call_args[1]["api_key"] == "llamacpp"


def test_api_key_empty_string_falls_back_to_placeholder():
    with patch("modulo.model_backends.llamacpp.ChatOpenAI") as mock:
        LLamaCppBackend(api_key="", model_id="llama-model")
    assert mock.call_args[1]["api_key"] == "llamacpp"


def test_explicit_api_key_passed_through():
    with patch("modulo.model_backends.llamacpp.ChatOpenAI") as mock:
        LLamaCppBackend(api_key="sk-custom", model_id="llama-model")
    assert mock.call_args[1]["api_key"] == "sk-custom"


def test_base_url_trailing_slash_stripped():
    with patch("modulo.model_backends.llamacpp.ChatOpenAI") as mock:
        LLamaCppBackend(api_key=None, model_id="llama-model", base_url="http://llamacpp:8080/v1/")
    assert mock.call_args[1]["base_url"] == "http://llamacpp:8080/v1"
