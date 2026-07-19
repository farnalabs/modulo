"""VertexAIBackend — wraps ChatVertexAI for Google Vertex AI models."""

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any

from langchain_core.messages import BaseMessage, HumanMessage
from langchain_google_vertexai import ChatVertexAI

from modulo.model_backends.base import (
    HEALTH_CHECK_TIMEOUT,
    HEALTH_DETAIL_MAX_LENGTH,
    HealthResult,
    ModelBackendBase,
)

logger = logging.getLogger(__name__)

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
            await asyncio.wait_for(
                self._model.ainvoke([HumanMessage(content="ping")], max_tokens=1),
                timeout=HEALTH_CHECK_TIMEOUT,
            )
            return HealthResult(ok=True)
        except TimeoutError:
            logger.warning("Health check timed out for VertexAIBackend")
            return HealthResult(ok=False, detail="Health check timed out")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Health check failed for VertexAIBackend: %s", exc)
            return HealthResult(ok=False, detail=str(exc)[:HEALTH_DETAIL_MAX_LENGTH])

    async def invoke(self, messages: list[BaseMessage], **kwargs: Any) -> BaseMessage:
        return await self._model.ainvoke(messages, **kwargs)

    def stream(
        self,
        messages: list[BaseMessage],
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[BaseMessage]:
        return self._model.astream(messages, tools=tools, **kwargs)
