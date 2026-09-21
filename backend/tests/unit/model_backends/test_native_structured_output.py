"""FAR-898: tests for native structured output plumbing across model backends.

Covers:
- Every known ModelBackendBase subclass explicitly declares supports_native_structured_output
- Only OpenAI and Anthropic backends are True
- A False backend is never passed the output_schema kwarg (tested via _invoke_node_model)
- output_schema=None → no kwarg, no behavioural change
- When flag is True and schema is supplied, the backend receives it
- FakeStructuredOutputBackend reusable test double
"""

import json
from collections.abc import Callable
from typing import Any

import pytest
from langchain_core.messages import AIMessage, BaseMessage

from modulo.model_backends.base import ModelBackendBase

# ---------------------------------------------------------------------------
# Reusable test double (FAR-898, reused by FAR-899..902)
# ---------------------------------------------------------------------------


class FakeStructuredOutputBackend(ModelBackendBase):
    """Minimal backend that records every kwarg it receives.

    Used to verify that native structured-output plumbing forwards the
    schema correctly (or omits it correctly) without hitting a real
    provider.

    Parameters
    ----------
    output_factory:
        A callable ``(attempt_number) -> str`` that produces the
        ``response.content`` for each successive invoke.  Defaults to
        always returning ``"{}"``.
    """

    supports_tools: bool = False
    supports_native_structured_output: bool = False

    def __init__(
        self,
        output_factory: Callable[[int], str] | None = None,
    ) -> None:
        self._output_factory = output_factory or (lambda _n: "{}")
        self._attempt = 0
        self.invoke_kwargs_received: list[dict[str, Any]] = []

    @property
    def backend_id(self) -> str:
        return "fake/structured-output"

    async def invoke(
        self,
        messages: list[BaseMessage],
        output_schema: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> BaseMessage:
        self._attempt += 1
        recorded: dict[str, Any] = dict(kwargs)
        if output_schema is not None:
            recorded["output_schema"] = output_schema
        self.invoke_kwargs_received.append(recorded)
        return AIMessage(content=self._output_factory(self._attempt))

    def stream(
        self,
        messages: list[BaseMessage],
        tools: list[dict[str, Any]] | None = None,
        output_schema: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> Any:
        self.invoke_kwargs_received.append(kwargs)
        raise NotImplementedError("stream not implemented in test double")


# ---------------------------------------------------------------------------
# Flag-declaration tests
# ---------------------------------------------------------------------------


class TestFlagDeclaration:
    """Known ModelBackendBase subclasses must explicitly declare the flag."""

    def test_base_class_default_is_false(self) -> None:
        assert ModelBackendBase.supports_native_structured_output is False

    def test_openai_compatible_backend_flag(self) -> None:
        from modulo.model_backends.module import OpenAICompatibleBackend

        assert OpenAICompatibleBackend.supports_native_structured_output is True

    def test_anthropic_backend_flag(self) -> None:
        from modulo.model_backends.anthropic import AnthropicBackend

        assert AnthropicBackend.supports_native_structured_output is True

    def test_fake_backend_flag_is_false(self) -> None:
        assert FakeStructuredOutputBackend.supports_native_structured_output is False

    @pytest.mark.timeout(300)
    def test_flag_declared_on_all_subclasses(self) -> None:
        """Every ModelBackendBase subclass must explicitly declare the flag.

        Discovers subclasses dynamically so a NEW backend that forgets to
        declare ``supports_native_structured_output`` is caught at test time.
        Only OpenAI-compatible and Anthropic backends are True; all others
        inherit the base class default (False) but MUST still declare it.
        """
        import importlib
        import pkgutil

        import modulo.model_backends as mb_pkg

        # Force-import every module in model_backends/ so all subclasses register.
        for _importer, modname, _ispkg in pkgutil.walk_packages(mb_pkg.__path__, prefix=mb_pkg.__name__ + "."):
            importlib.import_module(modname)

        # Only DIRECT subclasses of ModelBackendBase need an explicit
        # declaration. Indirect subclasses (e.g. OpenCodeBackend extends
        # OpenAICompatibleBackend) inherit from their parent and are fine.
        direct = [
            cls for cls in ModelBackendBase.__subclasses__() if cls.__module__.startswith("modulo.model_backends.")
        ]
        assert len(direct) > 0, "Expected at least one direct ModelBackendBase subclass"

        undeclared = [cls.__name__ for cls in direct if "supports_native_structured_output" not in cls.__dict__]
        assert not undeclared, f"These backends must explicitly declare supports_native_structured_output: {undeclared}"


# ---------------------------------------------------------------------------
# Kwarg-forwarding tests (via _invoke_node_model)
# ---------------------------------------------------------------------------


class TestKwargForwarding:
    """Verify the output_schema kwarg reaches (or is omitted from) the backend."""

    async def test_false_backend_never_receives_kwarg(self) -> None:
        """A backend with flag=False must never see output_schema in kwargs.

        Tested via _invoke_node_model to verify the caller (node_runner)
        does not forward the kwarg when the backend flag is False.
        """
        from unittest.mock import patch

        from modulo.core.pipeline_engine import node_runner as nr

        backend = FakeStructuredOutputBackend()
        assert backend.supports_native_structured_output is False

        class _FakeHub:
            async def get(self, _id: Any) -> Any:
                return backend

        schema = {"type": "object", "properties": {"name": {"type": "string"}}}
        with patch(
            "modulo.core.pipeline_engine.decorator.get_model_backend_hub",
            return_value=_FakeHub(),
        ):
            await nr._invoke_node_model(
                "prompt",
                "11111111-2222-3333-4444-555555555555",
                "n1",
                output_schema_json=schema,
            )
        # The backend was called, but output_schema was NOT forwarded
        assert len(backend.invoke_kwargs_received) == 1
        assert "output_schema" not in backend.invoke_kwargs_received[0]

    async def test_none_schema_no_kwarg(self) -> None:
        """output_schema_json=None → no kwarg forwarded."""
        from unittest.mock import patch

        from modulo.core.pipeline_engine import node_runner as nr

        backend = FakeStructuredOutputBackend()

        class _FakeHub:
            async def get(self, _id: Any) -> Any:
                return backend

        with patch(
            "modulo.core.pipeline_engine.decorator.get_model_backend_hub",
            return_value=_FakeHub(),
        ):
            await nr._invoke_node_model(
                "prompt",
                "11111111-2222-3333-4444-555555555555",
                "n1",
                output_schema_json=None,
            )
        assert len(backend.invoke_kwargs_received) == 1
        assert "output_schema" not in backend.invoke_kwargs_received[0]

    async def test_no_schema_arg_no_kwarg(self) -> None:
        """Omitting output_schema_json entirely → no kwarg forwarded."""
        from unittest.mock import patch

        from modulo.core.pipeline_engine import node_runner as nr

        backend = FakeStructuredOutputBackend()

        class _FakeHub:
            async def get(self, _id: Any) -> Any:
                return backend

        with patch(
            "modulo.core.pipeline_engine.decorator.get_model_backend_hub",
            return_value=_FakeHub(),
        ):
            await nr._invoke_node_model(
                "prompt",
                "11111111-2222-3333-4444-555555555555",
                "n1",
            )
        assert len(backend.invoke_kwargs_received) == 1
        assert "output_schema" not in backend.invoke_kwargs_received[0]

    async def test_true_backend_receives_schema(self) -> None:
        """When flag=True and schema is supplied, the backend sees it."""
        from unittest.mock import patch

        from modulo.core.pipeline_engine import node_runner as nr

        backend = FakeStructuredOutputBackend()
        backend.supports_native_structured_output = True

        class _FakeHub:
            async def get(self, _id: Any) -> Any:
                return backend

        schema = {"type": "object", "properties": {"name": {"type": "string"}}}
        with patch(
            "modulo.core.pipeline_engine.decorator.get_model_backend_hub",
            return_value=_FakeHub(),
        ):
            await nr._invoke_node_model(
                "prompt",
                "11111111-2222-3333-4444-555555555555",
                "n1",
                output_schema_json=schema,
            )
        assert len(backend.invoke_kwargs_received) == 1
        assert backend.invoke_kwargs_received[0]["output_schema"] == schema

    async def test_output_factory_produces_correct_content(self) -> None:
        """The FakeStructuredOutputBackend output_factory works across calls."""
        backend = FakeStructuredOutputBackend(output_factory=lambda n: f'{{"attempt": {n}}}')
        r1 = await backend.invoke([BaseMessage(content="a", type="human")])
        r2 = await backend.invoke([BaseMessage(content="b", type="human")])
        assert r1.content == '{"attempt": 1}'
        assert r2.content == '{"attempt": 2}'


# ---------------------------------------------------------------------------
# FIX 1: OpenAI structured output uses with_structured_output (not raw bind)
# ---------------------------------------------------------------------------


class TestOpenAIStructuredOutputMechanism:
    """FIX 1 (CRITICAL): the OpenAI path must use with_structured_output,
    not a raw bind(response_format=...), which 400s on the wrong schema shape."""

    async def test_invoke_uses_with_structured_output(self) -> None:
        """OpenAICompatibleBackend.invoke() calls with_structured_output(method='json_schema').

        FAR-1118: schema is deep-copied and a 'title' is injected when absent,
        so the assertion checks the provider-facing schema (with title).
        """
        from unittest.mock import AsyncMock, MagicMock, patch

        from langchain_core.messages import HumanMessage

        from modulo.model_backends.module import OpenAICompatibleBackend

        schema = {"type": "object", "properties": {"name": {"type": "string"}}}

        with patch("modulo.model_backends.module.ChatOpenAI") as mock_chat_cls:
            mock_chat = MagicMock()
            mock_structured = AsyncMock()
            mock_structured.ainvoke = AsyncMock(return_value={"name": "Alice"})
            mock_chat.with_structured_output = MagicMock(return_value=mock_structured)
            mock_chat_cls.return_value = mock_chat

            backend = OpenAICompatibleBackend(api_key="sk-test", model_id="gpt-4o")
            result = await backend.invoke(
                [HumanMessage(content="hi")],
                output_schema=schema,
            )

        # Assert with_structured_output was called with correct parameters.
        # FAR-1118: title-less schemas get a 'title' injected before passing.
        expected_schema = {**schema, "title": "StructuredOutput"}
        mock_chat.with_structured_output.assert_called_once_with(
            schema=expected_schema,
            method="json_schema",
        )
        # Assert result is an AIMessage with JSON content
        assert hasattr(result, "content")
        import json

        parsed = json.loads(result.content)
        assert parsed == {"name": "Alice"}

    async def test_invoke_without_schema_does_not_call_with_structured_output(self) -> None:
        """When output_schema is None, with_structured_output must not be called."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from langchain_core.messages import HumanMessage

        from modulo.model_backends.module import OpenAICompatibleBackend

        with patch("modulo.model_backends.module.ChatOpenAI") as mock_chat_cls:
            mock_chat = MagicMock()
            mock_chat.ainvoke = AsyncMock(return_value=AIMessage(content="{}"))
            mock_chat_cls.return_value = mock_chat

            backend = OpenAICompatibleBackend(api_key="sk-test", model_id="gpt-4o")
            await backend.invoke([HumanMessage(content="hi")])

        mock_chat.with_structured_output.assert_not_called()


# ---------------------------------------------------------------------------
# FIX 2: empty-dict schema → no kwarg
# ---------------------------------------------------------------------------


class TestEmptyDictSchemaGuard:
    """FIX 2 (MAJOR): output_schema_json={} must not be forwarded."""

    async def test_empty_dict_schema_not_forwarded(self) -> None:
        """An empty dict {} is treated as 'no schema' — no kwarg forwarded."""
        from unittest.mock import patch

        from modulo.core.pipeline_engine import node_runner as nr

        backend = FakeStructuredOutputBackend()
        backend.supports_native_structured_output = True

        class _FakeHub:
            async def get(self, _id: Any) -> Any:
                return backend

        with patch(
            "modulo.core.pipeline_engine.decorator.get_model_backend_hub",
            return_value=_FakeHub(),
        ):
            await nr._invoke_node_model(
                "prompt",
                "11111111-2222-3333-4444-555555555555",
                "n1",
                output_schema_json={},
            )
        assert len(backend.invoke_kwargs_received) == 1
        assert "output_schema" not in backend.invoke_kwargs_received[0]


# ---------------------------------------------------------------------------
# FIX 3: hub sets flag False for unsupported providers
# ---------------------------------------------------------------------------


class TestHubCapabilityOverride:
    """FIX 3 (MAJOR): _build_backend must disable native structured output
    for providers that cannot deliver it."""

    def test_ollama_backend_has_flag_false(self) -> None:
        """An ollama provider's backend must have flag=False after _build_backend."""
        from unittest.mock import MagicMock, patch

        from modulo.core.model_backend_hub import _build_backend

        mock_creds = {"api_key": "unused"}

        with patch("modulo.model_backends.module.OpenAICompatibleBackend") as mock_backend:
            instance = MagicMock()
            instance.supports_native_structured_output = True
            mock_backend.return_value = instance

            _build_backend("ollama", "llama3", mock_creds, {})

            assert instance.supports_native_structured_output is False

    def test_openai_backend_keeps_flag_true(self) -> None:
        """An openai provider's backend keeps flag=True after _build_backend."""
        from unittest.mock import MagicMock, patch

        from modulo.core.model_backend_hub import _build_backend

        mock_creds = {"api_key": "sk-test"}

        with patch("modulo.model_backends.module.OpenAICompatibleBackend") as mock_backend:
            instance = MagicMock()
            instance.supports_native_structured_output = True
            mock_backend.return_value = instance

            _build_backend("openai", "gpt-4o", mock_creds, {})

            assert instance.supports_native_structured_output is True

    @pytest.mark.parametrize(
        "provider",
        ["ollama", "llamacpp", "localai", "tgi", "vllm"],
    )
    def test_denied_provider_flag_set_false(self, provider: str) -> None:
        """Every provider in _NO_NATIVE_STRUCTURED_OUTPUT_PROVIDERS gets flag=False."""
        from unittest.mock import MagicMock, patch

        from modulo.core.model_backend_hub import _build_backend

        mock_creds = {"api_key": "unused"}

        with patch("modulo.model_backends.module.OpenAICompatibleBackend") as mock_backend:
            instance = MagicMock()
            instance.supports_native_structured_output = True
            mock_backend.return_value = instance

            _build_backend(provider, "model", mock_creds, {})

            assert instance.supports_native_structured_output is False, f"Provider {provider!r} should have flag=False"


# ---------------------------------------------------------------------------
# FIX 6: schema bounds validation
# ---------------------------------------------------------------------------


class TestSchemaBounds:
    """FIX 6 (MAJOR): _is_safe_schema rejects invalid schemas before dispatch."""

    def test_valid_schema_passes(self) -> None:
        from modulo.core.pipeline_engine.node_runner import _is_safe_schema

        schema = {"type": "object", "properties": {"name": {"type": "string"}}}
        ok, reason = _is_safe_schema(schema)
        assert ok is True
        assert reason == ""

    def test_non_dict_rejected(self) -> None:
        from modulo.core.pipeline_engine.node_runner import _is_safe_schema

        ok, reason = _is_safe_schema("not a dict")  # type: ignore[arg-type]
        assert ok is False
        assert "not a dict" in reason

    def test_non_serialisable_schema_rejected(self) -> None:
        from modulo.core.pipeline_engine.node_runner import _is_safe_schema

        schema: dict[str, Any] = {"type": "object", "value": object()}
        ok, reason = _is_safe_schema(schema)
        assert ok is False
        assert "JSON-serialisable" in reason

    def test_circular_schema_rejected(self) -> None:
        from modulo.core.pipeline_engine.node_runner import _is_safe_schema

        schema: dict[str, Any] = {"type": "object"}
        schema["self"] = schema
        ok, reason = _is_safe_schema(schema)
        assert ok is False
        assert "JSON-serialisable" in reason

    def test_oversized_schema_rejected(self) -> None:
        from modulo.core.pipeline_engine.node_runner import _is_safe_schema

        # Build a schema that serialises to > 100 KB
        big_value = "x" * 200_000
        schema: dict[str, Any] = {"type": "object", "data": big_value}
        ok, reason = _is_safe_schema(schema)
        assert ok is False
        assert "bytes" in reason

    def test_too_deep_schema_rejected(self) -> None:
        from modulo.core.pipeline_engine.node_runner import _is_safe_schema

        # Build a schema with nesting depth > 10
        schema: dict[str, Any] = {}
        current = schema
        for _ in range(15):
            current["nested"] = {}
            current = current["nested"]
        ok, reason = _is_safe_schema(schema)
        assert ok is False
        assert "depth" in reason

    def test_external_ref_rejected(self) -> None:
        from modulo.core.pipeline_engine.node_runner import _is_safe_schema

        schema = {"type": "object", "definitions": {"Foo": {"$ref": "https://example.com/schema.json#/Foo"}}}
        ok, reason = _is_safe_schema(schema)
        assert ok is False
        assert "external $ref" in reason

    def test_local_ref_allowed(self) -> None:
        from modulo.core.pipeline_engine.node_runner import _is_safe_schema

        schema = {"type": "object", "definitions": {"Foo": {"$ref": "#/definitions/Foo"}}}
        ok, _reason = _is_safe_schema(schema)
        assert ok is True

    async def test_oversized_schema_not_forwarded(self) -> None:
        """An oversized schema is caught by bounds check and not sent to provider."""
        from unittest.mock import patch

        from modulo.core.pipeline_engine import node_runner as nr

        backend = FakeStructuredOutputBackend()
        backend.supports_native_structured_output = True

        class _FakeHub:
            async def get(self, _id: Any) -> Any:
                return backend

        big_value = "x" * 200_000
        schema: dict[str, Any] = {"type": "object", "data": big_value}
        with patch(
            "modulo.core.pipeline_engine.decorator.get_model_backend_hub",
            return_value=_FakeHub(),
        ):
            await nr._invoke_node_model(
                "prompt",
                "11111111-2222-3333-4444-555555555555",
                "n1",
                output_schema_json=schema,
            )
        # Backend was called but output_schema was NOT forwarded
        assert len(backend.invoke_kwargs_received) == 1
        assert "output_schema" not in backend.invoke_kwargs_received[0]


# ---------------------------------------------------------------------------
# FIX 7: shared structured-output serialisation
# ---------------------------------------------------------------------------


class TestSerializeStructuredOutput:
    """serialize_structured_output keeps the json.loads(content) round-trip."""

    def test_dict_round_trips(self) -> None:
        from modulo.model_backends.base import serialize_structured_output

        message = serialize_structured_output({"a": 1, "b": [2, 3]})
        assert json.loads(message.content) == {"a": 1, "b": [2, 3]}

    def test_pydantic_model_round_trips(self) -> None:
        from pydantic import BaseModel

        from modulo.model_backends.base import serialize_structured_output

        class _Out(BaseModel):
            name: str
            count: int

        message = serialize_structured_output(_Out(name="x", count=2))
        assert json.loads(message.content) == {"name": "x", "count": 2}

    def test_list_round_trips(self) -> None:
        from modulo.model_backends.base import serialize_structured_output

        message = serialize_structured_output([{"a": 1}, {"b": 2}])
        assert json.loads(message.content) == [{"a": 1}, {"b": 2}]

    def test_non_dict_fallback_round_trips_as_string(self) -> None:
        """A non-dict/non-BaseModel result is str()-wrapped so loads never raises."""
        from modulo.model_backends.base import serialize_structured_output

        message = serialize_structured_output(42)
        assert json.loads(message.content) == "42"


# ---------------------------------------------------------------------------
# FAR-1118: title-less dict schemas must not crash the run
# ---------------------------------------------------------------------------

# A bare dict JSON Schema with NO top-level "title" — the exact shape Modulo
# produces from output_schema_json.  This is the schema that crashed production.
_TITLELESS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "name": {"type": "string"},
        "age": {"type": "integer"},
    },
}


