"""Unit tests for SchemaGenerationService."""

import json

import pytest

from modulo.core.schema_registry.generation import (
    SchemaGenerationError,
    SchemaGenerationService,
    _build_generate_prompt,
)


class _FakeBackend:
    """Minimal ModelBackendBase replacement for unit tests."""

    def __init__(self, response: str | None = None, fail: bool = False) -> None:
        self._response = response
        self._fail = fail

    @property
    def backend_id(self) -> str:
        return "test/fake"

    async def invoke(self, messages: list, **kwargs: object) -> object:
        if self._fail:
            raise RuntimeError("LLM unavailable")
        from langchain_core.messages import AIMessage

        return AIMessage(content=self._response or "{}")

    def stream(self, messages: list, **kwargs: object) -> object:
        raise NotImplementedError


class TestBuildGeneratePrompt:
    def test_builds_system_and_human_messages(self) -> None:
        messages = _build_generate_prompt("A user profile schema")
        assert len(messages) == 2
        assert "schema generation" in messages[0].content.lower()
        assert "user profile" in messages[1].content

    def test_includes_examples_when_provided(self) -> None:
        examples = [{"name": "Alice", "age": 30}]
        messages = _build_generate_prompt("A user profile schema", examples)
        assert len(messages) == 2
        body = messages[1].content
        assert "Example records" in body
        assert "Alice" in body

    def test_omits_examples_section_when_none(self) -> None:
        messages = _build_generate_prompt("A user profile schema")
        assert "Example records" not in messages[1].content

    def test_omits_examples_section_when_empty_list(self) -> None:
        messages = _build_generate_prompt("A user profile schema", [])
        assert "Example records" not in messages[1].content

    def test_custom_system_prompt(self) -> None:
        custom = "Custom system prompt"
        messages = _build_generate_prompt("desc", system_prompt=custom)
        assert messages[0].content == custom


