"""Unit tests for SchemaInferenceService."""

import json
from unittest.mock import AsyncMock, patch

import pytest

from modulo.core.schema_registry._common import parse_schema_from_response
from modulo.core.schema_registry.inference import (
    SchemaInferenceError,
    SchemaInferenceService,
    _build_infer_prompt,
    flag_rare_fields,
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


class _FlakyBackend:
    """Backend that fails the first N invocations, then succeeds.

    Drives the retry-with-backoff path in ``invoke_and_parse`` so a transient
    LLM failure on attempts 1 and 2 does not fail the request.
    """

    def __init__(self, response: str, failures_before_success: int) -> None:
        self._response = response
        self._failures_left = failures_before_success
        self.invoke_count = 0

    @property
    def backend_id(self) -> str:
        return "test/flaky"

    async def invoke(self, messages: list, **kwargs: object) -> object:
        self.invoke_count += 1
        if self._failures_left > 0:
            self._failures_left -= 1
            raise RuntimeError("transient LLM unavailable")
        from langchain_core.messages import AIMessage

        return AIMessage(content=self._response)

    def stream(self, messages: list, **kwargs: object) -> object:
        raise NotImplementedError


class TestBuildInferPrompt:
    def test_builds_system_and_human_messages(self) -> None:
        samples = [{"id": 1, "name": "foo"}]
        messages = _build_infer_prompt(samples)
        assert len(messages) == 2
        assert "schema inference" in messages[0].content.lower()
        assert "Sample data" in messages[1].content

    def test_truncates_large_sample_sets(self) -> None:
        samples = [{"i": i} for i in range(100)]
        messages = _build_infer_prompt(samples)
        assert "50 records" in messages[1].content

    def test_builds_with_empty_samples(self) -> None:
        messages = _build_infer_prompt([])
        assert len(messages) == 2
        assert "0 records" in messages[1].content

    def test_respects_custom_max_records(self) -> None:
        samples = [{"i": i} for i in range(100)]
        messages = _build_infer_prompt(samples, max_records=5)
        assert "5 records" in messages[1].content
        assert "100 records" not in messages[1].content

    def test_rare_fields_are_listed_in_prompt(self) -> None:
        samples = [{"common": i} for i in range(11)]
        samples[0]["rare"] = "x"
        messages = _build_infer_prompt(samples)
        assert "rarely-used fields" in messages[1].content.lower()
        assert "rare" in messages[1].content
        assert "11 samples" in messages[1].content

    def test_rare_fields_omitted_when_all_fields_common(self) -> None:
        samples = [{"common": 1, "id": 2}, {"common": 3, "id": 4}]
        messages = _build_infer_prompt(samples)
        assert "rarely-used fields" not in messages[1].content.lower()

    def test_system_prompt_instructs_rare_field_exclusion(self) -> None:
        messages = _build_infer_prompt([{"a": 1}])
        assert "rarely-used fields" in messages[0].content.lower()
        assert "exclude" in messages[0].content.lower()


class TestFlagRareFields:
    def test_no_fields_when_all_common(self) -> None:
        records = [{"id": 1, "title": "a"}, {"id": 2, "title": "b"}]
        assert not flag_rare_fields(records)

    def test_flags_fields_below_default_threshold(self) -> None:
        records = [{"common": i} for i in range(11)]
        records[0]["rare"] = 2
        assert flag_rare_fields(records) == ["rare"]

    def test_result_is_sorted_deterministically(self) -> None:
        records = [{"mid": i} for i in range(11)]
        records[0]["zeta"] = 1
        records[1]["alpha"] = 2
        assert flag_rare_fields(records) == ["alpha", "zeta"]

    def test_null_values_do_not_count_as_present(self) -> None:
        records = [{"id": i, "nullable": None} for i in range(11)]
        records[0]["nullable"] = "x"
        assert flag_rare_fields(records) == ["nullable"]

    def test_empty_containers_count_as_present(self) -> None:
        records = [{"id": 1, "tags": []}, {"id": 2, "tags": ["x"]}]
        assert not flag_rare_fields(records)

    def test_empty_records_return_empty(self) -> None:
        assert not flag_rare_fields([])

    def test_non_dict_records_are_ignored(self) -> None:
        records = [{"id": 1}, "not-a-dict", 42]
        assert not flag_rare_fields(records)

    def test_custom_threshold(self) -> None:
        records = [{"a": 1, "b": 2}, {"a": 3, "b": 4}, {"a": 5, "b": 6}, {"a": 7}]
        assert not flag_rare_fields(records, threshold=0.5)
        assert flag_rare_fields(records, threshold=0.8) == ["b"]

    def test_zero_threshold_flags_nothing(self) -> None:
        records = [{"a": 1}, {"b": 2}]
        assert not flag_rare_fields(records, threshold=0.0)

    def test_invalid_thresholds_raise(self) -> None:
        with pytest.raises(ValueError, match="threshold"):
            flag_rare_fields([{"a": 1}], threshold=1.5)
        with pytest.raises(ValueError, match="threshold"):
            flag_rare_fields([{"a": 1}], threshold=-0.1)
        with pytest.raises(ValueError, match="threshold"):
            flag_rare_fields([{"a": 1}], threshold="0.1")


class TestParseSchemaFromResponse:
    def test_parses_plain_json(self) -> None:
        raw = '{"type": "object", "properties": {"id": {"type": "string"}}}'
        result = parse_schema_from_response(raw)
        assert result["type"] == "object"
        assert "id" in result["properties"]

    def test_strips_markdown_fences(self) -> None:
        raw = '```json\n{"type": "object", "properties": {}}\n```'
        result = parse_schema_from_response(raw)
        assert result["type"] == "object"

    def test_adds_missing_type_and_properties(self) -> None:
        result = parse_schema_from_response("{}")
        assert result["type"] == "object"
        assert not result["properties"]

    def test_strips_markdown_without_lang_hint(self) -> None:
        raw = '```\n{"type": "object"}\n```'
        result = parse_schema_from_response(raw)
        assert result["type"] == "object"

    def test_strips_leading_trailing_whitespace(self) -> None:
        raw = '  \n  {"type": "object"}  \n  '
        result = parse_schema_from_response(raw)
        assert result["type"] == "object"

    def test_strips_markdown_surrounded_by_whitespace(self) -> None:
        raw = '\n\n```\n{"type": "object"}\n```\n\n'
        result = parse_schema_from_response(raw)
        assert result["type"] == "object"

    def test_raises_on_invalid_json(self) -> None:
        with pytest.raises(json.JSONDecodeError):
            parse_schema_from_response("not json")

    def test_raises_on_non_dict_json(self) -> None:
        with pytest.raises(ValueError, match="not a JSON object"):
            parse_schema_from_response('["list"]')

    def test_raises_on_empty_string(self) -> None:
        with pytest.raises(json.JSONDecodeError):
            parse_schema_from_response("")


class TestSchemaInferenceService:
    async def test_infer_returns_parsed_schema(self) -> None:
        expected = {"type": "object", "properties": {"name": {"type": "string"}}}
        backend = _FakeBackend(response=json.dumps(expected))
        service = SchemaInferenceService(backend)
        result = await service.infer([{"name": "test"}])
        assert result == expected

    async def test_infer_handles_markdown_wrapped_response(self) -> None:
        schema = {"type": "object", "properties": {}}
        backend = _FakeBackend(response=f"```json\n{json.dumps(schema)}\n```")
        service = SchemaInferenceService(backend)
        result = await service.infer([{"a": 1}])
        assert result == schema

    async def test_infer_raises_on_llm_failure(self) -> None:
        backend = _FakeBackend(fail=True)
        service = SchemaInferenceService(backend)
        with pytest.raises(SchemaInferenceError, match="LLM call failed"):
            await service.infer([{"a": 1}])

    async def test_infer_raises_on_unparseable_response(self) -> None:
        backend = _FakeBackend(response="not valid json")
        service = SchemaInferenceService(backend)
        with pytest.raises(SchemaInferenceError, match="Failed to parse"):
            await service.infer([{"a": 1}])

    async def test_infer_respects_max_sample_records(self) -> None:
        class _CapturingBackend:
            def __init__(self):
                self.captured_messages = None

            @property
            def backend_id(self) -> str:
                return "test/capture"

            async def invoke(self, messages, **kwargs):
                self.captured_messages = messages
                from langchain_core.messages import AIMessage

                return AIMessage(content='{"type": "object", "properties": {}}')

            def stream(self, messages, **kwargs):
                raise NotImplementedError

        samples = [{"i": i} for i in range(10)]
        backend = _CapturingBackend()
        service = SchemaInferenceService(backend, max_sample_records=3)
        result = await service.infer(samples)
        assert result == {"type": "object", "properties": {}}
        assert "3 records" in backend.captured_messages[1].content
        assert "10 records" not in backend.captured_messages[1].content

    async def test_infer_handles_empty_samples(self) -> None:
        backend = _FakeBackend(response='{"type": "object", "properties": {}}')
        service = SchemaInferenceService(backend)
        result = await service.infer([])
        assert result == {"type": "object", "properties": {}}

    async def test_infer_with_nested_structures(self) -> None:
        schema = {
            "type": "object",
            "properties": {
                "id": {"type": "integer"},
                "nested": {
                    "type": "object",
                    "properties": {
                        "key": {"type": "string"},
                    },
                },
            },
        }
        backend = _FakeBackend(response=json.dumps(schema))
        service = SchemaInferenceService(backend)
        samples = [{"id": 1, "nested": {"key": "val"}}]
        result = await service.infer(samples)
        assert result == schema

    async def test_infer_with_mixed_field_presence(self) -> None:
        schema = {
            "type": "object",
            "properties": {
                "id": {"type": "integer"},
                "optional_field": {"type": "string"},
            },
            "required": ["id"],
        }
        backend = _FakeBackend(response=json.dumps(schema))
        service = SchemaInferenceService(backend)
        samples = [
            {"id": 1, "optional_field": "present"},
            {"id": 2},
        ]
        result = await service.infer(samples)
        assert result == schema

    async def test_infer_with_all_null_values(self) -> None:
        schema = {
            "type": "object",
            "properties": {},
        }
        backend = _FakeBackend(response=json.dumps(schema))
        service = SchemaInferenceService(backend)
        samples = [{"id": None, "name": None}]
        result = await service.infer(samples)
        assert result == schema

    async def test_infer_raises_on_non_string_content(self) -> None:
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
        service = SchemaInferenceService(backend)
        with pytest.raises(SchemaInferenceError, match="Expected string response"):
            await service.infer([{"a": 1}])

    async def test_infer_raises_on_non_list_samples(self) -> None:
        """Non-list samples must be rejected before any LLM call."""
        service = SchemaInferenceService(_FakeBackend(fail=True))
        with pytest.raises(SchemaInferenceError, match="samples must be a list of dicts"):
            await service.infer("not-a-list")

    async def test_infer_raises_on_non_dict_record(self) -> None:
        """A sample set containing a non-dict record must be rejected before any LLM call."""
        service = SchemaInferenceService(_FakeBackend(fail=True))
        with pytest.raises(SchemaInferenceError, match="samples must be a list of dicts"):
            await service.infer([{"id": 1}, "not-a-dict"])

    async def test_infer_uses_custom_system_prompt(self) -> None:
        """A service-level custom system prompt must replace the default inference prompt."""

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
        service = SchemaInferenceService(backend, system_prompt="Custom inference guidance")
        result = await service.infer([{"a": 1}])
        assert result == {"type": "object", "properties": {}}
        assert backend.messages[0].content == "Custom inference guidance"

    async def test_infer_retries_transient_failures_then_succeeds(self) -> None:
        """A transient LLM failure on attempts 1 and 2 must not fail the request
        when a later attempt succeeds (retry-with-backoff recovery)."""
        expected = {"type": "object", "properties": {"name": {"type": "string"}}}
        backend = _FlakyBackend(response=json.dumps(expected), failures_before_success=2)
        service = SchemaInferenceService(backend)

        with patch("modulo.core.schema_registry._common.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            result = await service.infer([{"name": "test"}])

        assert result == expected
        assert backend.invoke_count == 3
        assert mock_sleep.await_count == 2
