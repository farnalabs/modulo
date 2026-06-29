"""Tests for modulo.db.enums — ModelBackendProvider enum."""

from sqlalchemy import CheckConstraint

from modulo.db.enums import ModelBackendProvider
from modulo.db.models.model_backend import ModelBackend, _PROVIDER_VALUES

EXPECTED_PROVIDERS = frozenset({
    "ai21",
    "anthropic",
    "azure_openai",
    "bedrock",
    "cohere",
    "custom",
    "deepseek",
    "fireworks",
    "gemini",
    "grok",
    "groq",
    "jan",
    "llamacpp",
    "lm_studio",
    "localai",
    "mistral",
    "ollama",
    "openai",
    "openrouter",
    "perplexity",
    "qwen",
    "replicate",
    "tgi",
    "togetherai",
    "vertexai",
    "vllm",
    "watsonx",
})


class TestModelBackendProviderEnum:
    def test_all_expected_providers_present(self) -> None:
        actual = frozenset(m.value for m in ModelBackendProvider)
        assert actual == EXPECTED_PROVIDERS

    def test_each_value_matches_lowercase_name(self) -> None:
        for member in ModelBackendProvider:
            assert member.value == member.name.lower()

    def test_all_members_have_unique_values(self) -> None:
        values = [m.value for m in ModelBackendProvider]
        assert len(values) == len(set(values))

    def test_check_constraint_includes_all_enum_values(self) -> None:
        constraint = next(
            c
            for c in ModelBackend.__table_args__
            if isinstance(c, CheckConstraint) and c.name == "ck_model_backends_provider"
        )
        sql = str(constraint.sqltext)
        for value in _PROVIDER_VALUES:
            assert repr(value) in sql

    def test_provider_values_are_sorted(self) -> None:
        assert _PROVIDER_VALUES == sorted(_PROVIDER_VALUES)

    def test_enum_and_module_agree(self) -> None:
        assert frozenset(m.value for m in ModelBackendProvider) == frozenset(_PROVIDER_VALUES)
