"""OpenAIBackend — wraps ChatOpenAI as a Modulo ModelBackendBase."""

from collections.abc import AsyncIterator
from typing import Any

from langchain_core.messages import BaseMessage
from langchain_openai import ChatOpenAI

from modulo.model_backends.base import HealthResult, ModelBackendBase, _openai_compatible_health_check


class OpenAIBackend(ModelBackendBase):
    """Thin adapter over ChatOpenAI."""

    supports_tools: bool = True

    def __init__(self, api_key: str, model_id: str, **default_params: Any) -> None:
        self._model = ChatOpenAI(model=model_id, api_key=api_key, **default_params)
        self._backend_id = f"openai/{model_id}"
        self._api_key = api_key

    @property
    def backend_id(self) -> str:
        return self._backend_id

    def __repr__(self) -> str:
        return f"OpenAIBackend(model_id={self._backend_id!r})"

    async def health_check(self) -> HealthResult:
        return await _openai_compatible_health_check(
            base_url="https://api.openai.com/v1",
            api_key=self._api_key,
        )

    async def invoke(self, messages: list[BaseMessage], **kwargs: Any) -> BaseMessage:
        return await self._model.ainvoke(messages, **kwargs)

    def stream(self, messages: list[BaseMessage], **kwargs: Any) -> AsyncIterator[BaseMessage]:
        return self._model.astream(messages, **kwargs)
