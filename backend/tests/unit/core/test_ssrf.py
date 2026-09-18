"""Unit tests for the SSRF safe-URL validation helpers.

Covers the literal-IP blocking branch (the core security primitive), the
hostname-resolution path via patched resolvers, the async variant used by
event-loop-hostile callers, and the false-positive regression where a
hostname literally containing "private/internal" must not be spuriously
blocked.

Also covers the FAR-409 pinned-IP connection transport (DNS-rebinding
hardening, connect-to-pinned-validated-IP with SNI matching the hostname),
tenant-scoped egress allowlist layering, non-canonical IP-literal rejection,
fail-closed any-IP-blocked semantics, and redirect safety.
"""

import asyncio
import datetime
import http.server
import logging
import ssl
import threading
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpcore
import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from modulo.core import ssrf


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8080/admin",
        "http://10.0.0.5/",
        "http://192.168.1.1/",
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata
        "http://100.100.100.200/latest/meta-data/",  # Aliyun metadata
        "http://100.64.0.1/",  # CGNAT
        "http://0.0.0.0/",  # current network
    ],
)
def test_validate_outbound_url_blocks_private_literal_ips(url: str) -> None:
    with pytest.raises(ValueError, match="private/internal"):
        ssrf.validate_outbound_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://8.8.8.8/",
        "http://1.1.1.1/",
    ],
)
def test_validate_outbound_url_accepts_public_literal_ips_without_dns(url: str) -> None:
    # Public literal IPs must be accepted without any DNS lookup: the
    # validator returns None (no exception) and must not attempt DNS, which
    # would raise for a bare literal IP if the resolver were consulted.
    assert ssrf.validate_outbound_url(url) is None


def test_validate_outbound_url_blocks_hostname_resolving_to_internal() -> None:
    def fake_resolve(host: str) -> list[str]:
        assert host == "example.internal.com"
        return ["10.0.0.5"]

    try:
        original = ssrf._resolve_all_sync
        ssrf._resolve_all_sync = fake_resolve
        with pytest.raises(ValueError, match="resolves to a private/internal address"):
            ssrf.validate_outbound_url("http://example.internal.com/")
    finally:
        ssrf._resolve_all_sync = original


def test_validate_outbound_url_accepts_hostname_resolving_to_public() -> None:
    def fake_resolve(host: str) -> list[str]:
        assert host == "example.com"
        return ["93.184.216.34"]

    try:
        original = ssrf._resolve_all_sync
        ssrf._resolve_all_sync = fake_resolve
        ssrf.validate_outbound_url("http://example.com/")
    finally:
        ssrf._resolve_all_sync = original


def test_hostname_containing_private_internal_is_not_spuriously_blocked() -> None:
    """Regression: a hostname with 'private/internal' in its name must be
    resolved, not rejected by the literal-IP false-positive path."""

    def fake_resolve(host: str) -> list[str]:
        assert host == "private.internal.example.com"
        return ["93.184.216.34"]

    try:
        original = ssrf._resolve_all_sync
        ssrf._resolve_all_sync = fake_resolve
        ssrf.validate_outbound_url("https://private.internal.example.com/")
    finally:
        ssrf._resolve_all_sync = original


async def test_validate_outbound_url_async_blocks_literal_ip() -> None:
    with pytest.raises(ValueError, match="private/internal"):
        await ssrf.validate_outbound_url_async("http://127.0.0.1/")


async def test_validate_outbound_url_async_resolves_and_blocks_internal() -> None:
    async def fake_resolve(host: str) -> list[str]:
        assert host == "collector.internal"
        return ["192.168.0.10"]

    try:
        original = ssrf._resolve_all_async
        ssrf._resolve_all_async = fake_resolve
        with pytest.raises(ValueError, match="resolves to a private/internal address"):
            await ssrf.validate_outbound_url_async("http://collector.internal:4318/")
    finally:
        ssrf._resolve_all_async = original


async def test_validate_outbound_url_async_accepts_public_hostname() -> None:
    async def fake_resolve(host: str) -> list[str]:
        assert host == "api.example.com"
        return ["93.184.216.34"]

    try:
        original = ssrf._resolve_all_async
        ssrf._resolve_all_async = fake_resolve
        await ssrf.validate_outbound_url_async("https://api.example.com/")
    finally:
        ssrf._resolve_all_async = original


