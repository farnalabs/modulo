"""FAR-512: outbound connectors + model backends migrated to the pinned-IP transport.

Before FAR-512, a ``base_url``-bearing connector validated its egress target
(:func:`modulo.core.ssrf.validate_outbound_url`) and then built its own
``httpx.AsyncClient`` — a validate-then-connect pattern. A hostname under
attacker DNS control could answer public at validation time and internal
(169.254.169.254) at connect time (DNS rebinding), escaping the gate.

These tests prove the migration actually pins: a connector's ``_client()`` now
builds a :class:`modulo.core.ssrf.PinnedAsyncHTTPTransport` whose pin map is
``{hostname: validated_ip}``, so the connection goes to the VALIDATED address
and never re-resolves the host. The resolver is stubbed to FLIP from the
validated public address to the cloud-metadata address between validation and
connect — the transport must still target the validated IP and refuse the
flipped host.
"""

from __future__ import annotations

import asyncio

import pytest

import modulo.core.ssrf as ssrf
from modulo.connectors.gitlab import GitLabConnector
from modulo.connectors.rest import RestConnector

# Resolver flip: first validation resolves PUBLIC (accepted), any later lookup
# answers with the cloud-metadata address (what an attacker's rebinding DNS
# would serve at connect time).
_VALIDATED_PUBLIC = "93.184.216.34"
_REBOUND_METADATA = "169.254.169.254"


def _flip_resolver(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub ``ssrf._resolve_all_sync`` to flip public -> metadata after the first call."""

    def fake(host: str) -> list[str]:
        if host not in _flip_resolver._seen:  # type: ignore[attr-defined]
            _flip_resolver._seen.append(host)  # type: ignore[attr-defined]
            return [_VALIDATED_PUBLIC]
        return [_REBOUND_METADATA]

    _flip_resolver._seen = []  # type: ignore[attr-defined]
    monkeypatch.setattr(ssrf, "_resolve_all_sync", fake)


def _pinned_hosts(client: object) -> dict[str, str]:
    """Return the pin map of the client's pinned transport."""
    transport = client._transport
    backend = transport._pool._network_backend
    return backend._pinned_hosts


def _aclose(client: object) -> None:
    asyncio.run(client.aclose())


def test_gitlab_client_pins_validated_ip_despite_resolver_flip(monkeypatch: pytest.MonkeyPatch) -> None:
    """A ``base_url``-bearing connector's ``_client()`` pins the validated IP.

    The resolver flips to the metadata address after the validation lookup. The
    pinned transport must have captured the VALIDATED address in its pin map —
    the transport never re-resolves, so the rebound metadata answer is simply
    irrelevant to the connection.
    """
    _flip_resolver(monkeypatch)
    connector = GitLabConnector(token="test-token", base_url="https://gitlab.example.com/api/v4")

    client = connector._client()
    try:
        assert _pinned_hosts(client) == {"gitlab.example.com": _VALIDATED_PUBLIC}
    finally:
        _aclose(client)


def test_gitlab_client_pins_per_validation_and_fails_closed_on_rebind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each fresh ``_client()`` re-validates + pins; a rebind fails closed.

    ``GitLabConnector._client()`` builds a fresh client per call (not cached).
    The FIRST validation sees a public address and pins it onto the transport
    (so that one client never re-resolves). A SECOND ``_client()`` — now under
    the flipped resolver — must re-validate, see the private address, and fail
    closed rather than connect. This is the per-client pin that closes rebind.
    """
    _flip_resolver(monkeypatch)
    connector = GitLabConnector(token="test-token", base_url="https://gitlab.example.com/api/v4")

    first = connector._client()
    try:
        assert _pinned_hosts(first) == {"gitlab.example.com": _VALIDATED_PUBLIC}
    finally:
        _aclose(first)

    # A second client construction re-validates; the resolver now answers the
    # rebound metadata (private) address, so the gate fails closed.
    with pytest.raises(ValueError, match="private/internal"):
        connector._client()


def test_rest_client_pins_base_url_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """The REST connector pins its tenant-supplied ``base_url`` host.

    REST is the highest-risk surface (tenant base_url + templated paths), so its
    production ``_client()`` (no injected ``transport`` seam) must build a pinned
    transport for the ``base_url`` host, capturing the validated address.
    """
    _flip_resolver(monkeypatch)
    connector = RestConnector(
        {"base_url": "https://rest-target.example.com", "path": "/items"},
        {"auth_mode": "bearer", "token": "t"},
    )

    client = connector._client()
    try:
        assert _pinned_hosts(client) == {"rest-target.example.com": _VALIDATED_PUBLIC}
    finally:
        _aclose(client)


def test_rest_client_refuses_unpinned_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """A host outside the pin map is refused fail-closed.

    A redirected ``follow_redirects`` path or a templated URL that points at a
    different host is NOT silently re-validated at connect time — the pinned
    backend raises ``UnpinnedHostError``. This is what previously let a rebind
    escape: connect re-resolved the host. Now only validated hosts connect.
    """
    _flip_resolver(monkeypatch)
    connector = RestConnector(
        {"base_url": "https://rest-target.example.com", "path": "/items"},
        {"auth_mode": "bearer", "token": "t"},
    )

    client = connector._client()
    try:
        backend = client._transport._pool._network_backend
        with pytest.raises(ssrf.UnpinnedHostError):
            asyncio.run(backend.connect_tcp("rebound-internal.example", 443))
    finally:
        _aclose(client)


def test_sync_pinned_helpers_wire_correctly(monkeypatch: pytest.MonkeyPatch) -> None:
    """Wiring guard for the sync builders used by synchronous ``_client()``.

    Exercises ``pinned_async_transport_sync`` directly so the connector-level
    tests are unambiguous about where the pin happens. A blocked target still
    fails closed through the sync path.
    """
    _flip_resolver(monkeypatch)
    transport = ssrf.pinned_async_transport_sync("https://pinned-target.example.com/")
    try:
        backend = transport._pool._network_backend
        assert backend._pinned_hosts == {"pinned-target.example.com": _VALIDATED_PUBLIC}
    finally:
        asyncio.run(transport.aclose())


def test_sync_pinned_client_still_blocks_internal_target(monkeypatch: pytest.MonkeyPatch) -> None:
    """A connector whose validator now sees a private address fails closed.

    The pin builder validates synchronously; a target that resolves private must
    not produce a client — the same fail-closed the old ``validate_outbound_url``
    path provided.
    """
    monkeypatch.setattr(ssrf, "_resolve_all_sync", lambda _host: ["10.0.0.5"])
    with pytest.raises(ValueError, match="private/internal"):
        ssrf.pinned_async_client_sync("https://blocked.example.com/")
