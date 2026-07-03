"""GrokBackend — wraps ChatOpenAI pointed at xAI's OpenAI-compatible endpoint."""

from collections.abc import AsyncIterator
from typing import Any

from langchain_core.messages import BaseMessage
from langchain_openai import ChatOpenAI

from modulo.model_backends.base import HealthResult, ModelBackendBase, _openai_compatible_health_check

XAI_BASE_URL = "https://api.x.ai/v1"


class GrokBackend(ModelBackendBase):
    """Thin adapter over ChatOpenAI targeting xAI Grok's OpenAI-compatible API."""

    def __init__(self, api_key: str, model_id: str, **default_params: Any) -> None:
        self._model = ChatOpenAI(
            model=model_id,
            api_key=api_key,
            base_url=XAI_BASE_URL,
            **default_params,
        )
        self._backend_id = f"grok/{model_id}"
        self._api_key = api_key

    @property
    def backend_id(self) -> str:
        return self._backend_id

    def __repr__(self) -> str:
        return f"GrokBackend(model_id={self._backend_id!r})"

    async def health_check(self) -> HealthResult:
        return await _openai_compatible_health_check(
            base_url=XAI_BASE_URL,
            api_key=self._api_key,
        )

    async def invoke(self, messages: list[BaseMessage], **kwargs: Any) -> BaseMessage:
        return await self._model.ainvoke(messages, **kwargs)

    def stream(self, messages: list[BaseMessage], **kwargs: Any) -> AsyncIterator[BaseMessage]:
        return self._model.astream(messages, **kwargs)
