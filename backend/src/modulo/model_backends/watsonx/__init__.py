"""WatsonXBackend — wraps ChatWatsonx as a Modulo ModelBackendBase."""

from typing import Any

from langchain_ibm import ChatWatsonx

from modulo.model_backends.base import (
    LangChainChatForwardingMixin,
    ModelBackendBase,
)

WATSONX_BASE_URL = "https://us-south.ml.cloud.ibm.com"


class WatsonXBackend(LangChainChatForwardingMixin, ModelBackendBase):
    """Thin adapter over ChatWatsonx for IBM watsonx.ai models."""

    supports_tools: bool = True
    supports_native_structured_output: bool = False

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
