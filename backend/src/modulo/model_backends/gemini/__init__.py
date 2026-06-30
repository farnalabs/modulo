"""GeminiBackend — wraps ChatGoogleGenerativeAI for Google Gemini models."""

from collections.abc import AsyncIterator
from typing import Any

from langchain_core.messages import BaseMessage
from langchain_google_genai import ChatGoogleGenerativeAI

from modulo.model_backends.base import ModelBackendBase

GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"


class GeminiBackend(ModelBackendBase):
    """Thin adapter over ChatGoogleGenerativeAI targeting Gemini API."""

    def __init__(self, api_key: str, model_id: str, **default_params: Any) -> None:
        self._model = ChatGoogleGenerativeAI(
            model=model_id,
            api_key=api_key,
            **default_params,
        )
        self._backend_id = f"gemini/{model_id}"

    @property
    def backend_id(self) -> str:
        return self._backend_id

    def __repr__(self) -> str:
        return f"GeminiBackend(model_id={self._backend_id!r})"

    async def invoke(self, messages: list[BaseMessage], **kwargs: Any) -> BaseMessage:
        return await self._model.ainvoke(messages, **kwargs)

    def stream(self, messages: list[BaseMessage], **kwargs: Any) -> AsyncIterator[BaseMessage]:
        return self._model.astream(messages, **kwargs)
