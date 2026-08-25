"""RESTConnector — generic verb-agnostic REST integration connector (FAR-408).

A declarative connector that lets a ConnectorInstance declare an arbitrary
HTTP endpoint (URL, method, headers, body, records extraction) and have pipeline
nodes call it with runtime variables rendered into the request. It is the
``ConnectorType.REST`` implementation of the FAR-401 "point Modulo at any
external system" design: no per-vendor client, just a templated HTTP call.

CONFIG SHAPE (``config_json``)
------------------------------
A single connector instance describes ONE endpoint (or a map of named
resources, each with its own endpoint). All template fields are rendered with
Jinja2 (``SandboxedEnvironment`` — the same sandbox ``node_runner`` uses) against
the runtime variables supplied per call:
``ConnectorQuery.filters`` for ``query()`` and ``ConnectorPayload.data`` for
``write()``.

    base_url:             "https://api.example.com"            # required
    method:               "GET"                                # default verb (query path)
    path:                 "/v1/users/{{ user_id }}"            # URL template
    headers:              {"Accept": "application/json"}       # header templates
    params:               {"page": "{{ page }}"}               # query params (URL-encoded by httpx)
    body:                 {"name": "{{ name }}"}               # JSON body template (write path)
    operations:           { "<resource>": { "method": ..., "path": ..., "headers": {},
                                             "params": {}, "body": {}, "records_path": ...,
                                             "next_cursor_path": ..., "passthrough": ...,
                                             "idempotency_header": ... } }
    records_path:         "data.items"                         # JMESPath expression into JSON response for records
    next_cursor_path:     "data.next_cursor"                   # optional pagination cursor (JMESPath)
    allowed_hosts:        ["api.example.com"]                  # optional scheme/host allowlist
    passthrough:          false                                # force single-record wrap of the raw body when set
    max_response_size:    <bytes>                              # optional max response body size (default 10 MiB)
    idempotency_header:   <header-name>                        # optional header that makes a
                                                                 #   non-GET/HEAD request safe to retry
    fan_out:              {                                     # optional fan-out / iterator mode (FAR-411)
                            "enabled": true,                    #   when truthy + items_path set, write() iterates
                            "items_path": "data.items",         #   JMESPath into payload.data resolving to the
                                                                 #   item list (Sized or lazy generator)
                            "max_cardinality": 1000,            #   fail-closed cap (len() for Sized; buffer to
                                                                 #   cap+1 for lazy before emit)
                            "per_item_timeout": 30.0,           #   per-item HTTP timeout (default = connector timeout)
                            "max_retries": 2                    #   per-item retries (attempts = max_retries + 1)
                          }
    rate_limit:           {                                     # optional per-destination token bucket (FAR-411)
                            "requests_per_second": 10.0,        #   refill rate
                            "burst": 20                         #   burst capacity
                          }

FAN-OUT / ITERATOR (FAR-411)
---------------------------
When ``fan_out.enabled`` is true and ``fan_out.items_path`` resolves to a
sequence inside the write ``data``, ``write()`` fans out ONE request per item:

* **Sequential emit** — a single code path iterates the items in order, so a
  per-item failure fails the node (no concurrent/bounded-concurrency fork in
  v1).
* **Cardinality guard (fail-closed)** — a Sized source is ``len()``-checked
  BEFORE any request and raises :class:`RESTCardinalityExceededError` with
  zero partial emit. A lazy/unsized generator is buffered up to
  ``max_cardinality + 1`` and then fails before emitting (memory bounded by
  the cap) — an unsized source is NEVER claimed to produce zero partial emit.
* **Empty iterator** — 0 items succeeds vacuously with no calls (an empty
  fan-out is a no-op, not a failure).
* **Per-destination token bucket** — the ONE outbound-call enforcement point.
  Each item consumes a token from a :class:`modulo.connectors._rate_bucket.TokenBucket`
  keyed by host; when empty the call awaits refill. This is **per-process**
  (each uvicorn/SAQ worker owns its own bucket) and **best-effort** — it is
  NOT Redis-backed in this ticket. Note the divergence: existing connectors
  (github, linear, …) have no bucket, so REST is deliberately stricter.
* **Per-item outcome state** — the result carries ``outcomes`` (one record per
  item: index, item, status ``success``/``failure``, result/error), plus
  ``success_count``/``failure_count`` and an explicit ``cardinality_over_cap``
  flag. A mid-fan-out failure raises :class:`RESTFanOutFailureError` carrying
  the outcomes collected so far for operator reconciliation. A durable outbox
  (fire-and-forget mode) is NOT part of v1.
* **Node-budget reconciliation** — ``fan_out.max_cardinality x
  per_item_timeout x (max_retries + 1)`` is reconciled against the node's
  ``timeout_seconds`` budget by the graph validator at save/run time (FAR-411)
  so an oversubscribed fan-out is warned before a run, not discovered mid-run.

VERB-AGNOSTIC READ/WRITE MAPPING
--------------------------------
A REST connector is verb-agnostic, so the capability contract (``read`` /
``write``) maps onto the two public surfaces, not onto any single HTTP verb:

* ``query()`` is the **read** surface — ``_TracedConnector`` calls it with
  ``acl_operation="read"``. It performs the operation's configured verb
  (default ``GET`` ; safe, idempotent verbs make sense here).
* ``write()`` is the **write** surface — ``_TracedConnector`` calls it with
  ``acl_operation="write"``. It performs the operation's configured verb
  (default ``POST`` ; mutable verbs belong here).

A ``PUT`` / ``DELETE`` / ``PATCH`` is neither cleanly read nor write, but it
*mutates* the remote system, so it belongs on the **write** surface (ACL
``write``). The connector does not infer semantics from the verb — the author
declares the method; the surface (query/write) fixes the ACL gate. The
capability set is ``{Capability.READ, Capability.WRITE}``.

AUTH MODES (``credentials_ciphertext`` / ``creds`` dict)
-------------------------------------------------------
Credentials are stored as an ENCRYPTED **JSON dict** (matching the
``secrets_backend`` JSON-dict shape) so multi-field creds round-trip — never the
single ``api_key`` fallback. Read ``auth_mode`` + named fields from that dict:

    auth_mode:  "bearer" | "api_key" | "basic"
    # bearer ->  token
    # api_key -> api_key + header_name (default "X-API-Key") and/or
    #            in: "header" (default) | "query" + query_param_name (default "api_key")
    # basic   -> username + password

Templating uses the existing ``node_runner`` ``jinja2.sandbox.SandboxedEnvironment``;
the only runtime dependencies added here are ``httpx``, ``jinja2`` and
``jmespath``.

TRANSPORT
---------
A single lazily-created, connection-pooled ``httpx.AsyncClient`` is reused
across calls and closed via :meth:`RestConnector.close`. The client never
follows redirects, so HTTP 3xx responses are surfaced as errors (with
``Retry-After``/``location`` metadata) rather than silently passing through.

RETRY
-----
Idempotent verbs (``GET``/``HEAD``) are retried up to 3x with exponential
backoff + jitter, honouring ``Retry-After`` and the retryable status set
(``429``/``5xx``). Mutating verbs are retried only when the operation declares
an ``idempotency_header``. Transport failures are retried for idempotent verbs
and surface as a typed :class:`RESTConnectError`.
"""

