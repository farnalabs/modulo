"""BedrockBackend — wraps ChatBedrock as a Modulo ModelBackendBase."""

from typing import Any

from langchain_aws import ChatBedrock

from modulo.model_backends.base import (
    LangChainChatForwardingMixin,
    ModelBackendBase,
)


class BedrockBackend(LangChainChatForwardingMixin, ModelBackendBase):
    """Thin adapter over ChatBedrock."""

    supports_tools: bool = True
    supports_native_structured_output: bool = False

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
