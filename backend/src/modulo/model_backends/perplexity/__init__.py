"""PerplexityBackend — wraps ChatOpenAI pointed at PerplexityAI's API."""

from collections.abc import AsyncIterator
from typing import Any

from langchain_core.messages import BaseMessage
from langchain_openai import ChatOpenAI

from modulo.model_backends.base import HealthResult, ModelBackendBase, openai_compatible_health_check

PERPLEXITY_BASE_URL = "https://api.perplexity.ai"


class PerplexityBackend(ModelBackendBase):
    """Thin adapter over ChatOpenAI targeting PerplexityAI's API."""

    supports_tools: bool = True

    def __init__(self, api_key: str, model_id: str, **default_params: Any) -> None:
        self._model = ChatOpenAI(
            model=model_id,
            api_key=api_key,
            base_url=PERPLEXITY_BASE_URL,
            **default_params,
        )
        self._backend_id = f"perplexity/{model_id}"
        self._api_key = api_key

    @property
    def backend_id(self) -> str:
        return self._backend_id

    def __repr__(self) -> str:
        return f"PerplexityBackend(model_id={self._backend_id!r})"

    async def health_check(self) -> HealthResult:
        return await openai_compatible_health_check(
            base_url=PERPLEXITY_BASE_URL,
            api_key=self._api_key,
        )

    async def invoke(self, messages: list[BaseMessage], **kwargs: Any) -> BaseMessage:
        return await self._model.ainvoke(messages, **kwargs)

    def stream(
    self, messages: list[BaseMessage], tools: list[dict] | None = None, **kwargs: Any,
) -> AsyncIterator[BaseMessage]:
        return self._model.astream(messages, tools=tools, **kwargs)