from __future__ import annotations

import asyncio
import base64
import inspect
import json
import logging
import random
import re
import time
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, cast
from urllib.parse import urlparse

import httpx
import jmespath
from jinja2.sandbox import SandboxedEnvironment

from modulo.connectors._rate_bucket import TokenBucket
from modulo.connectors._retry_headers import parse_retry_after
from modulo.connectors._safe_page import safe_records_list
from modulo.connectors.base import (
    ConnectorBase,
    ConnectorPayload,
    ConnectorQuery,
    ConnectorResult,
    ConnectorType,
    HealthResult,
)

_log = logging.getLogger(__name__)

# Standard HTTP verbs the connector will issue (all else is rejected).
_ALLOWED_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})

# Auth/transport headers the user/agent must never be able to override through a
# rendered request header (FAR-408 injection guard). ``host``/``content-length``
# are derived by httpx, so a rendered override would corrupt the request.
_AUTH_PROTECTED_HEADERS = frozenset({"authorization", "proxy-authorization", "host", "content-length"})

# C0 control chars (minus tab, which is legal in header values) + DEL.
_CONTROL_CHARS = frozenset({chr(c) for c in range(0x20) if c != 0x09} | {"\x7f"})

_DEFAULT_TIMEOUT = 30.0
_DEFAULT_MAX_RESPONSE_SIZE = 10 * 1024 * 1024  # 10 MiB
# ``_MAX_RETRIES`` is the default number of ATTEMPTS for the retry loop (the
# loop runs ``range(_MAX_RETRIES)``); a configured fan-out ``max_retries`` is
# counted in RETRIES (attempts = max_retries + 1), so the default connector
# retry semantics are ``_MAX_RETRIES - 1`` retries.
_MAX_RETRIES = 3
# Sane upper bound for a configured retry budget — a request with more retries
# than this is clamped rather than honoured verbatim (backoff/Retry-After waits
# mean the per-item budget is finite).
_MAX_SANE_RETRIES = 10
_DEFAULT_MAX_FANOUT_CARDINALITY = 1000
_MAX_FANOUT_CARDINALITY_CAP = 100_000
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
_RETRYABLE_METHODS = frozenset({"GET", "HEAD"})

# Legacy ``_dot_get`` index syntax (``items.0``) is NOT valid JMESPath; a
# connector must declare ``items[0]`` instead. We reject dot-index paths with a
# clear actionable error rather than silently rewriting them (a legit numeric
# key such as ``data.2024`` must never be rewritten to ``data[2024]``).
_MAX_RETRY_WAIT = 30.0
_DOT_INDEX = re.compile(r"\.\d+(?![A-Za-z_])")

SsrfValidator = Callable[[str], Awaitable[None] | None]


def _collect_strings(value: Any) -> list[str]:
    """Collect every leaf string inside *value* (dicts, lists, scalars)."""
    result: list[str] = []
    if isinstance(value, str):
        result.append(value)
    elif isinstance(value, dict):
        for v in value.values():
            result.extend(_collect_strings(v))
    elif isinstance(value, list):
        for v in value:
            result.extend(_collect_strings(v))
    return result


def _reject_control_chars(value: str, *, what: str) -> None:
    """Raise ValueError if *value* contains CR/LF or other control characters."""
    bad = _CONTROL_CHARS.intersection(value)
    if bad:
        offending = " ".join(repr(c) for c in sorted(bad))
        raise ValueError(f"REST {what} contains control characters (header injection): {offending}")


class RESTError(ValueError):
    """Base class for RestConnector request errors."""


