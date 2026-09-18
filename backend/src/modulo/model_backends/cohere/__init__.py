"""CohereBackend — wraps ChatCohere as a Modulo ModelBackendBase."""

from typing import Any

from langchain_cohere import ChatCohere

from modulo.model_backends.base import (
    HealthResult,
    LangChainChatForwardingMixin,
    ModelBackendBase,
    openai_compatible_health_check,
)

COHERE_BASE_URL = "https://api.cohere.ai/v1"


class CohereBackend(LangChainChatForwardingMixin, ModelBackendBase):
    """Thin adapter over ChatCohere."""

    supports_tools: bool = True
    supports_native_structured_output: bool = False

    def __init__(self, api_key: str, model_id: str, **default_params: Any) -> None:
        self._model = ChatCohere(
            model=model_id,
            api_key=api_key,
            base_url=COHERE_BASE_URL,
            **default_params,
        )
        self._backend_id = f"cohere/{model_id}"
        self._api_key = api_key

    @property
    def backend_id(self) -> str:
        return self._backend_id

    def __repr__(self) -> str:
        return f"CohereBackend(model_id={self._backend_id!r})"

    async def health_check(self) -> HealthResult:
        return await openai_compatible_health_check(
            base_url=COHERE_BASE_URL,
            api_key=self._api_key,
        )
