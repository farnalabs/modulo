"""Shared SSRF-safe URL validation for outbound requests.

Blocks private/loopback/link-local/cloud-metadata/CGNAT ranges via DNS
resolution. Used by notification endpoints, SSO test connections,
observability test, and error-forwarder test paths.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import socket
from urllib.parse import urlparse

_log = logging.getLogger(__name__)

# Extra ranges not covered by ipaddress.is_private (cloud metadata, CGNAT).
_EXCLUDED_NETWORKS = [
    ipaddress.ip_network("169.254.0.0/16"),  # AWS/GCP/Azure link-local metadata
    ipaddress.ip_network("100.64.0.0/10"),  # CGNAT
    ipaddress.ip_network("198.18.0.0/15"),  # benchmarking
    ipaddress.ip_network("0.0.0.0/8"),  # current network
    ipaddress.ip_network("100.100.100.200/32"),  # Aliyun metadata
]

# Configurable allowlist for self-hosted deployments on private networks.
# Comma-separated CIDR list in SSRF_ALLOW_PRIVATE_RANGES env var.
_allow_private_networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []


def _check_allowlist() -> None:
    raw = os.environ.get("SSRF_ALLOW_PRIVATE_RANGES", "")
    if not raw:
        return
    for cidr in raw.split(","):
        cidr = cidr.strip()
        if cidr:
            try:
                _allow_private_networks.append(ipaddress.ip_network(cidr, strict=False))
            except ValueError:
                _log.warning("ssrf.invalid_allowlist_entry", extra={"cidr": cidr})


_check_allowlist()


def _is_blocked_ip(ip_str: str) -> bool:
    """Check if an IP address should be blocked."""
    try:
        addr = ipaddress.ip_address(ip_str)
    except ValueError:
        return True  # fail-closed on unparseable

    # Check configurable allowlist first
    for net in _allow_private_networks:
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


def validate_outbound_url(url: str) -> None:
    """Validate that a URL does not point to an internal/private destination.

    Performs synchronous DNS resolution. For use in sync contexts (Pydantic
    validators, route handlers). Raises ValueError if the URL is unsafe.

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

    # Check if hostname is a raw IP
    try:
        ip = ipaddress.ip_address(decoded)
        if _is_blocked_ip(str(ip)):
            raise ValueError(
                f"URL targets a private/internal network address: {decoded}. "
                "Use a public URL or add the address to SSRF_ALLOW_PRIVATE_RANGES."
            )
        return  # valid public IP, no DNS needed
    except ValueError:
        if "private/internal" in str(decoded):
            raise

    # For hostnames, resolve and check ALL A/AAAA records synchronously
    try:
        addrinfos = socket.getaddrinfo(decoded, 0, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except (OSError, socket.gaierror):
        # Fail-closed on DNS resolution failure
        raise ValueError(f"DNS resolution failed for {decoded}. Cannot verify the target is not internal.") from None

    for _family, _type, _proto, _canonname, sockaddr in addrinfos:
        ip_str = sockaddr[0]
        assert isinstance(ip_str, str)
        if _is_blocked_ip(ip_str):
            raise ValueError(
                f"URL hostname {decoded} resolves to a private/internal address ({ip_str}). Use a public URL."
            )
