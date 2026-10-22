from typing import Any

import httpx
from langchain_google_genai import ChatGoogleGenerativeAI

from modulo.model_backends.base import (
    HealthResult,
    LangChainChatForwardingMixin,
    ModelBackendBase,
    openai_compatible_health_check,
    pinned_endpoint_transport,
)

GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"


class GeminiBackend(LangChainChatForwardingMixin, ModelBackendBase):
    """Thin adapter over ChatGoogleGenerativeAI targeting Gemini API."""

    supports_tools: bool = True
    supports_native_structured_output: bool = False

    def __init__(
        self,
        api_key: str,
        model_id: str,
        base_url: str | None = None,
        **default_params: Any,
    ) -> None:
        self._base_url = base_url or GEMINI_BASE_URL
        model_kwargs: dict[str, Any] = dict(default_params)
        # FAR-2046 / FAR-512: a custom endpoint must never bypass the SSRF gate.
        # ChatGoogleGenerativeAI forwards ``client_args`` to the httpx clients
        # it builds (google-genai's HttpOptions.client_args / async_client_args),
        # so carrying the pinned transport there closes the DNS-rebinding
        # window for the async path this adapter uses. The same transport is
        # handed to the sync client too — LangChain offers one field for both —
        # and this adapter never issues a sync request.
        self._pin_transport: httpx.AsyncHTTPTransport | None = None
        if base_url:
            self._pin_transport = pinned_endpoint_transport(base_url)
            client_args = dict(model_kwargs.get("client_args") or {})
            client_args["transport"] = self._pin_transport
            model_kwargs["client_args"] = client_args
            model_kwargs["base_url"] = base_url
        self._model = ChatGoogleGenerativeAI(model=model_id, api_key=api_key, **model_kwargs)
        self._backend_id = f"gemini/{model_id}"
        self._api_key = api_key

    async def aclose(self) -> None:
        """Close the pinned transport owned by this backend (idempotent).

        The transport was built here (not by LangChain), so it is closed here.
        The model's own ``aclose`` is not used: it closes the sync google-genai
        client first, which cannot close an async transport.
        """
        transport, self._pin_transport = self._pin_transport, None
        if transport is not None:
            await transport.aclose()

    @property
    def backend_id(self) -> str:
        return self._backend_id

    def __repr__(self) -> str:
        return f"GeminiBackend(model_id={self._backend_id!r})"

    async def health_check(self) -> HealthResult:
        return await openai_compatible_health_check(
            base_url=self._base_url,
            api_key=None,
            extra_headers={"x-goog-api-key": self._api_key},
        )
