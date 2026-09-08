"""Unit tests for modulo.cli.apply.plan (FAR-681 slice 1)."""

from __future__ import annotations

from modulo.cli.apply.models import ModelBackendEntity, SchemaEntity
from modulo.cli.apply.plan import build_plan, canonical_hash, plan_entity

SCHEMA_ENTITY = SchemaEntity.model_validate({"name": "alpha", "description": "Old description"})

BACKEND_ENTITY = ModelBackendEntity.model_validate(
    {
        "name": "openai",
        "display_name": "OpenAI",
        "provider": "openai",
        "model_id": "gpt-x",
        "api_key": "${env:SK}",
        "default_params": {"temperature": 0.5},
    }
)


class TestCanonicalHash:
    def test_stable_across_key_order_and_lengths(self) -> None:
        assert canonical_hash({"a": 1, "b": [1, 2]}) == canonical_hash({"b": [1, 2], "a": 1})

    def test_different_payloads_differ(self) -> None:
        assert canonical_hash({"a": 1}) != canonical_hash({"a": 2})

    def test_returns_hex_digest(self) -> None:
        digest = canonical_hash({"x": "y"})
        assert len(digest) == 64


class TestPlanDecisionTable:
    def test_absent_entity_is_created(self) -> None:
        decision = plan_entity("schema", "alpha", SCHEMA_ENTITY.managed_view(), None)
        assert decision.status == "created"
        assert decision.reason is None

    def test_matching_entity_is_unchanged(self) -> None:
        current = {
            "name": "alpha",
            "description": "Old description",
            "abstract_name": None,
            "versions": [],
        }
        decision = plan_entity("schema", "alpha", SCHEMA_ENTITY.managed_view(), current)
        assert decision.status == "unchanged"
        assert decision.desired_hash == decision.current_hash

    def test_differing_fields_is_updated(self) -> None:
        current = {
            "name": "alpha",
            "description": "Changed elsewhere",
            "abstract_name": None,
            "versions": [],
        }
        decision = plan_entity("schema", "alpha", SCHEMA_ENTITY.managed_view(), current)
        assert decision.status == "updated"
        assert decision.desired_hash != decision.current_hash

    def test_version_difference_is_updated(self) -> None:
        schema_with_version = SchemaEntity.model_validate(
            {
                "name": "alpha",
                "versions": [
                    {
                        "version": "v1",
                        "version_number": 1,
                        "definition_json": {"type": "object"},
                    }
                ],
            }
        )
        current = {
            "name": "alpha",
            "description": None,
            "abstract_name": None,
            "versions": [],
        }
        decision = plan_entity("schema", "alpha", schema_with_version.managed_view(), current)
        assert decision.status == "updated"

    def test_identical_version_is_unchanged(self) -> None:
        schema_with_version = SchemaEntity.model_validate(
            {
                "name": "alpha",
                "versions": [
                    {
                        "version": "v1",
                        "version_number": 1,
                        "definition_json": {"type": "object"},
                    }
                ],
            }
        )
        current = {
            "name": "alpha",
            "description": None,
            "abstract_name": None,
            "versions": [
                {
                    "version": "v1",
                    "version_number": 1,
                    "definition_json": {"type": "object"},
                    "published": False,
                }
            ],
        }
        decision = plan_entity("schema", "alpha", schema_with_version.managed_view(), current)
        assert decision.status == "unchanged"

    def test_backend_provider_mismatch_is_blocked(self) -> None:
        current = {
            "name": "openai",
            "display_name": "OpenAI",
            "provider": "anthropic",
            "model_id": "claude",
            "default_params": {},
            "visibility": "org",
            "tier": "native",
        }
        decision = plan_entity("model_backend", "openai", BACKEND_ENTITY.managed_view(), current)
        assert decision.status == "blocked"
        assert decision.reason is not None
        assert "provider" in decision.reason

    def test_changed_version_content_is_blocked(self) -> None:
        """Same version string + different definition_json can never converge
        (versions are immutable via apply) — block instead of 'updated'."""
        schema_with_version = SchemaEntity.model_validate(
            {
                "name": "alpha",
                "versions": [
                    {
                        "version": "v1",
                        "version_number": 1,
                        "definition_json": {"type": "object", "properties": {"changed": {"type": "string"}}},
                    }
                ],
            }
        )
        current = {
            "name": "alpha",
            "description": None,
            "abstract_name": None,
            "versions": [
                {
                    "version": "v1",
                    "version_number": 1,
                    "definition_json": {"type": "object"},
                    "published": False,
                }
            ],
        }
        decision = plan_entity("schema", "alpha", schema_with_version.managed_view(), current)
        assert decision.status == "blocked"
        assert decision.reason is not None
        assert "version v1 exists with different content" in decision.reason
        assert "immutable via apply" in decision.reason

    def test_changed_version_number_blocks(self) -> None:
        schema_with_version = SchemaEntity.model_validate(
            {
                "name": "alpha",
                "versions": [
                    {
                        "version": "v1",
                        "version_number": 2,
                        "definition_json": {"type": "object"},
                    }
                ],
            }
        )
        current = {
            "name": "alpha",
            "description": None,
            "abstract_name": None,
            "versions": [
                {
                    "version": "v1",
                    "version_number": 1,
                    "definition_json": {"type": "object"},
                    "published": False,
                }
            ],
        }
        decision = plan_entity("schema", "alpha", schema_with_version.managed_view(), current)
        assert decision.status == "blocked"

    def test_new_version_string_is_still_updated(self) -> None:
        """A NEW version string next to an identical existing one is not a conflict."""
        schema_with_versions = SchemaEntity.model_validate(
            {
                "name": "alpha",
                "versions": [
                    {"version": "v1", "version_number": 1, "definition_json": {"type": "object"}},
                    {"version": "v2", "version_number": 2, "definition_json": {"type": "object"}},
                ],
            }
        )
        current = {
            "name": "alpha",
            "description": None,
            "abstract_name": None,
            "versions": [
                {
                    "version": "v1",
                    "version_number": 1,
                    "definition_json": {"type": "object"},
                    "published": False,
                }
            ],
        }
        decision = plan_entity("schema", "alpha", schema_with_versions.managed_view(), current)
        assert decision.status == "updated"
        assert decision.reason is None

    def test_matching_backend_is_unchanged(self) -> None:
        current = {
            "name": "openai",
            "display_name": "OpenAI",
            "provider": "openai",
            "model_id": "gpt-x",
            "default_params": {"temperature": 0.5},
            "visibility": "org",
            "tier": "native",
        }
        decision = plan_entity("model_backend", "openai", BACKEND_ENTITY.managed_view(), current)
        assert decision.status == "unchanged"

    def test_api_key_not_compared_for_drift(self) -> None:
        current = {
            "name": "openai",
            "display_name": "OpenAI",
            "provider": "openai",
            "model_id": "gpt-x",
            "default_params": {},
            "visibility": "org",
            "tier": "native",
            "api_key": "whateverserver-side",
        }
        backend = ModelBackendEntity.model_validate(
            {
                "name": "openai",
                "display_name": "OpenAI",
                "provider": "openai",
                "model_id": "gpt-x",
                "api_key": "${env:SK}",
            }
        )
        decision = plan_entity("model_backend", "openai", backend.managed_view(), current)
        assert decision.status == "unchanged"


