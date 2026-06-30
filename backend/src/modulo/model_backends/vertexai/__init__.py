"""VertexAIBackend — wraps ChatVertexAI for Google Vertex AI models."""

from collections.abc import AsyncIterator
from typing import Any

from langchain_core.messages import BaseMessage
from langchain_google_vertexai import ChatVertexAI

from modulo.model_backends.base import ModelBackendBase

VERTEXAI_DEFAULT_LOCATION = "us-central-1"


class VertexAIBackend(ModelBackendBase):
    """Thin adapter over ChatVertexAI for Google Vertex AI (Gemini, Claude, Llama)."""

    def __init__(
        self,
        project: str,
        model_id: str,
        location: str = VERTEXAI_DEFAULT_LOCATION,
        **default_params: Any,
    ) -> None:
        self._model = ChatVertexAI(
            model_name=model_id,
            project=project,
            location=location,
            **default_params,
        )
        self._backend_id = f"vertexai/{model_id}"

    @property
    def backend_id(self) -> str:
        return self._backend_id

    def __repr__(self) -> str:
        return f"VertexAIBackend(model_id={self._backend_id!r})"

    async def invoke(self, messages: list[BaseMessage], **kwargs: Any) -> BaseMessage:
        return await self._model.ainvoke(messages, **kwargs)

    def stream(self, messages: list[BaseMessage], **kwargs: Any) -> AsyncIterator[BaseMessage]:
        return self._model.astream(messages, **kwargs)
