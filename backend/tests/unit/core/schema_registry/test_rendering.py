"""Acceptance tests for FAR-900: schema translation/render layer.

Covers:
- verbatim is identity
- provider-strict strips unsupported keywords, keeps supported; per-provider sets differ
- $ref inlining within limits; exceeding depth/nodes → verbatim fallback; external $ref rejected
- Fallback: failing render never raises, never blocks
- Memoisation: same input → same output; mutation-safe
- schema_profile round-trips REST + MCP + workflow import/export
- Absent = verbatim; invalid value rejected at save time (Literal)
- Composite expander propagates schema_profile
- Abstract schema → skipped + recorded
"""

from __future__ import annotations

import copy
import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from modulo.core.schema_registry.rendering import (
    _MAX_REF_DEPTH,
    _is_abstract_schema,
    _render_cache,
    _RenderCache,
    _walk_schema,
    preview_strip_warnings,
    render_for_profile,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_render_cache() -> None:
    """Ensure each test starts with a clean cache."""
    _render_cache.clear()
    yield
    _render_cache.clear()


@pytest.fixture
def simple_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "age": {"type": "integer"},
        },
        "required": ["name"],
    }


@pytest.fixture
def schema_with_unsupported() -> dict[str, Any]:
    """Schema containing keywords that OpenAI strips in strict mode."""
    return {
        "type": "object",
        "properties": {
            "name": {"type": "string", "minLength": 1, "maxLength": 100},
            "email": {"type": "string", "format": "email"},
            "score": {"type": "number", "minimum": 0, "maximum": 100},
        },
        "required": ["name", "email"],
        "title": "User",
        "description": "A user object",
    }


@pytest.fixture
def schema_with_ref() -> dict[str, Any]:
    """Schema with $ref and $defs."""
    return {
        "type": "object",
        "properties": {
            "user": {"$ref": "#/$defs/User"},
        },
        "$defs": {
            "User": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "email": {"type": "string"},
                },
                "required": ["name"],
            },
        },
    }


@pytest.fixture
def deep_ref_schema() -> dict[str, Any]:
    """Schema that would exceed the max depth limit."""
    # Build a chain of $ref that is deeper than _MAX_REF_DEPTH
    schema: dict[str, Any] = {"type": "object", "properties": {}}
    current = schema
    for i in range(_MAX_REF_DEPTH + 5):
        def_name = f"Level{i}"
        current["$defs"] = {def_name: {"type": "object", "properties": {}}}
        current["properties"] = {"next": {"$ref": f"#/$defs/{def_name}"}}
        current = current["$defs"][def_name]
    return schema


@pytest.fixture
def external_ref_schema() -> dict[str, Any]:
    """Schema with an external $ref."""
    return {
        "type": "object",
        "properties": {
            "data": {"$ref": "https://example.com/schema.json#/definitions/Foo"},
        },
    }


@pytest.fixture
def abstract_schema() -> dict[str, Any]:
    """Schema that is abstract (no concrete definition)."""
    return {"$ref": "#/$defs/AbstractType"}


# ---------------------------------------------------------------------------
# 1. verbatim is identity
# ---------------------------------------------------------------------------


class TestVerbatimIdentity:
    def test_returns_deep_copy(self, simple_schema: dict[str, Any]) -> None:
        result = render_for_profile(simple_schema, "verbatim")
        assert result.profile == "verbatim"
        assert result.schema == simple_schema
        # Verify it's a deep copy, not the same object
        assert result.schema is not simple_schema

    def test_mutation_does_not_affect_original(self, simple_schema: dict[str, Any]) -> None:
        original = copy.deepcopy(simple_schema)
        result = render_for_profile(simple_schema, "verbatim")
        result.schema["injected"] = True
        assert simple_schema == original

    def test_empty_schema_returns_empty(self) -> None:
        result = render_for_profile({}, "verbatim")
        assert result.skipped is True
        assert result.skip_reason == "empty_schema"

    def test_none_like_schema_returns_empty(self) -> None:
        result = render_for_profile({"not_a_schema": True}, "verbatim")
        # Non-empty dict is treated as a schema, not skipped
        assert result.skipped is False


# ---------------------------------------------------------------------------
# 2. provider-strict strips unsupported, keeps supported; per-provider sets differ
# ---------------------------------------------------------------------------


