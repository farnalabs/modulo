"""Real-network evidence for the SSRF pinned transport (FAR-1127).

Raises the SSRF egress-control evidence (``cap-core-ssrf``) above mock-only
coverage by exercising the REAL pinned transport and the REAL resolver against
a loopback HTTP server spawned inside the test process (stdlib
``http.server`` on ``127.0.0.1:0``).

No mocks are used anywhere in this module: the transport, the resolver and the
sockets are all live. The only network ranges touched are loopback and
reserved literals; no external host is ever contacted and no metadata service
is ever requested. Loopback is reachable only through an explicit per-target
allowlist, so the suite cannot pass by refusing everything — a genuine
positive case proves the transport connects, reads a response and consults the
pin, while every negative case asserts a typed refusal (``ValueError`` /
``UnpinnedHostError``) rather than a generic connection error.
"""

from __future__ import annotations

import http.server
import threading
from collections.abc import Iterator
from typing import ClassVar

import httpx
import pytest

from modulo.core import ssrf

# Opt out of the autouse DNS shim (tests/conftest.py) so the REAL resolver is
# consulted — the module's filename also opts out, this marker makes it explicit.
pytestmark = pytest.mark.real_ssrf_dns

_SERVER_BODY = b"real-network-evidence"


class _QuietHandler(http.server.BaseHTTPRequestHandler):
    """Minimal GET handler that records every path it serves.

    The hit log proves a real TCP connection reached the loopback server; the
    ``/redirect`` endpoint issues a 302 that would pivot off-box if the pin
    failed, so refusing its final hop is asserted without a real request to
    the metadata service.
    """

    hits: ClassVar[list[str]] = []

    def do_GET(self) -> None:
        _QuietHandler.hits.append(self.path)
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "http://169.254.169.254/latest/meta-data/")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = _SERVER_BODY
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: object) -> None:
        pass


