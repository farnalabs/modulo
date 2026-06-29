"""TogetherAIBackend — wraps ChatOpenAI pointed at TogetherAI's OpenAI-compatible endpoint."""

from collections.abc import AsyncIterator
from typing import Any

from langchain_core.messages import BaseMessage
from langchain_openai import ChatOpenAI

from modulo.model_backends.base import ModelBackendBase

TOGETHERAI_BASE_URL = "https://api.together.xyz/v1"


class TogetherAIBackend(ModelBackendBase):
    """Thin adapter over ChatOpenAI targeting TogetherAI's OpenAI-compatible API."""

    def __init__(
        self,
        api_key: str,
        model_id: str,
        **default_params: Any,
    ) -> None:
        self._model = ChatOpenAI(
            model=model_id,
            api_key=api_key,
            base_url=TOGETHERAI_BASE_URL,
            **default_params,
        )
        self._backend_id = f"togetherai/{model_id}"

    @property
    def backend_id(self) -> str:
        return self._backend_id

    def __repr__(self) -> str:
        return f"TogetherAIBackend(model_id={self._backend_id!r})"

    async def invoke(self, messages: list[BaseMessage], **kwargs: Any) -> BaseMessage:
        return await self._model.ainvoke(messages, **kwargs)

    def stream(self, messages: list[BaseMessage], **kwargs: Any) -> AsyncIterator[BaseMessage]:
        return self._model.astream(messages, **kwargs)
