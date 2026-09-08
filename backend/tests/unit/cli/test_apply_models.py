"""Unit tests for modulo.cli.apply.models (FAR-681 slice 1)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from modulo.cli.apply.models import ENV_REF_PATTERN, SECRET_REF_PATTERN, ApplyConfig, ModelBackendEntity


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
            ("${env:OPENAI_API_KEY}", True),
            ("${env:VAR_1}", True),
            ("secretref://my/provider-key", True),
            ("sk-literal-inline-secret", False),
            ("", False),
            ("secretref://with space", False),
            ("${env:lowercase}", False),
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

    def test_inline_secret_message_mentions_forbidden(self) -> None:
        backend = {
            "name": "b",
            "display_name": "b",
            "provider": "openai",
            "model_id": "gpt",
            "api_key": "sk-literal",
        }
        with pytest.raises(ValidationError) as exc_info:
            ModelBackendEntity.model_validate(backend)
        assert "forbidden" in str(exc_info.value)

    def test_env_ref_var_extraction(self) -> None:
        backend = ModelBackendEntity.model_validate(
            {"name": "b", "display_name": "b", "provider": "openai", "model_id": "gpt", "api_key": "${env:SK_KEY}"}
        )
        assert backend.env_ref_var() == "SK_KEY"
        assert ENV_REF_PATTERN.match("${env:SK_KEY}") is not None

    def test_backend_secret_ref_not_resolved_by_model(self) -> None:
        backend = ModelBackendEntity.model_validate(
            {"name": "b", "display_name": "b", "provider": "openai", "model_id": "gpt", "api_key": "secretref://k"}
        )
        assert backend.env_ref_var() is None
        assert SECRET_REF_PATTERN.match("secretref://k") is not None


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
