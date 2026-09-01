"""FAR-526C: the ConnectorBase "owns a pinned client + redactor" contract.

``ConnectorBase`` does not force a single client implementation — each connector
may build its client differently — so this suite proves the CONTRACT a conforming
subclass must satisfy, rather than a specific implementation:

1. A ConnectorBase subclass whose ``_client()`` builds through the shared
   ``pinned_async_client_sync`` factory returns a client whose transport is
   PINNED to the validated address (never an unpinned ``httpx`` pool).
2. A credential value echoed back in a remote error (e.g. a 401 that re-prints
   the token) is redacted to ``***`` by the shared ``CredentialRedactor``, so a
   leaked credential cannot escape through run error detail / health checks.

A connector that silently drops the pin, or that ships an un-redacted error, is
precisely the regression these assertions catch.
"""

from __future__ import annotations

from typing import Any

import pytest

import modulo.core.ssrf as ssrf
from modulo.connectors.base import (
    ConnectorBase,
    ConnectorPayload,
    ConnectorQuery,
    ConnectorResult,
    ConnectorType,
    HealthResult,
)
from modulo.connectors.security import CredentialRedactor

_PUBLIC = "93.184.216.34"


class _SeamConnector(ConnectorBase):
    """Minimal ConnectorBase subclass that builds a pinned client in ``_client()``."""

    def __init__(self, token: str, base_url: str) -> None:
        self._token = token
        self._base_url = base_url

    @property
    def connector_type(self) -> ConnectorType:
        return ConnectorType.CUSTOM

    def _client(self) -> Any:
        return ssrf.pinned_async_client_sync(
            self._base_url,
            base_url=self._base_url,
            headers={"Authorization": f"Bearer {self._token}"},
            timeout=30,
        )

    async def health_check(self) -> HealthResult:
        return HealthResult(ok=True)

    async def query(self, q: ConnectorQuery) -> ConnectorResult:
        return ConnectorResult()

    async def write(self, payload: ConnectorPayload) -> dict[str, Any]:
        return {}


def _pinned_hosts(client: Any) -> dict[str, tuple[str, ...]]:
    transport = client._transport
    backend = transport._pool._network_backend
    return backend._pinned_hosts


def _aclose(client: Any) -> None:
    import asyncio

    asyncio.run(client.aclose())


def test_connector_base_subclass_client_is_pinned(monkeypatch: pytest.MonkeyPatch) -> None:
    """A ConnectorBase ``_client()`` built through the factory is pinned.

    The resolver is stubbed so the validated address is known; the produced
    client's transport must pin exactly that address for the base_url host.
    """
    monkeypatch.setattr(ssrf, "_resolve_all_sync", lambda _host: [_PUBLIC])
    connector = _SeamConnector(token="seam-token", base_url="https://seam.example.com")

    client = connector._client()
    try:
        assert _pinned_hosts(client) == {"seam.example.com": (_PUBLIC,)}
    finally:
        _aclose(client)


def test_connector_base_subclass_client_rejects_unpinned_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pinned client refuses a host outside its pin map (fail-closed)."""
    monkeypatch.setattr(ssrf, "_resolve_all_sync", lambda _host: [_PUBLIC])
    connector = _SeamConnector(token="seam-token", base_url="https://seam.example.com")

    client = connector._client()
    try:
        transport = client._transport
        backend = transport._pool._network_backend
        with pytest.raises(ssrf.UnpinnedHostError):
            import asyncio

            asyncio.run(backend.connect_tcp("rebound-internal.example", 443))
    finally:
        _aclose(client)


def test_credential_echo_survives_redaction() -> None:
    """A credential echoed back in an error message is redacted to ``***``.

    Simulates a 401 whose message re-prints the token (the classic way a remote
    API leaks a credential back to the caller). The shared redactor must strip
    it so the error detail a connector surfaces never carries the live value.
    """
    token = "ghp_fake_credential_value_1234567890"  # nosec B105 - test fixture value, not a credential
    redactor = CredentialRedactor([token], scrub_url=True, chain_cause=False)

    error = ValueError(f"401 Unauthorized from https://api.example.com: invalid token {token} in header")
    repaired = redactor.redact_exc(error)

    assert isinstance(repaired, ValueError)
    assert token not in str(repaired)
    assert "***" in str(repaired)
