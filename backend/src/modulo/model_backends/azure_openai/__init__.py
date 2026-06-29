"""AzureOpenAIBackend — wraps ChatOpenAI with Azure OpenAI configuration."""

from collections.abc import AsyncIterator
from typing import Any

from langchain_core.messages import BaseMessage
from langchain_openai import ChatOpenAI

from modulo.model_backends.base import ModelBackendBase


class AzureOpenAIBackend(ModelBackendBase):
    """Thin adapter over ChatOpenAI configured for Azure OpenAI."""

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

    async def invoke(self, messages: list[BaseMessage], **kwargs: Any) -> BaseMessage:
        return await self._model.ainvoke(messages, **kwargs)

    def stream(self, messages: list[BaseMessage], **kwargs: Any) -> AsyncIterator[BaseMessage]:
        return self._model.astream(messages, **kwargs)
