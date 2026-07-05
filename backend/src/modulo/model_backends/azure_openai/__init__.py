"""AzureOpenAIBackend — wraps ChatOpenAI with Azure OpenAI configuration."""

import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx
from langchain_core.messages import BaseMessage
from langchain_openai import ChatOpenAI

from modulo.model_backends.base import (
    HEALTH_CHECK_TIMEOUT,
    HEALTH_DETAIL_MAX_LENGTH,
    HealthResult,
    ModelBackendBase,
)

logger = logging.getLogger(__name__)


class AzureOpenAIBackend(ModelBackendBase):
    """Thin adapter over ChatOpenAI configured for Azure OpenAI."""

    supports_tools: bool = True

    def __init__(
        self,
        api_key: str,
        model_id: str,
        azure_endpoint: str,
        api_version: str = "2024-10-01-preview",
        **default_params: Any,
    ) -> None:
        self._model = ChatOpenAI(
            model=model_id,
            api_key=api_key,
            azure_deployment=model_id,
            azure_endpoint=azure_endpoint.rstrip("/"),
            api_version=api_version,
            **default_params,
        )
        self._backend_id = f"azure_openai/{model_id}"
        self._azure_endpoint = azure_endpoint.rstrip("/")
        self._api_version = api_version
        self._api_key = api_key

    @property
    def azure_endpoint(self) -> str:
        return self._azure_endpoint

    @property
    def api_version(self) -> str:
        return self._api_version

    @property
    def backend_id(self) -> str:
        return self._backend_id

    def __repr__(self) -> str:
        return f"AzureOpenAIBackend(model_id={self._backend_id!r}, endpoint={self._azure_endpoint!r})"

    async def health_check(self) -> HealthResult:
        try:
            async with httpx.AsyncClient(timeout=HEALTH_CHECK_TIMEOUT) as client:
                response = await client.get(
                    f"{self._azure_endpoint}/openai/deployments",
                    params={"api-version": self._api_version},
                    headers={"api-key": self._api_key},
                )
                if response.is_success:
                    return HealthResult(ok=True)
                return HealthResult(ok=False, detail=response.text[:HEALTH_DETAIL_MAX_LENGTH])
        except httpx.TimeoutException:
            logger.warning("Health check timed out for AzureOpenAIBackend")
            return HealthResult(ok=False, detail="Health check timed out")
        except httpx.HTTPError as exc:
            logger.warning("Health check failed for AzureOpenAIBackend: %s", exc)
            return HealthResult(ok=False, detail=str(exc)[:HEALTH_DETAIL_MAX_LENGTH])

    async def invoke(self, messages: list[BaseMessage], **kwargs: Any) -> BaseMessage:
        return await self._model.ainvoke(messages, **kwargs)

    def stream(
    self, messages: list[BaseMessage], tools: list[dict] | None = None, **kwargs: Any,
) -> AsyncIterator[BaseMessage]:
        return self._model.astream(messages, tools=tools, **kwargs)
