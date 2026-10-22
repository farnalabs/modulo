"""MistralBackend — wraps ChatMistralAI as a Modulo ModelBackendBase."""

from typing import Any

from langchain_mistralai import ChatMistralAI

from modulo.model_backends.base import (
    HealthResult,
    LangChainChatForwardingMixin,
    ModelBackendBase,
    openai_compatible_health_check,
)

MISTRAL_BASE_URL = "https://api.mistral.ai/v1"


class MistralBackend(LangChainChatForwardingMixin, ModelBackendBase):
    """Thin adapter over ChatMistralAI for Mistral's API."""

    supports_tools: bool = True
    supports_native_structured_output: bool = False

    def __init__(
        self,
        api_key: str,
        model_id: str,
        endpoint: str | None = None,
        **default_params: Any,
    ) -> None:
        self._base_url = endpoint or MISTRAL_BASE_URL
        model_kwargs: dict[str, Any] = dict(default_params)
        if endpoint:
            model_kwargs["endpoint"] = endpoint
        self._model = ChatMistralAI(model=model_id, mistral_api_key=api_key, **model_kwargs)
        self._backend_id = f"mistral/{model_id}"
        self._api_key = api_key

    @property
    def backend_id(self) -> str:
        return self._backend_id

    def __repr__(self) -> str:
        return f"MistralBackend(model_id={self._backend_id!r})"

    async def health_check(self) -> HealthResult:
        return await openai_compatible_health_check(
            base_url=self._base_url,
            api_key=self._api_key,
        )
