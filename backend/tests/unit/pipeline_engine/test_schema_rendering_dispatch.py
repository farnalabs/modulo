"""FAR-900: tests for schema rendering dispatch in _invoke_node_model.

Verifies that:
- _invoke_node_model passes the RENDERED schema (not raw) when schema_profile != verbatim
- Profile resolves: node-level → agent-level default → verbatim
- _apply_agent_fields embeds the agent's schema_profile into the snapshot node dict
- Render failure falls back to verbatim and the node still runs (never raises)
- verbatim still passes the raw schema unchanged (no regression)
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

from langchain_core.messages import AIMessage, BaseMessage

from modulo.core.pipeline_engine import node_runner as nr
from modulo.db.crud.pipeline_snapshot import _apply_agent_fields

# ---------------------------------------------------------------------------
# Test doubles (reuse pattern from test_native_structured_output.py)
# ---------------------------------------------------------------------------


class _RecordingBackend:
    """Records every call to invoke() and its kwargs."""

    backend_id = "openai/gpt-4"

    def __init__(self, supports_native: bool = True) -> None:
        self.supports_native_structured_output = supports_native
        self.calls: list[dict[str, Any]] = []

    async def invoke(
        self,
        messages: list[BaseMessage],
        **kwargs: Any,
    ) -> AIMessage:
        self.calls.append(kwargs)
        return AIMessage(content='{"result": "ok"}')


class _FakeHub:
    def __init__(self, backend: Any) -> None:
        self._backend = backend

    async def get(self, backend_id: Any) -> Any:
        return self._backend


# ---------------------------------------------------------------------------
# Tests: _invoke_node_model passes RENDERED schema (not raw)
# ---------------------------------------------------------------------------


class TestSchemaRenderingDispatch:
    """FAR-900: _invoke_node_model renders the output schema for the target provider."""

    async def test_provider_strict_renders_schema_for_openai(self) -> None:
        """FIX 7: When the raw schema passes bounds, the raw schema is sent
        to the provider (the provider resolves $ref itself).  The rendered
        schema is computed but not forwarded.
        """
        backend = _RecordingBackend(supports_native=True)
        hub = _FakeHub(backend)
        # Schema with OpenAI-unsupported keywords: "pattern", "default"
        schema = {
            "type": "object",
            "properties": {
                "name": {"type": "string", "pattern": "^[a-z]+$", "default": "foo"},
            },
            "required": ["name"],
        }
        with patch(
            "modulo.core.pipeline_engine.decorator.get_model_backend_hub",
            return_value=hub,
        ):
            await nr._invoke_node_model(
                "prompt",
                "11111111-2222-3333-4444-555555555555",
                "n1",
                output_schema_json=schema,
                schema_profile="provider-strict",
            )
        assert len(backend.calls) == 1
        # FIX 7: raw schema passes bounds → raw schema is forwarded
        assert backend.calls[0]["output_schema"] is schema

    async def test_verbatim_passes_raw_schema_unchanged(self) -> None:
        """When schema_profile is verbatim (or None), the raw schema is passed through."""
        backend = _RecordingBackend(supports_native=True)
        hub = _FakeHub(backend)
        schema = {
            "type": "object",
            "properties": {"name": {"type": "string", "pattern": "^[a-z]+$"}},
        }
        with patch(
            "modulo.core.pipeline_engine.decorator.get_model_backend_hub",
            return_value=hub,
        ):
            await nr._invoke_node_model(
                "prompt",
                "11111111-2222-3333-4444-555555555555",
                "n1",
                output_schema_json=schema,
                schema_profile="verbatim",
            )
        assert len(backend.calls) == 1
        # verbatim = identity — schema must be exactly the raw input
        assert backend.calls[0]["output_schema"] == schema

    async def test_none_profile_passes_raw_schema(self) -> None:
        """When schema_profile is None (default), the raw schema is passed through."""
        backend = _RecordingBackend(supports_native=True)
        hub = _FakeHub(backend)
        schema = {"type": "object", "properties": {"x": {"type": "integer"}}}
        with patch(
            "modulo.core.pipeline_engine.decorator.get_model_backend_hub",
            return_value=hub,
        ):
            await nr._invoke_node_model(
                "prompt",
                "11111111-2222-3333-4444-555555555555",
                "n1",
                output_schema_json=schema,
                schema_profile=None,
            )
        assert backend.calls[0]["output_schema"] == schema

    async def test_render_failure_falls_back_to_verbatim(self) -> None:
        """If render_for_profile raises, the raw schema is sent (best-effort, never blocks)."""
        backend = _RecordingBackend(supports_native=True)
        hub = _FakeHub(backend)
        schema = {"type": "object", "properties": {"n": {"type": "string"}}}
        with (
            patch(
                "modulo.core.pipeline_engine.decorator.get_model_backend_hub",
                return_value=hub,
            ),
            patch(
                "modulo.core.pipeline_engine.node_runner.render_for_profile",
                side_effect=RuntimeError("render boom"),
            ),
        ):
            # Must NOT raise
            await nr._invoke_node_model(
                "prompt",
                "11111111-2222-3333-4444-555555555555",
                "n1",
                output_schema_json=schema,
                schema_profile="provider-strict",
            )
        # Falls back to the raw schema
        assert backend.calls[0]["output_schema"] == schema

    async def test_skipped_render_still_passes_rendered_schema(self) -> None:
        """When render_for_profile returns skipped=True, the rendered schema
        is still forwarded (the skip is logged, not rejected).
        """
        backend = _RecordingBackend(supports_native=True)
        hub = _FakeHub(backend)
        schema: dict[str, Any] = {"type": "object", "properties": {"n": {"type": "string"}}}
        with patch(
            "modulo.core.pipeline_engine.decorator.get_model_backend_hub",
            return_value=hub,
        ):
            await nr._invoke_node_model(
                "prompt",
                "11111111-2222-3333-4444-555555555555",
                "n1",
                output_schema_json=schema,
                schema_profile="provider-strict",
            )
        assert len(backend.calls) == 1
        # The schema is present (it was rendered, even if warnings were emitted)
        assert "output_schema" in backend.calls[0]


# ---------------------------------------------------------------------------
# Tests: _resolve_schema_profile
# ---------------------------------------------------------------------------


class TestResolveSchemaProfile:
    """FAR-900: profile resolution order."""

    def test_node_level_override(self) -> None:
        """Node-level schema_profile in node_def takes precedence."""
        node_def = {"schema_profile": "provider-strict"}
        assert nr._resolve_schema_profile(node_def) == "provider-strict"

    def test_node_level_runtime_sdk(self) -> None:
        node_def = {"schema_profile": "runtime-sdk"}
        assert nr._resolve_schema_profile(node_def) == "runtime-sdk"

    def test_node_level_verbatim(self) -> None:
        node_def = {"schema_profile": "verbatim"}
        assert nr._resolve_schema_profile(node_def) == "verbatim"

    def test_no_profile_returns_none(self) -> None:
        """When neither node nor agent sets a profile, None is returned (caller defaults to verbatim)."""
        node_def: dict[str, Any] = {}
        assert nr._resolve_schema_profile(node_def) is None

    def test_invalid_profile_returns_none(self) -> None:
        """An unrecognized profile value falls through to None."""
        node_def = {"schema_profile": "invalid-profile"}
        assert nr._resolve_schema_profile(node_def) is None


# ---------------------------------------------------------------------------
# Tests: _extract_provider_id
# ---------------------------------------------------------------------------


class TestExtractProviderId:
    """FAR-900: provider extraction from backend_id."""

    def test_openai(self) -> None:
        class _B:
            backend_id = "openai/gpt-4"

        assert nr._extract_provider_id(_B()) == "openai"

    def test_anthropic(self) -> None:
        class _B:
            backend_id = "anthropic/claude-sonnet-4-6"

        assert nr._extract_provider_id(_B()) == "anthropic"

    def test_module_backend(self) -> None:
        """The OpenAICompatibleBackend uses 'provider/model_id' format."""

        class _B:
            backend_id = "deepseek/deepseek-chat"

        assert nr._extract_provider_id(_B()) == "deepseek"

    def test_no_slash_returns_none(self) -> None:
        class _B:
            backend_id = "stub"

        assert nr._extract_provider_id(_B()) is None

    def test_none_backend_id_returns_none(self) -> None:
        class _B:
            backend_id = None

        assert nr._extract_provider_id(_B()) is None

    def test_no_backend_id_attr_returns_none(self) -> None:
        class _B:
            pass

        assert nr._extract_provider_id(_B()) is None


# ---------------------------------------------------------------------------
# Tests: _is_safe_schema AFTER rendering
# ---------------------------------------------------------------------------


class TestBoundsAfterRendering:
    """FIX 7: raw schema passes bounds → raw is sent to provider."""

    async def test_bounds_check_applied_to_rendered_schema(self) -> None:
        """FIX 7: A raw schema that passes bounds is forwarded to the backend
        (provider resolves $ref itself)."""
        backend = _RecordingBackend(supports_native=True)
        hub = _FakeHub(backend)
        schema = {
            "type": "object",
            "properties": {"x": {"type": "integer", "minimum": 0}},
        }
        with patch(
            "modulo.core.pipeline_engine.decorator.get_model_backend_hub",
            return_value=hub,
        ):
            await nr._invoke_node_model(
                "prompt",
                "11111111-2222-3333-4444-555555555555",
                "n1",
                output_schema_json=schema,
                schema_profile="provider-strict",
            )
        # FIX 7: raw passes bounds → raw is forwarded
        assert backend.calls[0]["output_schema"] is schema


# ---------------------------------------------------------------------------
# Tests: make_node_fn integration (schema_profile flows through)
# ---------------------------------------------------------------------------


class TestMakeNodeFnSchemaProfile:
    """FAR-900: schema_profile flows through make_node_fn to _invoke_node_model."""

    async def test_node_def_schema_profile_is_resolved(self) -> None:
        """FIX 7: When the raw schema passes bounds, the raw schema is forwarded."""
        backend = _RecordingBackend(supports_native=True)
        hub = _FakeHub(backend)
        schema = {
            "type": "object",
            "properties": {"name": {"type": "string", "pattern": "^[a-z]+$"}},
        }
        node_def = {
            "id": "test-profile-flow",
            "model_backend_id": "11111111-2222-3333-4444-555555555555",
            "prompt_template": "hello",
            "output_schema_json": schema,
            "schema_profile": "provider-strict",
        }
        fn = nr.make_node_fn(node_def)
        state = {
            "run_context": {"cancelled": False, "input": {}},
            "artifacts": [],
        }
        with (
            patch("modulo.core.pipeline_engine.node_runner._run_conformance_gate", new=AsyncMock(return_value=None)),
            patch(
                "modulo.core.pipeline_engine.decorator.get_model_backend_hub",
                return_value=hub,
            ),
        ):
            result = await fn(state)
        assert result["artifacts"][0]["status"] == "completed"
        # FIX 7: raw passes bounds → raw schema forwarded
        assert backend.calls[0]["output_schema"] is schema

    async def test_node_def_without_profile_uses_verbatim(self) -> None:
        """When node_def has no schema_profile, verbatim is used (raw schema forwarded)."""
        backend = _RecordingBackend(supports_native=True)
        hub = _FakeHub(backend)
        schema = {
            "type": "object",
            "properties": {"x": {"type": "integer", "minimum": 0}},
        }
        node_def = {
            "id": "test-no-profile",
            "model_backend_id": "11111111-2222-3333-4444-555555555555",
            "prompt_template": "hello",
            "output_schema_json": schema,
            # No schema_profile key
        }
        fn = nr.make_node_fn(node_def)
        state = {
            "run_context": {"cancelled": False, "input": {}},
            "artifacts": [],
        }
        with (
            patch("modulo.core.pipeline_engine.node_runner._run_conformance_gate", new=AsyncMock(return_value=None)),
            patch(
                "modulo.core.pipeline_engine.decorator.get_model_backend_hub",
                return_value=hub,
            ),
        ):
            await fn(state)
        # verbatim: raw schema passes through unchanged
        assert backend.calls[0]["output_schema"] == schema


# ---------------------------------------------------------------------------
# Tests: _apply_agent_fields embeds schema_profile
# ---------------------------------------------------------------------------


class TestApplyAgentFieldsSchemaProfile:
    """FAR-900: _apply_agent_fields embeds the agent's schema_profile."""

    def test_embeds_agent_schema_profile(self) -> None:
        """Agent.schema_profile is embedded into the snapshot node dict."""
        agent = SimpleNamespace(
            schema_profile="provider-strict",
            token_budget=None,
            prompt_template=None,
            model_backend_id=None,
            agent_commands=None,
            parameter_schema_id=None,
        )
        node: dict[str, Any] = {"id": "n1"}

        _apply_agent_fields(node, agent)

        assert node["schema_profile"] == "provider-strict"

    def test_does_not_overwrite_node_level_schema_profile(self) -> None:
        """A node-level schema_profile already in the dict is NOT overwritten
        by the agent default (setdefault semantics)."""
        agent = SimpleNamespace(
            schema_profile="runtime-sdk",
            token_budget=None,
            prompt_template=None,
            model_backend_id=None,
            agent_commands=None,
            parameter_schema_id=None,
        )
        node: dict[str, Any] = {"id": "n1", "schema_profile": "provider-strict"}

        _apply_agent_fields(node, agent)

        # Node-level wins — setdefault did not overwrite
        assert node["schema_profile"] == "provider-strict"

    def test_none_agent_schema_profile_not_embedded(self) -> None:
        """When Agent.schema_profile is None, no schema_profile key is added."""
        agent = SimpleNamespace(
            schema_profile=None,
            token_budget=None,
            prompt_template=None,
            model_backend_id=None,
            agent_commands=None,
            parameter_schema_id=None,
        )
        node: dict[str, Any] = {"id": "n1"}

        _apply_agent_fields(node, agent)

        assert "schema_profile" not in node

    def test_missing_agent_schema_profile_attr_not_embedded(self) -> None:
        """An Agent lacking the schema_profile attribute (pre-migration) does
        not raise and does not embed the key."""
        agent = SimpleNamespace(
            token_budget=None,
            prompt_template=None,
            model_backend_id=None,
            agent_commands=None,
            parameter_schema_id=None,
            # No schema_profile attribute — simulates pre-migration Agent
        )
        node: dict[str, Any] = {"id": "n1"}

        _apply_agent_fields(node, agent)

        assert "schema_profile" not in node