class RESTStatusError(RESTError):
    """A non-2xx HTTP status (3xx included — redirects are not silently followed)."""

    def __init__(self, message: str, *, status_code: int, location: str = "", retry_after: float | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.location = location
        self.retry_after = retry_after


class RESTConnectError(RESTError):
    """A transport-level failure (connect/timeout/read) — never retried here."""

    def __init__(self, message: str) -> None:
        super().__init__(message)


class RESTResponseTooLargeError(RESTError):
    """The response body exceeded the configured ``max_response_size`` cap."""

    def __init__(self, message: str) -> None:
        super().__init__(message)


class RESTCardinalityExceededError(RESTError):
    """A fan-out source exceeded the configured ``max_cardinality`` (fail-closed).

    This is raised BEFORE any request is emitted — the guard is fail-closed, so
    a cap hit never produces a partial fan-out. Semantics are documented (see
    the module docstring):

    * **Sized** collections (``list``/``tuple``/``set``) are ``len()``-checked
      up front, so ``source_cardinality`` is the exact size.
    * **Lazy/unsized** generators cannot be known ahead of time, so the source
      is buffered up to ``fanout_capacity + 1`` eager items and then fails
      (memory bounded by the cap). ``source_cardinality`` is ``None`` for a
      lazy source — only the fact that it exceeded the cap is known.

    ``cardinality_over_cap`` is always ``True`` on this path, and the fan-out
    success result carries ``cardinality_over_cap: False`` so a cap hit is
    never silent.
    """

    def __init__(
        self,
        message: str,
        *,
        source_cardinality: int | None,
        fanout_capacity: int,
        lazy: bool = False,
    ) -> None:
        super().__init__(message)
        self.source_cardinality = source_cardinality
        self.fanout_capacity = fanout_capacity
        self.lazy = lazy
        self.cardinality_over_cap = True


class RESTFanOutFailureError(RESTError):
    """A fan-out item failed mid-loop — per-item failure fails the node (FAR-411).

    Carries the per-item outcome records collected up to and including the
    failed item so an operator can reconcile exactly which items were
    successfully written vs failed. ``outcomes`` is best-effort: it reflects
    the items processed before the failure plus the failed item itself, never
    the items that were not yet attempted (sequential emit aborts on the first
    failure).
    """

    def __init__(
        self,
        message: str,
        *,
        outcomes: list[dict[str, Any]],
        success_count: int,
        failure_count: int,
        failed_index: int,
        failed_item: str,
        failed_error: str,
    ) -> None:
        super().__init__(message)
        self.outcomes = outcomes
        self.success_count = success_count
        self.failure_count = failure_count
        self.failed_index = failed_index
        self.failed_item = failed_item
        self.failed_error = failed_error


class RESTRateLimitTimeoutError(RESTError):
    """The per-destination rate-limit wait exceeded its bounded deadline.

    Raised from :meth:`RestConnector._acquire_rate_token` when the token-bucket
    refill cannot supply a token within ``request_timeout``; on the fan-out path
    this surfaces as a per-item failure (never an unbounded spin).
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)


class RESTFanOutCancelledError(asyncio.CancelledError):
    """A fan-out interrupted by cancellation, carrying partial outcome state.

    Subclasses :class:`asyncio.CancelledError` so real cancellation semantics are
    preserved at the caller, while the accumulated ``outcomes`` / ``success_count``
    are attached for operator reconciliation (FAR-411): an operator can see exactly
    which items were written before the run was cancelled.
    """

    def __init__(
        self,
        message: str,
        *,
        outcomes: list[dict[str, Any]],
        success_count: int,
        failure_count: int,
    ) -> None:
        super().__init__(message)
        self.outcomes = outcomes
        self.success_count = success_count
        self.failure_count = failure_count


class SecurityGuard:
    """Connector-local port for the SSRF + output-injection guards (FAR-408 layering).

    Injected at the composition root (see ``connector_hub._build_connector``) so the
    connector does not reach into ``modulo.core`` directly. ``validate_url`` is
    awaited (or called) and raises on a disallowed target; ``filter_strings``
    raises on a string that fails the injection filter. The composition root is the
    single place that binds this port to the real ``modulo.core`` guards; the
    constructor default (when no guard is injected) is an inert stub used only by
    tests and ad-hoc direct construction.
    """

    def __init__(
        self,
        *,
        validate_url: Callable[[str], Awaitable[None] | None],
        filter_strings: Callable[[Sequence[str], str], None],
    ) -> None:
        self._validate_url = validate_url
        self._filter_strings = filter_strings

    async def validate_url(self, url: str) -> None:
        result = self._validate_url(url)
        if inspect.isawaitable(result):
            await result

    def filter_strings(self, values: Sequence[str], *, resource: str) -> None:
        self._filter_strings(values, resource)


def _stub_security_guard() -> SecurityGuard:
    """Inert guard used when no guard is injected (direct construction, tests).

    The ``modulo.core`` SSRF + injection guards are bound by the composition root
    (``connector_hub``) — never by the connector itself — so this stub performs no
    enforcement. Directly-constructed connectors that need real guarding must
    inject a real ``SecurityGuard``.
    """

    async def validate_url(url: str) -> None:
        return None

    def filter_strings(values: Sequence[str], resource: str) -> None:
        return None

    return SecurityGuard(validate_url=validate_url, filter_strings=filter_strings)


@dataclass(frozen=True)
class RestRequest:
    """A fully-rendered, validated request (the stringly-typed dict is gone).

    ``records_path`` / ``next_cursor_path`` are JMESPath expressions;
    ``passthrough`` forces a single-record raw-body wrap; ``idempotency_header``
    marks the request safe to retry even when the verb is mutating.
    """

    method: str
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    params: dict[str, Any] = field(default_factory=dict)
    body: Any = None
    records_path: str | None = None
    next_cursor_path: str | None = None
    passthrough: bool = False
    idempotency_header: str | None = None


class RestConnector(ConnectorBase):
    """A declarative, verb-agnostic REST connector.

    ``config`` is the ``config_json`` and ``creds`` the decrypted credentials
    dict (see the module docstring for both shapes). ``transport`` and
    ``ssrf_validator`` are test seams — production callers pass neither.
    ``security_guard`` is the injection/SSRF port; the composition root wires the
    production ``modulo.core`` implementation.
    """

    def __init__(
        self,
        config: dict[str, Any] | None,
        creds: dict[str, Any] | None,
        *,
        transport: httpx.BaseTransport | None = None,
        ssrf_validator: SsrfValidator | None = None,
        security_guard: SecurityGuard | None = None,
        timeout: float = _DEFAULT_TIMEOUT,
        max_connections: int = 10,
        max_keepalive_connections: int = 5,
    ) -> None:
        self._config = config or {}
        self._creds = creds or {}
        self._transport = transport
        self._timeout = float(timeout or _DEFAULT_TIMEOUT)
        self._ssrf_validator = ssrf_validator
        self._security_guard = security_guard or _stub_security_guard()
        self._base_url = str(self._config.get("base_url", "")).rstrip("/")
        if not self._base_url:
            raise ValueError("REST connector requires 'base_url' in config_json")
        self._env = SandboxedEnvironment()
        self._auth = self._normalise_auth(self._creds)
        raw_size = self._config.get("max_response_size", _DEFAULT_MAX_RESPONSE_SIZE)
        self._max_response_size = int(raw_size)
        self._max_connections = int(max_connections)
        self._max_keepalive = int(max_keepalive_connections)
        self._cached_client: httpx.AsyncClient | None = None

        # FAR-411 fan-out / iterator config. ``fan_out`` is optional; when absent
        # or disabled the connector behaves exactly as before (single call).
        raw_fanout = self._config.get("fan_out")
        self._fanout_config: dict[str, Any] = raw_fanout if isinstance(raw_fanout, dict) else {}
        self._fanout_enabled = bool(self._fanout_config.get("enabled") or self._fanout_config.get("items_path"))
        self._fanout_items_path = self._fanout_config.get("items_path")
        raw_cardinality = int(self._fanout_config.get("max_cardinality", _DEFAULT_MAX_FANOUT_CARDINALITY))
        if raw_cardinality < 1:
            raise ValueError(f"REST fan_out.max_cardinality must be >= 1 (got {raw_cardinality})")
        if raw_cardinality > _MAX_FANOUT_CARDINALITY_CAP:
            raise ValueError(
                f"REST fan_out.max_cardinality must be <= {_MAX_FANOUT_CARDINALITY_CAP} (got {raw_cardinality})"
            )
        self._max_fanout_cardinality = raw_cardinality
        self._fanout_per_item_timeout = float(self._fanout_config.get("per_item_timeout", self._timeout))
        # max_retries (retries, not attempts); attempts = max_retries + 1. The
        # connector's own ``_send`` loop runs ``_MAX_RETRIES`` (3) attempts, so
        # the default of ``_MAX_RETRIES - 1`` retries keeps the vendor
        # reconciliation (``attempts = max_retries + 1``) in lockstep. The
        # configured value is clamped to a sane upper bound (honoured, not
        # verbatim) because backoff/Retry-After waits make a huge budget
        # pathological within a finite per-item timeout.
        self._fanout_max_retries = int(self._fanout_config.get("max_retries", _MAX_RETRIES - 1))
        if self._fanout_max_retries < 0:
            raise ValueError("REST fan_out.max_retries must be >= 0")
        self._fanout_max_retries = min(self._fanout_max_retries, _MAX_SANE_RETRIES)

        # FAR-411 per-destination token bucket (single outbound enforcement point).
        # Best-effort per-process: each uvicorn/SAQ worker owns its own buckets.
        raw_rate = self._config.get("rate_limit")
        self._rate_limit_config: dict[str, Any] = raw_rate if isinstance(raw_rate, dict) else {}
        self._rate_buckets: dict[str, Any] = {}
        self._rate_lock = asyncio.Lock()

    # ── ConnectorBase surface ──────────────────────────────────────────────

    @property
    def connector_type(self) -> ConnectorType:
        return ConnectorType.REST

    def _client(self) -> httpx.AsyncClient:
        """Return the lazily-created, connection-pooled client (never closed here)."""
        if self._cached_client is None:
            kwargs: dict[str, Any] = {
                "timeout": self._timeout,
                "follow_redirects": False,
                "limits": httpx.Limits(
                    max_connections=self._max_connections,
                    max_keepalive_connections=self._max_keepalive,
                ),
            }
            if self._transport is not None:
                kwargs["transport"] = self._transport
            self._cached_client = httpx.AsyncClient(**kwargs)
        return self._cached_client

    async def close(self) -> None:
        """Release the pooled client (idempotent; safe to call after an exception)."""
        client = self._cached_client
        self._cached_client = None
        if client is not None:
            await client.aclose()

    async def health_check(self) -> HealthResult:
        """Verify the target is reachable and the credentials are accepted.

        Issues a ``GET`` against ``base_url`` + the configured ``path`` after
        validating the target through the SSRF/allowlist guard, sending the
        credentials (``apply_auth`` headers + query params). A sub-400 status
        means the endpoint + credentials are live; any other status is a non-OK
        result. Never raises — like the other connectors.
        """
        try:
            request = await self._build_health_request()
            client = self._client()
            resp, _body_text = await self._send(client, request)
            if resp.status_code < 400:
                return HealthResult(ok=True, detail=f"HTTP {resp.status_code}: {request.url}")
            return HealthResult(ok=False, detail=f"HTTP {resp.status_code}: {request.url}")
        except asyncio.CancelledError:
            raise
        except (RESTError, ValueError) as exc:
            return HealthResult(ok=False, detail=self._redact(str(exc))[:200])

    async def query(self, q: ConnectorQuery) -> ConnectorResult:
        """Read surface. Renders the operation from ``q.filters`` and issues it."""
        resource = q.resource
        self._require_resource(resource)
        if q.cursor is not None:
            raise ValueError(
                "REST connector pagination is response-driven: supply the idempotent filter "
                "that the operation templates, not a direct start cursor; use the returned "
                "next_cursor for the next page"
            )
        context = dict(q.filters or {})
        context.setdefault("resource", resource)
        request = await self._build_request(resource, context, surface="read")
        result = cast(ConnectorResult, await self._execute(request, surface="read"))
        if q.limit is not None:
            result.records = result.records[: q.limit]
            result.total = len(result.records)
        return result

    async def write(self, payload: ConnectorPayload) -> dict[str, Any]:
        """Write surface. Renders the operation from ``payload.data`` and issues it.

        When ``fan_out`` is enabled and ``fan_out.items_path`` resolves to a
        sequence in ``payload.data``, this fans out one request per item
        (sequential, cardinality-guarded, per-destination rate-limited) and
        returns per-item outcome state. Otherwise it is the single-call write.
        """
        resource = payload.resource
        self._require_resource(resource)
        data = dict(payload.data or {})
        data.setdefault("resource", resource)

        if self._fanout_enabled and self._fanout_items_path:
            items = self._resolve_fanout_items(data)
            return await self._fanout_write(items, resource, data)

        request = await self._build_request(resource, data, surface="write")
        return cast(dict[str, Any], await self._execute(request, surface="write"))

    # ── Fan-out / iterator (FAR-411) ───────────────────────────────────────

    def _resolve_fanout_items(self, data: dict[str, Any]) -> Any:
        """Resolve the fan-out item source from *data* via ``fan_out.items_path``.

        Returns ``None`` when the path is absent/unset. A resolved value may be
        a Sized sequence (list/tuple) or a lazy generator/iterator; the
        cardinality guard handles both. A path that resolves to ``None`` is the
        empty-iterator case (vacuously, zero items).
        """
        path = self._fanout_items_path
        if not path:
            return None
        return RestConnector._search_jmespath(str(path), data)

    def _apply_cardinality_guard(self, items: Any) -> list[Any]:
        """Fail-closed cardinality guard on the fan-out source.

        * **Sized** sources are ``len()``-checked up front; an over-cap source
          raises :class:`RESTCardinalityExceededError` with zero partial emit.
        * **Lazy/unsized** generators are buffered up to ``max_cardinality + 1``
          eager items and then fail before emitting — memory is bounded by the
          cap. An unsized source is NEVER claimed to emit zero partial work.

        Returns the items as a concrete list ready for sequential iteration.
        """
        cap = self._max_fanout_cardinality
        if items is None:
            return []
        try:
            size = len(items)
        except TypeError:
            size = None
        if size is not None:
            if size > cap:
                raise RESTCardinalityExceededError(
                    f"REST fan-out source has {size} items, exceeding max_cardinality {cap} "
                    f"(fail-closed — no request emitted)",
                    source_cardinality=size,
                    fanout_capacity=cap,
                )
            return list(items)
        buffered: list[Any] = []
        for item in items:
            buffered.append(item)
            if len(buffered) > cap:
                raise RESTCardinalityExceededError(
                    f"REST fan-out source exceeded max_cardinality {cap} (lazy source — buffered "
                    f"{len(buffered)} eager items before fail-closed)",
                    source_cardinality=None,
                    fanout_capacity=cap,
                    lazy=True,
                )
        return buffered

    def _fanout_item_context(self, base_context: dict[str, Any], item: Any, index: int) -> dict[str, Any]:
        """Build the per-item render context for a fan-out request.

        The item's own fields are merged over the base payload context (so a
        template like ``{{ name }}`` resolves from the item), and the item is
        exposed as ``item`` plus ``item_index`` for templates that need the
        whole record or the position.
        """
        context = dict(base_context)
        if isinstance(item, dict):
            context.update(item)
        context["item"] = item
        context["item_index"] = index
        return context

    async def _acquire_rate_token(self, destination: str, *, deadline_seconds: float | None = None) -> None:
        """Consume one token from the per-destination bucket (best-effort).

        Each call waits until a token is available (refill is continuous).
        *deadline_seconds*, when provided, bounds the wait: if a token cannot be
        supplied within that window a :class:`RESTRateLimitTimeoutError` is
        raised rather than spinning forever (the per-item fan-out budget). A
        missing/disabled ``rate_limit`` config is a no-op. This is per-process —
        each uvicorn/SAQ worker owns its own bucket; it is NOT Redis-backed in
        v1 (future work).
        """
        rate = self._rate_limit_config.get("requests_per_second")
        if rate is None:
            return
        requests_per_second = float(rate)
        burst = int(self._rate_limit_config.get("burst", max(1, int(requests_per_second))))
        if requests_per_second <= 0:
            return

        if destination not in self._rate_buckets:
            async with self._rate_lock:
                if destination not in self._rate_buckets:
                    self._rate_buckets[destination] = TokenBucket(
                        rate=requests_per_second,
                        burst=burst,
                    )
        bucket = self._rate_buckets[destination]
        deadline = time.monotonic() + max(0.0, deadline_seconds) if deadline_seconds is not None else None
        # consume() returns False when the bucket is empty (consuming nothing);
        # wait out a bounded refill hop and retry. The deadline bounds the wait so
        # a saturated per-destination bucket never spins past the per-item budget.
        while not await bucket.consume():
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0.0:
                    raise RESTRateLimitTimeoutError(
                        f"REST rate-limit wait exceeded deadline {deadline_seconds:.1f}s for {destination}"
                    )
                hop = min(1.0 / requests_per_second, 1.0, remaining)
            else:
                hop = min(1.0 / requests_per_second, 1.0)
            await asyncio.sleep(hop)

    async def _fanout_write(
        self,
        items: Any,
        resource: str,
        base_context: dict[str, Any],
    ) -> dict[str, Any]:
        """Sequentially emit one request per item, recording per-item outcomes.

        A per-item failure fails the node — :class:`RESTFanOutFailureError` is
        raised carrying the outcomes collected so far. An empty (or unresolved)
        source succeeds vacuously with zero calls.
        """
        # Cardinality guard runs BEFORE any request (fail-closed, zero partial emit).
        resolved = self._apply_cardinality_guard(items)

        outcomes: list[dict[str, Any]] = []
        success_count = 0
        for index, item in enumerate(resolved):
            # ``item_summary`` (redacted + elided) is the ONLY item echo — the raw
            # item payload is not persisted to an outcome record, so a
            # credential-bearing item is never written to logs or the run result.
            outcome: dict[str, Any] = {"index": index, "item_summary": self._item_summary(item)}
            try:
                context = self._fanout_item_context(base_context, item, index)
                request = await self._build_request(resource, context, surface="write")
                result = await self._execute(
                    request,
                    surface="write",
                    request_timeout=self._fanout_per_item_timeout,
                    max_retries=self._fanout_max_retries,
                )
                outcome["status"] = "success"
                outcome["result"] = self._result_summary(result)
                success_count += 1
                outcomes.append(outcome)
            except asyncio.CancelledError as exc:  # NOSONAR
                raise RESTFanOutCancelledError(
                    f"REST fan-out cancelled at item {index} of {len(resolved)} "
                    f"after {success_count} successful item(s)",
                    outcomes=outcomes,
                    success_count=success_count,
                    failure_count=sum(1 for o in outcomes if o["status"] == "failure"),
                ) from exc
            except Exception as exc:
                outcome["status"] = "failure"
                outcome["error"] = self._redact(str(exc))
                outcomes.append(outcome)
                raise RESTFanOutFailureError(
                    f"REST fan-out failed at item {index} of {len(resolved)}: {self._redact(str(exc))}",
                    outcomes=outcomes,
                    success_count=success_count,
                    failure_count=1,
                    failed_index=index,
                    failed_item=self._item_summary(item),
                    failed_error=self._redact(str(exc)),
                ) from exc

        return {
            "fanout": True,
            "total": len(outcomes),
            "success_count": success_count,
            "failure_count": 0,
            "cardinality_over_cap": False,
            "outcomes": outcomes,
        }

    # ── Operation resolution ───────────────────────────────────────────────

    def _require_resource(self, resource: str) -> None:
        if resource is None or resource == "":
            raise ValueError("REST connector requires a resource name")
        ops = self._config.get("operations")
        if isinstance(ops, dict) and ops and resource not in ops:
            raise ValueError(f"Unsupported REST resource: {resource!r}")

    def _operation_spec(self, resource: str, *, default_method: str) -> dict[str, Any]:
        """Merge the top-level config with the per-resource operation (dict-spread).

        Reads every key uniformly from a single ``{**self._config, **spec}`` merge
        so per-resource overrides land cleanly without the previous special-casing.
        """
        ops = self._config.get("operations")
        spec: dict[str, Any] = {}
        if isinstance(ops, dict) and isinstance(ops.get(resource), dict):
            spec = ops[resource]
        merged: dict[str, Any] = {**self._config, **spec}
        method = str(merged.get("method") or default_method).upper()
        if method not in _ALLOWED_METHODS:
            raise ValueError(f"REST method {method!r} is not allowed (expected one of {sorted(_ALLOWED_METHODS)})")
        return {
            "method": method,
            "path": merged.get("path"),
            "headers": merged.get("headers", {}),
            "params": merged.get("params", {}),
            "body": merged.get("body"),
            "records_path": merged.get("records_path"),
            "next_cursor_path": merged.get("next_cursor_path"),
            "passthrough": bool(merged.get("passthrough", False)),
            "idempotency_header": merged.get("idempotency_header"),
        }

    # ── Auth ───────────────────────────────────────────────────────────────

    @staticmethod
    def _normalise_auth(creds: dict[str, Any]) -> dict[str, Any]:
        mode = str(creds.get("auth_mode", "")).strip().lower()
        if mode not in {"bearer", "api_key", "basic"}:
            raise ValueError(
                f"REST connector requires creds['auth_mode'] to be one of 'bearer', 'api_key', 'basic' — got {mode!r}"
            )
        auth: dict[str, Any] = {"mode": mode}
        if mode == "bearer":
            if not creds.get("token"):
                raise ValueError("REST bearer auth requires creds['token']")
            auth["token"] = str(creds["token"])
        elif mode == "basic":
            if not creds.get("username") or not creds.get("password"):
                raise ValueError("REST basic auth requires creds['username'] and creds['password']")
            auth["username"] = str(creds["username"])
            auth["password"] = str(creds["password"])
        else:  # api_key
            if not creds.get("api_key"):
                raise ValueError("REST api_key auth requires creds['api_key']")
            auth["api_key"] = str(creds["api_key"])
            auth_in = creds.get("in")
            auth["in"] = str(auth_in if auth_in is not None else "header").lower()
            if auth["in"] not in {"header", "query"}:
                raise ValueError(f"REST api_key auth 'in' must be 'header' or 'query' — got {auth['in']!r}")
            if auth["in"] == "header":
                header_name = creds.get("header_name")
                auth["header_name"] = str(header_name) if header_name is not None else "X-API-Key"
            else:
                query_param_name = creds.get("query_param_name")
                auth["query_param_name"] = str(query_param_name) if query_param_name is not None else "api_key"
        return auth

    @property
    def _protected_header_names(self) -> frozenset[str]:
        """Set of header names (lowercased) the rendered headers may not override."""
        names = set(_AUTH_PROTECTED_HEADERS)
        if self._auth["mode"] == "api_key" and self._auth["in"] == "header":
            names.add(self._auth["header_name"].lower())
            names.add("x-api-key")
        return frozenset(names)

    def apply_auth(self, headers: dict[str, str]) -> dict[str, str]:
        """Inject the credential headers into *headers* (post-injection-guard)."""
        mode = self._auth["mode"]
        if mode == "bearer":
            headers["Authorization"] = f"Bearer {self._auth['token']}"
        elif mode == "basic":
            raw = f"{self._auth['username']}:{self._auth['password']}"
            headers["Authorization"] = f"Basic {base64.b64encode(raw.encode()).decode()}"
        elif mode == "api_key" and self._auth["in"] == "header":
            headers[self._auth["header_name"]] = self._auth["api_key"]
        return headers

    def _secret_values(self) -> list[str]:
        """Credential strings that must be redacted from error detail.

        Values shorter than 4 chars are ignored — redacting a 1-2 char secret
        would mangle every occurrence of the common substring it appears in.
        """
        secrets: list[str] = []
        for key in ("token", "api_key", "password", "secret"):
            value = self._creds.get(key)
            if isinstance(value, str) and len(value) >= 4:
                secrets.append(value)
        return secrets

    def _redact(self, text: str) -> str:
        """Strip credential values from *text* so error detail never echoes secrets."""
        redacted = text
        for secret in self._secret_values():
            redacted = redacted.replace(secret, "***")
        return redacted

    def _item_summary(self, item: Any) -> str:
        """A bounded, redacted string summary of a fan-out item for outcome records.

        The full item payload is never persisted — a credential-bearing item would
        otherwise be echoed into logs and the persisted run result. Only a lightly
        redacted, length-capped repr is kept (FAR-411).
        """
        try:
            text = repr(item)
        except Exception:
            text = f"<{type(item).__name__}>"
        redacted = self._redact(text)
        return redacted if len(redacted) <= 80 else redacted[:77] + "..."

    def _result_summary(self, result: Any) -> str:
        """A bounded, redacted string summary of a fan-out result for outcome records."""
        try:
            text = json.dumps(result, default=str)
        except (TypeError, ValueError):
            text = repr(result)
        redacted = self._redact(text)
        if len(redacted) <= 200:
            return redacted
        return redacted[:197] + "..."

    # ── Templating ─────────────────────────────────────────────────────────

    def _render(self, value: Any, context: dict[str, Any]) -> Any:
        """Recursively render Jinja template strings in *value* against *context*.

        Non-string leaves are returned unchanged so structured bodies and lists
        round-trip; only the string templates are rendered. Undefined variables
        render to empty under the sandbox's default ``Undefined`` (the same
        lenient behaviour ``node_runner`` relies on).
        """
        if isinstance(value, str):
            try:
                return self._env.from_string(value).render(**context)
            except Exception as exc:  # sandbox raises on unsafe access
                raise ValueError(f"REST template error: {exc}") from exc
        if isinstance(value, dict):
            return {k: self._render(v, context) for k, v in value.items()}
        if isinstance(value, list):
            return [self._render(v, context) for v in value]
        return value

    # ── Request builder (injection guard) ──────────────────────────────────

    async def _build_request(
        self,
        resource: str,
        context: dict[str, Any],
        *,
        surface: str,
    ) -> RestRequest:
        """Render the operation into a validated :class:`RestRequest`.

        The injection guard runs "write-path-style" filtering on EVERY surface:
        (a) header names/values must not contain CR/LF or control chars;
        (b) rendered headers may not override auth/transport headers;
        (c) the target URL must pass scheme/host allowlist + SSRF validation;
        (d) the rendered URL/params/headers/body are screened with the same
        ``filter_output_for_injection`` the write path uses. Authentication
        credentials (header mode via ``apply_auth``, query-mode ``api_key`` via
        ``_request_kwargs``) are applied AFTER this screening so the secret is
        never fed through the injection filter.
        """
        default_method = "GET" if surface == "read" else "POST"
        spec = self._operation_spec(resource, default_method=default_method)

        headers: dict[str, str] = {}
        rendered_headers = self._render(spec["headers"], context)
        if isinstance(rendered_headers, dict):
            for name, value in rendered_headers.items():
                name_str = str(name)
                value_str = str(value)
                _reject_control_chars(name_str, what="header name")
                _reject_control_chars(value_str, what="header value")
                if name_str.lower() in self._protected_header_names:
                    raise ValueError(f"REST rendered header overrides protected header {name_str!r}")
                headers[name_str] = value_str

        params: dict[str, Any] = {}
        rendered_params = self._render(spec["params"], context)
        if isinstance(rendered_params, dict):
            for key, value in rendered_params.items():
                if value is not None:
                    params[str(key)] = value

        body: Any = None
        if spec["body"] is not None:
            body = self._render(spec["body"], context)

        path = spec["path"]
        if path is None or path == "":
            raise ValueError("REST connector requires a 'path' in config_json (or per-resource operation)")
        url = self._base_url + str(self._render(path, context))

        await self._validate_target_url(url)

        # Write-path-style injection screening of everything that reaches the wire.
        # The prompt-injection TEXT classifier is a write-side concern (the hub
        # guards write payloads with filter_payload_for_injection). On the READ
        # surface it only adds false positives — a legitimate agent-supplied search
        # term like ``q=import os`` would otherwise throw OutputRejectedError. The
        # read surface relies on the real HTTP controls above (control-char
        # rejection, protected-header set, SSRF/allowlist).
        if surface == "write":
            screened: list[str] = [url]
            screened.extend(headers.values())
            screened.extend(str(v) for v in params.values())
            screened.extend(_collect_strings(body))
            self._security_guard.filter_strings(screened, resource=resource)

        return RestRequest(
            method=spec["method"],
            url=url,
            headers=headers,
            params=params,
            body=body,
            records_path=spec["records_path"],
            next_cursor_path=spec["next_cursor_path"],
            passthrough=spec["passthrough"],
            idempotency_header=spec["idempotency_header"],
        )

    async def _build_health_request(self) -> RestRequest:
        """Build the health-probe request against ``base_url`` + the configured path."""
        path = self._config.get("path")
        url = self._base_url + (str(self._render(path, {})) if path else "")
        await self._validate_target_url(url)
        return RestRequest(method="GET", url=url, headers={}, params={}, body=None)

    async def _validate_target_url(self, url: str) -> None:
        """Enforce the scheme/host allowlist and SSRF safety on *url*.

        Scheme must be ``http``/``https``. When ``config['allowed_hosts']`` is
        set, the host must be in that list (or a subdomain of an entry). Always
        runs SSRF validation (via the injected guard's ``validate_url``) to block
        private/loopback/metadata targets — unless a test seam is injected.
        """
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            raise ValueError(f"REST URL must use http:// or https:// scheme — got {parsed.scheme!r}")
        host = (parsed.hostname or "").rstrip(".").strip("[]")
        if not host:
            raise ValueError(f"REST URL must have a hostname: {url!r}")
        allowed = self._config.get("allowed_hosts")
        if (
            isinstance(allowed, list)
            and allowed
            and not any(host == str(allowed_host) or host.endswith("." + str(allowed_host)) for allowed_host in allowed)
        ):
            raise ValueError(f"REST URL host {host!r} is not in allowed_hosts: {allowed!r}")
        if self._ssrf_validator is not None:
            result = self._ssrf_validator(url)
            if inspect.isawaitable(result):
                await result
            return
        await self._security_guard.validate_url(url)

    # ── Send + transform ───────────────────────────────────────────────────

    async def _execute(
        self,
        request: RestRequest,
        *,
        surface: str,
        request_timeout: float | None = None,
        max_retries: int | None = None,
    ) -> ConnectorResult | dict[str, Any]:
        """Resolve + send + surface-map — the one shared dispatch used by query/write."""
        client = self._client()
        resp, body_text = await self._send(client, request, request_timeout=request_timeout, max_retries=max_retries)
        if surface == "read":
            return self._transform(request, resp, body_text)
        return self._write_result(resp, body_text)

    async def _send(
        self,
        client: httpx.AsyncClient,
        request: RestRequest,
        request_timeout: float | None = None,
        *,
        max_retries: int | None = None,
    ) -> tuple[httpx.Response, str]:
        """Run the request with retry/backoff for idempotent verbs.

        GET/HEAD (and any verb with a declared ``idempotency_header``) retries on
        ``429``/``5xx`` and transient transport failures, honouring ``Retry-After``.
        Mutating verbs without an idempotency header never retry. ``request_timeout``,
        when provided, is the per-request timeout (used by the fan-out path for
        a distinct ``per_item_timeout``); when None the pooled client's default
        applies. ``max_retries``, when provided (the fan-out path passes
        ``fan_out.max_retries``), is the retry budget in RETRIES; the loop runs
        ``max_retries + 1`` attempts (clamped to a sane upper bound).
        """
        retries = _MAX_RETRIES - 1 if max_retries is None else max_retries
        retries = max(0, min(retries, _MAX_SANE_RETRIES))
        # Rate-limit wait deadline: bound the token-bucket refill by the per-request
        # timeout so a saturated destination never spins past the per-item budget.
        rate_wait_timeout = request_timeout if request_timeout is not None else self._timeout
        kwargs = self._request_kwargs(request)
        if not self._is_retryable(request):
            await self._acquire_rate_token(self._base_url, deadline_seconds=rate_wait_timeout)
            return await self._perform_request(client, request, kwargs, request_timeout=request_timeout)
        return await self._send_retryable(client, request, kwargs, rate_wait_timeout, request_timeout, retries + 1)

    async def _send_retryable(
        self,
        client: httpx.AsyncClient,
        request: RestRequest,
        kwargs: dict[str, Any],
        rate_wait_timeout: float | None,
        request_timeout: float | None,
        attempts: int,
    ) -> tuple[httpx.Response, str]:
        """Retry loop for idempotent verbs — see :meth:`_send` for the contract.

        A token is acquired before every wire attempt so each retry is metered,
        and a bounded backoff is applied between attempts.
        """
        last_delay = 0.0
        for attempt in range(attempts):
            await asyncio.sleep(last_delay)
            await self._acquire_rate_token(self._base_url, deadline_seconds=rate_wait_timeout)
            try:
                return await self._perform_request(client, request, kwargs, request_timeout=request_timeout)
            except RESTStatusError as exc:
                if not self._is_status_retryable(exc, attempt, attempts):
                    raise
                last_delay = self._retry_delay(exc, attempt)
            except RESTConnectError:
                if attempt == attempts - 1:
                    raise
                last_delay = self._backoff(attempt)
        raise AssertionError("unreachable")  # pragma: no cover

    def _is_status_retryable(self, exc: RESTStatusError, attempt: int, attempts: int) -> bool:
        """Whether a ``RESTStatusError`` should trigger another attempt."""
        return attempt != attempts - 1 and exc.status_code in _RETRYABLE_STATUS

    def _request_kwargs(self, request: RestRequest) -> dict[str, Any]:
        """Build the httpx kwargs, injecting auth headers + idempotency key once."""
        headers = self.apply_auth(dict(request.headers))
        if request.idempotency_header:
            headers[request.idempotency_header] = str(uuid.uuid4())
        params = dict(request.params)
        # api_key-in-query creds are applied right before the wire (after the
        # injection-guard screening in _build_request) so the secret is never
        # screened. This is also the path health_check() routes through, so a
        # query-mode api_key is sent on the health probe too. A rendered/context
        # param of the same name is a collision — a hard error, never a silent
        # override.
        if self._auth["mode"] == "api_key" and self._auth["in"] == "query":
            auth_param = self._auth["query_param_name"]
            if auth_param in params:
                raise ValueError(
                    f"REST param name {auth_param!r} collides with the api_key credential query param name"
                )
            params[auth_param] = self._auth["api_key"]
        kwargs: dict[str, Any] = {"headers": headers, "params": params}
        if request.body is not None:
            if isinstance(request.body, (dict, list)):
                kwargs["json"] = request.body
            else:
                kwargs["content"] = str(request.body)
        return kwargs

    def _is_retryable(self, request: RestRequest) -> bool:
        if request.method in _RETRYABLE_METHODS:
            return True
        return request.idempotency_header is not None

    async def _perform_request(
        self,
        client: httpx.AsyncClient,
        request: RestRequest,
        kwargs: dict[str, Any],
        request_timeout: float | None = None,
    ) -> tuple[httpx.Response, str]:
        """A single HTTP attempt: stream, cap the body, then classify the status."""
        body_text = ""
        try:
            stream_kwargs: dict[str, Any] = dict(kwargs)
            if request_timeout is not None:
                stream_kwargs["timeout"] = request_timeout
            async with client.stream(request.method, request.url, **stream_kwargs) as resp:
                body_text = await self._consume_body(resp)
        except httpx.HTTPError as exc:
            raise RESTConnectError(
                self._redact(f"REST transport error: {request.method} {request.url} — {type(exc).__name__}: {exc}")
            ) from exc
        if resp.status_code >= 300:
            raise RESTStatusError(
                self._status_detail(resp, request, body_text),
                status_code=resp.status_code,
                location=resp.headers.get("location", ""),
                retry_after=parse_retry_after(resp),
            )
        return resp, body_text

    async def _consume_body(self, resp: httpx.Response) -> str:
        """Read the body, aborting past ``max_response_size`` (never unbounded)."""
        cap = self._max_response_size
        content_length = resp.headers.get("content-length")
        if content_length and content_length.isdigit() and int(content_length) > cap:
            raise RESTResponseTooLargeError(
                f"REST response too large: Content-Length {content_length} exceeds cap {cap} bytes"
            )
        chunks: list[bytes] = []
        total = 0
        async for chunk in resp.aiter_bytes():
            total += len(chunk)
            if total > cap:
                raise RESTResponseTooLargeError(f"REST response too large: exceeded cap {cap} bytes")
            chunks.append(chunk)
        return b"".join(chunks).decode(resp.encoding or "utf-8", errors="replace")

    @staticmethod
    def _backoff(attempt: int) -> float:
        """Exponential backoff with jitter (0.5s, 1.0s, 2.0s)."""
        base = 0.5 * (2**attempt)
        return float(base + random.uniform(0, 0.25))  # noqa: S311 — jitter, not a security secret

    @staticmethod
    def _retry_delay(exc: RESTStatusError, attempt: int) -> float:
        if exc.retry_after is not None:
            # Cap an untrusted Retry-After: a server can say 3600 and we must not
            # sleep ~1h per retry. The cap bounds every retry hop (default 30s).
            return min(max(0.0, exc.retry_after), _MAX_RETRY_WAIT)
        return RestConnector._backoff(attempt)

    def _status_detail(self, resp: httpx.Response, request: RestRequest, body_text: str) -> str:
        location = resp.headers.get("location", "")
        location_part = f" (location: {location})" if location else ""
        body = self._redact(body_text[:200])
        return f"REST HTTP {resp.status_code} for {request.method} {request.url}{location_part}: {body}"

    def _transform(self, request: RestRequest, resp: httpx.Response, body_text: str) -> ConnectorResult:
        """Map a REST response onto :class:`ConnectorResult`.

        JSON responses yield a list of dicts for ``records`` (via the
        ``records_path`` JMESPath expression when configured, else a top-level
        array, else the whole object as a single record). ``passthrough`` forces
        a single ``{"body": ..., "content_type": ..., "status_code": ...,
        "headers": ...}`` record wrap even for JSON bodies, so the downstream
        JMESPath consumer still gets a uniform list-of-dicts shape.
        """
        content_type = resp.headers.get("content-type", "")
        parsed: Any = None
        if "json" in content_type.lower() or body_text.lstrip().startswith(("{", "[")):
            try:
                parsed = json.loads(body_text)
            except json.JSONDecodeError:
                parsed = None

        records: list[dict[str, Any]] = []
        next_cursor: str | None = None
        if request.passthrough:
            records = self._passthrough_record(resp, body_text, content_type)
        elif isinstance(parsed, list):
            records = safe_records_list(parsed)
        elif isinstance(parsed, dict):
            records_path = request.records_path
            if records_path:
                source = self._search_jmespath(records_path, parsed)
                if isinstance(source, list):
                    records = safe_records_list(source)
                elif isinstance(source, dict):
                    records = [source]
            else:
                records = [parsed] if parsed else []
            next_cursor = self._extract_cursor(parsed, request.next_cursor_path)
        elif parsed is None and not request.records_path:
            records = self._passthrough_record(resp, body_text, content_type)

        metadata: dict[str, Any] = {
            "status_code": resp.status_code,
            "content_type": content_type,
            "url": str(request.url),
            "method": str(request.method),
        }
        retry_after = parse_retry_after(resp)
        if retry_after is not None:
            metadata["retry_after"] = retry_after
        return ConnectorResult(
            records=records,
            next_cursor=next_cursor,
            total=len(records),
            metadata=metadata,
        )

    def _passthrough_record(
        self,
        resp: httpx.Response,
        body_text: str,
        content_type: str,
    ) -> list[dict[str, Any]]:
        return [
            {
                "body": body_text,
                "content_type": content_type,
                "status_code": resp.status_code,
                "headers": dict(resp.headers.items()),
            }
        ]

    @staticmethod
    def _search_jmespath(path: str, data: Any) -> Any:
        """Run a validated JMESPath search, rejecting legacy dot-index syntax.

        ``data.items.0`` is not expressible in JMESPath (identifiers cannot start
        with a digit); a connector must declare ``data.items[0]``. Rather than
        silently rewriting a ``.0`` segment, REJECT it with a clear actionable
        error so a wrong path fails loud instead of returning wrong data.
        """
        if _DOT_INDEX.search(path):
            raise ValueError(
                f"REST JMESPath path {path!r} is invalid: use bracket index syntax like [0], not dot index like .0"
            )
        return jmespath.search(path, data)

    @staticmethod
    def _extract_cursor(parsed: dict[str, Any], path: str | None) -> str | None:
        if not path:
            return None
        value = RestConnector._search_jmespath(path, parsed)
        return str(value) if isinstance(value, str) and value else None

    def _write_result(self, resp: httpx.Response, body_text: str) -> dict[str, Any]:
        """Map a REST write response onto a JSON-serialisable result dict."""
        if body_text:
            try:
                parsed = json.loads(body_text)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict):
                return parsed
            if isinstance(parsed, list):
                return {"records": parsed}
        return {
            "status_code": resp.status_code,
            "body": body_text,
            "content_type": resp.headers.get("content-type", ""),
        }
