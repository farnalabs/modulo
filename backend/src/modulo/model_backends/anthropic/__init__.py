import json
from collections.abc import AsyncIterator
from typing import Any

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import AIMessage, BaseMessage

from modulo.model_backends.base import HealthResult, ModelBackendBase, openai_compatible_health_check

ANTHROPIC_BASE_URL = "https://api.anthropic.com"


class AnthropicBackend(ModelBackendBase):
    """Thin adapter over ChatAnthropic."""

    supports_tools: bool = True
    supports_native_structured_output: bool = True

    def __init__(self, api_key: str, model_id: str, **default_params: Any) -> None:
        self._model = ChatAnthropic(model=model_id, api_key=api_key, **default_params)
        self._backend_id = f"anthropic/{model_id}"
        self._api_key = api_key

    @property
    def backend_id(self) -> str:
        return self._backend_id

    def __repr__(self) -> str:
        return f"AnthropicBackend(model_id={self._backend_id!r})"

    async def health_check(self) -> HealthResult:
        return await openai_compatible_health_check(
            base_url=ANTHROPIC_BASE_URL,
            api_key=None,
            extra_headers={"x-api-key": self._api_key, "anthropic-version": "2023-06-01"},
        )

    async def invoke(
        self,
        messages: list[BaseMessage],
        output_schema: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> BaseMessage:
        if output_schema is not None:
            structured = self._model.with_structured_output(schema=output_schema)
            result = await structured.ainvoke(messages, **kwargs)
            # with_structured_output() returns dict | BaseModel; serialise
            # to JSON so _invoke_node_model's json.loads() round-trips it.
            if hasattr(result, "model_dump"):
                content = json.dumps(result.model_dump())
            elif isinstance(result, dict):
                content = json.dumps(result)
            else:
                content = str(result)
            return AIMessage(content=content)
        return await self._model.ainvoke(messages, **kwargs)

    def stream(
        self,
        messages: list[BaseMessage],
        tools: list[dict[str, Any]] | None = None,
        output_schema: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[BaseMessage]:
        if output_schema is not None:
            structured = self._model.with_structured_output(schema=output_schema)
            return structured.astream(messages, tools=tools, **kwargs)  # type: ignore[return-value]
        return self._model.astream(messages, tools=tools, **kwargs)
