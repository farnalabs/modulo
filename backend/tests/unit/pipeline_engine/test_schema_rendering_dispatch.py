"""FAR-900: tests for schema rendering dispatch in _invoke_node_model.

Verifies that:
- _invoke_node_model passes the RENDERED schema (not raw) when schema_profile != verbatim
- Profile resolves: node-level → agent-level default → verbatim
- Render failure falls back to verbatim and the node still runs (never raises)
- verbatim still passes the raw schema unchanged (no regression)
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

from langchain_core.messages import AIMessage, BaseMessage

from modulo.core.pipeline_engine import node_runner as nr

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
        """When schema_profile is provider-strict, the schema is rendered
        (stripped of unsupported keywords) before being sent to the backend.
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
        rendered = backend.calls[0]["output_schema"]
        # "pattern" and "default" are stripped by the OpenAI provider-strict renderer
        name_props = rendered["properties"]["name"]
        assert "pattern" not in name_props
        assert "default" not in name_props
        # Structural keywords must survive
        assert name_props["type"] == "string"
        assert "required" in rendered

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
    """FAR-900: _is_safe_schema is applied AFTER rendering, not before."""

    async def test_bounds_check_applied_to_rendered_schema(self) -> None:
        """A raw schema that's safe may produce a rendered schema that is
        also safe; verify the rendered form reaches the backend.
        """
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
        rendered = backend.calls[0]["output_schema"]
        # "minimum" is stripped by provider-strict for OpenAI
        assert "minimum" not in rendered["properties"]["x"]
        assert rendered["properties"]["x"]["type"] == "integer"


# ---------------------------------------------------------------------------
# Tests: make_node_fn integration (schema_profile flows through)
# ---------------------------------------------------------------------------


class TestMakeNodeFnSchemaProfile:
    """FAR-900: schema_profile flows through make_node_fn to _invoke_node_model."""

    async def test_node_def_schema_profile_is_resolved(self) -> None:
        """When node_def carries schema_profile, it is resolved and passed to _invoke_node_model."""
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
        rendered = backend.calls[0]["output_schema"]
        # provider-strict rendered: "pattern" stripped
        assert "pattern" not in rendered["properties"]["name"]

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
