"""Unit tests for VllmBackend adapter."""

from unittest.mock import patch

import pytest

from modulo.model_backends.vllm import DEFAULT_VLLM_BASE_URL, VllmBackend


@pytest.fixture()
def backend():
    with patch("modulo.model_backends.vllm.ChatOpenAI"):
        return VllmBackend(api_key=None, model_id="llama-3.1-8b-instruct")


def test_default_base_url(backend):
    assert backend.base_url == DEFAULT_VLLM_BASE_URL


def test_custom_base_url():
    with patch("modulo.model_backends.vllm.ChatOpenAI"):
        backend = VllmBackend(api_key=None, model_id="llama3", base_url="http://vllm:8000/v1")
    assert backend.base_url == "http://vllm:8000/v1"


def test_api_key_placeholder():
    with patch("modulo.model_backends.vllm.ChatOpenAI") as mock:
        VllmBackend(api_key=None, model_id="llama3")
    assert mock.call_args[1]["api_key"] == "vllm"


def test_api_key_empty_string_falls_back_to_placeholder():
    with patch("modulo.model_backends.vllm.ChatOpenAI") as mock:
        VllmBackend(api_key="", model_id="llama3")
    assert mock.call_args[1]["api_key"] == "vllm"


def test_explicit_api_key_passed_through():
    with patch("modulo.model_backends.vllm.ChatOpenAI") as mock:
        VllmBackend(api_key="sk-vllm-key", model_id="llama3")
    assert mock.call_args[1]["api_key"] == "sk-vllm-key"


def test_base_url_trailing_slash_stripped():
    with patch("modulo.model_backends.vllm.ChatOpenAI") as mock:
        VllmBackend(api_key=None, model_id="llama3", base_url="http://vllm:8000/v1/")
    assert mock.call_args[1]["base_url"] == "http://vllm:8000/v1"
