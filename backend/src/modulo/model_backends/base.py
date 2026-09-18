import asyncio
import json
import logging
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, ClassVar

import httpx
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from modulo.core.ssrf import pinned_async_client_sync

logger = logging.getLogger(__name__)

HEALTH_CHECK_TIMEOUT = 10.0
HEALTH_DETAIL_MAX_LENGTH = 500


class ProviderUnavailableError(RuntimeError):
    """The provider gateway is unavailable or returned an upstream HTTP 5xx.

    Raised instead of the raw SDK error (``InternalServerError`` /
    ``APIConnectionError`` / a gateway's misleading ``AuthenticationError``)
    when the provider's model endpoint is down. The run error mapper derives
    the run's ``error_code`` from the exception type name, so this type
    distinguishes a gateway outage from a genuinely bad API key — which still
    surfaces as the SDK's authentication error.
    """


# ---------------------------------------------------------------------------
# FIX 7 (FAR-898): shared serialisation for structured-output paths
# ---------------------------------------------------------------------------


def serialize_structured_output(result: Any) -> AIMessage:
    """Serialise a ``with_structured_output()`` result into an ``AIMessage``.

    Both the OpenAI-compatible and Anthropic structured-output paths produce
    a dict, list, or Pydantic BaseModel; callers (``_invoke_node_model``)
    always ``json.loads(response.content)`` to recover the data.  This helper
    guarantees that round-trip by JSON-encoding the result into the
    ``AIMessage.content`` field — a single contract both backends share.
    """
    if hasattr(result, "model_dump"):
        content = json.dumps(result.model_dump())
    elif isinstance(result, (dict, list)):
        # dict/list are JSON-native, so json.loads recovers the original shape.
        # A list must NOT fall through to the str() branch below: str([1, 2])
        # emits a Python repr with single quotes ('[1, 2]') that json.loads does
        # not parse back into a list.
        content = json.dumps(result)
    else:
        # Defensive fallback for an unexpected result shape (e.g. a bare
        # scalar): ``str()`` guarantees ``json.dumps`` succeeds, so the
        # ``json.loads(content)`` round-trip callers rely on never raises.
        # The trade-off is that a scalar round-trips as a string rather than
        # the raw value — acceptable because every current caller supplies a
        # dict- or BaseModel-producing schema, so this branch is unreachable
        # in practice.
        content = json.dumps(str(result))
    return AIMessage(content=content)


@dataclass(frozen=True)
class HealthResult:
    ok: bool
    detail: str = ""


async def openai_compatible_health_check(
    base_url: str,
    api_key: str | None,
    extra_headers: dict[str, str] | None = None,
) -> HealthResult:
    """Try GET {base_url}/models to verify reachability + credentials.

    For Bearer-auth endpoints, pass *api_key*. For providers that use
    custom auth headers (x-api-key, x-goog-api-key, api-key), pass the
    key via *extra_headers* and set *api_key* to None.

    PINNED TRANSPORT (FAR-512): the probe uses a pinned-IP client so the
    validated address is pinned onto the connection (never re-resolved at
    connect time, closing the DNS-rebinding window), with ``trust_env=False``
    so a proxy cannot re-resolve the destination server-side.
    """
    url = f"{base_url.rstrip('/')}/models"
    headers: dict[str, str] = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    if extra_headers:
        headers.update(extra_headers)
    try:
        async with pinned_async_client_sync(base_url, trust_env=False, timeout=HEALTH_CHECK_TIMEOUT) as client:
            response = await client.get(url, headers=headers)
            if response.is_success:
                return HealthResult(ok=True)
            return HealthResult(ok=False, detail=response.text[:HEALTH_DETAIL_MAX_LENGTH])
    except ValueError as exc:
        return HealthResult(ok=False, detail=str(exc)[:HEALTH_DETAIL_MAX_LENGTH])
    except httpx.TimeoutException:
        logger.warning("Health check timed out for %s", url)
        return HealthResult(ok=False, detail="Health check timed out")
    except httpx.HTTPError as exc:
        logger.warning("Health check failed for %s: %s", url, exc)
        return HealthResult(ok=False, detail=str(exc)[:HEALTH_DETAIL_MAX_LENGTH])