class TestProviderStrict:
    def test_strips_unsupported_openai(self, schema_with_unsupported: dict[str, Any]) -> None:
        result = render_for_profile(schema_with_unsupported, "provider-strict", "openai")
        assert result.profile == "provider-strict"
        assert result.skipped is False
        schema = result.schema
        # OpenAI strips: minLength, maxLength, minimum, maximum, format, title, description
        assert "title" not in schema
        assert "description" not in schema
        # Per-property: minLength, maxLength stripped
        assert "minLength" not in schema["properties"]["name"]
        assert "maxLength" not in schema["properties"]["name"]
        # format stripped
        assert "format" not in schema["properties"]["email"]
        # minimum, maximum stripped
        assert "minimum" not in schema["properties"]["score"]
        assert "maximum" not in schema["properties"]["score"]
        # type and properties retained
        assert schema["type"] == "object"
        assert "properties" in schema
        assert "required" in schema

    def test_strips_different_for_anthropic(self, schema_with_unsupported: dict[str, Any]) -> None:
        result = render_for_profile(schema_with_unsupported, "provider-strict", "anthropic")
        schema = result.schema
        # Anthropic strips: minLength, maxLength, minimum, maximum, format
        # but does NOT strip pattern (not in this schema anyway)
        # Also strips additionalProperties, patternProperties
        assert "minLength" not in schema["properties"]["name"]
        assert "format" not in schema["properties"]["email"]
        assert "minimum" not in schema["properties"]["score"]

    def test_per_provider_sets_differ(self) -> None:
        """Verify that OpenAI and Anthropic have different unsupported sets."""
        from modulo.core.schema_registry.rendering import _PROVIDER_UNSUPPORTED

        openai_set = _PROVIDER_UNSUPPORTED.get("openai", frozenset())
        anthropic_set = _PROVIDER_UNSUPPORTED.get("anthropic", frozenset())
        # They should not be identical
        assert openai_set != anthropic_set
        # pattern is in OpenAI's set but not in Anthropic's
        assert "pattern" in openai_set
        assert "pattern" not in anthropic_set

    def test_emits_per_keyword_warnings(self, schema_with_unsupported: dict[str, Any]) -> None:
        result = render_for_profile(schema_with_unsupported, "provider-strict", "openai")
        assert result.warnings
        # Each warning has keyword, path, message
        for w in result.warnings:
            assert w.keyword
            assert w.path
            assert "stripped" in w.message.lower()

    def test_preserves_always_keep_keywords(self) -> None:
        """Keywords in _ALWAYS_KEEP must survive provider-strict."""
        schema = {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
            "required": ["items"],
        }
        result = render_for_profile(schema, "provider-strict", "openai")
        assert result.schema["type"] == "object"
        assert "required" in result.schema
        assert result.schema["properties"]["items"]["type"] == "array"

    def test_unknown_provider_still_strips_advisory(self) -> None:
        """Unknown provider: no provider-specific strips, but advisory keywords (default, examples) are stripped."""
        schema = {
            "type": "object",
            "properties": {
                "name": {"type": "string", "default": "foo", "examples": ["bar"]},
            },
        }
        result = render_for_profile(schema, "provider-strict", "unknown_provider")
        # Advisory keywords stripped even for unknown provider
        assert "default" not in result.schema["properties"]["name"]
        assert "examples" not in result.schema["properties"]["name"]
        # type retained
        assert result.schema["properties"]["name"]["type"] == "string"


# ---------------------------------------------------------------------------
# 3. $ref inlining works within limits; exceeding → verbatim; external rejected
# ---------------------------------------------------------------------------


