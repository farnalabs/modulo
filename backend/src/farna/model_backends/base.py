import asyncio
import json
import logging
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, ClassVar

import httpx
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from farna.core.ssrf import (
    pinned_async_client_sync,
    pinned_async_transport_sync,
    validate_outbound_url,
)

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
    """Outcome of one backend's health check.

    ``detail`` carries the provider error text for the caller's own log or
    status column. The remaining fields are transport facts the diagnostics
    surface (``modulo doctor``) needs WITHOUT re-deriving the endpoint or
    re-reading the response body — in particular ``server_time`` and
    ``proxy_via`` let the doctor report clock drift and an intercepting proxy
    from the health check's own response.
    """

    ok: bool
    detail: str = ""
    #: HTTP status the provider answered with, or ``None`` if it never answered.
    status_code: int | None = None
    #: The provider's ``Date`` response header in UTC, when it sent a valid one.
    server_time: datetime | None = None
    #: ``Via`` / ``X-Forwarded-For`` from the response, when present.
    proxy_via: str | None = None
    #: Failure class when no response was received: ``"timeout"`` or
    #: ``"connection"``. ``None`` when the provider answered.
    error_kind: str | None = None


def _parse_server_time(headers: httpx.Headers) -> datetime | None:
    """Return the response's ``Date`` header as an aware UTC datetime, or None.

    An absent or malformed header yields ``None`` — clock drift is a bonus
    reading, never a reason for the check itself to fail.
    """
    raw = headers.get("date")
    if not raw:
        return None
    try:
        parsed = parsedate_to_datetime(raw)
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _proxy_via(headers: httpx.Headers) -> str | None:
    """The response's ``Via`` / ``X-Forwarded-For`` value, if it carries one."""
    via = headers.get("via")
    if via:
        return str(via)
    forwarded_for = headers.get("x-forwarded-for")
    return str(forwarded_for) if forwarded_for else None


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
            server_time = _parse_server_time(response.headers)
            proxy_via = _proxy_via(response.headers)
            if response.is_success:
                return HealthResult(
                    ok=True,
                    status_code=response.status_code,
                    server_time=server_time,
                    proxy_via=proxy_via,
                )
            return HealthResult(
                ok=False,
                detail=response.text[:HEALTH_DETAIL_MAX_LENGTH],
                status_code=response.status_code,
                server_time=server_time,
                proxy_via=proxy_via,
            )
    except ValueError as exc:
        return HealthResult(
            ok=False,
            detail=str(exc)[:HEALTH_DETAIL_MAX_LENGTH],
            error_kind="connection",
        )
    except httpx.TimeoutException:
        logger.warning("Health check timed out for %s", url)
        return HealthResult(ok=False, detail="Health check timed out", error_kind="timeout")
    except httpx.HTTPError as exc:
        logger.warning("Health check failed for %s: %s", url, exc)
        return HealthResult(
            ok=False,
            detail=str(exc)[:HEALTH_DETAIL_MAX_LENGTH],
            error_kind="connection",
        )


# ---------------------------------------------------------------------------
# FAR-2046 / FAR-512: the SSRF gate for an adapter-supplied custom endpoint.
#
# The OpenAI-compatible path validates AND pins (``pinned_async_client_sync``),
# which is the only shape that closes the DNS-rebinding window. The legacy
# provider adapters (anthropic / cohere / gemini / mistral) each need the same
# guarantee, but their LangChain classes differ in what they can accept:
#
#   * ``ChatMistralAI`` accepts an ``async_client`` — pass a pinned client.
#   * ``ChatGoogleGenerativeAI`` accepts ``client_args`` that are forwarded to
#     its httpx clients — pass a pinned transport there.
#   * ``ChatAnthropic`` and ``ChatCohere`` build their own HTTP clients
#     internally and expose no injection point. For those two the endpoint is
#     resolved and validated at construction (:func:`validate_custom_endpoint`)
#     and the residual DNS-rebinding window is documented on that helper — a
#     fail-closed limitation, never a silent pass.
# ---------------------------------------------------------------------------

_ENDPOINT_CLIENT_TIMEOUT_SECONDS = 30.0


def pinned_endpoint_client(url: str, *, headers: dict[str, str] | None = None) -> httpx.AsyncClient:
    """Build a pinned, validated async client for an adapter's custom endpoint.

    Validates and resolves ``url`` through the SSRF gate (blocked ranges,
    metadata floor, and the ``SSRF_ALLOW_PRIVATE_RANGES`` opt-in) and pins the
    validated address set onto the connection, so the completion call cannot
    re-resolve the host at connect time. Raises ``ValueError`` for a
    private/internal target without the deployer opt-in.

    For adapters whose LangChain class accepts a client (``ChatMistralAI``'s
    ``async_client``) — the caller owns the returned client and must close it.
    ``base_url`` is set to the endpoint because those adapters issue requests on
    relative paths (``/chat/completions``); the transport's host pin is what
    keeps the connection on the validated address.
    """
    return pinned_async_client_sync(
        url,
        trust_env=False,
        timeout=_ENDPOINT_CLIENT_TIMEOUT_SECONDS,
        limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
        loudness_guard=True,
        base_url=url,
        headers=headers,
    )


def pinned_endpoint_transport(url: str) -> httpx.AsyncHTTPTransport:
    """Build a pinned, validated async transport for an adapter's custom endpoint.

    For adapters that pass HTTP-client kwargs rather than a ready-made client
    (``ChatGoogleGenerativeAI``'s ``client_args["transport"]``). Validates and
    pins exactly like :func:`pinned_endpoint_client`, and raises ``ValueError``
    for a private/internal target without the deployer opt-in.
    """
    return pinned_async_transport_sync(url, trust_env=False, loudness_guard=True)


def validate_custom_endpoint(url: str) -> None:
    """SSRF-gate an adapter endpoint whose client cannot carry a pinned transport.

    Used by the adapters whose LangChain class offers no injection point for an
    HTTP client (``ChatAnthropic``, ``ChatCohere``): the endpoint is resolved
    and validated at construction and a private/internal target without the
    ``SSRF_ALLOW_PRIVATE_RANGES`` opt-in raises ``ValueError`` (fail closed).

    RESIDUAL RISK — DNS rebinding: validation resolves the hostname, verifies
    every resolved address is non-internal, then returns; the provider SDK
    re-resolves the hostname when it connects. A hostname under DNS control can
    therefore resolve to a public address here and to an internal/metadata
    address at request time. This is a documented limitation of those two
    SDKs, whose HTTP clients cannot be replaced — not a silent bypass. Every
    adapter that CAN accept a pinned client/transport does (gemini, mistral,
    and the whole OpenAI-compatible family).
    """
    validate_outbound_url(url)


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
        return ProviderUnavailableError(  # NOSONAR python:S112 — explicit typed outage classification
            f"{self.backend_id} provider gateway{self._gateway_error_context()} returned {status_desc} on the model endpoint — upstream outage, not an auth failure. Detail: {detail}"
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
        except Exception as exc:  # NOSONAR python:S112 — health checks must contain provider failures
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
        del output_schema  # Contract parameter; streaming structured output is unsupported.
        return self._model.astream(messages, tools=tools, **kwargs)