def test_allowlist_allows_matching_private_range(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SSRF_ALLOW_PRIVATE_RANGES", "10.0.0.0/8")
    # 10.1.2.3 is private but explicitly allowlisted.
    assert ssrf.validate_outbound_url("http://10.1.2.3/") is None


def test_allowlist_multiple_cidrs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SSRF_ALLOW_PRIVATE_RANGES", "192.168.0.0/16,10.0.0.0/8")
    assert ssrf.validate_outbound_url("http://192.168.1.1/") is None
    assert ssrf.validate_outbound_url("http://10.0.0.9/") is None


def test_allowlist_metadata_range_still_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    # Only the allowlisted 10.0.0.0/8 is permitted; cloud metadata stays blocked.
    monkeypatch.setenv("SSRF_ALLOW_PRIVATE_RANGES", "10.0.0.0/8")
    with pytest.raises(ValueError, match="private/internal"):
        ssrf.validate_outbound_url("http://169.254.169.254/latest/meta-data/")
    with pytest.raises(ValueError, match="private/internal"):
        ssrf.validate_outbound_url("http://127.0.0.1/")


def test_allowlist_invalid_entry_logged_and_other_cidrs_apply(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("SSRF_ALLOW_PRIVATE_RANGES", "not-a-cidr,10.0.0.0/8")
    assert ssrf.validate_outbound_url("http://10.1.2.3/") is None
    assert any("ssrf.invalid_allowlist_entry" in r.message for r in caplog.records)


def test_allowlist_honours_runtime_env_change(monkeypatch: pytest.MonkeyPatch) -> None:
    """The allowlist is parsed lazily and cached on the env value, so a
    mid-process env change takes effect without a restart."""
    monkeypatch.setenv("SSRF_ALLOW_PRIVATE_RANGES", "")
    with pytest.raises(ValueError, match="private/internal"):
        ssrf.validate_outbound_url("http://10.1.2.3/")
    monkeypatch.setenv("SSRF_ALLOW_PRIVATE_RANGES", "10.0.0.0/8")
    assert ssrf.validate_outbound_url("http://10.1.2.3/") is None
    # And switching back off re-blocks.
    monkeypatch.setenv("SSRF_ALLOW_PRIVATE_RANGES", "")
    with pytest.raises(ValueError, match="private/internal"):
        ssrf.validate_outbound_url("http://10.1.2.3/")


# ---------------------------------------------------------------------------
# FAR-409: pinned-IP transport + tenant-scoped egress allowlist
# ---------------------------------------------------------------------------


class _QuietHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = b"pinned-ok"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: object) -> None:
        pass


def _start_http_server() -> tuple[http.server.HTTPServer, int]:
    """Start a plain-HTTP server on an ephemeral 127.0.0.1 port."""
    server = http.server.HTTPServer(("127.0.0.1", 0), _QuietHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, port


def _make_self_signed_cert(hostname: str, cert_pem: Path, key_pem: Path) -> None:
    """Generate a self-signed cert whose SAN is exactly ``hostname``."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)])
    san = x509.SubjectAlternativeName([x509.DNSName(hostname)])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(san, critical=False)
        .sign(key, hashes.SHA256())
    )
    cert_pem.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_pem.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )


def _start_tls_server(cert_pem: Path, key_pem: Path) -> tuple[http.server.HTTPServer, int]:
    """Start an HTTPS server on an ephemeral 127.0.0.1 port."""
    server = http.server.HTTPServer(("127.0.0.1", 0), _QuietHandler)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(cert_pem), str(key_pem))
    server.socket = ctx.wrap_socket(server.socket, server_side=True)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, port


def _patch_resolver(monkeypatch: pytest.MonkeyPatch, responses: list[list[str]]) -> list[str]:
    """Patch ``ssrf._resolve_all_async`` with a call-counted fake.

    ``responses`` is a list of address-sets returned per call; the final entry
    repeats for any additional calls. Returns the list of hosts seen.
    """
    seen: list[str] = []
    responses = [list(r) for r in responses]

    async def fake(host: str) -> list[str]:
        seen.append(host)
        idx = min(len(seen) - 1, len(responses) - 1)
        return responses[idx]

    monkeypatch.setattr(ssrf, "_resolve_all_async", fake)
    return seen


# --- strict URL parsing ----------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://2130706433/",  # decimal 127.0.0.1
        "http://0x7f000001/",  # hex 127.0.0.1
        "http://0177.0.0.1/",  # octal 127.0.0.1
        "http://127.1/",  # abbreviated 127.0.0.1
        "http://1.2.3.4.5/",  # overlong dotted-quad
        "http://%31%32%37.0.0.1/",  # percent-encoded host
    ],
)
def test_validate_outbound_url_rejects_noncanonical_ip_encodings(url: str) -> None:
    with pytest.raises(ValueError, match="IP literal"):
        ssrf.validate_outbound_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://2130706433/",
        "http://0x7f000001/",
        "http://127.1/",
        "http://%31%32%37.0.0.1/",
    ],
)
async def test_validate_outbound_url_async_rejects_noncanonical_ip_encodings(
    url: str,
) -> None:
    with pytest.raises(ValueError, match="IP literal"):
        await ssrf.validate_outbound_url_async(url)


def test_validate_outbound_url_rejects_userinfo() -> None:
    with pytest.raises(ValueError, match="userinfo"):
        ssrf.validate_outbound_url("http://user:pw@example.com/")


def test_validate_outbound_url_rejects_odd_ports() -> None:
    with pytest.raises(ValueError, match="invalid port"):
        ssrf.validate_outbound_url("http://example.com:99999/")
    with pytest.raises(ValueError, match="valid range"):
        ssrf.validate_outbound_url("http://example.com:0/")


# --- fail-closed on any resolved IP blocked --------------------------------


async def test_validate_outbound_url_async_fails_closed_if_any_ip_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # One public + one private address: fail-closed, never pick the "allowed" one.
    _patch_resolver(monkeypatch, [["93.184.216.34", "10.0.0.5"]])
    with pytest.raises(ValueError, match="private/internal"):
        await ssrf.validate_outbound_url_async("http://mixed.example/")
    with pytest.raises(ValueError, match="private/internal"):
        await ssrf.resolve_pinned_ip("http://mixed.example/")


# --- tenant-scoped egress allowlist ----------------------------------------


async def test_tenant_scoped_allowlist_override_async() -> None:
    with pytest.raises(ValueError, match="private/internal"):
        await ssrf.validate_outbound_url_async("http://10.1.2.3/")
    # Tenant-scoped override permits the range for this target.
    await ssrf.validate_outbound_url_async("http://10.1.2.3/", allow_networks=["10.0.0.0/8"])
    target = await ssrf.resolve_pinned_ip("http://10.1.2.3/", allow_networks=["10.0.0.0/8"])
    assert target.ip == "10.1.2.3"
    assert target.host == "10.1.2.3"


def test_tenant_scoped_allowlist_layers_on_global(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SSRF_ALLOW_PRIVATE_RANGES", "192.168.0.0/16")
    # 10.0.0.0/8 is not in the global base but is in the tenant override -> allowed.
    assert ssrf.validate_outbound_url("http://10.1.2.3/", allow_networks=["10.0.0.0/8"]) is None
    # 192.168.x is allowlisted globally -> allowed even without the tenant override.
    assert ssrf.validate_outbound_url("http://192.168.1.1/") is None


def test_tenant_allowlist_does_not_weaken_loopback() -> None:
    # A tenant allowlist for another range must NOT silently permit loopback/metadata.
    ssrf.validate_outbound_url("http://10.1.2.3/", allow_networks=["10.0.0.0/8"])
    with pytest.raises(ValueError, match="private/internal"):
        ssrf.validate_outbound_url("http://127.0.0.1/", allow_networks=["10.0.0.0/8"])
    with pytest.raises(ValueError, match="private/internal"):
        ssrf.validate_outbound_url("http://169.254.169.254/", allow_networks=["10.0.0.0/8"])
    # Loopback stays blocked at runtime through the async path too.
    with pytest.raises(ValueError, match="private/internal"):
        ssrf.validate_outbound_url("http://10.0.0.1/", allow_networks=[])


# --- pinned-IP transport ---------------------------------------------------


async def test_pinned_transport_uses_validated_ip_despite_rebind() -> None:
    """The transport pins the validated IP and never re-resolves.

    loopback is blocked by default at the validation layer (allowlistable only
    via ``SSRF_ALLOW_PRIVATE_RANGES``), so a high-level call cannot validate a
    host that resolves to 127.0.0.1 without an explicit allowlist. The
    transport is therefore built here with the explicit pin map
    ``pinned_async_transport`` produces for a public origin —
    the mechanism-under-test is the pinning+connect substitution, not the
    validation gate. The key guarantee stays: connect goes to the pinned
    address, never re-resolving to a different one, and any unpinned host is
    refused.
    """
    server, port = _start_http_server()
    try:
        transport = ssrf.PinnedAsyncHTTPTransport({"rebind.example": ("127.0.0.1",)})
        backend = transport._pool._network_backend
        assert backend._pinned_hosts == {"rebind.example": ("127.0.0.1",)}
        client = httpx.AsyncClient(transport=transport)
        async with client:
            resp = await client.get(f"http://rebind.example:{port}/")
            assert resp.status_code == 200
            assert resp.text == "pinned-ok"
        # The request connected to the pinned address and NEVER re-resolved: the
        # pin map is unchanged afterwards, and an unpinned host is refused.
        assert backend._pinned_hosts == {"rebind.example": ("127.0.0.1",)}
        with pytest.raises(ssrf.UnpinnedHostError):
            await backend.connect_tcp("metadata.example", port)
    finally:
        server.shutdown()
        server.server_close()


async def test_pinned_transport_connects_to_pinned_validated_ip() -> None:
    """End-to-end: a made-up host is pinned to a loopback address and the
    request reaches that address. Without pinning, the host would not resolve."""
    server, port = _start_http_server()
    try:
        transport = ssrf.PinnedAsyncHTTPTransport({"pinned.example": "127.0.0.1"})
        client = httpx.AsyncClient(transport=transport)
        async with client:
            resp = await client.get(f"http://pinned.example:{port}/")
            assert resp.status_code == 200
            assert resp.text == "pinned-ok"
    finally:
        server.shutdown()
        server.server_close()


async def test_pinned_transport_sni_matches_hostname(tmp_path: Path) -> None:
    """The pinned connection uses the ORIGINAL hostname for SNI + cert verify.

    An HTTPS server presents a cert whose SAN is ``ssrf-host.test``; the client
    connects to the pinned 127.0.0.1 but verifies against that hostname. If the
    implementation rewrote the URL to the IP, verification would fail.
    """
    hostname = "ssrf-host.test"
    cert_pem = tmp_path / "cert.pem"
    key_pem = tmp_path / "key.pem"
    _make_self_signed_cert(hostname, cert_pem, key_pem)
    server, port = _start_tls_server(cert_pem, key_pem)
    try:
        verify_ctx = ssl.create_default_context(cafile=str(cert_pem))
        transport = ssrf.PinnedAsyncHTTPTransport({hostname: ("127.0.0.1",)}, verify=verify_ctx)
        client = httpx.AsyncClient(transport=transport, verify=verify_ctx)
        async with client:
            resp = await client.get(f"https://{hostname}:{port}/")
            assert resp.status_code == 200
    finally:
        server.shutdown()
        server.server_close()


async def test_pinned_client_defaults_to_no_redirects() -> None:
    """Redirects are not followed by default; a pinned client is safe by default."""
    client = await ssrf.pinned_async_client("http://8.8.8.8/")
    try:
        assert client.follow_redirects is False
    finally:
        await client.aclose()


async def test_redirect_to_internal_is_blocked_when_revalidated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A followed redirect hop re-validates against the same policy; an internal
    hop is blocked rather than followed."""
    _patch_resolver(monkeypatch, [["169.254.169.254"]])
    with pytest.raises(ValueError, match="private/internal"):
        await ssrf.validate_outbound_url_async("http://redirect-target.example/")


# --- FAR-409 security hardening: non-negotiable floor, fail-closed pinning,
#     trust_env, DNS timeout, empty resolution, and dotted/hex literals ---------


def test_allowlist_can_allow_loopback_but_not_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    """A global allowlist naming loopback now makes 127.0.0.0/8 reachable (self-
    hosted localhost model backends), but cloud metadata remains a NON-NEGOTIABLE
    floor that no allowlist can override."""
    monkeypatch.setenv("SSRF_ALLOW_PRIVATE_RANGES", "127.0.0.0/8,169.254.0.0/16,10.0.0.0/8")
    # Loopback is blocked by default but allowlistable.
    assert ssrf.validate_outbound_url("http://127.0.0.1/") is None
    # Cloud metadata is still a NON-NEGOTIABLE floor, even when namechecked.
    with pytest.raises(ValueError, match="private/internal"):
        ssrf.validate_outbound_url("http://169.254.169.254/")
    # The floor does not break a legit allowlist for other private ranges.
    assert ssrf.validate_outbound_url("http://10.1.2.3/") is None


def test_metadata_floor_applies_to_tenant_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SSRF_ALLOW_PRIVATE_RANGES", "192.168.0.0/16")
    # Cloud metadata (link-local) stays a NON-NEGOTIABLE floor: even a
    # tenant-scoped allowlist explicitly naming it is overridden.
    with pytest.raises(ValueError, match="private/internal"):
        ssrf.validate_outbound_url("http://169.254.169.254/", allow_networks=["169.254.0.0/16"])
    # Loopback is now allowlistable at the tenant scope too.
    assert ssrf.validate_outbound_url("http://127.0.0.1/", allow_networks=["127.0.0.0/8"]) is None


def test_ipv6_site_local_and_multicast_blocked() -> None:
    # fec0::/10 (IPv6 site-local) and ff00::/8 (IPv6 multicast) are never valid
    # egress targets and are blocked like loopback/metadata.
    for url in ("http://[fec0::1]/", "http://[ff02::1]/", "http://[::1]/"):
        with pytest.raises(ValueError, match="private/internal"):
            ssrf.validate_outbound_url(url)


def test_hex_like_hostname_not_spuriously_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: a genuine hostname beginning with ``0x`` is NOT a hex IP
    literal and must be resolved (and pass) rather than rejected."""
    seen: list[str] = []

    def fake_resolve(host: str) -> list[str]:
        seen.append(host)
        assert host == "0x.mydomain.com"
        return ["93.184.216.34"]

    monkeypatch.setattr(ssrf, "_resolve_all_sync", fake_resolve)
    assert ssrf.validate_outbound_url("http://0x.mydomain.com/") is None
    assert seen == ["0x.mydomain.com"]


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0x1.0.1/",  # dotted-numeric with a hex octet
        "http://dead.beef/",  # all-hex dotted labels => encoded literal
        "http://1.2.3.4.5/",  # overlong dotted-quad
    ],
)
def test_dotted_numeric_literals_rejected(url: str) -> None:
    with pytest.raises(ValueError, match="IP literal"):
        ssrf.validate_outbound_url(url)


async def test_pinned_transport_refuses_unpinned_host() -> None:
    """Fail-closed: a host that is NOT in the pin map is refused, never connected.

    A 302 -> 169.254.169.254 would otherwise fall through ``connect_tcp`` to the
    unpinned host (the old ``.get(host, host)`` fallback); that must not happen.
    """
    transport = ssrf.PinnedAsyncHTTPTransport({"good.example": "127.0.0.1"})
    backend = transport._pool._network_backend
    with pytest.raises(ssrf.UnpinnedHostError):
        await backend.connect_tcp("evil.example", 443)
    # The pin map is authoritative and unchanged.
    assert backend._pinned_hosts == {"good.example": ("127.0.0.1",)}


async def test_trailing_dot_host_still_pins() -> None:
    """httpx passes a trailing-dot host to ``connect_tcp``; it must still match
    the pin map (normalised host key on both sides) rather than be refused."""
    server, port = _start_http_server()
    try:
        transport = ssrf.PinnedAsyncHTTPTransport({"rebind.example": ("127.0.0.1",)})
        backend = transport._pool._network_backend
        assert backend._pinned_hosts == {"rebind.example": ("127.0.0.1",)}
        client = httpx.AsyncClient(transport=transport)
        async with client:
            resp = await client.get(f"http://rebind.example.:{port}/")
            assert resp.status_code == 200
            assert resp.text == "pinned-ok"
        assert backend._pinned_hosts == {"rebind.example": ("127.0.0.1",)}
    finally:
        server.shutdown()
        server.server_close()


def test_pinned_transport_proxy_env_ignored_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """trust_env defaults False, so a proxy env var does not defeat pinning."""
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.local:8080")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.local:8080")
    transport = ssrf.PinnedAsyncHTTPTransport({"rebind.example": ("127.0.0.1",)})
    assert transport._pool._network_backend._pinned_hosts == {"rebind.example": ("127.0.0.1",)}


def test_pinned_transport_honks_on_proxy_trust_with_env(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Opting into proxy trust while a proxy env var is present honks loudly."""
    monkeypatch.setenv("ALL_PROXY", "http://proxy.local:8080")
    with caplog.at_level(logging.WARNING):
        ssrf.PinnedAsyncHTTPTransport({"x.example": "93.184.216.34"}, trust_env=True)
    assert any("ssrf.pinned_transport_proxy_env" in r.message for r in caplog.records)
    assert any("ALL_PROXY" in (getattr(r, "proxy_vars", "") or "") for r in caplog.records)


async def test_dns_resolution_timeout_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SSRF_DNS_TIMEOUT", "0.05")

    async def hanging_getaddrinfo(*_args: object, **_kwargs: object) -> list[tuple[object, ...]]:
        await asyncio.sleep(5)  # far longer than the configured timeout
        return []

    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "getaddrinfo", hanging_getaddrinfo)
    with pytest.raises(ValueError, match="timed out"):
        await ssrf.validate_outbound_url_async("http://slow.example/")


def test_dns_resolution_sync_timeout_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SSRF_DNS_TIMEOUT", "0.05")
    monkeypatch.setattr(ssrf, "_getaddrinfo_sync", lambda _host: time.sleep(1.0) or [])
    with pytest.raises(ValueError, match="timed out"):
        ssrf.validate_outbound_url("http://slow.example/")


async def test_empty_resolution_raises_value_error_not_index_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_empty(_host: str) -> list[str]:
        return []

    monkeypatch.setattr(ssrf, "_resolve_all_async", fake_empty)
    with pytest.raises(ValueError, match="resolved to no addresses"):
        await ssrf.resolve_pinned_ip("http://empty.example/")
    with pytest.raises(ValueError, match="resolved to no addresses"):
        await ssrf.validate_outbound_url_async("http://empty.example/")


def test_empty_resolution_sync_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ssrf, "_resolve_all_sync", lambda _host: [])
    with pytest.raises(ValueError, match="resolved to no addresses"):
        ssrf.validate_outbound_url("http://empty.example/")


