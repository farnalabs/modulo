"""Unit tests for LmStudioBackend adapter."""

from unittest.mock import patch

import pytest

from modulo.model_backends.lm_studio import DEFAULT_LM_STUDIO_BASE_URL, LmStudioBackend


@pytest.fixture()
def backend():
    with patch("modulo.model_backends.lm_studio.ChatOpenAI"):
        return LmStudioBackend(api_key=None, model_id="lm-studio-model")


def test_default_base_url(backend):
    assert backend.base_url == DEFAULT_LM_STUDIO_BASE_URL


def test_custom_base_url():
    with patch("modulo.model_backends.lm_studio.ChatOpenAI"):
        backend = LmStudioBackend(api_key=None, model_id="lm-studio-model", base_url="http://lm-studio:1234/v1")
    assert backend.base_url == "http://lm-studio:1234/v1"


def test_api_key_placeholder():
    with patch("modulo.model_backends.lm_studio.ChatOpenAI") as mock:
        LmStudioBackend(api_key=None, model_id="lm-studio-model")
    assert mock.call_args[1]["api_key"] == "lm_studio"


def test_api_key_empty_string_falls_back_to_placeholder():
    with patch("modulo.model_backends.lm_studio.ChatOpenAI") as mock:
        LmStudioBackend(api_key="", model_id="lm-studio-model")
    assert mock.call_args[1]["api_key"] == "lm_studio"


def test_explicit_api_key_passed_through():
    with patch("modulo.model_backends.lm_studio.ChatOpenAI") as mock:
        LmStudioBackend(api_key="sk-custom", model_id="lm-studio-model")
    assert mock.call_args[1]["api_key"] == "sk-custom"


def test_base_url_trailing_slash_stripped():
    with patch("modulo.model_backends.lm_studio.ChatOpenAI") as mock:
        LmStudioBackend(api_key=None, model_id="lm-studio-model", base_url="http://lm-studio:1234/v1/")
    assert mock.call_args[1]["base_url"] == "http://lm-studio:1234/v1"
