"""FAR-898: tests for _invoke_node_model native structured output threading.

Verifies that output_schema_json is forwarded to the backend's invoke()
only when the backend supports native structured output AND the schema
is non-None, and omitted otherwise.
"""

from typing import Any
from unittest.mock import patch

import pytest
from langchain_core.messages import AIMessage, BaseMessage

from modulo.core.pipeline_engine import node_runner as nr

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _RecordingBackend:
    """Records every call to invoke() and its kwargs."""

    def __init__(self, supports_native: bool = False) -> None:
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
    """Minimal ModelBackendHub stand-in that returns a pre-configured backend."""

    def __init__(self, backend: Any) -> None:
        self._backend = backend

    async def get(self, backend_id: Any) -> Any:
        return self._backend


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestInvokeNodeModelSchemaThreading:
    """output_schema_json must be forwarded to the backend IFF conditions met."""

    async def test_schema_forwarded_when_backend_supports_it(self) -> None:
        backend = _RecordingBackend(supports_native=True)
        hub = _FakeHub(backend)
        schema = {"type": "object", "properties": {"name": {"type": "string"}}}
        with patch(
            "modulo.core.pipeline_engine.decorator.get_model_backend_hub",
            return_value=hub,
        ):
            result = await nr._invoke_node_model(
                "prompt",
                "11111111-2222-3333-4444-555555555555",
                "n1",
                output_schema_json=schema,
            )
        assert result == {"result": "ok"}
        assert len(backend.calls) == 1
        assert backend.calls[0]["output_schema"] == schema

    async def test_schema_omitted_when_backend_does_not_support_it(self) -> None:
        backend = _RecordingBackend(supports_native=False)
        hub = _FakeHub(backend)
        schema = {"type": "object", "properties": {"name": {"type": "string"}}}
        with patch(
            "modulo.core.pipeline_engine.decorator.get_model_backend_hub",
            return_value=hub,
        ):
            await nr._invoke_node_model(
                "prompt",
                "11111111-2222-3333-4444-555555555555",
                "n1",
                output_schema_json=schema,
            )
        assert len(backend.calls) == 1
        # output_schema must NOT be in the call kwargs
        assert "output_schema" not in backend.calls[0]

    async def test_no_schema_no_kwarg(self) -> None:
        backend = _RecordingBackend(supports_native=True)
        hub = _FakeHub(backend)
        with patch(
            "modulo.core.pipeline_engine.decorator.get_model_backend_hub",
            return_value=hub,
        ):
            await nr._invoke_node_model(
                "prompt",
                "11111111-2222-3333-4444-555555555555",
                "n1",
            )
        assert len(backend.calls) == 1
        assert "output_schema" not in backend.calls[0]

    async def test_none_schema_no_kwarg(self) -> None:
        backend = _RecordingBackend(supports_native=True)
        hub = _FakeHub(backend)
        with patch(
            "modulo.core.pipeline_engine.decorator.get_model_backend_hub",
            return_value=hub,
        ):
            await nr._invoke_node_model(
                "prompt",
                "11111111-2222-3333-4444-555555555555",
                "n1",
                output_schema_json=None,
            )
        assert "output_schema" not in backend.calls[0]

    async def test_json_string_response_is_parsed(self) -> None:
        """The output is JSON-parsed when the response content is a string."""
        backend = _RecordingBackend(supports_native=True)
        hub = _FakeHub(backend)
        with patch(
            "modulo.core.pipeline_engine.decorator.get_model_backend_hub",
            return_value=hub,
        ):
            result = await nr._invoke_node_model(
                "prompt",
                "11111111-2222-3333-4444-555555555555",
                "n1",
            )
        assert result == {"result": "ok"}

    async def test_hub_none_raises(self) -> None:
        with pytest.raises(RuntimeError, match="ModelBackendHub not available"):
            await nr._invoke_node_model(
                "prompt",
                "11111111-2222-3333-4444-555555555555",
                "n1",
                output_schema_json={"type": "object"},
            )

    async def test_raw_schema_used_when_rendered_fails_bounds(self) -> None:
        """When the rendered schema fails bounds but the raw one passes, send raw.

        Covers the ``elif raw_ok`` fallback: ``_is_safe_schema`` returns True
        for the raw schema (first call) and False for the rendered copy (second
        call), so the raw schema is forwarded and the native flag is set.
        """
        backend = _RecordingBackend(supports_native=True)
        hub = _FakeHub(backend)
        schema = {"type": "object", "properties": {"name": {"type": "string"}}}
        flag = [False]
        with (
            patch(
                "modulo.core.pipeline_engine.decorator.get_model_backend_hub",
                return_value=hub,
            ),
            patch.object(nr, "_is_safe_schema", side_effect=[(True, ""), (False, "too large")]),
        ):
            result = await nr._invoke_node_model(
                "prompt",
                "11111111-2222-3333-4444-555555555555",
                "n1",
                output_schema_json=schema,
                _native_output_flag=flag,
            )
        assert result == {"result": "ok"}
        assert backend.calls[0]["output_schema"] == schema
        assert flag[0] is True
