"""Deterministic, input-keyed LangChain chat model used by automated tests."""

from collections.abc import Mapping, Sequence
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from modulo.model_backends.base import HealthResult


class UnexpectedInputError(LookupError):
    """Raised when a stub invocation has no fixture for its normalized input."""

    def __init__(self, normalized_input: str) -> None:
        super().__init__(f"No StubModelBackend fixture matches input: {normalized_input!r}")
        self.normalized_input = normalized_input


def normalize_input(messages: Sequence[BaseMessage]) -> str:
    """Return the stable fixture key for a LangChain message sequence."""
    content = "\n".join(_content_as_text(message.content) for message in messages)
    return " ".join(content.split())


def _content_as_text(content: str | list[str | dict[str, Any]] | None) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content

    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict):
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "\n".join(parts)


class StubModelBackend(BaseChatModel):
    """A strict test double with responses keyed by normalized input content."""

    fixture_map: dict[str, str]

    def __init__(self, fixture_map: Mapping[str, str] | None = None, **kwargs: Any) -> None:
        normalized_fixtures: dict[str, str] = {}
        for input_content, response in (fixture_map or {}).items():
            normalized_key = " ".join(input_content.split())
            existing = normalized_fixtures.get(normalized_key)
            if existing is not None and existing != response:
                raise ValueError(f"Conflicting fixtures normalize to {normalized_key!r}")
            normalized_fixtures[normalized_key] = response
        super().__init__(fixture_map=normalized_fixtures, **kwargs)

    @property
    def _llm_type(self) -> str:
        return "modulo-stub"

    def __repr__(self) -> str:
        count = len(self.fixture_map)
        return f"StubModelBackend(fixtures={count})"

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        del stop, run_manager, kwargs
        return self._result_for(messages)

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        del stop, run_manager, kwargs
        return self._result_for(messages)

    async def health_check(self) -> HealthResult:
        return HealthResult(ok=True, detail="Stub backend always healthy")

    def _result_for(self, messages: Sequence[BaseMessage]) -> ChatResult:
        normalized_input = normalize_input(messages)
        try:
            response = self.fixture_map[normalized_input]
        except KeyError as error:
            raise UnexpectedInputError(normalized_input) from error
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=response))])
