"""CohereBackend — wraps ChatCohere as a Modulo ModelBackendBase."""

from typing import Any

from langchain_cohere import ChatCohere

from modulo.model_backends.base import (
    HealthResult,
    LangChainChatForwardingMixin,
    ModelBackendBase,
    openai_compatible_health_check,
    validate_custom_endpoint,
)

COHERE_BASE_URL = "https://api.cohere.ai/v1"


class CohereBackend(LangChainChatForwardingMixin, ModelBackendBase):
    """Thin adapter over ChatCohere."""

    supports_tools: bool = True
    supports_native_structured_output: bool = False

    def __init__(
        self,
        api_key: str,
        model_id: str,
        base_url: str | None = None,
        **default_params: Any,
    ) -> None:
        if base_url:
            # FAR-2046 / FAR-512: a custom endpoint must never bypass the SSRF
            # gate. ChatCohere's validator builds its own cohere.Client /
            # cohere.AsyncClient and offers no HTTP-client injection point, so
            # the pinned transport cannot be attached — resolve and validate
            # the host here and fail closed on a private/internal target (see
            # ``validate_custom_endpoint`` for the documented rebinding
            # residual). Do not replace this with a bare URL pass-through.
            validate_custom_endpoint(base_url)
        self._base_url = base_url or COHERE_BASE_URL
        model_kwargs: dict[str, Any] = dict(default_params)
        if base_url:
            model_kwargs["base_url"] = base_url
        # FAR-2046: ``cohere_api_key`` is the field ChatCohere reads; the
        # previous ``api_key=`` spelling was not a ChatCohere field and left the
        # key unset (the SDK then raised / read CO_API_KEY).
        self._model = ChatCohere(model=model_id, cohere_api_key=api_key, **model_kwargs)
        self._backend_id = f"cohere/{model_id}"
        self._api_key = api_key

    @property
    def backend_id(self) -> str:
        return self._backend_id

    def __repr__(self) -> str:
        return f"CohereBackend(model_id={self._backend_id!r})"

    async def health_check(self) -> HealthResult:
        return await openai_compatible_health_check(
            base_url=self._base_url,
            api_key=self._api_key,
        )
