"""Ai21Backend — wraps ChatOpenAI pointed at AI21 Labs' OpenAI-compatible endpoint."""

from collections.abc import AsyncIterator
from typing import Any

from langchain_core.messages import BaseMessage
from langchain_openai import ChatOpenAI

from modulo.model_backends.base import HealthResult, ModelBackendBase, openai_compatible_health_check

AI21_BASE_URL = "https://api.ai21.com/studio/v1"


class Ai21Backend(ModelBackendBase):
    """Thin adapter over ChatOpenAI targeting AI21 Labs' OpenAI-compatible API."""

    supports_tools: bool = True

    def __init__(
        self,
        api_key: str,
        model_id: str,
        **default_params: Any,
    ) -> None:
        self._model = ChatOpenAI(
            model=model_id,
            api_key=api_key,
            base_url=AI21_BASE_URL,
            **default_params,
        )
        self._backend_id = f"ai21/{model_id}"
        self._api_key = api_key

    @property
    def backend_id(self) -> str:
        return self._backend_id

    def __repr__(self) -> str:
        return f"Ai21Backend(model_id={self._backend_id!r})"

    async def health_check(self) -> HealthResult:
        return await openai_compatible_health_check(
            base_url=AI21_BASE_URL,
            api_key=self._api_key,
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
