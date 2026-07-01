"""QwenBackend — wraps ChatOpenAI pointed at Alibaba Cloud's DashScope API."""

from collections.abc import AsyncIterator
from typing import Any

from langchain_core.messages import BaseMessage
from langchain_openai import ChatOpenAI

from modulo.model_backends.base import ModelBackendBase


class QwenBackend(ModelBackendBase):
    def __init__(self, api_key: str, model_id: str, **default_params: Any) -> None:
        self._model = ChatOpenAI(
            model=model_id,
            api_key=api_key,
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            **default_params,
        )
        self._backend_id = f"qwen/{model_id}"

    @property
    def backend_id(self) -> str:
        return self._backend_id

    def __repr__(self) -> str:
        return f"QwenBackend(model_id={self._backend_id!r})"

    async def invoke(self, messages: list[BaseMessage], **kwargs: Any) -> BaseMessage:
        return await self._model.ainvoke(messages, **kwargs)

    def stream(self, messages: list[BaseMessage], **kwargs: Any) -> AsyncIterator[BaseMessage]:
        return self._model.astream(messages, **kwargs)
