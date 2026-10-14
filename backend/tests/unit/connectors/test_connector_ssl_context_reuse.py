"""FAR-1650: a connector builds its egress TLS context once per instance.

Every connector builds a fresh ``httpx.AsyncClient`` per call through
``pinned_async_client_sync``. That factory deliberately rebuilds the pinned
transport (and its connection pool) per call, but it also rebuilt the TLS
context inside that transport every time: ``ssl.create_default_context`` loads
the CA bundle from disk (~47 ms of a ~52 ms client build, measured 2026-10-11)
even though the resulting context is identical for every connector in the
process.

These tests pin the fix: ONE TLS context per connector instance across any
number of client constructions, actually used by the pinned transport, with
``SSRF_TRUST_PROXY``-driven CA-bundle selection kept in lock-step with
``modulo.core.ssrf``.

The ``modulo.connectors._egress`` imports are function-local so the
construction-count test below still runs (and still fails for the right
reason) on a tree where the fix does not exist.
"""

from __future__ import annotations

import ssl
from typing import Any

import httpx
import pytest

from modulo.connectors.asana import AsanaConnector

TOKEN = "test-token"  # nosec B105 - test fixture value, not a credential


class _CountingSSLContextFactory:
    """Counts ``ssl.create_default_context`` invocations (the CA-bundle load)."""

    def __init__(self) -> None:
        self.calls = 0
        self._real = ssl.create_default_context

    def __call__(self, *args: Any, **kwargs: Any) -> ssl.SSLContext:
        self.calls += 1
        return self._real(*args, **kwargs)


@pytest.fixture
def ssl_context_builds(monkeypatch: pytest.MonkeyPatch) -> _CountingSSLContextFactory:
    counter = _CountingSSLContextFactory()
    monkeypatch.setattr(ssl, "create_default_context", counter)
    return counter


async def _closed(client: httpx.AsyncClient) -> httpx.AsyncClient:
    """Close a connector-built client the way ``async with self._client()`` does."""
    await client.aclose()
    return client


async def test_tls_context_built_once_per_connector_instance_not_per_call(
    ssl_context_builds: _CountingSSLContextFactory,
) -> None:
    """TWO ``_client()`` calls on ONE instance build ONE TLS context.

    The client itself is still built per call (its pinned transport and pool are
    per-request by design); only the TLS context is reused. Before FAR-1650 the
    counter reads 2 here — one CA-bundle load per request.
    """
    connector = AsanaConnector(TOKEN)

    first = await _closed(connector._client())
    second = await _closed(connector._client())

    assert first is not second
    assert ssl_context_builds.calls == 1


def test_each_connector_instance_builds_its_own_context(
    ssl_context_builds: _CountingSSLContextFactory,
) -> None:
    """The cache is per connector instance — no process-wide singleton."""
    from modulo.connectors._egress import egress_ssl_context

    first = AsanaConnector(TOKEN)
    second = AsanaConnector(TOKEN)

    first_ctx = egress_ssl_context(first)
    second_ctx = egress_ssl_context(second)

    assert first_ctx is not second_ctx
    assert ssl_context_builds.calls == 2


async def test_pinned_transport_reuses_the_instance_ssl_context() -> None:
    """The reuse is real: the client's transport holds the instance context."""
    from modulo.connectors._egress import egress_ssl_context

    connector = AsanaConnector(TOKEN)
    instance_ctx = egress_ssl_context(connector)

    client = connector._client()
    try:
        assert client._transport._pool._ssl_context is instance_ctx
    finally:
        await client.aclose()


async def test_context_is_reused_after_the_previous_client_was_closed() -> None:
    """The realistic path: ``async with self._client()`` closes every client."""
    from modulo.connectors._egress import egress_ssl_context

    connector = AsanaConnector(TOKEN)
    expected = egress_ssl_context(connector)

    async with connector._client() as first:
        assert first._transport._pool._ssl_context is expected
    async with connector._client() as second:
        assert second._transport._pool._ssl_context is expected


@pytest.mark.parametrize(
    "value",
    ["__unset__", "__empty__", "1", "true", "TRUE", "yes", "on", "off", "no", "0", " True "],
    ids=["unset", "empty", "one", "lower-true", "upper-true", "yes", "on", "off", "no", "zero", "padded-true"],
)
def test_egress_trust_env_matches_ssrf_default(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    """``connectors._egress`` shares the ONE ``core.ssrf`` trust-env resolver.

    ``connectors._egress`` imports the shared resolver from
    ``modulo.core.ssrf`` under the name ``_egress_trust_env`` (import-linter's
    ``connectors-do-not-import-core`` contract carries a deliberate per-module
    allowance for this shared security primitive). Pinning the bound name to
    the SSRF module's function OBJECT is what stops drift: a future edit that
    re-introduces a parallel ``SSRF_TRUST_PROXY`` read here would bind a
    different callable and fail this test. Comparing return values alone would
    not — since the dedup, both sides are the same function, so such a
    comparison would only test that a function equals itself.
    """
    from modulo.connectors import _egress
    from modulo.core import ssrf

    if value == "__unset__":
        monkeypatch.delenv("SSRF_TRUST_PROXY", raising=False)
    elif value == "__empty__":
        monkeypatch.setenv("SSRF_TRUST_PROXY", "")
    else:
        monkeypatch.setenv("SSRF_TRUST_PROXY", value)

    # Dedup pin: both names must be the *same* function object, not two mirrors
    # that happen to agree on today's inputs.
    assert _egress._egress_trust_env is ssrf.default_trust_env

    # Behavioural: the shared resolver maps ``SSRF_TRUST_PROXY`` exactly as
    # documented (true/1/yes/on honour a proxy; anything else keeps the safe
    # default), asserted against a concrete expectation rather than by
    # comparing the function to itself.
    expected = value.strip().lower() in ("1", "true", "yes", "on")
    assert _egress._egress_trust_env() is expected


def test_cached_context_trust_anchors_match_a_plain_verify_true_build(monkeypatch: pytest.MonkeyPatch) -> None:
    """The cached context is a drop-in for the plain ``verify=True`` build.

    Every connector passes ``verify=egress_ssl_context(self)`` to the pinned
    egress factory, so the instance's cached context replaces httpx's own
    per-call build. Pin the TRUST STORE behind that swap: for the default path
    (``SSRF_TRUST_PROXY`` unset — both builds then load the same certifi CA
    bundle) the cached context has the same CA count, ``verify_mode`` and
    ``check_hostname`` a plain ``verify=True`` context has. A context built
    with different anchors would silently change which certificate authorities
    every connector egress trusts.
    """
    from modulo.connectors._egress import egress_ssl_context

    monkeypatch.delenv("SSRF_TRUST_PROXY", raising=False)

    connector = AsanaConnector(TOKEN)
    cached = egress_ssl_context(connector)
    plain = httpx.create_ssl_context(verify=True, trust_env=False)

    plain_anchors = plain.get_ca_certs()
    cached_anchors = cached.get_ca_certs()
    assert len(cached_anchors) == len(plain_anchors)
    assert len(plain_anchors) > 0
    assert cached.verify_mode == plain.verify_mode
    assert plain.verify_mode == ssl.CERT_REQUIRED
    assert cached.check_hostname is plain.check_hostname
    assert cached.check_hostname is True
