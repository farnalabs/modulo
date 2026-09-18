"""Tests for modulo.db.runner_binding_constraints — save-time binding rules.

Covers the FAR-592 / D6 new code in ``db/runner_binding_constraints.py`` that had
no unit coverage: reserved-var detection, target/source validation, and the
canonical pair validator.
"""

import pytest

from modulo.db.runner_binding_constraints import (
    KNOWN_SOURCE_FIELDS_ALL,
    RESERVED_ENV_VAR_PREFIXES,
    RESERVED_ENV_VARS,
    BindingValidationError,
    _validate_source_field,
    is_reserved_env_var,
    known_source_fields_for,
    validate_binding_pair,
    validate_target_env_var,
)


def test_reserved_env_vars_contains_critical_entries():
    assert "GITHUB_TOKEN" in RESERVED_ENV_VARS
    assert "MODULO_API_KEY" in RESERVED_ENV_VARS
    assert "DOCKER_HOST" in RESERVED_ENV_VARS
    assert "LD_PRELOAD" in RESERVED_ENV_VARS


def test_reserved_env_var_prefixes():
    assert RESERVED_ENV_VAR_PREFIXES == ("MODULO_", "APP_MODULO_", "GIT_")


def test_known_source_fields_default_is_api_key():
    assert frozenset({"api_key"}) == KNOWN_SOURCE_FIELDS_ALL
    assert known_source_fields_for("openai") == KNOWN_SOURCE_FIELDS_ALL
    assert known_source_fields_for("localai") == KNOWN_SOURCE_FIELDS_ALL


class TestIsReservedEnvVar:
    def test_exact_match_case_insensitive(self):
        assert is_reserved_env_var("github_token")
        assert is_reserved_env_var("  MODULO_API_KEY  ")

    def test_prefix_match_case_insensitive(self):
        assert is_reserved_env_var("MODULO_RUN_ID")
        assert is_reserved_env_var("app_modulo_opencode_api_key")
        assert is_reserved_env_var("GIT_PROXY_COMMAND")
        assert is_reserved_env_var("git_ssh_command")

    def test_non_reserved_returns_false(self):
        assert not is_reserved_env_var("MY_API_KEY")
        assert not is_reserved_env_var("OPENAI_TOKEN")
        assert not is_reserved_env_var("")


class TestValidateTargetEnvVar:
    def test_valid_target_returns_canonical(self):
        assert validate_target_env_var("OPENAI_API_KEY") == "OPENAI_API_KEY"

    def test_empty_raises(self):
        with pytest.raises(BindingValidationError, match="must not be empty"):
            validate_target_env_var("")

    def test_too_long_raises(self):
        with pytest.raises(BindingValidationError, match="exceeds"):
            validate_target_env_var("X" * 129)

    def test_invalid_pattern_raises(self):
        with pytest.raises(BindingValidationError, match=r"\[A-Za-z_\]"):
            validate_target_env_var("1BAD")
        with pytest.raises(BindingValidationError, match=r"\[A-Za-z_\]"):
            validate_target_env_var("BAD-NAME")

    def test_reserved_raises(self):
        with pytest.raises(BindingValidationError, match="reserved"):
            validate_target_env_var("GITHUB_TOKEN")
        with pytest.raises(BindingValidationError, match="reserved"):
            validate_target_env_var("MODULO_RUN_ID")


class TestValidateSourceField:
    def test_valid_source_returns_canonical(self):
        assert _validate_source_field("api_key", "openai") == "api_key"

    def test_empty_raises(self):
        with pytest.raises(BindingValidationError, match="must not be empty"):
            _validate_source_field("", "openai")

    def test_too_long_raises(self):
        with pytest.raises(BindingValidationError, match="exceeds"):
            _validate_source_field("x" * 65, "openai")

    def test_invalid_pattern_raises(self):
        with pytest.raises(BindingValidationError, match=r"\[A-Za-z_\]"):
            _validate_source_field("1bad", "openai")

    def test_unknown_field_raises(self):
        with pytest.raises(BindingValidationError, match="not a known credential field"):
            _validate_source_field("password", "openai")


class TestValidateBindingPair:
    def test_valid_pair_round_trips(self):
        target, source = validate_binding_pair(
            target_env_var="OPENAI_API_KEY", source_field="api_key", provider="openai"
        )
        assert target == "OPENAI_API_KEY"
        assert source == "api_key"

    def test_bad_target_propagates(self):
        with pytest.raises(BindingValidationError):
            validate_binding_pair(target_env_var="GITHUB_TOKEN", source_field="api_key", provider="openai")

    def test_bad_source_propagates(self):
        with pytest.raises(BindingValidationError):
            validate_binding_pair(target_env_var="OPENAI_API_KEY", source_field="secret", provider="openai")


def test_binding_validation_error_is_value_error():
    assert issubclass(BindingValidationError, ValueError)