class TestBuildPlan:
    def test_full_plan_report_shape(self) -> None:
        blocked_backend = ModelBackendEntity.model_validate(
            {
                "name": "other",
                "display_name": "Other",
                "provider": "openai",
                "model_id": "gpt-x",
                "api_key": "${env:SK}",
            }
        )
        desired = {
            "schema": [("alpha", SCHEMA_ENTITY.managed_view())],
            "model_backend": [
                ("other", blocked_backend.managed_view()),
            ],
        }
        current = {
            "schema": {
                "alpha": {
                    "name": "alpha",
                    "description": "Edited elsewhere",
                    "abstract_name": None,
                    "versions": [],
                }
            },
            "model_backend": {
                "other": {
                    "name": "other",
                    "display_name": "Other",
                    "provider": "mistral",
                    "model_id": "mism-x",
                    "default_params": {},
                    "visibility": "org",
                    "tier": "native",
                }
            },
        }
        report = build_plan(desired, current)
        assert not report["created"]
        assert not report["unchanged"]
        updated_names = [e["name"] for e in report["updated"]]
        assert updated_names == ["alpha"]
        blocked_names = [e["name"] for e in report["blocked"]]
        assert blocked_names == ["other"]
        assert report["blocked"][0]["reason"]

    def test_unresolved_refs_enter_blocked(self) -> None:
        desired: dict = {"schema": [], "model_backend": []}
        report = build_plan(desired, {"schema": {}, "model_backend": []}, [("model_backend", "b", "unresolved")])
        assert [e["name"] for e in report["blocked"]] == ["b"]
        assert not report["created"]
        assert not report["updated"]
        assert not report["unchanged"]
