"""JanBackend — wraps ChatOpenAI pointed at Jan's OpenAI-compatible endpoint."""

from collections.abc import AsyncIterator
from typing import Any

from langchain_core.messages import BaseMessage
from langchain_openai import ChatOpenAI

from modulo.model_backends.base import ModelBackendBase

DEFAULT_JAN_BASE_URL = "http://localhost:1337/v1"


class JanBackend(ModelBackendBase):
    """Thin adapter over ChatOpenAI targeting Jan's OpenAI-compatible API.

    Jan does not require an API key by default, but the LangChain ChatOpenAI
    client enforces a non-None api_key. If None is passed, we use "jan"
    as a placeholder.
    """

    def __init__(
        self,
        api_key: str | None,
        model_id: str,
        base_url: str = DEFAULT_JAN_BASE_URL,
        **default_params: Any,
    ) -> None:
        self._model = ChatOpenAI(
            model=model_id,
            api_key=api_key or "jan",
            base_url=base_url.rstrip("/"),
            **default_params,
        )
        self._backend_id = f"jan/{model_id}"
        self._base_url = base_url

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def backend_id(self) -> str:
        return self._backend_id

    def __repr__(self) -> str:
        return f"JanBackend(model_id={self._backend_id!r}, base_url={self._base_url!r})"

    async def invoke(self, messages: list[BaseMessage], **kwargs: Any) -> BaseMessage:
        return await self._model.ainvoke(messages, **kwargs)

    def stream(self, messages: list[BaseMessage], **kwargs: Any) -> AsyncIterator[BaseMessage]:
        return self._model.astream(messages, **kwargs)
