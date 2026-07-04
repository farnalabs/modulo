"""VertexAIBackend — wraps ChatVertexAI for Google Vertex AI models."""

from collections.abc import AsyncIterator
from typing import Any

from langchain_core.messages import BaseMessage, HumanMessage
from langchain_google_vertexai import ChatVertexAI

from modulo.model_backends.base import HealthResult, ModelBackendBase

VERTEXAI_DEFAULT_LOCATION = "us-central1"


class VertexAIBackend(ModelBackendBase):
    """Thin adapter over ChatVertexAI for Google Vertex AI (Gemini, Claude, Llama)."""

    supports_tools: bool = True

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

    async def health_check(self) -> HealthResult:
        try:
            await self._model.ainvoke([HumanMessage(content="ping")], max_tokens=1)
            return HealthResult(ok=True)
        except Exception as exc:
            return HealthResult(ok=False, detail=str(exc)[:500])

    async def invoke(self, messages: list[BaseMessage], **kwargs: Any) -> BaseMessage:
        return await self._model.ainvoke(messages, **kwargs)

    def stream(
    self, messages: list[BaseMessage], tools: list[dict] | None = None, **kwargs: Any,
) -> AsyncIterator[BaseMessage]:
        return self._model.astream(messages, **kwargs)