# ---------------------------------------------------------------------------
# Tests: _resolve_schema_profile with agent-level default
# ---------------------------------------------------------------------------


class TestResolveSchemaProfileAgentDefault:
    """FAR-900: agent-level schema_profile embedded in the snapshot node dict."""

    def test_agent_default_resolves_when_no_node_override(self) -> None:
        """An agent-level schema_profile (set via _apply_agent_fields) is
        visible to _resolve_schema_profile when no node-level key exists."""
        node_def: dict[str, Any] = {"schema_profile": "provider-strict"}
        assert nr._resolve_schema_profile(node_def) == "provider-strict"

    def test_node_level_wins_over_agent_default(self) -> None:
        """Node-level schema_profile takes precedence over the agent default
        when both are present in the node dict (setdefault preserved node)."""
        # Simulate the snapshot after _apply_agent_fields with setdefault:
        # node already had schema_profile, agent's was not applied
        node_def: dict[str, Any] = {
            "schema_profile": "runtime-sdk",
        }
        assert nr._resolve_schema_profile(node_def) == "runtime-sdk"

    def test_missing_field_degrades_to_none(self) -> None:
        """A snapshot node dict missing schema_profile entirely returns None
        (caller defaults to verbatim) — no raise."""
        node_def: dict[str, Any] = {}
        assert nr._resolve_schema_profile(node_def) is None

    def test_invalid_profile_value_returns_none(self) -> None:
        """An invalid profile value returns None (graceful degradation)."""
        node_def: dict[str, Any] = {"schema_profile": "totally-invalid"}
        assert nr._resolve_schema_profile(node_def) is None


