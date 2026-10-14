"""Shared TLS-context factory for connector egress clients (FAR-1650).

Every connector builds a fresh ``httpx.AsyncClient`` per call through
``modulo.core.ssrf.pinned_async_client_sync``. That factory is deliberately
un-cached: the pinned transport, its resolved address set and its connection
pool are per-request. But the TLS context httpx builds *inside* that transport
is not per-request at all — ``ssl.create_default_context(cafile=certifi.where())``
loads the CA bundle from disk on every construction, and the resulting context
is identical for every connector in the process. Measured 2026-10-11 on this
workstation: a connector client build costs ~52 ms, of which ~47 ms is that one
context (the same build with a pre-built context: ~4 ms).

:func:`egress_ssl_context` builds that context ONCE per connector instance and
hands it back for reuse; connectors pass it as ``verify=`` to
``pinned_async_client_sync``. httpx's ``create_ssl_context`` returns a supplied
``ssl.SSLContext`` unchanged instead of rebuilding it, so the construction count
drops to one per instance with no behavioural change: the same trust anchors,
the same ``trust_env``-driven CA-bundle selection, and the same pinned-IP
transport. A context supplies trust anchors only — the validated address is
still pinned onto the connection by ``core.ssrf``, and ``async with
self._client()`` still closes every client as before.

The cache is PER CONNECTOR INSTANCE, not a process-wide singleton: it is bounded
by the connector's lifetime, needs no reset seam to test, and keeps the change
observable per instance ("built once per instance, not per call").
``base.py`` stays a stdlib-only leaf, so the ``httpx`` dependency lives here
rather than on ``ConnectorBase``; only the cache *slot* is declared on the base
class.
"""

from __future__ import annotations

import ssl

import httpx

from modulo.connectors.base import ConnectorBase
from modulo.core.ssrf import default_trust_env as _egress_trust_env


def egress_ssl_context(connector: ConnectorBase) -> ssl.SSLContext:
    """Return ``connector``'s egress TLS context, building it at most once.

    The context is built exactly as httpx would build it for ``verify=True``
    with the transport's resolved ``trust_env`` (see
    ``default_trust_env`` in ``modulo.core.ssrf`` — the shared ``SSRF_TRUST_PROXY``
    resolver this module imports, so the CA-bundle selection cannot drift from
    the pinned transport's), so swapping a per-call build for this cached
    one changes only the construction count. Callers pass the result as
    ``verify=`` to ``pinned_async_client_sync`` / ``pinned_async_transport_sync``.
    """
    ctx = connector._egress_ssl_context
    if ctx is None:
        ctx = httpx.create_ssl_context(verify=True, trust_env=_egress_trust_env())
        connector._egress_ssl_context = ctx
    return ctx
