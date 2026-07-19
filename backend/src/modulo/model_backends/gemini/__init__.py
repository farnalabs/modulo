"""GeminiBackend — wraps ChatGoogleGenerativeAI for Google Gemini models."""

import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx
from langchain_core.messages import BaseMessage
from langchain_google_genai import ChatGoogleGenerativeAI

from modulo.model_backends.base import (
    HEALTH_CHECK_TIMEOUT,
    HEALTH_DETAIL_MAX_LENGTH,
    HealthResult,
    ModelBackendBase,
)

logger = logging.getLogger(__name__)

GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"


class GeminiBackend(ModelBackendBase):
    """Thin adapter over ChatGoogleGenerativeAI targeting Gemini API."""

    supports_tools: bool = True

    def __init__(self, api_key: str, model_id: str, **default_params: Any) -> None:
        self._model = ChatGoogleGenerativeAI(
            model=model_id,
            api_key=api_key,
            **default_params,
        )
        self._backend_id = f"gemini/{model_id}"
        self._api_key = api_key

    @property
    def backend_id(self) -> str:
        return self._backend_id

    def __repr__(self) -> str:
        return f"GeminiBackend(model_id={self._backend_id!r})"

    async def health_check(self) -> HealthResult:
        try:
            async with httpx.AsyncClient(timeout=HEALTH_CHECK_TIMEOUT) as client:
                response = await client.get(
                    f"{GEMINI_BASE_URL}/models",
                    headers={"x-goog-api-key": self._api_key},
                )
                if response.is_success:
                    return HealthResult(ok=True)
                return HealthResult(ok=False, detail=response.text[:HEALTH_DETAIL_MAX_LENGTH])
        except httpx.TimeoutException:
            logger.warning("Health check timed out for GeminiBackend")
            return HealthResult(ok=False, detail="Health check timed out")
        except httpx.HTTPError as exc:
            logger.warning("Health check failed for GeminiBackend: %s", exc)
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
