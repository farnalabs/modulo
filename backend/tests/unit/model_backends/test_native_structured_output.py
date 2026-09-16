"""FAR-898: tests for native structured output plumbing across model backends.

Covers:
- Every known ModelBackendBase subclass explicitly declares supports_native_structured_output
- Only OpenAI and Anthropic backends are True
- A False backend is never passed the output_schema kwarg (tested via _invoke_node_model)
- output_schema=None → no kwarg, no behavioural change
- When flag is True and schema is supplied, the backend receives it
- FakeStructuredOutputBackend reusable test double
"""

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
        """OpenAICompatibleBackend.invoke() calls with_structured_output(method='json_schema')."""
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

        # Assert with_structured_output was called with correct parameters
        mock_chat.with_structured_output.assert_called_once_with(
            schema=schema,
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
