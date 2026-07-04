"""OllamaBackend — wraps ChatOpenAI pointed at Ollama's OpenAI-compatible endpoint."""

from collections.abc import AsyncIterator
from typing import Any

from langchain_core.messages import BaseMessage
from langchain_openai import ChatOpenAI

from modulo.model_backends.base import HealthResult, ModelBackendBase, _openai_compatible_health_check

DEFAULT_OLLAMA_BASE_URL = "http://localhost:11434/v1"


class OllamaBackend(ModelBackendBase):
    """Thin adapter over ChatOpenAI targeting Ollama's OpenAI-compatible API.

    Ollama does not require an API key by default, but the LangChain ChatOpenAI
    client enforces a non-None api_key. If None is passed, we use "ollama"
    as a placeholder.
    """

    def __init__(
        self,
        api_key: str | None,
        model_id: str,
        base_url: str = DEFAULT_OLLAMA_BASE_URL,
        **default_params: Any,
    ) -> None:
        self._model = ChatOpenAI(
            model=model_id,
            api_key=api_key or "ollama",
            base_url=base_url.rstrip("/"),
            **default_params,
        )
        self._backend_id = f"ollama/{model_id}"
        self._base_url = base_url
        self._api_key = api_key or ""

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def backend_id(self) -> str:
        return self._backend_id

    def __repr__(self) -> str:
        return f"OllamaBackend(model_id={self._backend_id!r}, base_url={self._base_url!r})"

    async def health_check(self) -> HealthResult:
        return await _openai_compatible_health_check(
            base_url=self._base_url,
            api_key=self._api_key,
        )

    async def invoke(self, messages: list[BaseMessage], **kwargs: Any) -> BaseMessage:
        return await self._model.ainvoke(messages, **kwargs)

    def stream(
    self, messages: list[BaseMessage], tools: list[dict] | None = None, **kwargs: Any,
) -> AsyncIterator[BaseMessage]:
        return self._model.astream(messages, **kwargs)