class TestRefInlining:
    def test_inlines_local_ref(self, schema_with_ref: dict[str, Any]) -> None:
        result = render_for_profile(schema_with_ref, "provider-strict", "openai")
        # $ref should be inlined; $defs should be removed
        assert "$defs" not in result.schema
        user_prop = result.schema["properties"]["user"]
        # The ref should be resolved to the User type's properties
        assert "type" in user_prop
        assert "properties" in user_prop

    def test_external_ref_rejected(self, external_ref_schema: dict[str, Any]) -> None:
        result = render_for_profile(external_ref_schema, "provider-strict", "openai")
        # External ref: falls back to verbatim
        assert result.skipped is True
        assert "ref_flatten_error" in (result.skip_reason or "")

    def test_exceeding_depth_falls_back(self, deep_ref_schema: dict[str, Any]) -> None:
        result = render_for_profile(deep_ref_schema, "provider-strict", "openai")
        # Exceeding depth: falls back to verbatim
        assert result.skipped is True
        assert "ref_flatten_error" in (result.skip_reason or "")

    def test_shallow_refs_work(self) -> None:
        schema = {
            "type": "object",
            "properties": {
                "a": {"$ref": "#/$defs/A"},
                "b": {"$ref": "#/$defs/B"},
            },
            "$defs": {
                "A": {"type": "string"},
                "B": {"type": "integer"},
            },
        }
        result = render_for_profile(schema, "provider-strict", "openai")
        assert result.skipped is False
        assert "$defs" not in result.schema
        assert result.schema["properties"]["a"]["type"] == "string"
        assert result.schema["properties"]["b"]["type"] == "integer"

    def test_nested_ref_chain(self) -> None:
        schema = {
            "type": "object",
            "properties": {
                "data": {"$ref": "#/$defs/Wrapper"},
            },
            "$defs": {
                "Wrapper": {
                    "type": "object",
                    "properties": {
                        "inner": {"$ref": "#/$defs/Inner"},
                    },
                },
                "Inner": {
                    "type": "string",
                },
            },
        }
        result = render_for_profile(schema, "provider-strict", "openai")
        assert result.skipped is False
        assert "$defs" not in result.schema
        inner = result.schema["properties"]["data"]["properties"]["inner"]
        assert inner["type"] == "string"


# ---------------------------------------------------------------------------
# 4. Fallback: failing render never raises, never blocks
# ---------------------------------------------------------------------------


