from collections.abc import AsyncIterator
from typing import Any

from langchain_core.messages import BaseMessage
from langchain_openai import ChatOpenAI

from modulo.model_backends.base import HealthResult, ModelBackendBase, openai_compatible_health_check


class OpenAICompatibleBackend(ModelBackendBase):
    """Single backend for all OpenAI-compatible providers.
    Parameterized by base_url, api_key, and provider name."""

    supports_tools: bool = True

    def __init__(
        self,
        api_key: str | None = None,
        model_id: str = "",
        base_url: str | None = None,
        provider: str = "openai",
        **default_params: Any,
    ) -> None:
        resolved_api_key = api_key or provider
        self._base_url = base_url.rstrip("/") if base_url else None

        self._model = ChatOpenAI(
            model=model_id,
            api_key=resolved_api_key,
            base_url=self._base_url,
            **default_params,
        )
        self._backend_id = f"{provider}/{model_id}"
        self._api_key = resolved_api_key

    @property
    def base_url(self) -> str | None:
        return self._base_url

    @property
    def backend_id(self) -> str:
        return self._backend_id

    def __repr__(self) -> str:
        return f"OpenAICompatibleBackend(provider={self._backend_id!r})"

    async def health_check(self) -> HealthResult:
        return await openai_compatible_health_check(
            base_url=self._base_url or "https://api.openai.com/v1",
            api_key=self._api_key,
        )

    async def invoke(self, messages: list[BaseMessage], **kwargs: Any) -> BaseMessage:
        return await self._model.ainvoke(messages, **kwargs)

    def stream(
        self,
        messages: list[BaseMessage],
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[BaseMessage]:
        return self._model.astream(messages, tools=tools, **kwargs)