class TestSchemaGenerationService:
    async def test_generate_returns_parsed_schema(self) -> None:
        expected = {"type": "object", "properties": {"name": {"type": "string"}}}
        backend = _FakeBackend(response=json.dumps(expected))
        service = SchemaGenerationService(backend)
        result = await service.generate("A user profile")
        assert result == expected

    async def test_generate_with_examples(self) -> None:
        expected = {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "age": {"type": "integer"},
            },
            "required": ["name", "age"],
        }
        backend = _FakeBackend(response=json.dumps(expected))
        service = SchemaGenerationService(backend)
        examples = [{"name": "Alice", "age": 30}]
        result = await service.generate("A user profile", examples)
        assert result == expected

    async def test_generate_handles_markdown_wrapped_response(self) -> None:
        schema = {"type": "object", "properties": {}}
        backend = _FakeBackend(response=f"```json\n{json.dumps(schema)}\n```")
        service = SchemaGenerationService(backend)
        result = await service.generate("An empty schema")
        assert result == schema

    async def test_generate_raises_on_llm_failure(self) -> None:
        backend = _FakeBackend(fail=True)
        service = SchemaGenerationService(backend)
        with pytest.raises(SchemaGenerationError, match="LLM call failed"):
            await service.generate("A user profile")

    async def test_generate_raises_on_unparseable_response(self) -> None:
        backend = _FakeBackend(response="not valid json")
        service = SchemaGenerationService(backend)
        with pytest.raises(SchemaGenerationError, match="Failed to parse"):
            await service.generate("A user profile")

    async def test_generate_raises_on_empty_description(self) -> None:
        backend = _FakeBackend(response='{"type": "object", "properties": {}}')
        service = SchemaGenerationService(backend)
        with pytest.raises(SchemaGenerationError, match="description must be a non-empty string"):
            await service.generate("")

    async def test_generate_raises_on_blank_description(self) -> None:
        backend = _FakeBackend(response='{"type": "object", "properties": {}}')
        service = SchemaGenerationService(backend)
        with pytest.raises(SchemaGenerationError, match="description must be a non-empty string"):
            await service.generate("   ")

    async def test_generate_raises_on_non_string_content(self) -> None:
        class _NonStringContentBackend:
            @property
            def backend_id(self) -> str:
                return "test/nonstring"

            async def invoke(self, messages: list, **kwargs: object) -> object:
                from langchain_core.messages import AIMessage

                return AIMessage(content=["non-string", "content"])

            def stream(self, messages: list, **kwargs: object) -> object:
                raise NotImplementedError

        backend = _NonStringContentBackend()
        service = SchemaGenerationService(backend)
        with pytest.raises(SchemaGenerationError, match="Expected string response"):
            await service.generate("A user profile")

    async def test_generate_with_empty_examples_list(self) -> None:
        expected = {"type": "object", "properties": {}}
        backend = _FakeBackend(response=json.dumps(expected))
        service = SchemaGenerationService(backend)
        result = await service.generate("A schema", examples=[])
        assert result == expected

    async def test_generate_rejects_non_fenced_surrounding_text(self) -> None:
        expected = {"type": "object", "properties": {"id": {"type": "string"}}}
        backend = _FakeBackend(
            response=(f"Here is your schema:\n{json.dumps(expected)}\n\nLet me know if you need changes.")
        )
        service = SchemaGenerationService(backend)
        with pytest.raises(SchemaGenerationError, match="Failed to parse"):
            await service.generate("A schema")

    async def test_generate_with_complex_nested_schema(self) -> None:
        expected = {
            "type": "object",
            "properties": {
                "id": {"type": "integer", "description": "Unique identifier"},
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of tags",
                },
                "metadata": {
                    "type": "object",
                    "properties": {
                        "created": {"type": "string", "format": "date-time"},
                        "score": {"type": "number", "minimum": 0, "maximum": 100},
                    },
                    "required": ["created"],
                },
                "status": {"type": "string", "enum": ["active", "inactive", "archived"]},
            },
            "required": ["id", "status"],
        }
        backend = _FakeBackend(response=json.dumps(expected))
        service = SchemaGenerationService(backend)
        result = await service.generate("A complex schema")
        assert result == expected

    async def test_generate_raises_when_backend_returns_no_content(self) -> None:
        class _NoContentBackend:
            @property
            def backend_id(self) -> str:
                return "test/nocontent"

            async def invoke(self, messages: list, **kwargs: object) -> object:
                return object()

            def stream(self, messages: list, **kwargs: object) -> object:
                raise NotImplementedError

        backend = _NoContentBackend()
        service = SchemaGenerationService(backend)
        with pytest.raises(SchemaGenerationError, match="unexpected response type"):
            await service.generate("A schema")

    async def test_generate_raises_on_backend_timeout(self) -> None:
        class _TimeoutBackend:
            @property
            def backend_id(self) -> str:
                return "test/timeout"

            async def invoke(self, messages: list, **kwargs: object) -> object:
                raise TimeoutError

            def stream(self, messages: list, **kwargs: object) -> object:
                raise NotImplementedError

        backend = _TimeoutBackend()
        service = SchemaGenerationService(backend, timeout=1.0)
        with pytest.raises(SchemaGenerationError, match="timed out"):
            await service.generate("A schema")

    async def test_generate_uses_custom_system_prompt(self) -> None:
        """A service-level custom system prompt must replace the default generation prompt."""

        class _CapturingBackend:
            def __init__(self):
                self.messages = None

            @property
            def backend_id(self) -> str:
                return "test/capture"

            async def invoke(self, messages, **kwargs):
                self.messages = messages
                from langchain_core.messages import AIMessage

                return AIMessage(content='{"type": "object", "properties": {}}')

            def stream(self, messages, **kwargs):
                raise NotImplementedError

        backend = _CapturingBackend()
        service = SchemaGenerationService(backend, system_prompt="Custom generation guidance")
        result = await service.generate("A schema", examples=[{"a": 1}])
        assert result == {"type": "object", "properties": {}}
        assert backend.messages[0].content == "Custom generation guidance"