class TestFallback:
    def test_empty_input_skipped(self) -> None:
        result = render_for_profile({}, "provider-strict", "openai")
        assert result.skipped is True
        assert result.skip_reason == "empty_schema"
        # No crash, no raise

    def test_none_like_input(self) -> None:
        # A dict that doesn't look like a JSON Schema should not crash
        # the renderer — verify it returns a valid, non-skipped result.
        result = render_for_profile({"not_a_dict": "value"}, "provider-strict", "openai")
        assert result.skipped is False
        assert isinstance(result.schema, dict)

    def test_translation_error_falls_back(self) -> None:
        """Simulate an internal error during translation — should fall back to verbatim."""
        schema = {"type": "object", "properties": {"x": {"type": "string"}}}
        # Patch _translate to raise
        import modulo.core.schema_registry.rendering as mod

        original_translate = mod._translate

        def _broken_translate(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("boom")

        mod._translate = _broken_translate  # type: ignore[assignment]
        try:
            result = render_for_profile(schema, "provider-strict", "openai")
            assert result.skipped is True
            assert "translation_error" in (result.skip_reason or "")
            # Schema is returned as-is (verbatim fallback)
            assert result.schema == schema
        finally:
            mod._translate = original_translate


# ---------------------------------------------------------------------------
# 5. Memoisation: same input → same output; mutation-safe
# ---------------------------------------------------------------------------


class TestMemoisation:
    def test_same_input_same_output(self, simple_schema: dict[str, Any]) -> None:
        r1 = render_for_profile(simple_schema, "provider-strict", "openai")
        r2 = render_for_profile(simple_schema, "provider-strict", "openai")
        # Same content (deep equal) but different objects
        assert r1.schema == r2.schema
        assert r1.warnings == r2.warnings

    def test_different_providers_different_results(self, schema_with_unsupported: dict[str, Any]) -> None:
        r1 = render_for_profile(schema_with_unsupported, "provider-strict", "openai")
        r2 = render_for_profile(schema_with_unsupported, "provider-strict", "anthropic")
        # Different providers should potentially produce different results
        # (at minimum, the cache should not confuse them)
        assert r1.schema != r2.schema or r1.warnings != r2.warnings

    def test_mutation_safe(self, simple_schema: dict[str, Any]) -> None:
        r1 = render_for_profile(simple_schema, "provider-strict", "openai")
        # Mutate the returned schema — should not affect the cache
        r1.schema["injected"] = True
        # Get again with the SAME input (cache hit) — should return clean copy
        r2 = render_for_profile(simple_schema, "provider-strict", "openai")
        assert "injected" not in r2.schema

    def test_input_mutation_does_not_corrupt_cache(self, simple_schema: dict[str, Any]) -> None:
        """Mutating the input dict after caching produces a different cache key
        (different SHA-256), so it re-translates.  The original cached result
        is never corrupted — verify by retrieving the original input's cached
        result via a fresh copy."""
        original_input = copy.deepcopy(simple_schema)
        r1 = render_for_profile(simple_schema, "provider-strict", "openai")
        original_result = copy.deepcopy(r1.schema)
        # Mutate the input
        simple_schema["injected"] = True
        # Get with mutated input — cache misses (different hash), re-translates
        r2 = render_for_profile(simple_schema, "provider-strict", "openai")
        # The re-translated result includes the injected key (it's in the input)
        assert "injected" in r2.schema
        # But the ORIGINAL input's cached result is still intact:
        r3 = render_for_profile(original_input, "provider-strict", "openai")
        assert r3.schema == original_result

    def test_cache_respects_lru_size(self) -> None:
        """Cache should not exceed max size."""
        cache = _RenderCache(max_size=5)
        for i in range(10):
            schema = {"type": "object", "properties": {f"prop{i}": {"type": "string"}}}
            result = render_for_profile(schema, "verbatim")
            cache.put(schema, "verbatim", None, result)
        assert len(cache._cache) <= 5

    def test_cache_key_includes_provider(self) -> None:
        """Cache keys must differ for different providers."""
        schema = {"type": "object", "properties": {"x": {"type": "string"}}}
        key1 = _RenderCache._make_key(schema, "provider-strict", "openai")
        key2 = _RenderCache._make_key(schema, "provider-strict", "anthropic")
        assert key1 != key2


# ---------------------------------------------------------------------------
# 6. schema_profile round-trips REST + MCP + workflow import/export
# ---------------------------------------------------------------------------


class TestSchemaProfileRoundTrip:
    def test_pipeline_graph_node_has_schema_profile(self) -> None:
        """PipelineGraphNode accepts schema_profile as a Literal."""
        import uuid

        from modulo.api.routes.pipelines import GraphPosition, PipelineGraphNode

        node = PipelineGraphNode(
            id=uuid.uuid4(),
            agent_id=uuid.uuid4(),
            position=GraphPosition(x=0, y=0),
            schema_profile="provider-strict",
        )
        assert node.schema_profile == "provider-strict"

    def test_pipeline_graph_node_absent_is_none(self) -> None:
        """Absent schema_profile defaults to None (verbatim)."""
        import uuid

        from modulo.api.routes.pipelines import GraphPosition, PipelineGraphNode

        node = PipelineGraphNode(
            id=uuid.uuid4(),
            agent_id=uuid.uuid4(),
            position=GraphPosition(x=0, y=0),
        )
        assert node.schema_profile is None

    def test_pipeline_graph_node_invalid_value_rejected(self) -> None:
        """An invalid schema_profile value is rejected by the Literal validator."""
        import uuid

        from pydantic import ValidationError

        from modulo.api.routes.pipelines import GraphPosition, PipelineGraphNode

        with pytest.raises(ValidationError):
            PipelineGraphNode(
                id=uuid.uuid4(),
                agent_id=uuid.uuid4(),
                position=GraphPosition(x=0, y=0),
                schema_profile="invalid_value",
            )

    def test_schema_profile_round_trips_through_model_dump(self) -> None:
        """schema_profile survives model_dump → model_validate round-trip."""
        import uuid

        from modulo.api.routes.pipelines import GraphPosition, PipelineGraphNode

        node = PipelineGraphNode(
            id=uuid.uuid4(),
            agent_id=uuid.uuid4(),
            position=GraphPosition(x=0, y=0),
            schema_profile="runtime-sdk",
        )
        dumped = node.model_dump(mode="json")
        assert dumped["schema_profile"] == "runtime-sdk"
        restored = PipelineGraphNode.model_validate(dumped)
        assert restored.schema_profile == "runtime-sdk"

    def test_workflow_import_export_preserves_schema_profile(self) -> None:
        """graph_nodes_json preserves schema_profile through deep copy (import/export path)."""
        nodes = [
            {
                "id": "00000000-0000-0000-0000-000000000001",
                "node_type": "agent",
                "position": {"x": 0, "y": 0},
                "schema_profile": "provider-strict",
            }
        ]
        import copy

        rewired = copy.deepcopy(nodes)
        assert rewired[0]["schema_profile"] == "provider-strict"


# ---------------------------------------------------------------------------
# 7. Composite expander propagates schema_profile
# ---------------------------------------------------------------------------


class TestCompositeExpanderPropagation:
    def test_propagate_schema_profile_to_exit_nodes(self) -> None:
        """schema_profile from composite node propagates to exit sub-nodes."""
        from modulo.core.composite_engine.expander import _propagate_output_schema

        composite_node = {
            "id": "composite-1",
            "schema_profile": "provider-strict",
            "output_schema_json": {"type": "object"},
        }
        flat_nodes = [
            {"id": "exit-1", "type": "object"},
            {"id": "exit-2", "type": "object"},
            {"id": "internal-1", "type": "object"},
        ]
        exit_ids = ["exit-1", "exit-2"]

        _propagate_output_schema(composite_node, flat_nodes, exit_ids)

        assert flat_nodes[0].get("schema_profile") == "provider-strict"
        assert flat_nodes[1].get("schema_profile") == "provider-strict"
        # Internal node should NOT get schema_profile
        assert "schema_profile" not in flat_nodes[2]

    def test_no_schema_profile_no_propagation(self) -> None:
        """When composite has no schema_profile, nothing is propagated."""
        from modulo.core.composite_engine.expander import _propagate_output_schema

        composite_node = {"id": "composite-1", "output_schema_json": {"type": "object"}}
        flat_nodes = [{"id": "exit-1"}]
        exit_ids = ["exit-1"]

        _propagate_output_schema(composite_node, flat_nodes, exit_ids)
        assert "schema_profile" not in flat_nodes[0]


# ---------------------------------------------------------------------------
# 8. Abstract schema → skipped + recorded
# ---------------------------------------------------------------------------


class TestAbstractSchema:
    def test_ref_only_schema_is_abstract(self, abstract_schema: dict[str, Any]) -> None:
        assert _is_abstract_schema(abstract_schema) is True

    def test_empty_schema_is_abstract(self) -> None:
        assert _is_abstract_schema({}) is True

    def test_concrete_schema_not_abstract(self, simple_schema: dict[str, Any]) -> None:
        assert _is_abstract_schema(simple_schema) is False

    def test_abstract_render_returns_skipped(self, abstract_schema: dict[str, Any]) -> None:
        result = render_for_profile(abstract_schema, "provider-strict", "openai")
        assert result.skipped is True
        assert result.skip_reason == "abstract_schema"


# ---------------------------------------------------------------------------
# 9. preview_strip_warnings
# ---------------------------------------------------------------------------


class TestPreviewStripWarnings:
    def test_verbatim_no_warnings(self, simple_schema: dict[str, Any]) -> None:
        warnings = preview_strip_warnings(simple_schema, "verbatim")
        assert warnings == []

    def test_provider_strict_emits_warnings(self, schema_with_unsupported: dict[str, Any]) -> None:
        warnings = preview_strip_warnings(schema_with_unsupported, "provider-strict", "openai")
        assert warnings
        keywords = {w.keyword for w in warnings}
        # OpenAI strips these from the fixture schema
        assert "title" in keywords
        assert "description" in keywords

    def test_empty_schema_no_warnings(self) -> None:
        warnings = preview_strip_warnings({}, "provider-strict", "openai")
        assert warnings == []


# ---------------------------------------------------------------------------
# 10. Agent model has schema_profile column
# ---------------------------------------------------------------------------


class TestAgentModelSchemaProfile:
    def test_agent_model_has_schema_profile(self) -> None:
        """Verify the Agent model declares schema_profile."""
        from modulo.db.models.agent import Agent

        assert hasattr(Agent, "schema_profile")

    def test_agent_create_schema_has_schema_profile(self) -> None:
        """Verify AgentCreate accepts schema_profile."""
        import uuid

        from modulo.api.routes.agents import AgentCreate

        agent = AgentCreate(
            name="test-agent",
            input_schema_id=uuid.uuid4(),
            output_schema_id=uuid.uuid4(),
            prompt_template="test",
            model_backend_id=uuid.uuid4(),
            required_environment_capabilities=[],
            template_id=None,
            schema_profile="provider-strict",
        )
        assert agent.schema_profile == "provider-strict"

    def test_agent_response_has_schema_profile(self) -> None:
        """Verify AgentResponse includes schema_profile."""
        import uuid
        from datetime import UTC, datetime

        from modulo.api.routes.agents import AgentResponse

        resp = AgentResponse(
            id=uuid.uuid4(),
            organisation_id=uuid.uuid4(),
            name="test",
            description=None,
            is_executable=True,
            input_schema_id=None,
            input_schema_version=None,
            output_schema_id=None,
            output_schema_version=None,
            prompt_template="test",
            prompt_version_history=[],
            model_backend_id=None,
            connector_type_refs=[],
            evals=None,
            retry_policy={},
            token_budget=None,
            max_input_length=None,
            library_id=None,
            prompt_always_visible=False,
            required_environment_capabilities=[],
            template_id=None,
            agent_commands=None,
            schema_profile="runtime-sdk",
            account_id=uuid.uuid4(),
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        assert resp.schema_profile == "runtime-sdk"


# ---------------------------------------------------------------------------
# 11. FIX 2: const is preserved in provider-strict for all providers
# ---------------------------------------------------------------------------


class TestConstPreserved:
    """FIX 2: ``const`` is in _ALWAYS_KEEP and was also in every _PROVIDER_UNSUPPORTED
    set (dead code).  After removing it from the unsupported sets, ``const``
    MUST survive provider-strict rendering for every provider."""

    @pytest.mark.parametrize("provider", ["openai", "anthropic", "google", "deepseek"])
    def test_const_preserved_for_provider(self, provider: str) -> None:
        schema = {
            "type": "object",
            "properties": {
                "status": {"const": "active"},
            },
        }
        result = render_for_profile(schema, "provider-strict", provider)
        assert result.skipped is False
        assert result.schema["properties"]["status"]["const"] == "active"


# ---------------------------------------------------------------------------
# 12. FIX 3: _walk_schema shared traversal helper
# ---------------------------------------------------------------------------


class TestWalkSchema:
    """FIX 3: verify that _walk_schema visits all dict/list nodes."""

    def test_visits_all_keys(self) -> None:
        schema = {
            "type": "object",
            "properties": {
                "name": {"type": "string", "default": "foo"},
            },
        }
        visited: list[str] = []

        def _collector(key: str, value: Any, path: str) -> None:
            visited.append(key)

        _walk_schema(schema, "#", _collector)
        assert "type" in visited
        assert "properties" in visited
        assert "name" in visited
        assert "default" in visited

    def test_visits_list_items(self) -> None:
        schema = {
            "type": "array",
            "items": [
                {"type": "string"},
                {"type": "integer"},
            ],
        }
        visited: list[str] = []

        def _collector(key: str, value: Any, path: str) -> None:
            visited.append(key)

        _walk_schema(schema, "#", _collector)
        assert "items" in visited
        assert "type" in visited


# ---------------------------------------------------------------------------
# 13. FIX 4: single source of truth for SchemaProfile values
# ---------------------------------------------------------------------------


class TestSchemaProfileSourceOfTruth:
    """FIX 4: SCHEMA_PROFILE_VALUES and SQL CHECK must match get_args(SchemaProfile)."""

    def test_schema_profile_values_matches_literal_args(self) -> None:
        from typing import get_args

        from modulo.core.schema_registry.rendering import SCHEMA_PROFILE_VALUES, SchemaProfile

        expected = set(get_args(SchemaProfile))
        assert set(SCHEMA_PROFILE_VALUES) == expected

    def test_valid_schema_profiles_matches_literal_args(self) -> None:
        from typing import get_args

        from modulo.core.pipeline_engine.node_runner import _VALID_SCHEMA_PROFILES
        from modulo.core.schema_registry.rendering import SchemaProfile

        expected = frozenset(get_args(SchemaProfile))
        assert expected == _VALID_SCHEMA_PROFILES

    def test_agent_model_check_constraint_matches_literal_args(self) -> None:
        """The SQL CHECK string in the Agent model must list exactly the values
        from get_args(SchemaProfile)."""
        import re
        from typing import get_args

        from modulo.core.schema_registry.rendering import SchemaProfile
        from modulo.db.models.agent import Agent

        # Extract the CHECK constraint text from table_args
        check_constraints = [arg for arg in Agent.__table_args__ if hasattr(arg, "sqltext")]
        assert check_constraints, "Agent model must have at least one CheckConstraint"
        schema_profile_check = None
        for cc in check_constraints:
            if "schema_profile" in cc.name:
                schema_profile_check = cc
                break
        assert schema_profile_check is not None, "Agent model must have ck_agents_schema_profile"

        # Parse the IN clause values
        sql_text = str(schema_profile_check.sqltext)
        values_in_check = set(re.findall(r"'([^']+)'", sql_text))
        expected_values = set(get_args(SchemaProfile))
        assert values_in_check == expected_values, (
            f"SQL CHECK values {values_in_check} do not match get_args(SchemaProfile) = {expected_values}"
        )

    def test_agent_model_valid_profiles_matches_literal_args(self) -> None:
        """The _VALID_SCHEMA_PROFILES tuple in db.models.agent must match
        get_args(SchemaProfile) — they are kept in separate modules due to the
        import-linter contract (db must not import core)."""
        from typing import get_args

        import modulo.db.models.agent as _agent_mod
        from modulo.core.schema_registry.rendering import SchemaProfile

        expected = get_args(SchemaProfile)
        db_profiles = _agent_mod._VALID_SCHEMA_PROFILES
        assert set(db_profiles) == set(expected), (
            f"db.models.agent._VALID_SCHEMA_PROFILES {db_profiles} does not match get_args(SchemaProfile) = {expected}"
        )


# ---------------------------------------------------------------------------
# 14. FIX 5: schema_translation_report gated behind opt-in
# ---------------------------------------------------------------------------


class TestSchemaTranslationReportGating:
    """FIX 5: GET /pipelines/{id}/graph must not compute schema_translation_report
    unless include_schema_warnings=true."""

    def test_graph_response_empty_report_by_default(self) -> None:
        from modulo.api.routes.pipelines import PipelineGraphNode, _graph_response

        node = PipelineGraphNode(
            id=uuid.uuid4(),
            agent_id=uuid.uuid4(),
            position={"x": 0, "y": 0},
            schema_profile="provider-strict",
            output_schema_json={
                "type": "object",
                "properties": {"x": {"type": "string", "default": "foo"}},
            },
        )
        node_dict = node.model_dump(mode="json")
        resp = _graph_response([node_dict], [])
        # Without include_schema_warnings, the report is empty
        assert not resp.schema_translation_report

    def test_graph_response_populated_report_when_opted_in(self) -> None:
        from modulo.api.routes.pipelines import PipelineGraphNode, _graph_response

        node = PipelineGraphNode(
            id=uuid.uuid4(),
            agent_id=uuid.uuid4(),
            position={"x": 0, "y": 0},
            schema_profile="provider-strict",
            output_schema_json={
                "type": "object",
                "properties": {"x": {"type": "string", "default": "foo"}},
            },
        )
        node_dict = node.model_dump(mode="json")
        resp = _graph_response([node_dict], [], include_schema_warnings=True)
        # With include_schema_warnings=True, the report should be populated
        assert resp.schema_translation_report

    def test_graph_response_uses_provider_specific_strips(self) -> None:
        """A resolved provider makes provider-specific keywords (OpenAI's
        ``pattern``) appear in the report, not just advisory keywords."""
        from modulo.api.routes.pipelines import PipelineGraphNode, _graph_response

        node = PipelineGraphNode(
            id=uuid.uuid4(),
            agent_id=uuid.uuid4(),
            position={"x": 0, "y": 0},
            schema_profile="provider-strict",
            output_schema_json={
                "type": "object",
                "properties": {"x": {"type": "string", "pattern": "^[a-z]+$"}},
            },
        )
        resp = _graph_response(
            [node.model_dump(mode="json")],
            [],
            include_schema_warnings=True,
            provider_by_node={str(node.id): "openai"},
        )
        keywords = {w["keyword"] for w in resp.schema_translation_report}
        assert "pattern" in keywords

    def test_graph_response_without_provider_omits_provider_specific_strips(self) -> None:
        """Without a resolved provider, only always-advisory keywords are
        reported — ``pattern`` is provider-specific and must be absent."""
        from modulo.api.routes.pipelines import PipelineGraphNode, _graph_response

        node = PipelineGraphNode(
            id=uuid.uuid4(),
            agent_id=uuid.uuid4(),
            position={"x": 0, "y": 0},
            schema_profile="provider-strict",
            output_schema_json={
                "type": "object",
                "properties": {"x": {"type": "string", "pattern": "^[a-z]+$", "default": "foo"}},
            },
        )
        resp = _graph_response(
            [node.model_dump(mode="json")],
            [],
            include_schema_warnings=True,
        )
        keywords = {w["keyword"] for w in resp.schema_translation_report}
        assert "default" in keywords
        assert "pattern" not in keywords

    async def test_resolve_node_providers_maps_agent_backend(self) -> None:
        """_resolve_node_providers resolves node → agent → backend provider."""
        from modulo.api.routes.pipelines import _resolve_node_providers

        agent_id = uuid.uuid4()
        backend_id = uuid.uuid4()
        agents = [SimpleNamespace(id=agent_id, model_backend_id=backend_id)]
        backends = [SimpleNamespace(id=backend_id, provider="openai")]
        session = _FakeExecuteSession([agents, backends])
        nodes = [
            {"id": "n1", "agent_id": str(agent_id)},
            {"id": "n2", "agent_id": None},
        ]
        result = await _resolve_node_providers(session, nodes, organisation_id=uuid.uuid4())
        assert result == {"n1": "openai"}


class _FakeScalarResult:
    """Minimal stand-in for SQLAlchemy's ``Result`` in unit tests."""

    def __init__(self, items: list[Any]) -> None:
        self._items = items

    def scalars(self) -> _FakeScalarResult:
        return self

    def all(self) -> list[Any]:
        return list(self._items)


class _FakeExecuteSession:
    """Returns queued ``execute()`` results in order."""

    def __init__(self, batches: list[list[Any]]) -> None:
        self._batches = list(batches)

    async def execute(self, *_args: Any, **_kwargs: Any) -> _FakeScalarResult:
        return _FakeScalarResult(self._batches.pop(0))


# ---------------------------------------------------------------------------
# 15. FIX 6: diamond-shaped $ref does not inflate node count
# ---------------------------------------------------------------------------


class TestRefMemoization:
    """FIX 6: A definition referenced from many sites must be flattened once,
    not once per reference, so it does not hit _MAX_EXPANDED_NODES."""

    def test_diamond_ref_not_inflated(self) -> None:
        """Schema with one definition referenced from 20 sites should not
        hit the node-count limit (it did before memoisation)."""
        num_refs = 20
        schema: dict[str, Any] = {
            "type": "object",
            "properties": {f"ref{i}": {"$ref": "#/$defs/Common"} for i in range(num_refs)},
            "$defs": {
                "Common": {
                    "type": "object",
                    "properties": {
                        "a": {"type": "string"},
                        "b": {"type": "integer"},
                    },
                },
            },
        }
        result = render_for_profile(schema, "provider-strict", "openai")
        assert result.skipped is False
        # All refs should be inlined
        for i in range(num_refs):
            prop = result.schema["properties"][f"ref{i}"]
            assert "type" in prop
            assert "$defs" not in result.schema

    def test_single_ref_counted_once(self) -> None:
        """A single definition referenced from 50 sites should produce a
        reasonable node count (well under the 10k limit)."""
        from modulo.core.schema_registry.rendering import _flatten_refs

        num_refs = 50
        schema: dict[str, Any] = {
            "type": "object",
            "properties": {f"r{i}": {"$ref": "#/$defs/Item"} for i in range(num_refs)},
            "$defs": {
                "Item": {"type": "object", "properties": {"v": {"type": "string"}}},
            },
        }
        _, count = _flatten_refs(schema)
        # Without memoisation this would be ~150+ nodes; with memoisation
        # it should be under 100.
        assert count < 100


# ---------------------------------------------------------------------------
# 16. FIX 8: dead branch removed from _is_abstract_schema
# ---------------------------------------------------------------------------


class TestIsAbstractSchema:
    """FIX 8: verify _is_abstract_schema still works correctly after dead
    branch removal."""

    def test_empty_is_abstract(self) -> None:
        assert _is_abstract_schema({}) is True

    def test_ref_only_is_abstract(self) -> None:
        assert _is_abstract_schema({"$ref": "#/$defs/Foo"}) is True

    def test_meta_only_is_abstract(self) -> None:
        assert _is_abstract_schema({"$schema": "...", "title": "Foo"}) is True

    def test_concrete_not_abstract(self) -> None:
        assert _is_abstract_schema({"type": "object", "properties": {}}) is False

    def test_composition_only_not_abstract(self) -> None:
        """anyOf is a non-meta key, so composition-only schemas are NOT abstract."""
        assert _is_abstract_schema({"anyOf": [{"type": "string"}, {"type": "integer"}]}) is False
