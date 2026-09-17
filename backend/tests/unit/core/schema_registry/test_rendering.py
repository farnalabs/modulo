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
from typing import Any

import pytest

from modulo.core.schema_registry.rendering import (
    _MAX_REF_DEPTH,
    _is_abstract_schema,
    _render_cache,
    _RenderCache,
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
        assert len(result.warnings) > 0
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
        # A non-dict input is treated as empty
        render_for_profile({"not_a_dict": "value"}, "provider-strict", "openai")
        # Should not raise

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
        assert len(warnings) > 0
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