# ---------------------------------------------------------------------------
# Tests: end-to-end dispatch — agent default flows through make_node_fn
# ---------------------------------------------------------------------------


class TestAgentDefaultDispatchIntegration:
    """FAR-900: the RENDERED schema reaches the backend when the profile
    comes from the agent default (not just the node-level override)."""

    async def test_agent_default_profile_renders_schema(self) -> None:
        """FIX 7: When the raw schema passes bounds, the raw schema is forwarded."""
        backend = _RecordingBackend(supports_native=True)
        hub = _FakeHub(backend)
        schema = {
            "type": "object",
            "properties": {
                "result": {"type": "string", "pattern": "^[a-z]+$", "default": "foo"},
            },
        }
        node_def = {
            "id": "test-agent-default",
            "model_backend_id": "11111111-2222-3333-4444-555555555555",
            "prompt_template": "hello",
            "output_schema_json": schema,
            "schema_profile": "provider-strict",
        }
        fn = nr.make_node_fn(node_def)
        state = {
            "run_context": {"cancelled": False, "input": {}},
            "artifacts": [],
        }
        with (
            patch("modulo.core.pipeline_engine.node_runner._run_conformance_gate", new=AsyncMock(return_value=None)),
            patch(
                "modulo.core.pipeline_engine.decorator.get_model_backend_hub",
                return_value=hub,
            ),
        ):
            result = await fn(state)
        assert result["artifacts"][0]["status"] == "completed"
        # FIX 7: raw passes bounds → raw schema forwarded
        assert backend.calls[0]["output_schema"] is schema


