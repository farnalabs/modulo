"""Unit tests for modulo.cli.apply.models (FAR-681 slice 1)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from modulo.cli.apply.models import (
    ENV_REF_PATTERN,
    SECRET_REF_PATTERN,
    ApplyConfig,
    ModelBackendEntity,
    parse_api_version_major,
)


def _config(tx: dict) -> ApplyConfig:
    return ApplyConfig.model_validate(tx)


class TestApiVersionGate:
    def test_supported_version_accepted(self) -> None:
        config = _config({"api_version": "modulo.dev/v1", "entities": {}})
        assert config.api_version == "modulo.dev/v1"

    def test_major_mismatch_rejected(self) -> None:
        with pytest.raises(ValidationError, match="major mismatch"):
            _config({"api_version": "modulo.dev/v2", "entities": {}})

    def test_minor_suffix_lenient(self) -> None:
        config = _config({"api_version": "modulo.dev/v1.3", "entities": {}})
        assert config.api_version == "modulo.dev/v1.3"

    def test_unrecognised_prefix_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _config({"api_version": "kubernetes/v1", "entities": {}})


class TestExtraForbid:
    def test_unknown_top_level_key_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _config({"api_version": "modulo.dev/v1", "entities": {}, "widgets": 1})

    def test_unknown_schema_field_rejected(self) -> None:
        tx = {
            "api_version": "modulo.dev/v1",
            "entities": {"schemas": [{"name": "s", "nope": True}]},
        }
        with pytest.raises(ValidationError):
            _config(tx)

    def test_unknown_backend_field_rejected(self) -> None:
        tx = {
            "api_version": "modulo.dev/v1",
            "entities": {"model_backends": [{"name": "b", "api_key": "x", "nope": 1}]},
        }
        with pytest.raises(ValidationError):
            _config(tx)


class TestSecretRefValidation:
    @pytest.mark.parametrize(
        ("value", "valid"),
        [
            pytest.param("${env:OPENAI_API_KEY}", True, id="uppercase_env_ref"),
            pytest.param("${env:VAR_1}", True, id="underscored_env_ref"),
            pytest.param("${env:lowercase}", True, id="lowercase_env_ref"),
            pytest.param("secretref://my/provider-key", True, id="secretref_valid"),
            pytest.param("sk-literal-inline-secret", False, id="inline_secret"),
            pytest.param("", False, id="empty_value"),
            pytest.param("secretref://with space", False, id="secretref_space"),
            pytest.param("${env:OPENAI_API_KEY}\n", False, id="env_ref_trailing_newline"),
            pytest.param("secretref://k\n", False, id="secretref_trailing_newline"),
        ],
    )
    def test_api_key_ref_patterns(self, value: str, valid: bool) -> None:
        backend = {"name": "b", "display_name": "b", "provider": "openai", "model_id": "gpt", "api_key": value}
        if valid:
            entity = ModelBackendEntity.model_validate(backend)
            assert entity.api_key == value
        else:
            with pytest.raises(ValidationError):
                ModelBackendEntity.model_validate(backend)

    def test_inline_secret_message_does_not_disclose_value(self) -> None:
        backend = {
            "name": "b",
            "display_name": "b",
            "provider": "openai",
            "model_id": "gpt",
            "api_key": "sk-super-secret-literal",
        }
        with pytest.raises(ValidationError) as exc_info:
            ModelBackendEntity.model_validate(backend)
        # The validator's own message must not echo any part of the value.
        validator_msg = exc_info.value.errors()[0]["msg"]
        assert "forbidden" in validator_msg
        assert "sk-super-secret-literal" not in validator_msg

    def test_env_ref_var_extraction(self) -> None:
        backend = ModelBackendEntity.model_validate(
            {"name": "b", "display_name": "b", "provider": "openai", "model_id": "gpt", "api_key": "${env:SK_KEY}"}
        )
        assert backend.env_ref_var() == "SK_KEY"
        assert ENV_REF_PATTERN.fullmatch("${env:SK_KEY}") is not None
        assert ENV_REF_PATTERN.fullmatch("${env:lowercase_ok}") is not None

    def test_backend_secret_ref_not_resolved_by_model(self) -> None:
        backend = ModelBackendEntity.model_validate(
            {"name": "b", "display_name": "b", "provider": "openai", "model_id": "gpt", "api_key": "secretref://k"}
        )
        assert backend.env_ref_var() is None
        assert SECRET_REF_PATTERN.fullmatch("secretref://k") is not None


class TestManagedViews:
    def test_backend_managed_view_excludes_api_key_and_name(self) -> None:
        backend = ModelBackendEntity.model_validate(
            {
                "name": "b",
                "display_name": "B",
                "provider": "openai",
                "model_id": "gpt",
                "api_key": "${env:SK}",
            }
        )
        view = backend.managed_view()
        assert view == {
            "display_name": "B",
            "provider": "openai",
            "model_id": "gpt",
            "default_params": {},
            "visibility": "org",
            "tier": "native",
        }

    def test_backend_managed_view_can_include_api_key(self) -> None:
        backend = ModelBackendEntity.model_validate(
            {"name": "b", "display_name": "B", "provider": "openai", "model_id": "gpt", "api_key": "${env:SK}"}
        )
        view = backend.managed_view(include_api_key=True)
        assert view["api_key"] == "${env:SK}"

    def test_schema_managed_view_excludes_name(self) -> None:
        schema = ApplyConfig.model_validate(
            {"api_version": "modulo.dev/v1", "entities": {"schemas": [{"name": "s"}]}}
        ).entities.schemas[0]
        view = schema.managed_view()
        assert view == {"description": None, "abstract_name": None, "versions": []}


class TestUniqueNames:
    def test_duplicate_schema_names_rejected(self) -> None:
        tx = {
            "api_version": "modulo.dev/v1",
            "entities": {"schemas": [{"name": "s"}, {"name": "s"}]},
        }
        with pytest.raises(ValidationError):
            _config(tx)

    def test_duplicate_backend_names_rejected(self) -> None:
        tx = {
            "api_version": "modulo.dev/v1",
            "entities": {
                "model_backends": [
                    {"name": "b", "display_name": "b", "provider": "p", "model_id": "m", "api_key": "${env:V}"}
                ]
                * 2
            },
        }
        with pytest.raises(ValidationError):
            _config(tx)


class TestMergeMajors:
    def test_minor_suffix_merges_across_documents(self) -> None:
        """Minors compare leniently: v1 + v1.3 merge (majors are what matter)."""
        base = _config({"api_version": "modulo.dev/v1", "entities": {}})
        other = _config({"api_version": "modulo.dev/v1.3", "entities": {}})
        merged = base.merge_entities(other)
        assert merged.api_version == "modulo.dev/v1"

    def test_major_mismatch_merge_rejected_via_parsed_majors(self) -> None:
        """Merge compares parsed majors: v1 vs v2 majors differ."""
        base = _config({"api_version": "modulo.dev/v1", "entities": {}})
        # model_construct bypasses the validators so a v2 doc can reach the
        # merge path (the normal validation gate rejects it at load time).
        forged = ApplyConfig.model_construct(api_version="modulo.dev/v2", entities=None)
        with pytest.raises(ValueError, match="major"):
            base.merge_entities(forged)

    def test_parse_api_version_major(self) -> None:
        assert parse_api_version_major("modulo.dev/v1") == 1
        assert parse_api_version_major("modulo.dev/v1.3") == 1
        with pytest.raises(ValueError, match="not parseable"):
            parse_api_version_major("modulo.dev/vx.3")
