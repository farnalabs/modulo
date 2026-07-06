"""Integration test for schema inference flow with real components.

Tests SchemaInferenceService wired to a stub LLM backend with actual
data, verifying the end-to-end inference pipeline (prompt building →
LLM call → response parsing → schema output).
"""

import json

import pytest

from modulo.core.schema_registry import SchemaInferenceError, SchemaInferenceService

pytestmark = pytest.mark.integration


class _StubBackend:
    """A deterministic stub that returns a fixed JSON schema response."""

    def __init__(self, response: str | None = None, fail: bool = False) -> None:
        self._response = response
        self._fail = fail

    @property
    def backend_id(self) -> str:
        return "test/stub"

    async def invoke(self, messages: list, **kwargs: object) -> object:
        if self._fail:
            raise RuntimeError("LLM unavailable")
        from langchain_core.messages import AIMessage

        return AIMessage(content=self._response or "{}")

    def stream(self, messages: list, **kwargs: object) -> object:
        raise NotImplementedError


_INFERRED_SCHEMA = {
    "type": "object",
    "properties": {
        "id": {"type": "integer", "description": "Record ID"},
        "title": {"type": "string", "description": "Record title"},
    },
    "required": ["id", "title"],
}


class TestSchemaInferenceFullFlow:
    """Full end-to-end flow: sample data → stub LLM → parsed schema."""

    pytestmark = pytest.mark.asyncio

    async def test_infer_from_realistic_records(self) -> None:
        """Stub responds with a valid schema; service parses and returns it."""
        backend = _StubBackend(response=json.dumps(_INFERRED_SCHEMA))
        service = SchemaInferenceService(backend)
        records = [
            {"id": 1, "title": "Bug: login fails"},
            {"id": 2, "title": "Feature: dark mode"},
        ]
        schema = await service.infer(records)
        assert schema == _INFERRED_SCHEMA

    async def test_infer_uses_all_fields(self) -> None:
        """Schema includes fields inferred from all sample record keys."""
        schema_result = {
            "type": "object",
            "properties": {
                "id": {"type": "integer"},
                "title": {"type": "string"},
                "assignee": {"type": "string"},
                "points": {"type": "integer"},
            },
            "required": ["id", "title"],
        }
        backend = _StubBackend(response=json.dumps(schema_result))
        service = SchemaInferenceService(backend)
        records = [
            {"id": 1, "title": "Bug", "assignee": "alice", "points": 3},
            {"id": 2, "title": "Feature", "assignee": "bob", "points": 8},
        ]
        schema = await service.infer(records)
        assert len(schema["properties"]) == 4
        assert "assignee" in schema["properties"]
        assert "points" in schema["properties"]

    async def test_infer_with_empty_records_list(self) -> None:
        """Empty sample list produces a valid schema from backend."""
        backend = _StubBackend(response=json.dumps({"type": "object", "properties": {}}))
        service = SchemaInferenceService(backend)
        schema = await service.infer([])
        assert schema == {"type": "object", "properties": {}}

    async def test_infer_handles_markdown_wrapped_response(self) -> None:
        """Markdown-wrapped JSON from backend is parsed correctly."""
        wrapped = "```json\n" + json.dumps(_INFERRED_SCHEMA) + "\n```"
        backend = _StubBackend(response=wrapped)
        service = SchemaInferenceService(backend)
        records = [{"id": 1, "title": "Test"}]
        schema = await service.infer(records)
        assert schema == _INFERRED_SCHEMA

    async def test_infer_raises_on_backend_failure(self) -> None:
        """Backend exception is wrapped in SchemaInferenceError."""
        backend = _StubBackend(fail=True)
        service = SchemaInferenceService(backend)
        with pytest.raises(SchemaInferenceError, match="LLM call failed"):
            await service.infer([{"id": 1}])

    async def test_infer_raises_on_unparseable_response(self) -> None:
        """Backend returns non-JSON; service raises SchemaInferenceError."""
        backend = _StubBackend(response="not valid json")
        service = SchemaInferenceService(backend)
        with pytest.raises(SchemaInferenceError, match="Failed to parse"):
            await service.infer([{"id": 1}])

    async def test_infer_with_deeply_nested_structures(self) -> None:
        """Nested object/array structures in samples are handled."""
        schema = {
            "type": "object",
            "properties": {
                "id": {"type": "integer"},
                "nested": {
                    "type": "object",
                    "properties": {
                        "x": {"type": "number"},
                    },
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
        }
        backend = _StubBackend(response=json.dumps(schema))
        service = SchemaInferenceService(backend)
        records = [
            {"id": 1, "nested": {"x": 1.5}, "tags": ["a", "b"]},
        ]
        result = await service.infer(records)
        assert "nested" in result["properties"]
        assert result["properties"]["nested"]["type"] == "object"
        assert result["properties"]["tags"]["type"] == "array"

    async def test_infer_with_non_string_aimessage_content(self) -> None:
        """AIMessage with list content (non-string) raises SchemaInferenceError."""

        class _ListContentBackend:
            @property
            def backend_id(self) -> str:
                return "test/listcontent"

            async def invoke(self, messages: list, **kwargs: object) -> object:
                from langchain_core.messages import AIMessage

                return AIMessage(content=[{"type": "text", "text": "response"}])

            def stream(self, messages: list, **kwargs: object) -> object:
                raise NotImplementedError

        backend = _ListContentBackend()
        service = SchemaInferenceService(backend)
        with pytest.raises(SchemaInferenceError, match="Expected string response"):
            await service.infer([{"a": 1}])
