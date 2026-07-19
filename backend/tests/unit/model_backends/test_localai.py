"""Unit tests for LocalAIBackend adapter."""

from unittest.mock import patch

import pytest

from modulo.model_backends.localai import DEFAULT_LOCALAI_BASE_URL, LocalAIBackend


@pytest.fixture()
def backend():
    with patch("modulo.model_backends.localai.ChatOpenAI"):
        return LocalAIBackend(api_key=None, model_id="gpt-4")


def test_default_base_url(backend):
    assert backend.base_url == DEFAULT_LOCALAI_BASE_URL


def test_custom_base_url():
    with patch("modulo.model_backends.localai.ChatOpenAI"):
        backend = LocalAIBackend(api_key=None, model_id="gpt-4", base_url="http://localai:8080/v1")
    assert backend.base_url == "http://localai:8080/v1"


def test_api_key_placeholder():
    with patch("modulo.model_backends.localai.ChatOpenAI") as mock:
        LocalAIBackend(api_key=None, model_id="gpt-4")
    assert mock.call_args[1]["api_key"] == "localai"


def test_api_key_empty_string_falls_back_to_placeholder():
    with patch("modulo.model_backends.localai.ChatOpenAI") as mock:
        LocalAIBackend(api_key="", model_id="gpt-4")
    assert mock.call_args[1]["api_key"] == "localai"


def test_explicit_api_key_passed_through():
    with patch("modulo.model_backends.localai.ChatOpenAI") as mock:
        LocalAIBackend(api_key="sk-custom", model_id="gpt-4")
    assert mock.call_args[1]["api_key"] == "sk-custom"


def test_base_url_trailing_slash_stripped():
    with patch("modulo.model_backends.localai.ChatOpenAI") as mock:
        LocalAIBackend(api_key=None, model_id="gpt-4", base_url="http://localai:8080/v1/")
    assert mock.call_args[1]["base_url"] == "http://localai:8080/v1"
