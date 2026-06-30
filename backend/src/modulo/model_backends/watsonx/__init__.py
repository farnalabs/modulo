"""WatsonXBackend — wraps ChatWatsonx as a Modulo ModelBackendBase."""

from collections.abc import AsyncIterator
from typing import Any

from langchain_core.messages import BaseMessage, HumanMessage
from langchain_ibm import ChatWatsonx

from modulo.model_backends.base import HealthResult, ModelBackendBase

WATSONX_BASE_URL = "https://us-south.ml.cloud.ibm.com"


class WatsonXBackend(ModelBackendBase):
    """Thin adapter over ChatWatsonx for IBM watsonx.ai models."""

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
            await self._model.ainvoke([HumanMessage(content="ping")], max_tokens=1)
            return HealthResult(ok=True)
        except Exception as exc:
            return HealthResult(ok=False, detail=str(exc)[:500])

    async def invoke(self, messages: list[BaseMessage], **kwargs: Any) -> BaseMessage:
        return await self._model.ainvoke(messages, **kwargs)

    def stream(self, messages: list[BaseMessage], **kwargs: Any) -> AsyncIterator[BaseMessage]:
        return self._model.astream(messages, **kwargs)
