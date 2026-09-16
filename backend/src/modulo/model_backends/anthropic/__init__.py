from collections.abc import AsyncIterator
from typing import Any

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import BaseMessage

from modulo.model_backends.base import (
    HealthResult,
    ModelBackendBase,
    openai_compatible_health_check,
    serialize_structured_output,
)
from modulo.model_backends.module import ProviderUnavailableError

try:
    from anthropic import APIConnectionError as AnthropicConnectionError
    from anthropic import APIStatusError as AnthropicStatusError
except ImportError:  # pragma: no cover — langchain-anthropic always brings anthropic
    AnthropicStatusError = type("AnthropicStatusError", (Exception,), {})  # type: ignore[assignment,misc]
    AnthropicConnectionError = type("AnthropicConnectionError", (Exception,), {})  # type: ignore[assignment,misc]

ANTHROPIC_BASE_URL = "https://api.anthropic.com"


class AnthropicBackend(ModelBackendBase):
    """Thin adapter over ChatAnthropic."""

    supports_tools: bool = True
    supports_native_structured_output: bool = True

    def __init__(self, api_key: str, model_id: str, **default_params: Any) -> None:
        self._model = ChatAnthropic(model=model_id, api_key=api_key, **default_params)
        self._backend_id = f"anthropic/{model_id}"
        self._api_key = api_key

    @property
    def backend_id(self) -> str:
        return self._backend_id

    def __repr__(self) -> str:
        return f"AnthropicBackend(model_id={self._backend_id!r})"

    async def health_check(self) -> HealthResult:
        return await openai_compatible_health_check(
            base_url=ANTHROPIC_BASE_URL,
            api_key=None,
            extra_headers={"x-api-key": self._api_key, "anthropic-version": "2023-06-01"},
        )

    def _classify_gateway_error(self, exc: Exception) -> Exception:
        """Return the exception to raise for an Anthropic call failure.

        HTTP 4xx (including ``AuthenticationError``) and 429 pass through
        unchanged — those are actionable as-is. HTTP 5xx and connection
        failures mean the provider gateway is down, not that the key is wrong,
        so they are re-raised as ``ProviderUnavailableError``.
        """
        if isinstance(exc, AnthropicStatusError) and exc.status_code < 500:
            return exc
        status = getattr(exc, "status_code", None)
        detail = getattr(exc, "message", None) or str(exc)
        status_desc = f"HTTP {status}" if status else "connection failure"
        return ProviderUnavailableError(
            f"{self._backend_id} provider gateway returned {status_desc} "
            f"on the model endpoint — upstream outage, not an auth failure. Detail: {detail}"
        )

    async def invoke(
        self,
        messages: list[BaseMessage],
        output_schema: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> BaseMessage:
        try:
            if output_schema is not None:
                structured = self._model.with_structured_output(schema=output_schema)
                result = await structured.ainvoke(messages, **kwargs)
                # FIX 5 + FIX 7 (FAR-898): wrap structured output in the same
                # error classification the normal path uses, and serialise via
                # the shared helper so both backends return the same shape.
                return serialize_structured_output(result)
            return await self._model.ainvoke(messages, **kwargs)
        except (AnthropicStatusError, AnthropicConnectionError) as exc:
            classified = self._classify_gateway_error(exc)
            if classified is exc:
                raise
            raise classified from exc

    def stream(
        self,
        messages: list[BaseMessage],
        tools: list[dict[str, Any]] | None = None,
        output_schema: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[BaseMessage]:
        # FIX 4 (FAR-898): streaming structured output is not yet supported —
        # with_structured_output in stream mode yields non-BaseMessage chunks
        # and conflicts with tool-calling.  output_schema is intentionally
        # ignored here; the non-streaming invoke() path handles structured output.
        return self._model.astream(messages, tools=tools, **kwargs)