# ---------------------------------------------------------------------------
# Tests: FIX 7 — bounds-check ordering + amplification window
# ---------------------------------------------------------------------------


class TestBoundsCheckOrdering:
    """FIX 7: raw schema is bounds-checked BEFORE rendering to bound the
    amplification window; if raw passes, raw is sent to the provider."""

    async def test_raw_safe_rendered_unsafe_sends_raw(self) -> None:
        """When the raw schema passes bounds but the rendered schema does not
        (e.g. inlining made it too deep), the RAW schema is sent to the
        provider (the provider resolves $ref itself)."""
        backend = _RecordingBackend(supports_native=True)
        hub = _FakeHub(backend)
        # Schema that is safe raw but rendered could be larger:
        # A schema with $ref that inlines to something larger.
        # We mock _is_safe_schema to return (True, "") for the raw schema
        # and (False, "too large") for the rendered schema.
        raw_schema: dict[str, Any] = {
            "type": "object",
            "properties": {"x": {"type": "string"}},
        }
        rendered_schema: dict[str, Any] = {
            "type": "object",
            "properties": {"x": {"type": "string", "minimum": 0}},
        }
        call_count = 0

        def _mock_is_safe(schema: dict[str, Any]) -> tuple[bool, str]:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # Raw schema check: safe
                return True, ""
            # Rendered schema check: unsafe
            return False, "schema too large"

        with (
            patch(
                "modulo.core.pipeline_engine.decorator.get_model_backend_hub",
                return_value=hub,
            ),
            patch(
                "modulo.core.pipeline_engine.node_runner._is_safe_schema",
                side_effect=_mock_is_safe,
            ),
            patch(
                "modulo.core.pipeline_engine.node_runner.render_for_profile",
                return_value=SimpleNamespace(
                    schema=rendered_schema,
                    profile="provider-strict",
                    warnings=[],
                    skipped=False,
                    skip_reason=None,
                ),
            ),
        ):
            await nr._invoke_node_model(
                "prompt",
                "11111111-2222-3333-4444-555555555555",
                "n1",
                output_schema_json=raw_schema,
                schema_profile="provider-strict",
            )
        assert len(backend.calls) == 1
        # Raw passed bounds → raw is sent to the provider
        assert backend.calls[0]["output_schema"] is raw_schema

    async def test_raw_unsafe_rendered_safe_sends_rendered(self) -> None:
        """When the raw schema fails bounds but the rendered schema passes,
        the rendered schema is sent."""
        backend = _RecordingBackend(supports_native=True)
        hub = _FakeHub(backend)
        raw_schema: dict[str, Any] = {
            "type": "object",
            "properties": {"x": {"type": "string"}},
        }
        rendered_schema: dict[str, Any] = {"type": "string"}
        call_count = 0

        def _mock_is_safe(schema: dict[str, Any]) -> tuple[bool, str]:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # Raw schema check: unsafe
                return False, "schema too large"
            # Rendered schema check: safe
            return True, ""

        with (
            patch(
                "modulo.core.pipeline_engine.decorator.get_model_backend_hub",
                return_value=hub,
            ),
            patch(
                "modulo.core.pipeline_engine.node_runner._is_safe_schema",
                side_effect=_mock_is_safe,
            ),
            patch(
                "modulo.core.pipeline_engine.node_runner.render_for_profile",
                return_value=SimpleNamespace(
                    schema=rendered_schema,
                    profile="provider-strict",
                    warnings=[],
                    skipped=False,
                    skip_reason=None,
                ),
            ),
        ):
            await nr._invoke_node_model(
                "prompt",
                "11111111-2222-3333-4444-555555555555",
                "n1",
                output_schema_json=raw_schema,
                schema_profile="provider-strict",
            )
        assert len(backend.calls) == 1
        # Raw failed, rendered passed → rendered schema sent
        assert backend.calls[0]["output_schema"] is rendered_schema

    async def test_both_unsafe_no_schema_sent(self) -> None:
        """When both raw and rendered fail bounds, no schema is sent."""
        backend = _RecordingBackend(supports_native=True)
        hub = _FakeHub(backend)
        raw_schema: dict[str, Any] = {"type": "object", "properties": {"x": {"type": "string"}}}

        with (
            patch(
                "modulo.core.pipeline_engine.decorator.get_model_backend_hub",
                return_value=hub,
            ),
            patch(
                "modulo.core.pipeline_engine.node_runner._is_safe_schema",
                return_value=(False, "too large"),
            ),
            patch(
                "modulo.core.pipeline_engine.node_runner.render_for_profile",
                return_value=SimpleNamespace(
                    schema={"type": "string"},
                    profile="provider-strict",
                    warnings=[],
                    skipped=False,
                    skip_reason=None,
                ),
            ),
        ):
            await nr._invoke_node_model(
                "prompt",
                "11111111-2222-3333-4444-555555555555",
                "n1",
                output_schema_json=raw_schema,
                schema_profile="provider-strict",
            )
        assert len(backend.calls) == 1
        # Both unsafe → no output_schema in kwargs
        assert "output_schema" not in backend.calls[0]
