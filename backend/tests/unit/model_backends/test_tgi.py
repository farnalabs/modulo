"""Unit tests for TgiBackend adapter."""

from unittest.mock import patch

import pytest

from modulo.model_backends.tgi import DEFAULT_TGI_BASE_URL, TgiBackend


@pytest.fixture()
def backend():
    with patch("modulo.model_backends.tgi.ChatOpenAI"):
        return TgiBackend(api_key=None, model_id="mistral")


def test_default_base_url(backend):
    assert backend.base_url == DEFAULT_TGI_BASE_URL


def test_custom_base_url():
    with patch("modulo.model_backends.tgi.ChatOpenAI"):
        backend = TgiBackend(api_key=None, model_id="mistral", base_url="http://tgi:8080/v1")
    assert backend.base_url == "http://tgi:8080/v1"


def test_api_key_placeholder():
    with patch("modulo.model_backends.tgi.ChatOpenAI") as mock:
        TgiBackend(api_key=None, model_id="mistral")
    assert mock.call_args[1]["api_key"] == "tgi"


def test_api_key_empty_string_falls_back_to_placeholder():
    with patch("modulo.model_backends.tgi.ChatOpenAI") as mock:
        TgiBackend(api_key="", model_id="mistral")
    assert mock.call_args[1]["api_key"] == "tgi"


def test_explicit_api_key_passed_through():
    with patch("modulo.model_backends.tgi.ChatOpenAI") as mock:
        TgiBackend(api_key="sk-custom", model_id="mistral")
    assert mock.call_args[1]["api_key"] == "sk-custom"


def test_base_url_trailing_slash_stripped():
    with patch("modulo.model_backends.tgi.ChatOpenAI") as mock:
        TgiBackend(api_key=None, model_id="mistral", base_url="http://tgi:8080/v1/")
    assert mock.call_args[1]["base_url"] == "http://tgi:8080/v1"
