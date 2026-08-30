"""Shared SSRF-safe URL validation for outbound requests.

Blocks private/link-local/cloud-metadata/CGNAT ranges via DNS resolution.
Loopback is blocked by default but allowlistable: a self-hosted deployment can
opt into localhost model backends and localhost-default connectors (Trivy,
SonarQube, TeamCity, 1Password, Jenkins, n8n, Grafana) by allowlisting the
loopback ranges. Used by notification endpoints, SSO test connections,
observability test, error-forwarder test paths, every ``base_url``-bearing
connector, and the OpenAI-compatible model backends.

Operating ``SSRF_ALLOW_PRIVATE_RANGES``
--------------------------------------
* **Dual-stack loopback needs BOTH entries.** ``localhost`` usually resolves to
  ``127.0.0.1`` *and* ``::1``, and validation fails closed if ANY resolved
  address is blocked. ``SSRF_ALLOW_PRIVATE_RANGES=127.0.0.0/8`` alone therefore
  leaves ``http://localhost:11434`` unreachable — set
  ``SSRF_ALLOW_PRIVATE_RANGES=127.0.0.0/8,::1/128``, or point the URL at the
  literal ``http://127.0.0.1:11434`` (a literal IP skips DNS entirely).
* **The env allowlist is cluster-wide, not per-call-site.** It is read by every
  validation call site, so allowlisting loopback for a localhost model backend
  ALSO lets a tenant-supplied connector ``base_url`` reach loopback on the same
  cluster. Where a narrower grant is wanted, pass ``allow_networks`` (a
  tenant-scoped overlay layered on the global floor) instead of widening the env
  var. Link-local, multicast and cloud-metadata ranges are a non-negotiable
  floor that no allowlist can open.

Accepted residual risks
-----------------------
* **Validate-then-connect (DNS rebinding).** Connectors and model backends call
  :func:`validate_outbound_url` and then connect with their own client, so a
  hostname under attacker DNS control can answer public at validation time and
  internal at connect time. This matches the pre-existing precedent for those
  call sites; the pinned transport below is the strictly safer option and should
  be preferred by new call sites that own their client construction.
* **No resolution cache.** Every validation performs a fresh DNS lookup (bounded
  by ``SSRF_DNS_TIMEOUT``), which costs one resolver round trip per client
  construction on connector-heavy paths. This is deliberate: caching resolutions
  would widen the rebinding window above, and the resolver's own OS-level cache
  already absorbs the repeat cost.

Beyond *validating* a URL, this module owns the **pinned-IP connection
transport**: an ``httpx`` transport that resolves and validates a target once,
then forces the TCP connection to the validated address while keeping the
**original** hostname for TLS SNI and certificate verification. This closes the
DNS-rebinding window that a validate-then-connect pattern leaves open (see the
note on :func:`validate_outbound_url`).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import ipaddress
import logging
import os
import socket
import ssl
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from urllib.parse import urlparse

import httpcore
import httpx
from httpcore._backends.base import SOCKET_OPTION

_log = logging.getLogger(__name__)

Network = ipaddress.IPv4Network | ipaddress.IPv6Network

# SSRF guard: blocks outbound requests to private/reserved/link-local/cloud-
# metadata ranges. The literals below are DESTINATION filters that must be
# blocked — never connection endpoints — so hardcoding them is required and
# safe (S1313 specifically exempts such documented network blocks). NOSONAR
# marks each ``ip_network`` literal which is not a client-configurable
# connection address.
_EXCLUDED_NETWORKS = [
    # 169.254.0.0/16 is the IPv4 link-local block (AWS EC2 IMDS, GCP, Azure
    # instance metadata) — the classic SSRF pivot target. NOSONAR.
    ipaddress.ip_network("169.254.0.0/16"),  # NOSONAR - AWS/GCP/Azure link-local metadata
    # 100.64.0.0/10 is the CGNAT shared-address space (RFC 6598) — reachable
    # only within a private network but never a safe public egress target.
    ipaddress.ip_network("100.64.0.0/10"),  # NOSONAR - CGNAT
    # 198.18.0.0/15 is the benchmarking/reserved range (RFC 2544/6815).
    ipaddress.ip_network("198.18.0.0/15"),  # NOSONAR - benchmarking
    ipaddress.ip_network("0.0.0.0/8"),  # NOSONAR - current network
    # 100.100.100.200/32 is the Alibaba Cloud (Aliyun) metadata endpoint.
    ipaddress.ip_network("100.100.100.200/32"),  # NOSONAR - Aliyun metadata
]

# IPv6 ranges that are never valid HTTP egress targets and must never be made
# reachable by an allowlist: site-local (deprecated but still routed in some
# stacks) and multicast. These sit alongside link-local / the metadata ranges
# above as a NON-NEGOTIABLE blocked floor.
_NON_NEGOTIABLE_BLOCKED = [
    ipaddress.ip_network("fec0::/10"),  # IPv6 site-local
    ipaddress.ip_network("ff00::/8"),  # IPv6 multicast
]

# Proxy environment variables that, when trusted, would route the pinned
# transport through a proxy and defeat pinning (the proxy re-resolves the
# host, so the pin map sees only the proxy host and the destination becomes
# unvalidated). The pinned transport is safe-by-default with trust_env=False;
# these are consulted only to honk loudly when a caller explicitly opts into
# proxy trust at the same time a proxy env var is present.
_PROXY_ENV_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")

# DNS resolution timeout (seconds). A hung resolver must fail closed, never
# stall the process. Configurable via SSRF_DNS_TIMEOUT.
_DNS_TIMEOUT_ENV = "SSRF_DNS_TIMEOUT"
_DNS_TIMEOUT_DEFAULT = 10.0

# Bounded thread pool for the synchronous DNS path so socket.getaddrinfo runs
# off the caller thread without unbounded thread creation. Worker threads are
# created only on first use; the pool is tiny and never grows past max_workers.
_RESOLVER_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=4)


class UnpinnedHostError(RuntimeError):
    """Raised when the pinned transport is asked to connect to a host that was
    never validated/pinned.

    The pinned transport ONLY ever connects to addresses it validated up front.
    Any other destination (for example a redirect hop) is refused — fail-closed,
    never connect to an unvalidated host.
    """


# Configurable allowlist for self-hosted deployments on private networks.
# Comma-separated CIDR list in SSRF_ALLOW_PRIVATE_RANGES env var. Parsed
# lazily and cached keyed on the raw env value: a stable value parses once for
# the process lifetime, while a mid-process change to the env var is honoured
# immediately (no import-time side effect, no mutable module global). Returns
# an immutable tuple so readers never race a mutation.
_allowlist_cache_key: str | None = None
_allowlist_parsed: tuple[Network, ...] = ()


@dataclass(frozen=True)
class PinnedTarget:
    """A validated outbound target with its pinned connect address.

    ``host`` is the **original** hostname as given in the URL — it is what the
    connection uses for TLS SNI and certificate verification, and it must never
    be replaced by ``ip`` (rewriting the URL to the IP breaks TLS). ``ip`` is
    the validated address the TCP connection is pinned to.
    """

    scheme: str
    host: str
    port: int | None
    ip: str


def _get_allowlist() -> tuple[Network, ...]:
    global _allowlist_cache_key, _allowlist_parsed
    raw = os.environ.get("SSRF_ALLOW_PRIVATE_RANGES", "")
    if raw == _allowlist_cache_key:
        return _allowlist_parsed
    parsed: list[Network] = []
    for raw_cidr in raw.split(","):
        cidr = raw_cidr.strip()
        if cidr:
            try:
                parsed.append(ipaddress.ip_network(cidr, strict=False))
            except ValueError:
                _log.warning("ssrf.invalid_allowlist_entry", extra={"cidr": cidr})
    _allowlist_cache_key = raw
    _allowlist_parsed = tuple(parsed)
    return _allowlist_parsed


def normalize_allow_networks(raw: Sequence[str] | None) -> tuple[Network, ...]:
    """Normalise a tenant-scoped egress allowlist to an immutable network tuple.

    Accepts CIDR strings (``"10.0.0.0/8"``) or ``ip_network`` objects. Returns
    ``()`` for ``None``. Invalid entries are logged and skipped, mirroring the
    global env allowlist so a malformed tenant rule never injects a fatal config
    error — the base global policy still applies as the floor.
    """
    if raw is None:
        return ()
    parsed: list[Network] = []
    for entry in raw:
        try:
            if isinstance(entry, str):
                parsed.append(ipaddress.ip_network(entry, strict=False))
            elif isinstance(entry, (ipaddress.IPv4Network, ipaddress.IPv6Network)):
                parsed.append(entry)
            else:
                _log.warning("ssrf.invalid_allowlist_entry_type", extra={"type": type(entry).__name__})
        except ValueError:
            _log.warning("ssrf.invalid_allowlist_entry", extra={"cidr": entry})
    return tuple(parsed)


def _get_effective_allowlist(extra: tuple[Network, ...]) -> tuple[Network, ...]:
    """Layer a tenant-scoped allowlist on top of the global base.

    The global ``SSRF_ALLOW_PRIVATE_RANGES`` stays the floor; ``extra`` adds to
    it for a single tenant. Returns the union, deduplicated, as an immutable
    tuple. Every enforcement path funnels through this so there is exactly one
    implementation of "is this address permitted?".
    """
    base = _get_allowlist()
    if not extra:
        return base
    return base + tuple(net for net in extra if net not in base)


def _is_blocked_ip(ip_str: str, extra_allow: tuple[Network, ...] = ()) -> bool:
    """Check if an IP address should be blocked.

    Order matters: the NON-NEGOTIABLE floor is checked *before* the allowlist is
    consulted. Link-local, multicast, cloud-metadata and the other
    ``_EXCLUDED_NETWORKS``/``_NON_NEGOTIABLE_BLOCKED`` ranges can never be made
    reachable by a tenant or global allowlist. Loopback is blocked by default
    but allowlistable. Only after those hard floors does the configurable
    allowlist get a say over the remaining private ranges.
    """
    try:
        addr = ipaddress.ip_address(ip_str)
    except ValueError:
        return True  # fail-closed on unparseable

    # NON-NEGOTIABLE floor — blocked regardless of any allowlist.
    # Loopback is intentionally NOT here: it is blocked by default (via
    # ``addr.is_private`` below) but ALLOWLISTABLE, so a self-hosted deployment
    # can reach a localhost Ollama/vLLM/LM Studio backend by putting the
    # loopback range in SSRF_ALLOW_PRIVATE_RANGES. Link-local, multicast and
    # the metadata/CGNAT floors below remain non-negotiable.
    if addr.is_link_local or addr.is_multicast:
        return True
    for net in _EXCLUDED_NETWORKS:
        if addr in net:
            return True
    for net in _NON_NEGOTIABLE_BLOCKED:
        if addr in net:
            return True

    # Configurable allowlist (global + tenant-scoped overlay) may permit other
    # private ranges. Link-local / multicast / metadata already returned True
    # above, so this can never weaken the floor; loopback falls through here,
    # so an allowlist covering it (e.g. 127.0.0.0/8) makes it reachable.
    for net in _get_effective_allowlist(extra_allow):
        if addr in net:
            return False

    # Standard private/reserved/unspecified.
    return addr.is_private or addr.is_reserved or addr.is_unspecified


def _remediation_hint(ip_str: str) -> str:
    """Return the actionable operator guidance for a blocked address.

    Loopback gets its own hint because the naive remediation does NOT work: on a
    dual-stack host ``localhost`` resolves to BOTH ``127.0.0.1`` and ``::1``, and
    :func:`_check_resolved` fails closed if *any* resolved address is blocked. An
    allowlist of only ``127.0.0.0/8`` therefore leaves ``http://localhost:11434``
    unreachable because ``::1`` is still blocked — both entries are required (or
    use a literal ``http://127.0.0.1:11434`` URL, which skips DNS entirely).

    Kept short on purpose: connector ``health_check`` implementations truncate
    the detail to 200 characters, so the fix must be in the first line.
    """
    with contextlib.suppress(ValueError):
        if ipaddress.ip_address(ip_str).is_loopback:
            return (
                "Set SSRF_ALLOW_PRIVATE_RANGES=127.0.0.0/8,::1/128 to allow loopback "
                "(BOTH entries; dual-stack), or use a public URL."
            )
    return "Add its CIDR to SSRF_ALLOW_PRIVATE_RANGES to allow this target, or use a public URL."


def _normalize_host(host: str) -> str:
    """Normalise a host key for the pin map (both sides).

    Lowercases and strips a trailing dot, so a ``rebind.example.`` URL (httpx
    preserves the trailing dot when it reaches ``connect_tcp``) pins against the
    same key the validation produced. TLS SNI/cert verification is unaffected —
    the transport substitutes only the TCP destination, not the origin hostname.
    """
    return host.rstrip(".").lower()


def _get_dns_timeout() -> float:
    """Return the configured DNS resolution timeout in seconds."""
    raw = os.environ.get(_DNS_TIMEOUT_ENV)
    if raw is None:
        return _DNS_TIMEOUT_DEFAULT
    try:
        value = float(raw)
    except ValueError:
        _log.warning("ssrf.invalid_dns_timeout", extra={"value": raw})
        return _DNS_TIMEOUT_DEFAULT
    return value if value > 0 else _DNS_TIMEOUT_DEFAULT


def _warn_if_proxied(trust_env: bool) -> None:
    """Honk loudly if a caller opted into httpx proxy trust while a proxy env
    var is present.

    ``trust_env=False`` is the safe default for the pinned transport: httpx then
    ignores HTTP_PROXY/HTTPS_PROXY/ALL_PROXY, so the connection goes straight to
    the pinned IP. When a caller explicitly passes ``trust_env=True`` that is an
    opt-in — we do not reject it, but we make the risk visible so a mis-set
    proxy variable is not silently trusted.
    """
    if not trust_env:
        return
    present = [name for name in _PROXY_ENV_VARS if os.environ.get(name)]
    if present:
        _log.warning(
            "ssrf.pinned_transport_proxy_env",
            extra={"proxy_vars": ",".join(present)},
        )


def _reject_noncanonical_ip_literal(host: str) -> None:
    """Reject alternate IP-literal encodings a resolver may interpret as an address.

    ``ipaddress.ip_address`` only accepts canonical dotted-quad IPv4 and IPv6;
    it rejects decimal integer (``2130706433``), hex (``0x7f000001``), octal
    (``0177.0.0.1``), abbreviated (``127.1``), overlong (``1.2.3.4.5``) and
    %-encoded/zone-id forms. Some resolvers are more permissive and treat these
    as addresses, so validation must reject them rather than reach the
    resolver. This deliberately does not regex-validate the domain — non-IP
    hostnames simply pass through to ``getaddrinfo``, and the resolved set is
    validated.
    """
    if "%" in host:
        raise ValueError("URL host contains a scope/percent-encoded IP literal")
    if not host:
        return
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return  # canonical IP literal — validated by the caller
    # A `0x` prefix is only an encoded IP literal when the remainder is a full
    # run of hex digits. `0x.mydomain.com` is a valid hostname, not a literal.
    if host.lower().startswith("0x") and _is_hex_run(host[2:]):
        raise ValueError("URL host is a hex-encoded IP literal")
    if host.isdigit():
        raise ValueError("URL host is a decimal/octal integer IP literal")
    if "." in host and _is_dotted_numeric_literal(host):
        raise ValueError("URL host is a non-canonical dotted-numeric IP literal")


_HEX_CHARS = "0123456789abcdef"


def _is_hex_run(label: str) -> bool:
    """True when ``label`` is a non-empty run of hexadecimal digits."""
    lowered = label.lower()
    if not lowered:
        return False
    return all(char in _HEX_CHARS for char in lowered)


def _is_numeric_literal_label(label: str) -> bool:
    """True when a dotted-host label is a numeric token a resolver may parse as
    part of an IP literal: decimal digits, a ``0x``/``0X``-prefixed hex run, or
    a bare hex run (some resolvers read ``a`` as a single hex octet)."""
    lower = label.lower()
    if lower.isdigit():
        return True
    if lower.startswith("0x"):
        return _is_hex_run(lower[2:])
    if lower.startswith("0X"):
        return _is_hex_run(lower[2:])
    return _is_hex_run(lower)


def _is_dotted_numeric_literal(host: str) -> bool:
    """True when a dotted hostname is entirely numeric-like — i.e. it is really
    an obfuscated/non-canonical IP literal rather than a real domain.

    ``0x.mydomain.com`` is not a literal (its labels are not all numeric-like);
    ``127.0x1.0.1`` and ``1.2.3.4.5`` are. A genuine domain almost always has a
    non-hex-letter TLD label, so all-numeric-like labels are a strong signal of
    an encoded address.
    """
    labels = host.split(".")
    if not labels:
        return False
    return all(_is_numeric_literal_label(label) for label in labels)


def _validate_port(parsed: object) -> int | None:
    """Extract and validate the URL port. Fails closed on odd/out-of-range ports."""
    try:
        port: int | None = parsed.port  # type: ignore[attr-defined]
    except ValueError:
        raise ValueError("URL has an invalid port") from None
    if port is not None and not (1 <= port <= 65535):
        raise ValueError("URL port is out of the valid range (1-65535)")
    return port


@dataclass(frozen=True)
class _UrlTarget:
    scheme: str
    host: str
    port: int | None
    literal_ip: str | None  # populated iff host is a canonical IP literal


def _parse_url_target(url: str) -> _UrlTarget:
    """Strictly parse and syntax-validate a URL before any resolution.

    Rejects: non-http(s) schemes, embedded userinfo, missing hostname,
    non-canonical IP literals, and odd/out-of-range ports. Does not resolve the
    host — resolution happens separately and the resolved set is validated.
    """
    if not url or not isinstance(url, str):
        raise ValueError("URL is required")

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("URL must use http:// or https:// scheme")

    if parsed.username is not None or parsed.password is not None:
        raise ValueError("URL must not contain userinfo credentials")

    host = parsed.hostname
    if not host:
        raise ValueError("URL must have a valid hostname")
    host = host.rstrip(".").strip("[]")
    if not host:
        raise ValueError("URL must have a valid hostname")

    _reject_noncanonical_ip_literal(host)
    port = _validate_port(parsed)

    literal_ip: str | None = None
    with contextlib.suppress(ValueError):
        literal_ip = str(ipaddress.ip_address(host))

    return _UrlTarget(scheme=parsed.scheme, host=host, port=port, literal_ip=literal_ip)


def _validate_literal_ip(decoded: str, extra_allow: tuple[Network, ...] = ()) -> bool:
    """Block private/internal literal IPs; return True when ``decoded`` is an IP.

    Returns False when ``decoded`` is a hostname (not a literal IP), signalling
    that the caller must DNS-resolve it. Raises ValueError for blocked literal
    IPs (fail-closed). Non-canonical IP literal encodings (decimal/hex integers,
    mixed-base dotted strings) are rejected earlier in ``_parse_url_target`` via
    ``_reject_noncanonical_ip_literal``, so ``decoded`` here is always a
    canonical ``ipaddress``-parseable literal.
    """
    try:
        ip = ipaddress.ip_address(decoded)
    except ValueError:
        return False
    if _is_blocked_ip(str(ip), extra_allow):
        raise ValueError(f"URL targets a private/internal network address: {decoded}. {_remediation_hint(str(ip))}")
    return True


def _check_resolved(decoded: str, ip_strings: Sequence[str], extra_allow: tuple[Network, ...] = ()) -> None:
    """Raise if a hostname resolves to **no** addresses or **any** blocked address.

    Fails CLOSED on an empty resolution: a host that resolves to nothing cannot
    be verified as non-internal, so it must not connect. This also prevents the
    ``ips[0]`` IndexError crash in :func:`resolve_pinned_ip` — both the validate
    and resolve paths funnel through here, so they stay consistent.
    """
    if not ip_strings:
        raise ValueError(f"URL hostname {decoded} resolved to no addresses. Cannot verify the target is not internal.")
    for ip_str in ip_strings:
        if _is_blocked_ip(ip_str, extra_allow):
            raise ValueError(
                f"URL hostname {decoded} resolves to a private/internal address ({ip_str}). {_remediation_hint(ip_str)}"
            )


_SockAddr = tuple[str, int] | tuple[str, int, int, int] | tuple[int, bytes]
_AddrInfoEntry = tuple[socket.AddressFamily, socket.SocketKind, int, str, _SockAddr]


def _getaddrinfo_sync(host: str) -> list[_AddrInfoEntry]:
    return socket.getaddrinfo(host, 0, socket.AF_UNSPEC, socket.SOCK_STREAM)


def _extract_ip_strings(addrinfos: Iterable[_AddrInfoEntry]) -> list[str]:
    result: list[str] = []
    for _fam, _typ, _proto, _canon, sockaddr in addrinfos:
        ip_str = sockaddr[0]
        if isinstance(ip_str, str):  # O-safe: assert would vanish under python -O
            result.append(ip_str)
    return result


def _resolve_all_sync(host: str) -> list[str]:
    """Resolve ``host`` synchronously with a bounded timeout.

    ``socket.getaddrinfo`` runs in a worker from a bounded pool so a hung
    resolver stalls the caller for at most ``_get_dns_timeout()`` seconds. On
    expiry the lookup fails CLOSED — a caller must never connect on an
    unverified resolution.
    """
    timeout = _get_dns_timeout()
    try:
        future = _RESOLVER_POOL.submit(_getaddrinfo_sync, host)
        addrinfos = future.result(timeout=timeout)
    except concurrent.futures.TimeoutError:
        raise ValueError(f"DNS resolution timed out for {host}. Cannot verify the target is not internal.") from None
    except OSError:
        # Fail-closed on DNS resolution failure
        raise ValueError(f"DNS resolution failed for {host}. Cannot verify the target is not internal.") from None
    return _extract_ip_strings(addrinfos)


async def _resolve_all_async(host: str) -> list[str]:
    """Async DNS resolution with a timeout that does not block the event loop.

    ``asyncio.wait_for`` bounds the resolver so a hung DNS call cannot stall the
    coroutine; on expiry the lookup fails CLOSED (fail-closed, never connect on
    an unverified resolution).
    """
    timeout = _get_dns_timeout()
    loop = asyncio.get_running_loop()
    try:
        addrinfos = await asyncio.wait_for(
            loop.getaddrinfo(host, 0, family=socket.AF_UNSPEC, type=socket.SOCK_STREAM),
            timeout=timeout,
        )
    except TimeoutError:
        raise ValueError(f"DNS resolution timed out for {host}. Cannot verify the target is not internal.") from None
    except OSError:
        raise ValueError(f"DNS resolution failed for {host}. Cannot verify the target is not internal.") from None
    return _extract_ip_strings(addrinfos)


def validate_outbound_url(url: str, *, allow_networks: Sequence[str] | None = None) -> None:
    """Validate that a URL does not point to an internal/private destination.

    Performs synchronous DNS resolution. For use in sync contexts (Pydantic
    validators, synchronous call sites). Raises ValueError if the URL is unsafe.

    ``allow_networks`` layers a tenant-scoped CIDR allowlist on top of the
    global ``SSRF_ALLOW_PRIVATE_RANGES`` floor.

    For async callers use :func:`validate_outbound_url_async` so DNS resolution
    does not block the event loop. For callers that actually perform the
    outbound request, use :func:`pinned_async_transport` (or
    :func:`resolve_pinned_ip`) so the validated address is pinned onto the
    connection, closing the DNS-rebinding window described below.

    NOTE — formerly accepted residual risk (DNS rebinding): this validation
    function resolves the hostname, verifies all resolved addresses are
    non-internal, then returns. It does NOT pin the validated address onto the
    subsequent outbound connection. A hostname under DNS control can therefore
    resolve to a public address during validation and to an internal/metadata
    address during the actual request performed by a call site on its own,
    bypassing this check. The pinned transport built here closes that window by
    connecting to the validated address while keeping the original hostname for
    SNI/cert.
    """
    target = _parse_url_target(url)
    extra = normalize_allow_networks(allow_networks)
    if target.literal_ip is not None:
        _validate_literal_ip(target.literal_ip, extra)
        return  # literal IP handled above
    _check_resolved(target.host, _resolve_all_sync(target.host), extra)


async def validate_outbound_url_async(url: str, *, allow_networks: Sequence[str] | None = None) -> None:
    """Async variant of :func:`validate_outbound_url` for event-loop-hostile callers.

    Resolves the hostname with ``asyncio.get_running_loop().getaddrinfo`` so the
    DNS lookup does not block the event loop. Raises ValueError if the URL is
    unsafe. ``allow_networks`` layers a tenant-scoped CIDR allowlist on the
    global floor.
    """
    target = _parse_url_target(url)
    extra = normalize_allow_networks(allow_networks)
    if target.literal_ip is not None:
        _validate_literal_ip(target.literal_ip, extra)
        return  # literal IP handled above
    _check_resolved(target.host, await _resolve_all_async(target.host), extra)


async def resolve_pinned_ip(url: str, *, allow_networks: Sequence[str] | None = None) -> PinnedTarget:
    """Resolve and validate a URL, returning the pinned connect target.

    Mirrors the validation performed by :func:`validate_outbound_url_async` but
    *returns* the validated address so a caller can pin the connection to it.
    Fails closed if **any** resolved address is blocked, or if a canonical
    literal-IP target is private/internal. Returns a :class:`PinnedTarget` whose
    ``host`` is the original hostname (for SNI/cert) and ``ip`` is the address
    to connect to.
    """
    target = _parse_url_target(url)
    extra = normalize_allow_networks(allow_networks)
    if target.literal_ip is not None:
        if _is_blocked_ip(target.literal_ip, extra):
            raise ValueError(
                f"URL targets a private/internal network address: {target.literal_ip}. "
                f"{_remediation_hint(target.literal_ip)}"
            )
        return PinnedTarget(scheme=target.scheme, host=target.host, port=target.port, ip=target.literal_ip)
    ips = await _resolve_all_async(target.host)
    _check_resolved(target.host, ips, extra)
    return PinnedTarget(scheme=target.scheme, host=target.host, port=target.port, ip=ips[0])


class _PinnedAsyncNetworkBackend(httpcore.AnyIOBackend):
    """httpcore async backend that substitutes the pinned IP at connect time.

    ``connect_tcp`` is called by httpcore with the **origin hostname**; this
    backend forwards the connection to the pinned validated address instead. TLS
    SNI and certificate verification still use the origin hostname (handled by
    httpcore at a higher layer), so the URL is never rewritten to the IP.
    """

    def __init__(self, pinned_hosts: Mapping[str, str]) -> None:
        super().__init__()
        self._pinned_hosts = {_normalize_host(host): ip for host, ip in pinned_hosts.items()}

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,  # noqa: ASYNC109
        local_address: str | None = None,
        socket_options: Iterable[SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        # Fail-closed: ONLY connect to a host we validated and pinned. Any other
        # destination — e.g. a redirect hop — is refused. This is what keeps a
        # 302->169.254.169.254 from escaping the pin map.
        key = _normalize_host(host)
        pinned = self._pinned_hosts.get(key)
        if pinned is None:
            raise UnpinnedHostError(
                f"SSRF: refusing to connect to unpinned host {host!r}. The pinned transport only "
                "connects to addresses it validated up front; a redirect or secondary request "
                "must be re-validated before it can be reached."
            )
        return await super().connect_tcp(
            pinned,
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )


class PinnedAsyncHTTPTransport(httpx.AsyncHTTPTransport):
    """An ``httpx.AsyncHTTPTransport`` pinned to validated addresses.

    Built with ``pinned_async_transport`` / ``pinned_async_client``. Pins the
    ``{hostname: validated_ip}`` mapping onto the underlying httpcore pool so the
    TCP connection goes to the validated IP while SNI/cert stay on the hostname.
    """

    def __init__(
        self,
        pinned_hosts: Mapping[str, str],
        verify: ssl.SSLContext | str | bool = True,
        http2: bool = False,
        trust_env: bool = False,
    ) -> None:
        # trust_env defaults to False: when httpcore honors a proxy (HTTP_PROXY /
        # HTTPS_PROXY / ALL_PROXY) it re-resolves the target server-side and
        # connect_tcp only ever sees the proxy host, which is not in the pin map
        # — the whole point of pinning is defeated. Safe by default.
        super().__init__(verify=verify, http2=http2, trust_env=trust_env)
        _warn_if_proxied(trust_env)
        # HTTPCORE SEAM: the pin is installed by overriding httpcore's PRIVATE
        # `_pool._network_backend` attribute. httpcore (httpx 0.28.x / httpcore
        # 1.0.x — the known-good range documented in backend/pyproject.toml)
        # routes connect_tcp through this attribute when building each connection.
        # This contract is private and unsupported: if httpcore ever stops routing
        # that attribute into connection creation, the pin is silently DROPPED
        # (re-opening the DNS-rebinding window) with no error at all. The guard
        # below makes that un-pin loud.
        backend = _PinnedAsyncNetworkBackend(pinned_hosts)
        self._pool._network_backend = backend


async def pinned_async_transport(
    url: str,
    *,
    allow_networks: Sequence[str] | None = None,
    verify: ssl.SSLContext | str | bool = True,
    http2: bool = False,
    trust_env: bool = False,
) -> httpx.AsyncHTTPTransport:
    """Build a pinned-IP async transport for ``url``.

    Resolves and validates ``url`` (fails closed), then returns an
    :class:`httpx.AsyncHTTPTransport` that connects to the validated address
    while keeping the original hostname for TLS SNI and certificate
    verification. ``allow_networks`` layers a tenant-scoped CIDR allowlist on
    the global floor.

    ``trust_env`` defaults to ``False``: honoring a proxy would let it re-resolve
    the target server-side and silently defeat pinning. Opt in only when the
    proxy itself is trusted (a warning is logged if a proxy env var is present).
    """
    target = await resolve_pinned_ip(url, allow_networks=allow_networks)
    return PinnedAsyncHTTPTransport(
        {target.host: target.ip},
        verify=verify,
        http2=http2,
        trust_env=trust_env,
    )


async def pinned_async_client(
    url: str,
    *,
    allow_networks: Sequence[str] | None = None,
    verify: ssl.SSLContext | str | bool = True,
    http2: bool = False,
    trust_env: bool = False,
    follow_redirects: bool = False,
) -> httpx.AsyncClient:
    """Build a pinned-IP ``httpx.AsyncClient`` for ``url``.

    The client's transport is pinned to the validated address while SNI/cert use
    the original hostname. Redirects default to not-followed; if
    ``follow_redirects`` is enabled the caller MUST re-validate each hop with
    :func:`resolve_pinned_ip` / :func:`validate_outbound_url_async` against the
    same policy (the pinned transport only protects the primary origin, and
    :class:`UnpinnedHostError` refuses any hop outside the pin map).

    ``trust_env`` defaults to ``False`` (safe-by-default; a proxy defeats
    pinning). Opt in only when the proxy is trusted.
    """
    transport = await pinned_async_transport(
        url,
        allow_networks=allow_networks,
        verify=verify,
        http2=http2,
        trust_env=trust_env,
    )
    return httpx.AsyncClient(
        transport=transport,
        verify=verify,
        http2=http2,
        trust_env=trust_env,
        follow_redirects=follow_redirects,
    )
