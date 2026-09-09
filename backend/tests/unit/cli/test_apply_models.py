"""Unit tests for modulo.cli.apply.models (FAR-681 slice 1)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from modulo.cli.apply.models import (
    ENV_REF_PATTERN,
    SECRET_REF_PATTERN,
    ApplyConfig,
    ApplyConfigError,
    ApplyGraphNode,
    ModelBackendEntity,
    PipelineEntity,
    TriggerEntity,
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


class TestTriggerEntityContracts:
    def test_inline_secret_in_config_json_rejected(self) -> None:
        tx = {
            "api_version": "modulo.dev/v1",
            "entities": {
                "triggers": [
                    {
                        "pipeline": "p",
                        "name": "hook",
                        "trigger_type": "webhook",
                        "config_json": {"hmac_secret": "sk-super-secret-literal"},
                    }
                ]
            },
        }
        with pytest.raises(ValidationError, match="must be a reference") as exc_info:
            _config(tx)
        # The validator's own message must not echo the secret value.
        assert "sk-super-secret-literal" not in str(exc_info.value.errors()[0]["msg"])

    def test_nested_sensitive_key_inline_secret_rejected(self) -> None:
        """FAR-681 QA (fail-open validator): the sensitive-key predicate tests
        the LEAF key of every walked path — smtp.password is a secret even
        though the top-level key is not."""
        tx = {
            "api_version": "modulo.dev/v1",
            "entities": {
                "triggers": [
                    {
                        "pipeline": "p",
                        "name": "hook",
                        "trigger_type": "webhook",
                        "config_json": {"smtp": {"password": "hunter2-literal"}},
                    }
                ]
            },
        }
        with pytest.raises(ValidationError, match="must be a reference") as exc_info:
            _config(tx)
        assert "smtp.password" in str(exc_info.value.errors()[0]["msg"])
        assert "hunter2-literal" not in str(exc_info.value.errors()[0]["msg"])

    def test_nested_sensitive_key_accepts_refs(self) -> None:
        tx = {
            "api_version": "modulo.dev/v1",
            "entities": {
                "triggers": [
                    {
                        "pipeline": "p",
                        "name": "hook",
                        "trigger_type": "webhook",
                        "config_json": {"smtp": {"password": "${env:SMTP_PASSWORD}", "host": "smtp.example"}},
                    }
                ]
            },
        }
        config = _config(tx)
        nested = config.entities.triggers[0].config_json["smtp"]
        assert nested["password"] == "${env:SMTP_PASSWORD}"

    def test_nested_non_sensitive_literal_allowed(self) -> None:
        """A literal under a non-sensitive LEAF key inside a nested dict is
        not a secret (leaf-key rule, matching the server mask)."""
        tx = {
            "api_version": "modulo.dev/v1",
            "entities": {
                "triggers": [
                    {
                        "pipeline": "p",
                        "name": "hook",
                        "trigger_type": "webhook",
                        "config_json": {"email": {"host": "smtp.example", "from": "bot@example"}},
                    }
                ]
            },
        }
        config = _config(tx)
        assert config.entities.triggers[0].config_json["email"]["host"] == "smtp.example"

    def test_secret_value_patterns_rejected_under_any_key(self) -> None:
        tx = {
            "api_version": "modulo.dev/v1",
            "entities": {
                "triggers": [
                    {
                        "pipeline": "p",
                        "name": "hook",
                        "trigger_type": "webhook",
                        "config_json": {"note": "ghp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},
                    }
                ]
            },
        }
        with pytest.raises(ValidationError, match="must be a reference"):
            _config(tx)

    def test_env_ref_and_secretref_parse_in_config_json(self) -> None:
        tx = {
            "api_version": "modulo.dev/v1",
            "entities": {
                "triggers": [
                    {
                        "pipeline": "p",
                        "name": "hook",
                        "trigger_type": "webhook",
                        "config_json": {"hmac_secret": "${env:HS}", "other": "secretref://vault/hook"},
                    }
                ]
            },
        }
        config = _config(tx)
        assert config.entities.triggers[0].config_json["hmac_secret"] == "${env:HS}"

    def test_empty_string_secret_value_allowed(self) -> None:
        """Empty strings are never masked server-side -> never a secret."""
        tx = {
            "api_version": "modulo.dev/v1",
            "entities": {
                "triggers": [
                    {
                        "pipeline": "p",
                        "name": "hook",
                        "trigger_type": "webhook",
                        "config_json": {"hmac_secret": ""},
                    }
                ]
            },
        }
        config = _config(tx)
        stored = config.entities.triggers[0].config_json["hmac_secret"]
        assert not stored

    def test_unknown_trigger_type_rejected(self) -> None:
        tx = {
            "api_version": "modulo.dev/v1",
            "entities": {"triggers": [{"pipeline": "p", "name": "hook", "trigger_type": "warp"}]},
        }
        with pytest.raises(ValidationError):
            _config(tx)

    def test_duplicate_trigger_identity_rejected(self) -> None:
        tx = {
            "api_version": "modulo.dev/v1",
            "entities": {
                "triggers": [
                    {"pipeline": "p", "name": "hook", "trigger_type": "webhook"},
                    {"pipeline": "p", "name": "hook", "trigger_type": "cron"},
                ]
            },
        }
        with pytest.raises(ValidationError, match="duplicate triggers"):
            _config(tx)

    def test_same_name_different_pipeline_distinct_identity(self) -> None:
        tx = {
            "api_version": "modulo.dev/v1",
            "entities": {
                "triggers": [
                    {"pipeline": "a", "name": "hook", "trigger_type": "webhook"},
                    {"pipeline": "b", "name": "hook", "trigger_type": "cron"},
                ]
            },
        }
        config = _config(tx)
        assert [t.display_key() for t in config.entities.triggers] == ["a/hook", "b/hook"]

    def test_create_payload_carries_name(self) -> None:
        entity = TriggerEntity.model_validate(
            {"pipeline": "p", "name": "hook", "trigger_type": "webhook", "config_json": {"hmac_secret": "${env:HS}"}}
        )
        payload = entity.create_payload({"hmac_secret": "resolved"})
        assert payload["name"] == "hook"
        assert payload["config_json"]["hmac_secret"] == "resolved"

    def test_update_payload_always_carries_spend_limit(self) -> None:
        entity = TriggerEntity.model_validate({"pipeline": "p", "name": "hook", "trigger_type": "cron"})
        payload = entity.update_payload({})
        assert "daily_spend_limit" in payload
        assert payload["daily_spend_limit"] is None

    def test_with_resolved_config_hashes_resolved_literals(self) -> None:
        """FAR-681 QA (env-ref drift): the desired view must hash the RESOLVED
        config, not the raw ${env:VAR} strings."""
        entity = TriggerEntity.model_validate(
            {"pipeline": "p", "name": "hook", "trigger_type": "webhook", "config_json": {"url": "${env:URL}"}}
        )
        resolved = entity.with_resolved_config({"url": "https://resolved.example"})
        current = {
            "trigger_type": "webhook",
            "active": True,
            "max_concurrent_runs": 1,
            "daily_spend_limit": None,
            "cron_expression": None,
            "cron_timezone": None,
            "config_json": {"url": "https://resolved.example"},
        }
        from modulo.cli.apply.plan import plan_entity

        decision = plan_entity("trigger", "p/hook", resolved.managed_view(), current)
        assert decision.status == "unchanged"

    def test_managed_view_quantizes_spend_limit_to_4dp(self) -> None:
        entity = TriggerEntity.model_validate(
            {"pipeline": "p", "name": "hook", "trigger_type": "cron", "daily_spend_limit": 10.55555}
        )
        assert entity.managed_view()["daily_spend_limit"] == pytest.approx(10.5556)

    def test_config_declares_secrets(self) -> None:
        """The helper runs on the RESOLVED mapping: sensitive LEAF keys always
        declare secrets; mask-pattern resolved values declare secrets; plain
        resolved values do not."""
        from modulo.cli.apply.models import config_declares_secrets

        assert config_declares_secrets({"hmac_secret": "${env:HS}", "note": "x"})
        assert config_declares_secrets({"smtp": {"password": "${env:PW}"}})
        assert config_declares_secrets({"token": "ghp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"})
        assert not config_declares_secrets({"url": "https://resolved.example", "note": "plain"})

    def test_slash_in_pipeline_name_rejected(self) -> None:
        with pytest.raises(ValidationError, match="composite-key separator"):
            PipelineEntity.model_validate({"name": "team/sample"})

    def test_slash_in_trigger_name_rejected(self) -> None:
        with pytest.raises(ValidationError, match="composite-key separator"):
            TriggerEntity.model_validate({"pipeline": "p", "name": "a/b", "trigger_type": "cron"})

    def test_slash_in_trigger_pipeline_ref_rejected(self) -> None:
        with pytest.raises(ValidationError, match="composite-key separator"):
            TriggerEntity.model_validate({"pipeline": "team/p", "name": "hook", "trigger_type": "cron"})


class TestPipelineEntityContracts:
    def test_graphless_managed_view_excludes_graph(self) -> None:
        entity = PipelineEntity.model_validate({"name": "sample", "max_concurrent_runs": 2})
        view = entity.managed_view()
        assert view == {"description": None, "max_concurrent_runs": 2}

    def test_declared_graph_managed_view_includes_graph(self) -> None:
        entity = PipelineEntity.model_validate(
            {
                "name": "sample",
                "graph": {
                    "nodes": [
                        {
                            "id": "00000000-0000-0000-0000-0000000000a1",
                            "agent": "worker",
                            "position": {"x": 0, "y": 0},
                        }
                    ]
                },
            }
        )
        view = entity.managed_view(graph={"nodes": [{"agent_id": "x"}], "edges": []})
        assert view["graph"] == {"nodes": [{"agent_id": "x"}], "edges": []}

    def test_node_extra_field_rejected(self) -> None:
        tx = {
            "api_version": "modulo.dev/v1",
            "entities": {
                "pipelines": [
                    {
                        "name": "sample",
                        "graph": {
                            "nodes": [
                                {
                                    "id": "00000000-0000-0000-0000-0000000000a1",
                                    "agent": "worker",
                                    "position": {"x": 0, "y": 0},
                                    "mystery_field": 1,
                                }
                            ]
                        },
                    }
                ]
            },
        }
        with pytest.raises(ValidationError):
            _config(tx)

    def test_node_agent_id_forbidden(self) -> None:
        """apply configs reference agents by NAME only."""
        tx = {
            "api_version": "modulo.dev/v1",
            "entities": {
                "pipelines": [
                    {
                        "name": "sample",
                        "graph": {
                            "nodes": [
                                {
                                    "id": "00000000-0000-0000-0000-0000000000a1",
                                    "agent_id": "00000000-0000-0000-0000-0000000000ff",
                                    "position": {"x": 0, "y": 0},
                                }
                            ]
                        },
                    }
                ]
            },
        }
        with pytest.raises(ValidationError):
            _config(tx)

    def test_node_field_set_mirrors_api_model(self) -> None:
        """Drift alarm: the apply node mirror must cover every API node field
        (except the agent_id <-> agent swap) — a silently-dropped declarative
        field would create permanent plan drift."""
        from modulo.api.routes.pipelines import PipelineGraphNode

        apply_fields = set(ApplyGraphNode.model_fields) - {"agent"}
        api_fields = set(PipelineGraphNode.model_fields) - {"agent_id"}
        assert apply_fields == api_fields

    def test_graph_duplicate_node_ids_rejected(self) -> None:
        tx = {
            "api_version": "modulo.dev/v1",
            "entities": {
                "pipelines": [
                    {
                        "name": "sample",
                        "graph": {
                            "nodes": [
                                {
                                    "id": "00000000-0000-0000-0000-0000000000a1",
                                    "agent": "worker",
                                    "position": {"x": 0, "y": 0},
                                }
                            ]
                            * 2
                        },
                    }
                ]
            },
        }
        with pytest.raises(ValidationError, match="unique"):
            _config(tx)


class TestSensitiveKeyTwin:
    def test_local_sensitive_key_patterns_equal_middleware(self) -> None:
        """Drift alarm: the CLI runs WITHOUT server settings, so it cannot
        import the FastAPI/DB-heavy mask middleware — the local pattern set
        must stay byte-identical to the middleware's."""
        from modulo.api.middleware.sensitive_mask import _SENSITIVE_KEY_PATTERNS
        from modulo.cli.apply import models

        assert models._SENSITIVE_KEY_PATTERNS == _SENSITIVE_KEY_PATTERNS

    def test_local_is_sensitive_key_matches_middleware(self) -> None:
        from modulo.api.middleware.sensitive_mask import is_sensitive_key as middleware_is_sensitive_key
        from modulo.cli.apply.models import is_sensitive_key

        for key in ("signing_secret", "API-KEY", "db password", "TokenValue", "hmac_secret", "note", "scan_interval"):
            assert is_sensitive_key(key) == middleware_is_sensitive_key(key), key


class TestForwardReferenceMergeGate:
    def test_trigger_pipeline_in_later_document_rejected(self) -> None:
        base = _config(
            {
                "api_version": "modulo.dev/v1",
                "entities": {"triggers": [{"pipeline": "later", "name": "hook", "trigger_type": "webhook"}]},
            }
        )
        other = _config({"api_version": "modulo.dev/v1", "entities": {"pipelines": [{"name": "later"}]}})
        with pytest.raises(ApplyConfigError, match="forward-references"):
            base.merge_entities(other)

    def test_trigger_pipeline_in_same_document_merges(self) -> None:
        base = _config(
            {
                "api_version": "modulo.dev/v1",
                "entities": {"pipelines": [{"name": "p"}]},
            }
        )
        other = _config(
            {
                "api_version": "modulo.dev/v1",
                "entities": {"triggers": [{"pipeline": "p", "name": "hook", "trigger_type": "webhook"}]},
            }
        )
        merged = base.merge_entities(other)
        assert [t.display_key() for t in merged.entities.triggers] == ["p/hook"]
