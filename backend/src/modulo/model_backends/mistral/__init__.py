"""MistralBackend — wraps ChatMistralAI as a Modulo ModelBackendBase."""

from collections.abc import AsyncIterator
from typing import Any

from langchain_core.messages import BaseMessage
from langchain_mistralai import ChatMistralAI

from modulo.model_backends.base import ModelBackendBase

MISTRAL_BASE_URL = "https://api.mistral.ai/v1"


class MistralBackend(ModelBackendBase):
    """Thin adapter over ChatMistralAI for Mistral's API."""

    def __init__(self, api_key: str, model_id: str, **default_params: Any) -> None:
        self._model = ChatMistralAI(
            model=model_id,
            mistral_api_key=api_key,
            endpoint=MISTRAL_BASE_URL,
            **default_params,
        )
        self._backend_id = f"mistral/{model_id}"

    @property
    def backend_id(self) -> str:
        return self._backend_id

    def __repr__(self) -> str:
        return f"MistralBackend(model_id={self._backend_id!r})"

    async def invoke(self, messages: list[BaseMessage], **kwargs: Any) -> BaseMessage:
        return await self._model.ainvoke(messages, **kwargs)

    def stream(self, messages: list[BaseMessage], **kwargs: Any) -> AsyncIterator[BaseMessage]:
        return self._model.astream(messages, **kwargs)
