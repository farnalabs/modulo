"""Unit tests for JanBackend adapter."""

from unittest.mock import patch

import pytest

from modulo.model_backends.jan import DEFAULT_JAN_BASE_URL, JanBackend


@pytest.fixture()
def backend():
    with patch("modulo.model_backends.jan.ChatOpenAI"):
        return JanBackend(api_key=None, model_id="jan-model")


def test_default_base_url(backend):
    assert backend.base_url == DEFAULT_JAN_BASE_URL


def test_custom_base_url():
    with patch("modulo.model_backends.jan.ChatOpenAI"):
        backend = JanBackend(api_key=None, model_id="jan-model", base_url="http://jan:1337/v1")
    assert backend.base_url == "http://jan:1337/v1"


def test_api_key_placeholder():
    with patch("modulo.model_backends.jan.ChatOpenAI") as mock:
        JanBackend(api_key=None, model_id="jan-model")
    assert mock.call_args[1]["api_key"] == "jan"


def test_api_key_empty_string_falls_back_to_placeholder():
    with patch("modulo.model_backends.jan.ChatOpenAI") as mock:
        JanBackend(api_key="", model_id="jan-model")
    assert mock.call_args[1]["api_key"] == "jan"


def test_explicit_api_key_passed_through():
    with patch("modulo.model_backends.jan.ChatOpenAI") as mock:
        JanBackend(api_key="sk-custom", model_id="jan-model")
    assert mock.call_args[1]["api_key"] == "sk-custom"


def test_base_url_trailing_slash_stripped():
    with patch("modulo.model_backends.jan.ChatOpenAI") as mock:
        JanBackend(api_key=None, model_id="jan-model", base_url="http://jan:1337/v1/")
    assert mock.call_args[1]["base_url"] == "http://jan:1337/v1"