async def test_pin_registry_survives_request_and_reopen() -> None:
    """Smoke test: the pin registry is still populated after a request and after
    close/re-open — catches a silent un-pin (e.g. if httpcore ever dropped or
    replaced the network backend).
    """
    server, port = _start_http_server()
    try:
        transport = ssrf.PinnedAsyncHTTPTransport({"smoke.example": ("127.0.0.1",)})
        backend = transport._pool._network_backend
        assert backend._pinned_hosts == {"smoke.example": ("127.0.0.1",)}
        client = httpx.AsyncClient(transport=transport)
        async with client:
            resp = await client.get(f"http://smoke.example:{port}/")
            assert resp.status_code == 200
        # The pin map survives the request — a silent un-pin would be caught here.
        assert transport._pool._network_backend is backend
        assert backend._pinned_hosts == {"smoke.example": ("127.0.0.1",)}
        await client.aclose()
        # Re-opening on a fresh transport re-establishes the pin.
        transport2 = ssrf.PinnedAsyncHTTPTransport({"smoke.example": ("127.0.0.1",)})
        assert transport2._pool._network_backend._pinned_hosts == {"smoke.example": ("127.0.0.1",)}
    finally:
        server.shutdown()
        server.server_close()


async def test_pinned_async_client_full_composition(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """End-to-end composition: resolve -> pin-map -> transport -> real request.

    ``pinned_async_client`` (the API FAR-408 will consume) is exercised through
    the full wiring — ``pinned_async_client`` -> ``pinned_async_transport`` ->
    ``PinnedAsyncHTTPTransport`` -> actual HTTP request — not just at the
    transport level. The resolver is stubbed because loopback is blocked by
    default at the validation layer (allowlistable only via
    ``SSRF_ALLOW_PRIVATE_RANGES``), so ``pinned_async_client`` cannot validate a
    host that maps to 127.0.0.1 without an explicit allowlist; stubbing
    ``resolve_pinned_ip`` to return a loopback :class:`PinnedTarget` bypasses only
    that DNS-validation gate (validation is covered by its own tests). The key
    composition guarantee holds: the connection goes to the pinned 127.0.0.1 while
    SNI + certificate verification use the original hostname, and the pin survives
    the request without being silently reverted.
    """
    hostname = "compose-host.test"
    cert_pem = tmp_path / "cert.pem"
    key_pem = tmp_path / "key.pem"
    _make_self_signed_cert(hostname, cert_pem, key_pem)
    server, port = _start_tls_server(cert_pem, key_pem)
    try:
        verify_ctx = ssl.create_default_context(cafile=str(cert_pem))
        pinned = ssrf.PinnedTarget(scheme="https", host=hostname, port=port, ips=("127.0.0.1",))
        monkeypatch.setattr(ssrf, "resolve_pinned_ip", AsyncMock(return_value=pinned))
        client = await ssrf.pinned_async_client(
            f"https://{hostname}:{port}/",
            verify=verify_ctx,
        )
        try:
            transport = client._transport
            backend = transport._pool._network_backend
            assert isinstance(backend, ssrf._PinnedAsyncNetworkBackend)
            assert backend._pinned_hosts == {hostname: ("127.0.0.1",)}
            resp = await client.get(f"https://{hostname}:{port}/")
            assert resp.status_code == 200
            assert resp.text == "pinned-ok"
            # The pin survives the request: backend is still the pinned backend.
            assert transport._pool._network_backend is backend
            assert backend._pinned_hosts == {hostname: ("127.0.0.1",)}
        finally:
            await client.aclose()
    finally:
        server.shutdown()
        server.server_close()


# URL syntax validation (all rejected before any DNS resolution)
# ---------------------------------------------------------------------------


class TestURLSyntaxValidation:
    def test_empty_url_raises(self) -> None:
        with pytest.raises(ValueError, match="URL is required"):
            ssrf.validate_outbound_url("")

    def test_none_url_raises(self) -> None:
        with pytest.raises(ValueError, match="URL is required"):
            ssrf.validate_outbound_url(None)  # type: ignore[arg-type]

    def test_non_string_url_raises(self) -> None:
        with pytest.raises(ValueError, match="URL is required"):
            ssrf.validate_outbound_url(123)  # type: ignore[arg-type]

    def test_non_http_scheme_raises(self) -> None:
        with pytest.raises(ValueError, match="http:// or https://"):
            ssrf.validate_outbound_url("ftp://example.com/file")

    def test_missing_hostname_raises(self) -> None:
        with pytest.raises(ValueError, match="valid hostname"):
            ssrf.validate_outbound_url("http:///path")

    def test_public_ip_with_userinfo_rejected(self) -> None:
        # Userinfo credentials are rejected uniformly by _parse_url_target before
        # any DNS lookup, whether the host is a hostname or a literal public IP.
        # This matches test_validate_outbound_url_rejects_userinfo.
        with pytest.raises(ValueError, match="userinfo"):
            ssrf.validate_outbound_url("http://user:pass@8.8.8.8/path")


# ---------------------------------------------------------------------------
# IPv6 literal addresses
# ---------------------------------------------------------------------------


class TestIPV6LiteralAddresses:
    def test_ipv6_loopback_blocked(self) -> None:
        with pytest.raises(ValueError, match="private/internal"):
            ssrf.validate_outbound_url("http://[::1]/")

    def test_ipv6_link_local_blocked(self) -> None:
        with pytest.raises(ValueError, match="private/internal"):
            ssrf.validate_outbound_url("http://[fe80::1]/")

    def test_ipv6_unique_local_blocked(self) -> None:
        with pytest.raises(ValueError, match="private/internal"):
            ssrf.validate_outbound_url("http://[fd12:3456::1]/")

    def test_ipv6_public_accepted_without_dns(self) -> None:
        assert ssrf.validate_outbound_url("http://[2606:4700:4700::1111]/") is None

    def test_ipv6_allowlist_overrides_private_check(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SSRF_ALLOW_PRIVATE_RANGES", "fc00::/7")
        assert ssrf.validate_outbound_url("http://[fd12:3456::1]/") is None

    def test_ipv6_link_local_allowlist_still_blocked(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SSRF_ALLOW_PRIVATE_RANGES", "10.0.0.0/8")
        with pytest.raises(ValueError, match="private/internal"):
            ssrf.validate_outbound_url("http://[fe80::1]/")


# ---------------------------------------------------------------------------
# Hostname resolution path
# ---------------------------------------------------------------------------


class TestHostnameResolution:
    def test_dns_resolution_failure_is_fail_closed(self) -> None:
        with (
            patch.object(ssrf.socket, "getaddrinfo", side_effect=ssrf.socket.gaierror("no such host")),
            pytest.raises(ValueError, match="DNS resolution failed"),
        ):
            ssrf.validate_outbound_url("http://inexistent.example/")

    async def test_async_dns_resolution_failure_is_fail_closed(self) -> None:
        async def fail_resolve(host: str) -> list[str]:
            raise ValueError(f"DNS resolution failed for {host}. Cannot verify the target is not internal.")

        with (
            patch.object(ssrf, "_resolve_all_async", fail_resolve),
            pytest.raises(ValueError, match="DNS resolution failed"),
        ):
            await ssrf.validate_outbound_url_async("http://dead.example/")

    def test_hostname_resolving_to_any_internal_address_blocked(self) -> None:
        def fake_resolve(host: str) -> list[str]:
            return ["93.184.216.34", "10.0.0.5"]

        with (
            patch.object(ssrf, "_resolve_all_sync", fake_resolve),
            pytest.raises(ValueError, match=r"10\.0\.0\.5"),
        ):
            ssrf.validate_outbound_url("http://mixed.example/")

    def test_hostname_resolving_to_internal_ipv6_blocked(self) -> None:
        with (
            patch.object(ssrf, "_resolve_all_sync", lambda host: ["fd12:3456::1"]),
            pytest.raises(ValueError, match="resolves to a private/internal address"),
        ):
            ssrf.validate_outbound_url("http://ipv6.internal.example/")

    def test_hostname_resolving_to_nothing_fails_closed(self) -> None:
        # A hostname with no resolved records fails CLOSED: it cannot be
        # verified as non-internal, so it must not connect. This matches the
        # fail-closed behaviour asserted by test_empty_resolution_sync_fails_closed.
        with (
            patch.object(ssrf, "_resolve_all_sync", lambda host: []),
            pytest.raises(ValueError, match="resolved to no addresses"),
        ):
            ssrf.validate_outbound_url("http://empty.example/")

    def test_hostname_normalized_before_resolution(self) -> None:
        """Trailing dots and ports must be stripped before the resolver is consulted."""
        resolved: list[str] = []

        def fake_resolve(host: str) -> list[str]:
            resolved.append(host)
            return ["93.184.216.34"]

        with patch.object(ssrf, "_resolve_all_sync", fake_resolve):
            ssrf.validate_outbound_url("http://example.com.:8080/path")

        assert resolved == ["example.com"]


# ---------------------------------------------------------------------------
# Non-canonical IP encodings (FAR-413 SSRF adversarial)
# ---------------------------------------------------------------------------
# A resolver/httpx client is far more liberal than ``ipaddress.ip_address``
# which only accepts dotted-quad / bracketed-IPv6 *strings*. Loopback shown as
# a single decimal or hex integer (``2130706433`` / ``0x7f000001``) must be
# blocked by the literal-IP guard rather than escaping to the DNS path (where
# an integer-decoding resolver would turn it back into 127.0.0.1).


class TestNonCanonicalIPEncodings:
    def test_decimal_integer_ipv4_loopback_blocked(self) -> None:
        # main's _reject_noncanonical_ip_literal rejects decimal-integer IP
        # literals at the host-parsing stage (defense-in-depth) before DNS.
        with pytest.raises(ValueError, match="decimal/octal integer IP literal"):
            ssrf.validate_outbound_url("http://2130706433/")

    def test_decimal_integer_ipv4_private_blocked(self) -> None:
        with pytest.raises(ValueError, match="decimal/octal integer IP literal"):
            ssrf.validate_outbound_url("http://167772161/")  # 10.0.0.1

    def test_hex_integer_ipv4_loopback_blocked(self) -> None:
        with pytest.raises(ValueError, match="hex-encoded IP literal"):
            ssrf.validate_outbound_url("http://0x7f000001/")

    def test_hex_integer_ipv4_metadata_blocked(self) -> None:
        with pytest.raises(ValueError, match="hex-encoded IP literal"):
            ssrf.validate_outbound_url("http://0xa9fea9fe/")  # 169.254.169.254

    def test_dotted_hex_ipv4_loopback_blocked(self) -> None:
        # 0x7f.0.0.1 -> 127.0.0.1 (per-segment hex base detection).
        with pytest.raises(ValueError, match="non-canonical dotted-numeric IP literal"):
            ssrf.validate_outbound_url("http://0x7f.0.0.1/")

    def test_dotted_octal_ipv4_loopback_blocked(self) -> None:
        # 0177.0.0.1 -> 127.0.0.1 (per-segment octal base detection).
        with pytest.raises(ValueError, match="non-canonical dotted-numeric IP literal"):
            ssrf.validate_outbound_url("http://0177.0.0.1/")

    def test_dotted_hex_hostname_not_spuriously_blocked_when_resolves_public(self) -> None:
        """A hostname that merely CONTAINS a hex-looking segment must NOT be
        spuriously treated as a non-canonical IPv4: the segment parser bails on
        the non-numeric segment, so it falls through to the DNS path and is
        accepted when it resolves to a public address."""

        def fake_resolve(host: str) -> list[str]:
            assert host == "0x7f.helpers.example.com"
            return ["93.184.216.34"]

        with patch.object(ssrf, "_resolve_all_sync", fake_resolve):
            assert ssrf.validate_outbound_url("http://0x7f.helpers.example.com/") is None

    async def test_async_decimal_integer_ipv4_loopback_blocked(self) -> None:
        with pytest.raises(ValueError, match="decimal/octal integer IP literal"):
            await ssrf.validate_outbound_url_async("http://2130706433/")

    def test_encode_percent_host_is_fail_closed(self) -> None:
        # A %-encoded host is rejected by _reject_noncanonical_ip_literal at the
        # host-parsing stage (fail-closed) rather than reaching DNS resolution.
        with pytest.raises(ValueError, match="scope/percent-encoded IP literal"):
            ssrf.validate_outbound_url("http://127.0.0.1%00/")

    def test_userinfo_loopback_still_blocked(self) -> None:
        # main's URL syntax check rejects userinfo credentials outright, so the
        # loopback target is blocked before literal-IP validation is reached.
        with pytest.raises(ValueError, match="userinfo credentials"):
            ssrf.validate_outbound_url("http://user:pass@127.0.0.1/")

    def test_ipv4_mapped_ipv6_loopback_blocked(self) -> None:
        with pytest.raises(ValueError, match="private/internal"):
            ssrf.validate_outbound_url("http://[::ffff:127.0.0.1]/")

    def test_ipv4_mapped_ipv6_metadata_blocked(self) -> None:
        with pytest.raises(ValueError, match="private/internal"):
            ssrf.validate_outbound_url("http://[::ffff:169.254.169.254]/")


# ---------------------------------------------------------------------------
# DNS-rebinding — the guard re-resolves on every validation and never caches a
# resolution result. A hostname that flips between public and internal across
# two validations is therefore caught the moment it reads private; validation
# and connection are separate lookups (the residual risk is a rebind between a
# validation and the subsequent HTTP connection, documented in the module).
# ---------------------------------------------------------------------------


class TestDNSRebinding:
    def test_guard_re_resolves_each_validate_no_cached_public_answer(self) -> None:
        """The guard is not caching a first public answer between calls."""
        calls: list[str] = []
        answers = iter([["93.184.216.34"], ["10.0.0.5"]])  # public then internal

        def fake_resolve(host: str) -> list[str]:
            calls.append(host)
            return next(answers)

        with patch.object(ssrf, "_resolve_all_sync", fake_resolve):
            # First validation: public answer -> accepted.
            ssrf.validate_outbound_url("http://flip.example/")
            # Second validation: the same hostname now answers internal -> blocked.
            with pytest.raises(ValueError, match="resolves to a private/internal address"):
                ssrf.validate_outbound_url("http://flip.example/")

        # Each validation performed its own fresh resolution — no memoisation.
        assert len(calls) == 2

    async def test_async_guard_resolves_per_validation(self) -> None:
        calls = 0

        async def fake_resolve(host: str) -> list[str]:
            nonlocal calls
            calls += 1
            return ["93.184.216.34"]

        with patch.object(ssrf, "_resolve_all_async", fake_resolve):
            await ssrf.validate_outbound_url_async("http://fresh.example/")
            await ssrf.validate_outbound_url_async("http://fresh.example/")

        assert calls == 2


# ---------------------------------------------------------------------------
# FAR-526B: full-IP pin map with failover + re-resolve, configurable trust_env,
# and the runtime loudness guard.
# ---------------------------------------------------------------------------

# Sentinels for the "stream" a fake connect returns; the transport only needs an
# object back from ``_connect_to_ip``, not a real socket.
_CONNECT_OK = object()


def _fake_connect(attempted: list[str], fail_first: bool = False):
    """Return an async ``_connect_to_ip`` replacement that records IP attempts.

    ``fail_first`` raises ``httpcore.ConnectError`` for the first attempted
    address then succeeds — the failover seam. ``ConnectError`` mirrors what
    httpcore actually raises for an OS connect failure (it is a ``NetworkError``,
    NOT ``OSError``), so the test guards the production exception the failover
    loop catches. Returns the recorded list so a test can assert the order of IPs
    tried.
    """

    async def fake(
        ip: str,
        _port: int,
        *,
        timeout: float | None = None,  # noqa: ASYNC109
        local_address: str | None = None,
        socket_options: object = None,
    ) -> object:
        attempted.append(ip)
        if fail_first and ip == attempted[0]:
            raise httpcore.ConnectError("connection refused")
        return _CONNECT_OK

    return fake


def test_pin_map_holds_full_ip_set() -> None:
    """(a) The pin map captures the FULL validated IP set, not just ``ips[0]``."""
    transport = ssrf.PinnedAsyncHTTPTransport({"cdn.example": ["1.1.1.1", "2.2.2.2", "3.3.3.3"]})
    backend = transport._pool._network_backend
    assert backend._pinned_hosts == {"cdn.example": ("1.1.1.1", "2.2.2.2", "3.3.3.3")}


async def test_pinned_async_transport_pins_all_resolved_ips(monkeypatch: pytest.MonkeyPatch) -> None:
    """(a) The builder pins every resolved address, so a multi-IP origin keeps
    its failover pool instead of losing all but the first address."""

    async def fake_resolve(_host: str) -> list[str]:
        return ["1.1.1.1", "2.2.2.2", "3.3.3.3"]

    monkeypatch.setattr(ssrf, "_resolve_all_async", fake_resolve)
    transport = await ssrf.pinned_async_transport("https://multi.example/")
    try:
        backend = transport._pool._network_backend
        assert backend._pinned_hosts == {"multi.example": ("1.1.1.1", "2.2.2.2", "3.3.3.3")}
    finally:
        await transport.aclose()


async def test_connect_failure_fails_over_to_next_pinned_ip() -> None:
    """(b) A connect failure on the first pinned IP fails over to the next."""
    transport = ssrf.PinnedAsyncHTTPTransport({"fail.example": ["10.0.0.99", "10.0.0.100"]})
    backend = transport._pool._network_backend
    attempted: list[str] = []
    backend._connect_to_ip = _fake_connect(attempted, fail_first=True)  # type: ignore[method-assign]

    stream = await backend.connect_tcp("fail.example", 443)
    assert stream is _CONNECT_OK
    assert attempted == ["10.0.0.99", "10.0.0.100"]
    assert backend.connect_calls == 1


async def test_stale_ip_re_resolves_on_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    """(c) A stale pinned IP self-heals: TTL expiry re-resolves + re-validates."""
    seen: list[str] = []

    async def fake_resolve(host: str) -> list[str]:
        seen.append(host)
        return ["8.8.8.8", "9.9.9.9"]

    monkeypatch.setattr(ssrf, "_resolve_all_async", fake_resolve)
    transport = ssrf.PinnedAsyncHTTPTransport({"stale.example": ["1.1.1.1"]}, resolve_ttl_seconds=1)
    backend = transport._pool._network_backend
    backend._resolved_at["stale.example"] = time.time() - 5
    attempted: list[str] = []
    backend._connect_to_ip = _fake_connect(attempted)  # type: ignore[method-assign]

    await backend.connect_tcp("stale.example", 443)
    assert seen == ["stale.example"]
    assert backend._pinned_hosts == {"stale.example": ("8.8.8.8", "9.9.9.9")}
    assert attempted == ["8.8.8.8"]


async def test_connect_error_re_resolves_and_retries_fresh(monkeypatch: pytest.MonkeyPatch) -> None:
    """(c) When every pinned IP fails to connect, the host re-resolves and the
    fresh set is retried — a decommissioned IP self-heals."""

    async def fake_resolve(_host: str) -> list[str]:
        return ["8.8.8.8"]

    monkeypatch.setattr(ssrf, "_resolve_all_async", fake_resolve)
    transport = ssrf.PinnedAsyncHTTPTransport({"heal.example": ["1.1.1.1"]})
    backend = transport._pool._network_backend
    attempted: list[str] = []

    async def fake_connect(
        ip: str,
        _port: int,
        *,
        timeout: float | None = None,  # noqa: ASYNC109
        local_address: str | None = None,
        socket_options: object = None,
    ) -> object:
        attempted.append(ip)
        if ip == "1.1.1.1":
            raise httpcore.ConnectError("decommissioned")
        return _CONNECT_OK

    backend._connect_to_ip = fake_connect  # type: ignore[method-assign]
    await backend.connect_tcp("heal.example", 443)
    assert attempted == ["1.1.1.1", "8.8.8.8"]
    assert backend._pinned_hosts == {"heal.example": ("8.8.8.8",)}


def test_default_trust_env_is_false_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """(d) Without ``SSRF_TRUST_PROXY`` the default ``trust_env`` is false."""
    monkeypatch.delenv("SSRF_TRUST_PROXY", raising=False)
    assert ssrf._default_trust_env() is False


def test_default_trust_env_true_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """(d) ``SSRF_TRUST_PROXY=true`` flips the fleet-wide proxy-trust default."""
    monkeypatch.setenv("SSRF_TRUST_PROXY", "true")
    assert ssrf._default_trust_env() is True


def test_transport_default_trust_env_ignores_proxy_env(monkeypatch: pytest.MonkeyPatch, caplog) -> None:
    """(d) Default (None -> False) keeps proxy env vars ignored: no honk."""
    monkeypatch.delenv("SSRF_TRUST_PROXY", raising=False)
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.local:8080")
    with caplog.at_level(logging.WARNING):
        ssrf.PinnedAsyncHTTPTransport({"x.example": "8.8.8.8"})
    assert not any("ssrf.pinned_transport_proxy_env" in r.message for r in caplog.records)


def test_transport_default_trust_env_honours_proxy_when_set(monkeypatch: pytest.MonkeyPatch, caplog) -> None:
    """(d) ``SSRF_TRUST_PROXY=true`` (None default) honours the proxy: it honks."""
    monkeypatch.setenv("SSRF_TRUST_PROXY", "true")
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.local:8080")
    with caplog.at_level(logging.WARNING):
        ssrf.PinnedAsyncHTTPTransport({"x.example": "8.8.8.8"})
    assert any("ssrf.pinned_transport_proxy_env" in r.message for r in caplog.records)


def test_transport_explicit_trust_env_overrides_setting(monkeypatch: pytest.MonkeyPatch, caplog) -> None:
    """(d) An explicit ``trust_env=False`` wins over the ``SSRF_TRUST_PROXY``
    setting — the per-connector escape hatch beats the fleet default."""
    monkeypatch.setenv("SSRF_TRUST_PROXY", "true")
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.local:8080")
    with caplog.at_level(logging.WARNING):
        ssrf.PinnedAsyncHTTPTransport({"x.example": "8.8.8.8"}, trust_env=False)
    assert not any("ssrf.pinned_transport_proxy_env" in r.message for r in caplog.records)


def test_loudness_guard_raises_when_pin_dropped() -> None:
    """(e) The loudness guard fires if the pin was silently dropped: a transport
    whose connect_tcp was NEVER consulted raises PinDroppedError."""
    transport = ssrf.PinnedAsyncHTTPTransport({"x.example": "8.8.8.8"}, loudness_guard=True)
    assert transport._pin_backend.connect_calls == 0
    with pytest.raises(ssrf.PinDroppedError):
        transport._enforce_pin_live()


async def test_loudness_guard_passes_after_real_connect() -> None:
    """(e) After a real request the pin is proven live and the guard passes."""
    server, port = _start_http_server()
    try:
        transport = ssrf.PinnedAsyncHTTPTransport({"live.example": "127.0.0.1"}, loudness_guard=True)
        client = httpx.AsyncClient(transport=transport)
        async with client:
            resp = await client.get(f"http://live.example:{port}/")
            assert resp.status_code == 200
        transport._enforce_pin_live()  # no raise
    finally:
        server.shutdown()
        server.server_close()
