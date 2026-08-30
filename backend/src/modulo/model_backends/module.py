from collections.abc import AsyncIterator
from typing import Any

from langchain_core.messages import BaseMessage
from langchain_openai import ChatOpenAI
from openai import APIConnectionError, APIStatusError

from modulo.core.ssrf import validate_outbound_url
from modulo.model_backends.base import HealthResult, ModelBackendBase, openai_compatible_health_check


class ProviderUnavailableError(RuntimeError):
    """The provider gateway is unavailable or returned an upstream HTTP 5xx.

    Raised instead of the raw ``openai`` error (``InternalServerError`` /
    ``APIConnectionError`` / a gateway's misleading ``AuthenticationError``)
    when the provider's model endpoint is down. The run error mapper derives
    the run's ``error_code`` from the exception type name, so this type
    distinguishes a gateway outage from a genuinely bad API key — which still
    surfaces as ``openai.AuthenticationError``.
    """


class OpenAICompatibleBackend(ModelBackendBase):
    """Single backend for all OpenAI-compatible providers.
    Parameterized by base_url, api_key, and provider name.
    """

    supports_tools: bool = True

    def __init__(
        self,
        api_key: str | None = None,
        model_id: str = "",
        base_url: str | None = None,
        provider: str = "openai",
        **default_params: Any,
    ) -> None:
        resolved_api_key = api_key or provider
        self._base_url = base_url.rstrip("/") if base_url else None
        if self._base_url:
            validate_outbound_url(self._base_url)

        self._model = ChatOpenAI(
            model=model_id,
            api_key=resolved_api_key,
            base_url=self._base_url,
            **default_params,
        )
        self._backend_id = f"{provider}/{model_id}"
        self._api_key = resolved_api_key

    @property
    def base_url(self) -> str | None:
        return self._base_url

    @property
    def backend_id(self) -> str:
        return self._backend_id

    def __repr__(self) -> str:
        return f"OpenAICompatibleBackend(provider={self._backend_id!r})"

    async def health_check(self) -> HealthResult:
        return await openai_compatible_health_check(
            base_url=self._base_url or "https://api.openai.com/v1",
            api_key=self._api_key,
        )

    def _classify_gateway_error(self, exc: Exception) -> Exception:
        """Return the exception to raise for an OpenAI-compatible call failure.

        HTTP 4xx (including ``AuthenticationError``) and 429 pass through
        unchanged — those are actionable as-is. HTTP 5xx and connection
        failures mean the provider gateway/completions path is down, not that
        the key is wrong, so they are re-raised as ``ProviderUnavailableError``.
        """
        if isinstance(exc, APIStatusError) and exc.status_code < 500:
            return exc
        status = getattr(exc, "status_code", None)
        detail = getattr(exc, "message", None) or str(exc)
        status_desc = f"HTTP {status}" if status else "connection failure"
        base_url = self._base_url or "https://api.openai.com/v1"
        return ProviderUnavailableError(
            f"{self._backend_id} provider gateway ({base_url}) returned {status_desc} "
            f"on the model endpoint — upstream outage, not an auth failure. Detail: {detail}"
        )

    async def invoke(self, messages: list[BaseMessage], **kwargs: Any) -> BaseMessage:
        try:
            return await self._model.ainvoke(messages, **kwargs)
        except (APIStatusError, APIConnectionError) as exc:
            classified = self._classify_gateway_error(exc)
            if classified is exc:
                raise
            raise classified from exc

    def stream(
        self,
        messages: list[BaseMessage],
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[BaseMessage]:
        async def _iter() -> AsyncIterator[BaseMessage]:
            try:
                async for chunk in self._model.astream(messages, tools=tools, **kwargs):
                    yield chunk
            except (APIStatusError, APIConnectionError) as exc:
                classified = self._classify_gateway_error(exc)
                if classified is exc:
                    raise
                raise classified from exc

        return _iter()
