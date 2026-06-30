"""BedrockBackend — wraps ChatBedrock as a Modulo ModelBackendBase."""

from collections.abc import AsyncIterator
from typing import Any

from langchain_aws import ChatBedrock
from langchain_core.messages import BaseMessage

from modulo.model_backends.base import ModelBackendBase


class BedrockBackend(ModelBackendBase):
    """Thin adapter over ChatBedrock."""

    def __init__(
        self,
        aws_access_key_id: str,
        aws_secret_access_key: str,
        model_id: str,
        region: str = "us-east-1",
        **default_params: Any,
    ) -> None:
        self._model = ChatBedrock(
            model_id=model_id,
            region_name=region,
            aws_access_key_id=aws_access_key_id,
            aws_secret_access_key=aws_secret_access_key,
            **default_params,
        )
        self._backend_id = f"bedrock/{model_id}"

    @property
    def backend_id(self) -> str:
        return self._backend_id

    def __repr__(self) -> str:
        return f"BedrockBackend(model_id={self._backend_id!r})"

    async def invoke(self, messages: list[BaseMessage], **kwargs: Any) -> BaseMessage:
        return await self._model.ainvoke(messages, **kwargs)

    def stream(self, messages: list[BaseMessage], **kwargs: Any) -> AsyncIterator[BaseMessage]:
        return self._model.astream(messages, **kwargs)
