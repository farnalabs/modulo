"""Shared SSRF-safe URL validation for outbound requests.

Blocks private/loopback/link-local/cloud-metadata/CGNAT ranges via DNS
resolution. Used by notification endpoints, SSO test connections,
observability test, and error-forwarder test paths.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import socket
from urllib.parse import urlparse

_log = logging.getLogger(__name__)

# Extra ranges not covered by ipaddress.is_private (cloud metadata, CGNAT).
# NOSONAR S1313: these are documented reserved/private network blocks that must
# be BLOCKED for outbound requests by this SSRF guard — they are destination
# filters, never connection endpoints, so hardcoding them is required and safe.
_EXCLUDED_NETWORKS = [
    ipaddress.ip_network("169.254.0.0/16"),  # NOSONAR - AWS/GCP/Azure link-local metadata
    ipaddress.ip_network("100.64.0.0/10"),  # NOSONAR - CGNAT
    ipaddress.ip_network("198.18.0.0/15"),  # NOSONAR - benchmarking
    ipaddress.ip_network("0.0.0.0/8"),  # NOSONAR - current network
    ipaddress.ip_network("100.100.100.200/32"),  # NOSONAR - Aliyun metadata
]

# Configurable allowlist for self-hosted deployments on private networks.
# Comma-separated CIDR list in SSRF_ALLOW_PRIVATE_RANGES env var. Parsed
# lazily and cached keyed on the raw env value: a stable value parses once for
# the process lifetime, while a mid-process change to the env var is honoured
# immediately (no import-time side effect, no mutable module global). Returns
# an immutable tuple so readers never race a mutation.
_allowlist_cache_key: str | None = None
_allowlist_parsed: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = ()


def _get_allowlist() -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    global _allowlist_cache_key, _allowlist_parsed
    raw = os.environ.get("SSRF_ALLOW_PRIVATE_RANGES", "")
    if raw == _allowlist_cache_key:
        return _allowlist_parsed
    parsed: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for cidr in raw.split(","):
        cidr = cidr.strip()
        if cidr:
            try:
                parsed.append(ipaddress.ip_network(cidr, strict=False))
            except ValueError:
                _log.warning("ssrf.invalid_allowlist_entry", extra={"cidr": cidr})
    _allowlist_cache_key = raw
    _allowlist_parsed = tuple(parsed)
    return _allowlist_parsed


def _is_blocked_ip(ip_str: str) -> bool:
    """Check if an IP address should be blocked."""
    try:
        addr = ipaddress.ip_address(ip_str)
    except ValueError:
        return True  # fail-closed on unparseable

    # Check configurable allowlist first
    for net in _get_allowlist():
        if addr in net:
            return False

    # Standard private/loopback/link-local
    if addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved or addr.is_unspecified:
        return True

    # Extra networks not in is_private
    return any(addr in net for net in _EXCLUDED_NETWORKS)


def _validate_url_syntax(url: str) -> str:
    """Validate URL syntax and extract hostname. Raises ValueError on failure."""
    if not url or not isinstance(url, str):
        raise ValueError("URL is required")

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("URL must use http:// or https:// scheme")

    hostname = parsed.hostname
    if not hostname:
        raise ValueError("URL must have a valid hostname")

    return hostname.rstrip(".").strip("[]")


def _validate_literal_ip(decoded: str) -> bool:
    """Block private/internal literal IPs; return True when ``decoded`` is an IP.

    Returns False when ``decoded`` is a hostname (not a literal IP), signalling
    that the caller must DNS-resolve it. Raises ValueError for blocked literal
    IPs (fail-closed).
    """
    try:
        ip = ipaddress.ip_address(decoded)
    except ValueError:
        return False
    if _is_blocked_ip(str(ip)):
        raise ValueError(
            f"URL targets a private/internal network address: {decoded}. "
            "Use a public URL or add the address to SSRF_ALLOW_PRIVATE_RANGES."
        )
    return True


def _check_resolved(decoded: str, ip_strings: list[str]) -> None:
    """Raise if any resolved address of a hostname is blocked."""
    for ip_str in ip_strings:
        if _is_blocked_ip(ip_str):
            raise ValueError(
                f"URL hostname {decoded} resolves to a private/internal address ({ip_str}). Use a public URL."
            )


def _resolve_all_sync(host: str) -> list[str]:
    try:
        addrinfos = socket.getaddrinfo(host, 0, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except OSError:
        # Fail-closed on DNS resolution failure (socket.gaierror is an OSError)
        raise ValueError(f"DNS resolution failed for {host}. Cannot verify the target is not internal.") from None
    result: list[str] = []
    for _fam, _typ, _proto, _canon, sockaddr in addrinfos:
        ip_str = sockaddr[0]
        if isinstance(ip_str, str):  # O-safe: assert would vanish under python -O
            result.append(ip_str)
    return result


async def _resolve_all_async(host: str) -> list[str]:
    """Async DNS resolution that does not block the event loop."""
    loop = asyncio.get_running_loop()
    try:
        addrinfos = await loop.getaddrinfo(host, 0, family=socket.AF_UNSPEC, type=socket.SOCK_STREAM)
    except OSError:
        raise ValueError(f"DNS resolution failed for {host}. Cannot verify the target is not internal.") from None
    result: list[str] = []
    for _fam, _typ, _proto, _canon, sockaddr in addrinfos:
        ip_str = sockaddr[0]
        if isinstance(ip_str, str):  # O-safe: assert would vanish under python -O
            result.append(ip_str)
    return result


def validate_outbound_url(url: str) -> None:
    """Validate that a URL does not point to an internal/private destination.

    Performs synchronous DNS resolution. For use in sync contexts (Pydantic
    validators, synchronous call sites). Raises ValueError if the URL is unsafe.

    For async callers use :func:`validate_outbound_url_async` so DNS resolution
    does not block the event loop.

    NOTE — Accepted residual risk (DNS rebinding): this function resolves the
    hostname, verifies all resolved addresses are non-internal, then returns.
    It does NOT pin the validated address onto the subsequent outbound
    connection. A hostname under DNS control can therefore resolve to a public
    address during validation and to an internal/metadata address during the
    actual request that each call site performs on its own, bypassing this
    check. The first line of defense is that the surrounding call sites are
    permission-gated (admin/operator tier controls the URL) and the primary
    documented mitigation is the ``SSRF_ALLOW_PRIVATE_RANGES`` allowlist, which
    admins on private networks are expected to lock down to only their trusted
    ranges. Fully closing the rebinding window requires pinning the connection
    to the resolved address (e.g. an httpx transport that forces the validated
    IP and requires SNI to match the hostname) — tracked separately from this
    hardening PR.
    """
    decoded = _validate_url_syntax(url)
    if _validate_literal_ip(decoded):
        return  # literal IP handled above
    _check_resolved(decoded, _resolve_all_sync(decoded))


async def validate_outbound_url_async(url: str) -> None:
    """Async variant of :func:`validate_outbound_url` for event-loop-hostile callers.

    Resolves the hostname with ``asyncio.get_running_loop().getaddrinfo`` so the
    DNS lookup does not block the event loop. Raises ValueError if the URL is
    unsafe.
    """
    decoded = _validate_url_syntax(url)
    if _validate_literal_ip(decoded):
        return  # literal IP handled above
    _check_resolved(decoded, await _resolve_all_async(decoded))
