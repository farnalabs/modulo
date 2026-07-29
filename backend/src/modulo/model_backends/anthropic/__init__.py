import logging
from collections.abc import AsyncIterator
from typing import Any

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import BaseMessage

from modulo.model_backends.base import HealthResult, ModelBackendBase, openai_compatible_health_check

logger = logging.getLogger(__name__)

ANTHROPIC_BASE_URL = "https://api.anthropic.com"


class AnthropicBackend(ModelBackendBase):
    """Thin adapter over ChatAnthropic."""

    supports_tools: bool = True

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

    async def invoke(self, messages: list[BaseMessage], **kwargs: Any) -> BaseMessage:
        return await self._model.ainvoke(messages, **kwargs)

    def stream(
        self,
        messages: list[BaseMessage],
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[BaseMessage]:
        return self._model.astream(messages, tools=tools, **kwargs)