class ModelBackendBase(ABC):
    """Abstract base for all model backends (real + stub)."""

    supports_tools: bool = False
    supports_native_structured_output: bool = False

    # SDK status-error classes that a concrete backend catches in ``invoke`` /
    # ``stream``. Used by the shared ``_classify_gateway_error`` below to
    # distinguish a 4xx (pass through) from a 5xx (upstream outage). Empty by
    # default — backends that classify gateway failures set it to their SDK's
    # status-error type(s).
    _status_error_types: ClassVar[tuple[type[Exception], ...]] = ()

    @abstractmethod
    async def invoke(
        self,
        messages: list[BaseMessage],
        output_schema: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> BaseMessage:
        """Send messages and return the assistant reply.

        When *output_schema* is supplied and the backend's
        ``supports_native_structured_output`` flag is True, the backend
        invokes the provider's native structured-output decoding (e.g.
        OpenAI ``response_format`` or Anthropic tool-use). When absent or
        None, behaviour is byte-identical to today.
        """

    @abstractmethod
    def stream(
        self,
        messages: list[BaseMessage],
        tools: list[dict[str, Any]] | None = None,
        output_schema: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[BaseMessage]:
        """Return an async iterator that yields token chunks."""

    @property
    @abstractmethod
    def backend_id(self) -> str:
        """Stable identifier for this backend (e.g. 'anthropic/claude-sonnet-4-6')."""

    def _gateway_error_context(self) -> str:
        """Provider-specific context appended to a gateway-outage message.

        Returns a string that includes its own leading separator (for example
        ``" (https://api.example.com/v1)"``), or ``""`` when a backend has no
        extra context to add.
        """
        return ""

    def _classify_gateway_error(self, exc: Exception) -> Exception:
        """Return the exception to raise for a provider-gateway call failure.

        HTTP 4xx (including authentication errors) and 429 pass through
        unchanged — those are actionable as-is. HTTP 5xx and connection
        failures mean the provider gateway is down, not that the key is wrong,
        so they are re-raised as ``ProviderUnavailableError``.

        Concrete backends narrow the caught exceptions to their SDK's status
        and connection error types in the surrounding ``except`` clause and
        declare the status-error type(s) in ``_status_error_types``.
        """
        status = getattr(exc, "status_code", None)
        if isinstance(exc, self._status_error_types) and status is not None and status < 500:
            return exc
        detail = getattr(exc, "message", None) or str(exc)
        status_desc = f"HTTP {status}" if status else "connection failure"
        return ProviderUnavailableError(
            f"{self.backend_id} provider gateway{self._gateway_error_context()} "
            f"returned {status_desc} on the model endpoint — upstream outage, "
            f"not an auth failure. Detail: {detail}"
        )

    async def health_check(self) -> HealthResult:
        """Verify connectivity. Default: minimal ping invoke. Override for efficiency."""
        try:
            await asyncio.wait_for(
                self.invoke([HumanMessage(content="ping")], max_tokens=1),
                timeout=HEALTH_CHECK_TIMEOUT,
            )
            return HealthResult(ok=True)
        except TimeoutError:
            logger.warning("Health check timed out for %s", type(self).__name__)
            return HealthResult(ok=False, detail="Health check timed out")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Health check failed for %s: %s", type(self).__name__, exc)
            return HealthResult(ok=False, detail=str(exc)[:HEALTH_DETAIL_MAX_LENGTH])


class LangChainChatForwardingMixin:
    """Forward ``invoke``/``stream`` straight to ``self._model``.

    Mixin for the thin provider adapters whose ``invoke`` and ``stream`` do
    nothing but delegate to a LangChain chat model held on ``self._model``.
    They neither implement native structured output nor classify gateway
    errors, so the delegation is identical for every provider — declaring it
    in each adapter duplicated the same block across all of them.

    Concrete subclasses still derive from ``ModelBackendBase`` (which declares
    the ``invoke``/``stream`` contract) and must set ``self._model`` in their
    ``__init__``.
    """

    _model: BaseChatModel

    async def invoke(
        self,
        messages: list[BaseMessage],
        output_schema: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> BaseMessage:
        # output_schema is accepted for contract compliance only: these
        # adapters declare supports_native_structured_output = False, so the
        # caller never forwards a schema here.
        return await self._model.ainvoke(messages, **kwargs)

    def stream(
        self,
        messages: list[BaseMessage],
        tools: list[dict[str, Any]] | None = None,
        output_schema: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[BaseMessage]:
        return self._model.astream(messages, tools=tools, **kwargs)