class TestOpenAICompatibleTitleInjection:
    """FAR-1118: OpenAICompatibleBackend must inject 'title' into title-less
    dict schemas before calling with_structured_output, and fall back to
    plain ainvoke on construction failure."""

    async def test_title_injected_for_titleless_schema(self) -> None:
        """with_structured_output receives a schema WITH top-level title.

        FAILS WITHOUT FIX: ValueError('Unsupported function ... must have a
        top-level title key') propagates uncaught.
        """
        from unittest.mock import AsyncMock, MagicMock, patch

        from langchain_core.messages import HumanMessage

        from modulo.model_backends.module import OpenAICompatibleBackend

        with patch("modulo.model_backends.module.ChatOpenAI") as mock_chat_cls:
            mock_chat = MagicMock()
            mock_structured = AsyncMock()
            mock_structured.ainvoke = AsyncMock(return_value={"name": "Alice", "age": 30})
            mock_chat.with_structured_output = MagicMock(return_value=mock_structured)
            mock_chat_cls.return_value = mock_chat

            backend = OpenAICompatibleBackend(api_key="sk-test", model_id="gpt-4o")
            await backend.invoke(
                [HumanMessage(content="hi")],
                output_schema=_TITLELESS_SCHEMA,
            )

        # The schema passed to with_structured_output must have a title
        call_kwargs = mock_chat.with_structured_output.call_args
        passed_schema = call_kwargs.kwargs.get("schema") or call_kwargs[1].get("schema")
        assert "title" in passed_schema, "Schema must have top-level 'title' injected"
        assert passed_schema["title"] == "StructuredOutput"
        # Original properties preserved
        assert "name" in passed_schema["properties"]

    async def test_caller_schema_not_mutated(self) -> None:
        """The caller's original dict must NOT have 'title' added in place.

        FAILS WITHOUT FIX: copy.deepcopy is not used, so the original dict
        gets mutated.
        """
        from unittest.mock import AsyncMock, MagicMock, patch

        from langchain_core.messages import HumanMessage

        from modulo.model_backends.module import OpenAICompatibleBackend

        original_schema = dict(_TITLELESS_SCHEMA)  # shallow copy for safety
        original_schema["properties"] = dict(_TITLELESS_SCHEMA["properties"])
        schema_copy = json.loads(json.dumps(original_schema))

        with patch("modulo.model_backends.module.ChatOpenAI") as mock_chat_cls:
            mock_chat = MagicMock()
            mock_structured = AsyncMock()
            mock_structured.ainvoke = AsyncMock(return_value={"name": "Bob"})
            mock_chat.with_structured_output = MagicMock(return_value=mock_structured)
            mock_chat_cls.return_value = mock_chat

            backend = OpenAICompatibleBackend(api_key="sk-test", model_id="gpt-4o")
            await backend.invoke(
                [HumanMessage(content="hi")],
                output_schema=original_schema,
            )

        # Original schema must be unchanged
        assert json.loads(json.dumps(original_schema)) == schema_copy
        assert "title" not in original_schema, "Original schema must not be mutated"

    async def test_invoke_returns_result_with_titleless_schema(self) -> None:
        """invoke() completes successfully with a title-less schema.

        FAILS WITHOUT FIX: ValueError propagates, crashing the run.
        """
        from unittest.mock import AsyncMock, MagicMock, patch

        from langchain_core.messages import HumanMessage

        from modulo.model_backends.module import OpenAICompatibleBackend

        with patch("modulo.model_backends.module.ChatOpenAI") as mock_chat_cls:
            mock_chat = MagicMock()
            mock_structured = AsyncMock()
            mock_structured.ainvoke = AsyncMock(return_value={"name": "Alice"})
            mock_chat.with_structured_output = MagicMock(return_value=mock_structured)
            mock_chat_cls.return_value = mock_chat

            backend = OpenAICompatibleBackend(api_key="sk-test", model_id="gpt-4o")
            result = await backend.invoke(
                [HumanMessage(content="hi")],
                output_schema=_TITLELESS_SCHEMA,
            )

        assert hasattr(result, "content")
        parsed = json.loads(result.content)
        assert parsed == {"name": "Alice"}

    async def test_construction_failure_falls_back_to_plain_invoke(self) -> None:
        """When with_structured_output raises ValueError, invoke() falls back.

        FAILS WITHOUT FIX: ValueError is not caught, so the run crashes
        instead of falling back to plain ainvoke.
        """
        from unittest.mock import AsyncMock, MagicMock, patch

        from langchain_core.messages import AIMessage, HumanMessage

        from modulo.model_backends.module import OpenAICompatibleBackend

        with patch("modulo.model_backends.module.ChatOpenAI") as mock_chat_cls:
            mock_chat = MagicMock()
            # with_structured_output raises ValueError (simulating the bug)
            mock_chat.with_structured_output = MagicMock(side_effect=ValueError("Unsupported function"))
            # plain ainvoke returns a normal result
            mock_chat.ainvoke = AsyncMock(return_value=AIMessage(content="fallback result"))
            mock_chat_cls.return_value = mock_chat

            backend = OpenAICompatibleBackend(api_key="sk-test", model_id="gpt-4o")
            result = await backend.invoke(
                [HumanMessage(content="hi")],
                output_schema=_TITLELESS_SCHEMA,
            )

        # Must have fallen back to plain ainvoke
        mock_chat.ainvoke.assert_awaited_once()
        assert result.content == "fallback result"

    async def test_existing_title_preserved(self) -> None:
        """A schema that already has a title is NOT overridden.

        FAILS WITHOUT FIX: N/A — this is a guard against over-injection.
        """
        from unittest.mock import AsyncMock, MagicMock, patch

        from langchain_core.messages import HumanMessage

        from modulo.model_backends.module import OpenAICompatibleBackend

        schema_with_title = {**_TITLELESS_SCHEMA, "title": "MyCustomTitle"}

        with patch("modulo.model_backends.module.ChatOpenAI") as mock_chat_cls:
            mock_chat = MagicMock()
            mock_structured = AsyncMock()
            mock_structured.ainvoke = AsyncMock(return_value={"name": "Alice"})
            mock_chat.with_structured_output = MagicMock(return_value=mock_structured)
            mock_chat_cls.return_value = mock_chat

            backend = OpenAICompatibleBackend(api_key="sk-test", model_id="gpt-4o")
            await backend.invoke(
                [HumanMessage(content="hi")],
                output_schema=schema_with_title,
            )

        call_kwargs = mock_chat.with_structured_output.call_args
        passed_schema = call_kwargs.kwargs.get("schema") or call_kwargs[1].get("schema")
        assert passed_schema["title"] == "MyCustomTitle"

    async def test_api_status_error_still_classified(self) -> None:
        """APIStatusError from structured ainvoke is still caught and classified.

        FAILS WITHOUT FIX: N/A — regression guard ensuring the outer except
        handler still sees provider errors.
        """
        from unittest.mock import AsyncMock, MagicMock, patch

        import httpx2
        from langchain_core.messages import HumanMessage
        from openai import APIStatusError

        from modulo.model_backends.base import ProviderUnavailableError
        from modulo.model_backends.module import OpenAICompatibleBackend

        request = httpx2.Request("POST", "https://api.openai.com/v1/chat/completions")
        server_error = APIStatusError(
            message="Internal Server Error",
            response=httpx2.Response(500, request=request),
            body={"error": {"message": "boom"}},
        )

        with patch("modulo.model_backends.module.ChatOpenAI") as mock_chat_cls:
            mock_chat = MagicMock()
            mock_structured = AsyncMock()
            mock_structured.ainvoke = AsyncMock(side_effect=server_error)
            mock_chat.with_structured_output = MagicMock(return_value=mock_structured)
            mock_chat_cls.return_value = mock_chat

            backend = OpenAICompatibleBackend(api_key="sk-test", model_id="gpt-4o")
            with pytest.raises(ProviderUnavailableError) as exc_info:
                await backend.invoke(
                    [HumanMessage(content="hi")],
                    output_schema=_TITLELESS_SCHEMA,
                )
            assert "HTTP 500" in str(exc_info.value)