@pytest.fixture
def loopback_server() -> Iterator[http.server.HTTPServer]:
    """Real HTTP server on an ephemeral loopback port, torn down with a bound join."""
    _QuietHandler.hits.clear()
    server = http.server.HTTPServer(("127.0.0.1", 0), _QuietHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _pinned_hosts(client: httpx.AsyncClient) -> dict[str, tuple[str, ...]]:
    return client._transport._pool._network_backend._pinned_hosts  # type: ignore[no-any-return]


def _connect_calls(client: httpx.AsyncClient) -> int:
    return client._transport._pool._network_backend.connect_calls


async def test_pinned_transport_reaches_real_loopback_server(
    loopback_server: http.server.HTTPServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Positive: the pinned client really connects and the response is read.

    ``localhost`` resolves via the REAL resolver to ``::1`` and ``127.0.0.1``;
    the server listens on IPv4 loopback. The pin map holds the FULL validated
    set and the transport fails over from ``::1`` (refused) to ``127.0.0.1``.
    The loudness guard + ``connect_calls`` prove the pinned backend's
    ``connect_tcp`` was consulted for the actual socket. Without the explicit
    allowlist this URL is refused, so refusing-everything cannot green it.
    """
    port = loopback_server.server_address[1]
    monkeypatch.setenv("SSRF_ALLOW_PRIVATE_RANGES", "127.0.0.0/8,::1/128")
    client = await ssrf.pinned_async_client(f"http://localhost:{port}/health", loudness_guard=True)
    try:
        resp = await client.get(f"http://localhost:{port}/health")
    finally:
        await client.aclose()

    assert resp.status_code == 200
    assert resp.text == _SERVER_BODY.decode()
    assert "127.0.0.1" in _pinned_hosts(client)["localhost"]
    assert _connect_calls(client) == 1
    assert _QuietHandler.hits.count("/health") == 1


async def test_sync_client_builder_reaches_real_loopback_server(
    loopback_server: http.server.HTTPServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Positive: the synchronous builder resolves, pins and connects for real."""
    port = loopback_server.server_address[1]
    monkeypatch.setenv("SSRF_ALLOW_PRIVATE_RANGES", "127.0.0.0/8")
    client = ssrf.pinned_async_client_sync(f"http://127.0.0.1:{port}/")
    try:
        resp = await client.get(f"http://127.0.0.1:{port}/")
    finally:
        await client.aclose()

    assert resp.status_code == 200
    assert resp.text == _SERVER_BODY.decode()
    assert _QuietHandler.hits.count("/") == 1


@pytest.mark.parametrize(
    "blocked_url",
    [
        pytest.param("http://169.254.169.254/latest/meta-data/", id="aws_imds_link_local_metadata"),
        pytest.param("http://127.0.0.1/", id="loopback_ipv4"),
        pytest.param("http://10.1.2.3/", id="private_10"),
        pytest.param("http://172.16.5.5/", id="private_172_16"),
        pytest.param("http://192.168.1.1/", id="private_192_168"),
    ],
)
async def test_blocked_literal_targets_are_typed_refusals(
    blocked_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Private/loopback/link-local/metadata literals are REFUSED, never a connect error."""
    monkeypatch.delenv("SSRF_ALLOW_PRIVATE_RANGES", raising=False)
    with pytest.raises(ValueError, match="private/internal") as excinfo:
        await ssrf.pinned_async_client(blocked_url)
    assert not isinstance(excinfo.value, (httpx.ConnectError, httpx.TransportError))
    with pytest.raises(ValueError, match="private/internal"):
        ssrf.validate_outbound_url(blocked_url)


@pytest.mark.parametrize(
    "blocked_url",
    [
        pytest.param("http://[::1]/", id="ipv6_loopback"),
        pytest.param("http://[fe80::1]/", id="ipv6_link_local"),
        pytest.param("http://[fec0::1]/", id="ipv6_site_local"),
    ],
)
async def test_ipv6_literal_targets_are_typed_refusals(
    blocked_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """IPv6 literals (loopback/link-local/site-local) are refused typed, not connected."""
    monkeypatch.delenv("SSRF_ALLOW_PRIVATE_RANGES", raising=False)
    with pytest.raises(ValueError, match="private/internal") as excinfo:
        await ssrf.pinned_async_client(blocked_url)
    assert not isinstance(excinfo.value, httpx.TransportError)


async def test_redirect_final_hop_to_blocked_address_is_refused(
    loopback_server: http.server.HTTPServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pin holds across a redirect: a 302 → 169.254.169.254 is refused.

    The redirect chain MUST step through the real loopback server (proving the
    redirect was genuinely followed) and the final hop is refused by the pin
    map BEFORE any connection to the blocked/metadata address is attempted.
    """
    port = loopback_server.server_address[1]
    monkeypatch.setenv("SSRF_ALLOW_PRIVATE_RANGES", "127.0.0.0/8,::1/128")
    client = await ssrf.pinned_async_client(
        f"http://127.0.0.1:{port}/redirect",
        follow_redirects=True,
    )
    try:
        with pytest.raises(ssrf.UnpinnedHostError, match=r"169\.254\.169\.254") as excinfo:
            await client.get(f"http://127.0.0.1:{port}/redirect")
    finally:
        await client.aclose()

    assert _QuietHandler.hits.count("/redirect") == 1
    assert not isinstance(excinfo.value, httpx.TransportError)


async def test_dns_name_resolving_to_loopback_is_refused_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A DNS name that resolves to a blocked address is refused (fail-closed).

    ``localhost`` resolves via the REAL resolver to ``::1``/``127.0.0.1``;
    without an allowlist the pinned client refuses it with a typed ValueError —
    never a connection error.
    """
    monkeypatch.delenv("SSRF_ALLOW_PRIVATE_RANGES", raising=False)
    with pytest.raises(ValueError, match="resolves to a private/internal address") as excinfo:
        await ssrf.pinned_async_client("http://localhost:1024/health")
    assert not isinstance(excinfo.value, httpx.TransportError)
    with pytest.raises(ValueError, match="resolves to a private/internal address"):
        ssrf.validate_outbound_url("http://localhost:1024/health")
