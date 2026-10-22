"""MistralBackend — wraps ChatMistralAI as a Modulo ModelBackendBase."""

from typing import Any

import httpx
from langchain_mistralai import ChatMistralAI

from modulo.model_backends.base import (
    HealthResult,
    LangChainChatForwardingMixin,
    ModelBackendBase,
    openai_compatible_health_check,
    pinned_endpoint_client,
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
        # FAR-2046 / FAR-512: a custom endpoint must never bypass the SSRF gate.
        # ChatMistralAI accepts an ``async_client`` constructor field and only
        # builds one when it is absent, so passing a pinned client closes the
        # DNS-rebinding window for the async path this adapter uses. The
        # adapter builds the same request headers LangChain would.
        self._http_async_client: httpx.AsyncClient | None = None
        if endpoint:
            self._http_async_client = pinned_endpoint_client(
                endpoint,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "Authorization": f"Bearer {api_key}",
                },
            )
            model_kwargs["async_client"] = self._http_async_client
            model_kwargs["endpoint"] = endpoint
        self._model = ChatMistralAI(model=model_id, mistral_api_key=api_key, **model_kwargs)
        self._backend_id = f"mistral/{model_id}"
        self._api_key = api_key

    async def aclose(self) -> None:
        """Close the pinned HTTP client owned by this backend (idempotent)."""
        client, self._http_async_client = self._http_async_client, None
        if client is not None:
            await client.aclose()

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
