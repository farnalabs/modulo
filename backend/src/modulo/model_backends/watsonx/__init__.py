"""WatsonXBackend — wraps ChatWatsonx as a Modulo ModelBackendBase."""

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any

from langchain_core.messages import BaseMessage, HumanMessage
from langchain_ibm import ChatWatsonx

from modulo.model_backends.base import (
    HEALTH_CHECK_TIMEOUT,
    HEALTH_DETAIL_MAX_LENGTH,
    HealthResult,
    ModelBackendBase,
)

logger = logging.getLogger(__name__)

WATSONX_BASE_URL = "https://us-south.ml.cloud.ibm.com"


class WatsonXBackend(ModelBackendBase):
    """Thin adapter over ChatWatsonx for IBM watsonx.ai models."""

    supports_tools: bool = True

    def __init__(
        self,
        api_key: str,
        model_id: str,
        project_id: str,
        url: str = WATSONX_BASE_URL,
        **default_params: Any,
    ) -> None:
        self._model = ChatWatsonx(
            model_id=model_id,
            url=url,
            project_id=project_id,
            api_key=api_key,
            **default_params,
        )
        self._backend_id = f"watsonx/{model_id}"

    @property
    def backend_id(self) -> str:
        return self._backend_id

    def __repr__(self) -> str:
        return f"WatsonXBackend(model_id={self._backend_id!r})"

    async def health_check(self) -> HealthResult:
        try:
            await asyncio.wait_for(
                self._model.ainvoke([HumanMessage(content="ping")], max_tokens=1),
                timeout=HEALTH_CHECK_TIMEOUT,
            )
            return HealthResult(ok=True)
        except TimeoutError:
            logger.warning("Health check timed out for WatsonXBackend")
            return HealthResult(ok=False, detail="Health check timed out")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Health check failed for WatsonXBackend: %s", exc)
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