class TestAnthropicTitleInjection:
    """FAR-1118: AnthropicBackend must also inject 'title' and fail open."""

    async def test_invoke_returns_result_with_titleless_schema(self) -> None:
        """invoke() completes successfully with a title-less schema.

        FAILS WITHOUT FIX: ValueError propagates from with_structured_output.
        """
        from unittest.mock import AsyncMock, MagicMock, patch

        from langchain_core.messages import HumanMessage

        from modulo.model_backends.anthropic import AnthropicBackend

        with patch("modulo.model_backends.anthropic.ChatAnthropic") as mock_chat_cls:
            mock_chat = MagicMock()
            mock_structured = AsyncMock()
            mock_structured.ainvoke = AsyncMock(return_value={"name": "Alice"})
            mock_chat.with_structured_output = MagicMock(return_value=mock_structured)
            mock_chat_cls.return_value = mock_chat

            backend = AnthropicBackend(api_key="sk-ant-test", model_id="claude-haiku-4-5")
            result = await backend.invoke(
                [HumanMessage(content="hi")],
                output_schema=_TITLELESS_SCHEMA,
            )

        assert hasattr(result, "content")
        parsed = json.loads(result.content)
        assert parsed == {"name": "Alice"}

    async def test_caller_schema_not_mutated(self) -> None:
        """The caller's original dict is not mutated by title injection."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from langchain_core.messages import HumanMessage

        from modulo.model_backends.anthropic import AnthropicBackend

        original_schema = json.loads(json.dumps(_TITLELESS_SCHEMA))

        with patch("modulo.model_backends.anthropic.ChatAnthropic") as mock_chat_cls:
            mock_chat = MagicMock()
            mock_structured = AsyncMock()
            mock_structured.ainvoke = AsyncMock(return_value={"name": "Bob"})
            mock_chat.with_structured_output = MagicMock(return_value=mock_structured)
            mock_chat_cls.return_value = mock_chat

            backend = AnthropicBackend(api_key="sk-ant-test", model_id="claude-haiku-4-5")
            await backend.invoke(
                [HumanMessage(content="hi")],
                output_schema=original_schema,
            )

        assert "title" not in original_schema

    async def test_existing_title_preserved(self) -> None:
        """A schema that already has a title is NOT overridden.

        Covers the false arm of the title-injection check: when the caller's
        schema already carries a top-level title, it must be passed through
        unchanged rather than replaced with the default.
        """
        from unittest.mock import AsyncMock, MagicMock, patch

        from langchain_core.messages import HumanMessage

        from modulo.model_backends.anthropic import AnthropicBackend

        schema_with_title = {**_TITLELESS_SCHEMA, "title": "MyCustomTitle"}

        with patch("modulo.model_backends.anthropic.ChatAnthropic") as mock_chat_cls:
            mock_chat = MagicMock()
            mock_structured = AsyncMock()
            mock_structured.ainvoke = AsyncMock(return_value={"name": "Alice"})
            mock_chat.with_structured_output = MagicMock(return_value=mock_structured)
            mock_chat_cls.return_value = mock_chat

            backend = AnthropicBackend(api_key="sk-ant-test", model_id="claude-haiku-4-5")
            await backend.invoke(
                [HumanMessage(content="hi")],
                output_schema=schema_with_title,
            )

        call_kwargs = mock_chat.with_structured_output.call_args
        passed_schema = call_kwargs.kwargs.get("schema") or call_kwargs[1].get("schema")
        assert passed_schema["title"] == "MyCustomTitle"
        assert schema_with_title["title"] == "MyCustomTitle"

    async def test_construction_failure_falls_back(self) -> None:
        """When with_structured_output raises ValueError, invoke() falls back."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from langchain_core.messages import AIMessage, HumanMessage

        from modulo.model_backends.anthropic import AnthropicBackend

        with patch("modulo.model_backends.anthropic.ChatAnthropic") as mock_chat_cls:
            mock_chat = MagicMock()
            mock_chat.with_structured_output = MagicMock(side_effect=ValueError("Unsupported function"))
            mock_chat.ainvoke = AsyncMock(return_value=AIMessage(content="fallback"))
            mock_chat_cls.return_value = mock_chat

            backend = AnthropicBackend(api_key="sk-ant-test", model_id="claude-haiku-4-5")
            result = await backend.invoke(
                [HumanMessage(content="hi")],
                output_schema=_TITLELESS_SCHEMA,
            )

        mock_chat.ainvoke.assert_awaited_once()
        assert result.content == "fallback"

    async def test_api_status_error_still_classified(self) -> None:
        """AnthropicStatusError from structured ainvoke is still classified."""
        from unittest.mock import AsyncMock, MagicMock, patch

        import httpx2
        from anthropic import APIStatusError as AnthropicAPIStatusError
        from langchain_core.messages import HumanMessage

        from modulo.model_backends.anthropic import AnthropicBackend
        from modulo.model_backends.base import ProviderUnavailableError

        request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
        server_error = AnthropicAPIStatusError(
            message="Internal Server Error",
            response=httpx2.Response(503, request=request),
            body={"error": {"message": "boom"}},
        )

        with patch("modulo.model_backends.anthropic.ChatAnthropic") as mock_chat_cls:
            mock_chat = MagicMock()
            mock_structured = AsyncMock()
            mock_structured.ainvoke = AsyncMock(side_effect=server_error)
            mock_chat.with_structured_output = MagicMock(return_value=mock_structured)
            mock_chat_cls.return_value = mock_chat

            backend = AnthropicBackend(api_key="sk-ant-test", model_id="claude-haiku-4-5")
            with pytest.raises(ProviderUnavailableError) as exc_info:
                await backend.invoke(
                    [HumanMessage(content="hi")],
                    output_schema=_TITLELESS_SCHEMA,
                )
            assert "HTTP 503" in str(exc_info.value)
