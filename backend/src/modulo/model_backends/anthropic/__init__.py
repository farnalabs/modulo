"""AnthropicBackend — wraps ChatAnthropic as a Modulo ModelBackendBase."""

from collections.abc import AsyncIterator
from typing import Any

import httpx
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import BaseMessage

from modulo.model_backends.base import HealthResult, ModelBackendBase

ANTHROPIC_BASE_URL = "https://api.anthropic.com"


class AnthropicBackend(ModelBackendBase):
    """Thin adapter over ChatAnthropic."""

    supports_tools: bool = True

    def __init__(self, api_key: str, model_id: str, **default_params: Any) -> None:
        self._model = ChatAnthropic(model=model_id, api_key=api_key, **default_params)
        self._backend_id = f"anthropic/{model_id}"
        self._api_key = api_key

    @property
    def backend_id(self) -> str:
        return self._backend_id

    def __repr__(self) -> str:
        return f"AnthropicBackend(model_id={self._backend_id!r})"

    async def health_check(self) -> HealthResult:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(
                    f"{ANTHROPIC_BASE_URL}/v1/models",
                    headers={
                        "x-api-key": self._api_key,
                        "anthropic-version": "2023-06-01",
                    },
                )
                if response.is_success:
                    return HealthResult(ok=True)
                return HealthResult(ok=False, detail=response.text[:500])
        except httpx.TimeoutException:
            return HealthResult(ok=False, detail="Health check timed out")
        except Exception as exc:
            return HealthResult(ok=False, detail=str(exc)[:500])

    async def invoke(self, messages: list[BaseMessage], **kwargs: Any) -> BaseMessage:
        return await self._model.ainvoke(messages, **kwargs)

    def stream(
    self, messages: list[BaseMessage], tools: list[dict] | None = None, **kwargs: Any,
) -> AsyncIterator[BaseMessage]:
        return self._model.astream(messages, **kwargs)
