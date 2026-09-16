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
