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

``trust_env`` / proxies
-----------------------
The pinned transport's ``trust_env`` is ``None`` by default and resolves to the
``SSRF_TRUST_PROXY`` setting (``False`` unless explicitly set). Because pinning
and proxy trust are in tension — a proxy re-resolves the target server-side, so
the transport's pin map only ever sees the proxy host — the safe default keeps
proxy env vars (``HTTP_PROXY``/``HTTPS_PROXY``/``ALL_PROXY``) ignored. Self-hosted
deployments behind a mandatory corporate proxy can set ``SSRF_TRUST_PROXY=true``
(or pass ``trust_env=True`` per connector) to honour it, accepting that the proxy
becomes the trusted egress boundary and the pin is effectively bypassed; a
warning is logged in that case. This is the escape hatch that hardcoding
``trust_env=False`` previously removed.

Failover / re-resolve policy
----------------------------
The pinned transport pins the **full** validated IP set (not ``ips[0]``) and
fails over/round-robins across it on connect. When every pinned address fails to
connect, or the resolution is older than ``resolve_ttl_seconds`` (default
:data:`_RESOLVE_TTL_DEFAULT_SECONDS`), the host is re-resolved and re-validated
fail-closed so a decommissioned/stale IP self-heals on a long-lived client. A
re-resolved set now containing a private/internal address is refused rather than
connected to.

Accepted residual risks
-----------------------
* **Validate-then-connect (DNS rebinding).** Connectors and model backends call
  :func:`validate_outbound_url` and then connect with their own client, so a
  hostname under attacker DNS control can answer public at validation time and
  internal at connect time. This matches the pre-existing precedent for those
  call sites; the pinned transport below is the strictly safer option and should
  be preferred by new call sites that own their client construction.
* **No validation cache.** Every *validation* performs a fresh DNS lookup (bounded
  by ``SSRF_DNS_TIMEOUT``), which costs one resolver round trip per client
  construction on connector-heavy paths. This is deliberate: caching resolutions
  would widen the rebinding window above, and the resolver's own OS-level cache
  already absorbs the repeat cost. The pinned transport's TTL re-resolve is the
  exception for its own connection pool, and it re-validates on every refresh.

Beyond *validating* a URL, this module owns the **pinned-IP connection
transport**: an ``httpx`` transport that resolves and validates a target once,
then forces the TCP connection to the validated address while keeping the
**original** hostname for TLS SNI and certificate verification. This closes the
DNS-rebinding window that a validate-then-connect pattern leaves open (see the
note on :func:`validate_outbound_url`).

FAR-526C — honest scope of the ``no-raw-httpx-client`` import gate
------------------------------------------------------------------
The semgrep rule ``no-raw-httpx-client`` forbids ``httpx.AsyncClient`` /
``httpx.Client`` CONSTRUCTION outside this factory module (``core/ssrf.py``) and
the REST connector's injected/pinned transport seam. That gate deliberately
covers the **connector egress surface** — every connector's ``_client()`` now
builds through :func:`pinned_async_client_sync` / :func:`pinned_async_client`
(Part A/B + FAR-526C migrations). The following are explicitly OUT OF SCOPE and
are **known residual**, documented here so the gate is honest about what it does
and does not cover:

* **SDK-internal clients.** ``langchain_openai``, the provider SDKs
  (``anthropic``, ``openai``), and similar third-party clients construct their
  own ``httpx`` pools internally; the rule cannot see their construction and does
  not attempt to. They are mitigated by injecting a pinned client where the SDK
  supports it (``model_backends`` wire the validated transport in) — i.e. the
  SDK is told to reuse OUR pinned client rather than building its own.
* **Core support modules that build their own client.** ``core/notifier``,
  ``core/error_tracking/**`` (forwarders + ``alert_dispatcher``),
  ``core/product_analytics/vendor_client``, ``core/watchdog/worker_liveness``,
  ``core/library_sync/client`` and ``core/reports/scheduler`` construct raw
  clients toward operator-config / per-org / multi-host endpoints. Several are
  genuinely multi-host (the notifier fans out to per-org notification
  endpoints), so a single pinned client cannot cover them — they need either a
  per-host pin or per-request validation. ``core/library_sync`` already
  validate-then-connects (an accepted residual, see the note on
  :func:`validate_outbound_url`); the others are pending a per-call-site pin and
  are excluded from the semgrep gate so it does not go red while they remain
  un-migrated. A NEW raw ``httpx`` construction anywhere else in the tree is
  still caught by the rule.
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
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpcore
import httpx
from httpcore._backends.base import SOCKET_OPTION

from modulo.core import egress_metrics

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

# Default TTL (seconds) before the pinned transport re-resolves a host to
# self-heal a decommissioned/stale IP. Configurable per-transport via
# ``resolve_ttl_seconds`` or override the module default with SSRF_RESOLVE_TTL.
_RESOLVE_TTL_DEFAULT_SECONDS = 300.0
_RESOLVE_TTL_ENV = "SSRF_RESOLVE_TTL"

# Proxy-trust default for the pinned transport. ``trust_env`` is ``None`` by
# default and resolves to this setting, so an operator can switch the whole
# fleet to honouring a corporate proxy (SSRF_TRUST_PROXY=true) without editing
# every connector — while keeping the safe-by-default (no proxy) behaviour when
# unset. Enabling proxy trust means the proxy re-resolves the target server-side
# and the pin map sees only the proxy host, so pinning is effectively bypassed;
# use it only where the proxy IS the trusted egress boundary.
_TRUST_PROXY_ENV = "SSRF_TRUST_PROXY"

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


class PinDroppedError(RuntimeError):
    """Raised when the pinned transport's runtime loudness guard detects the pin
    was silently dropped.

    The pin is installed by overriding httpcore's PRIVATE ``_pool._network_backend``
    attribute (see :class:`PinnedAsyncHTTPTransport`). If a future httpcore stops
    routing that attribute into connection creation, the DNS-rebinding pin would
    be dropped with NO error — re-opening the rebinding window. This error fires
    when a request completes but the pinned backend's ``connect_tcp`` was never
    consulted, making the un-pin loud instead of silent.
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
    """A validated outbound target with its full pinned connect-address set.

    ``host`` is the **original** hostname as given in the URL — it is what the
    connection uses for TLS SNI and certificate verification, and it must never
    be replaced by an IP (rewriting the URL to the IP breaks TLS). ``ips`` is the
    validated address set the TCP connection pins to (a host can resolve to
    several valid public addresses, e.g. CDNs); the transport fails over /
    round-robins across them and re-resolves on expiry. ``ip`` is a convenience
    alias for the first validated address.
    """

    scheme: str
    host: str
    port: int | None
    ips: tuple[str, ...]

    @property
    def ip(self) -> str:
        """The first validated address (backward-compatible alias)."""
        return self.ips[0]


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


def _default_trust_env() -> bool:
    """Resolve the process-wide default for the pinned transport's ``trust_env``.

    ``trust_env`` is exposed as ``bool | None`` so each caller can opt in or out
    explicitly, while leaving it ``None`` falls back to this setting. Reads
    ``SSRF_TRUST_PROXY`` (``true``/``1``/``yes``/``on`` honours a corporate proxy
    for every connector; unset/other keeps the safe-by-default ``False`` so a
    proxy env var does not silently defeat pinning).
    """
    return os.environ.get(_TRUST_PROXY_ENV, "").strip().lower() in ("1", "true", "yes", "on")


def _get_resolve_ttl() -> float:
    """Return the configured re-resolve TTL in seconds."""
    raw = os.environ.get(_RESOLVE_TTL_ENV)
    if raw is None:
        return _RESOLVE_TTL_DEFAULT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        _log.warning("ssrf.invalid_resolve_ttl", extra={"value": raw})
        return _RESOLVE_TTL_DEFAULT_SECONDS
    return value if value > 0 else _RESOLVE_TTL_DEFAULT_SECONDS


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


def _eh_label_host(url: str) -> str:
    """Extract a stable ``host`` label from a URL for egress metric records.

    Mirrors :func:`normalize_url_host` (lowercase, trailing-dot stripped, no
    brackets) but degrades to ``"unknown"`` on any parse problem so a metric
    record never blocks the egress path.
    """
    try:
        return normalize_url_host(url)
    except Exception:
        return "unknown"


def _classify_reject_reason(exc: BaseException, url: str) -> str:
    """Classify a resolve/validate ``ValueError`` into a stable egress reason.

    The egress factory records why a pinned client could not be built. The
    scheme check is derived from ``url`` (the first syntax gate); timeout /
    resolution-failure are read from the stable failure messages raised by this
    module. Anything else is a destination policy rejection (``blocked``):
    private/internal address, non-canonical IP literal, embedded userinfo, or a
    malformed host.
    """
    if urlparse(url).scheme not in ("http", "https"):
        return egress_metrics.REASON_BAD_SCHEME
    message = str(exc)
    if "DNS resolution timed out" in message:
        return egress_metrics.REASON_DNS_TIMEOUT
    if "DNS resolution failed" in message:
        return egress_metrics.REASON_DNS_FAILED
    return egress_metrics.REASON_BLOCKED


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


def validate_outbound_url_preflight(url: str, *, allow_networks: Sequence[str] | None = None) -> None:
    """DNS-free SSRF preflight: syntax floor plus literal-IP internal blocking.

    Enforces the cheap, engine-free parts of :func:`validate_outbound_url` (an
    ``http(s)`` scheme, no userinfo credentials, a valid hostname) and, for a
    literal-IP target, the private/loopback/link-local/cloud-metadata block.

    This deliberately does **not** resolve hostnames. Hostname resolution,
    rebinding-close pinning and per-connect validation are performed by
    :func:`resolve_pinned_ip` / :func:`pinned_async_client` at connect time —
    those are the primary SSRF boundary. Use this preflight where a caller wants
    a cheap synchronous guard ahead of (or in place of the exact-host check on) a
    pinned connection, so an obviously-internal literal such as
    ``https://169.254.169.254/...`` is rejected before any request, without a DNS
    round-trip. Raises ``ValueError`` when the target is unsafe.
    """
    target = _parse_url_target(url)
    extra = normalize_allow_networks(allow_networks)
    if target.literal_ip is not None:
        _validate_literal_ip(target.literal_ip, extra)


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
        return PinnedTarget(scheme=target.scheme, host=target.host, port=target.port, ips=(target.literal_ip,))
    ips = await _resolve_all_async(target.host)
    _check_resolved(target.host, ips, extra)
    return PinnedTarget(scheme=target.scheme, host=target.host, port=target.port, ips=tuple(ips))


def _resolve_pinned_ip_sync(url: str, *, allow_networks: Sequence[str] | None = None) -> PinnedTarget:
    """Synchronous :func:`resolve_pinned_ip` — for sync ``_client()`` builders.

    Mirrors the async variant (including the fail-closed semantics and the
    non-negotiable blocked floor) but resolves with the synchronous
    ``_resolve_all_sync`` path so a connector's synchronous ``_client()`` can
    pin its validated address without leaving the event loop. Connectors that
    build clients synchronously (``httpx.AsyncClient`` in a ``def _client``)
    use this + :func:`pinned_async_transport_sync` / :func:`pinned_async_client_sync`
    so they do NOT re-resolve the host at connect time.
    """
    target = _parse_url_target(url)
    extra = normalize_allow_networks(allow_networks)
    if target.literal_ip is not None:
        if _is_blocked_ip(target.literal_ip, extra):
            raise ValueError(
                f"URL targets a private/internal network address: {target.literal_ip}. "
                f"{_remediation_hint(target.literal_ip)}"
            )
        return PinnedTarget(scheme=target.scheme, host=target.host, port=target.port, ips=(target.literal_ip,))
    ips = _resolve_all_sync(target.host)
    _check_resolved(target.host, ips, extra)
    return PinnedTarget(scheme=target.scheme, host=target.host, port=target.port, ips=tuple(ips))


def _coerce_pin_ips(value: str | Sequence[str]) -> tuple[str, ...]:
    """Normalise a pin-map value to an immutable non-empty tuple of IP strings.

    Accepts a bare ``str`` (single pinned address, kept for backward
    compatibility with the pre-failover pin map) or a sequence of IP strings.
    """
    if isinstance(value, str):
        return (value,)
    return tuple(value)


class _PinnedAsyncNetworkBackend(httpcore.AnyIOBackend):
    """httpcore async backend that substitutes a pinned IP set at connect time.

    ``connect_tcp`` is called by httpcore with the **origin hostname**; this
    backend forwards the connection to one of the validated pinned addresses
    instead. TLS SNI and certificate verification still use the origin hostname
    (handled by httpcore at a higher layer), so the URL is never rewritten to an
    IP.

    Failover + re-resolve policy:
        * The FULL validated IP set is pinned (not ``ips[0]``), so a multi-IP
          origin (CDN, load-balanced vendor) keeps its failover pool. On connect
          the addresses are tried round-robin; a connect failure on one advances
          to the next (that is the failover).
        * After the pinned set is exhausted (every IP failed to connect) OR a
          resolution is older than ``resolve_ttl_seconds``, the host is
          **re-resolved** and re-validated (fail-closed). This self-heals a
          decommissioned/stale IP for a long-lived client instead of hanging
          onto a dead address. Re-validation is fail-closed: if the re-resolved
          set now contains a private/internal address, the transport refuses to
          connect rather than reach it.
    """

    def __init__(
        self,
        pinned_hosts: Mapping[str, str | Sequence[str]],
        *,
        allow_networks: Sequence[str] | None = None,
        re_resolve: bool = True,
        resolve_ttl_seconds: float | None = None,
        connector_type: str = egress_metrics.DEFAULT_CONNECTOR_TYPE,
    ) -> None:
        super().__init__()
        self._pinned_hosts: dict[str, tuple[str, ...]] = {
            _normalize_host(host): _coerce_pin_ips(ips) for host, ips in pinned_hosts.items()
        }
        self._allow_networks = normalize_allow_networks(allow_networks)
        self._re_resolve = re_resolve
        self._connector_type = connector_type or egress_metrics.DEFAULT_CONNECTOR_TYPE
        self._resolve_ttl = _get_resolve_ttl() if resolve_ttl_seconds is None else resolve_ttl_seconds
        # Per-host state: round-robin cursor + the time the pinned set was resolved.
        self._cursor: dict[str, int] = {}
        self._resolved_at: dict[str, float] = {host: time.time() for host in self._pinned_hosts}
        # Loudness guard: how many connections this backend actually routed the
        # `connect_tcp` call through (proves the pin was consulted, not dropped).
        self.connect_calls = 0

    async def _connect_to_ip(
        self,
        ip: str,
        port: int,
        *,
        timeout: float | None = None,  # noqa: ASYNC109
        local_address: str | None = None,
        socket_options: Iterable[SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        """Connect to a single pinned address (the test seam for failover)."""
        return await super().connect_tcp(
            ip,
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )

    async def _refresh_ips(self, host_key: str) -> tuple[str, ...]:
        """Re-resolve + re-validate ``host_key`` and return the fresh IP set.

        Fail-closed: a resolution that now contains any private/internal address
        raises ``ValueError`` so the transport refuses to reach it rather than
        connecting to an IP that was valid at pin time but is now the attack
        surface.
        """
        ips = await _resolve_all_async(host_key)
        _check_resolved(host_key, ips, self._allow_networks)
        self._resolved_at[host_key] = time.time()
        return tuple(ips)

    async def _try_set(
        self,
        host_key: str,
        ips: tuple[str, ...],
        port: int,
        *,
        timeout: float | None,  # noqa: ASYNC109
        local_address: str | None,
        socket_options: Iterable[SOCKET_OPTION] | None,
    ) -> httpcore.AsyncNetworkStream:
        """Try each IP in the set round-robin from the cursor, failing over on error."""
        # httpcore maps OS connect/read/write failures to its own exception
        # hierarchy (ConnectError / ConnectTimeout / ReadError / ...), NOT to
        # OSError. Catching only OSError left the failover loop dead — a connect
        # error never advanced to the next pinned IP. Catch the two httpcore
        # bases (NetworkError = connect/read/write errors, TimeoutException =
        # all timeouts) so any transport failure advances the failover.
        last_exc: Exception | None = None
        start = self._cursor.get(host_key, 0)
        for offset in range(len(ips)):
            idx = (start + offset) % len(ips)
            ip = ips[idx]
            try:
                stream = await self._connect_to_ip(
                    ip, port, timeout=timeout, local_address=local_address, socket_options=socket_options
                )
            except (httpcore.NetworkError, httpcore.TimeoutException) as exc:
                last_exc = exc
                self._cursor[host_key] = idx + 1
                continue
            self._cursor[host_key] = idx + 1
            self._resolved_at[host_key] = time.time()
            self.connect_calls += 1
            return stream
        # Every pinned IP failed to connect; re-raise the last connect error.
        assert last_exc is not None
        raise last_exc

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
        ips = self._pinned_hosts.get(key)
        if ips is None:
            _log.warning(
                "ssrf.reject_unpinned_host",
                extra={"host": host, "connector_type": self._connector_type},
            )
            egress_metrics.record_rejected(self._connector_type, host, egress_metrics.REASON_UNPINNED)
            raise UnpinnedHostError(
                f"SSRF: refusing to connect to unpinned host {host!r}. The pinned transport only "
                "connects to addresses it validated up front; a redirect or secondary request "
                "must be re-validated before it can be reached."
            )
        # TTL-based self-heal: re-resolve before connecting when the pinned set has
        # gone stale, so a decommissioned IP does not keep failing forever.
        if (
            self._re_resolve
            and self._resolve_ttl > 0
            and time.time() - self._resolved_at.get(key, 0.0) > self._resolve_ttl
        ):
            self._pinned_hosts[key] = await self._refresh_ips(key)
            ips = self._pinned_hosts[key]
        try:
            return await self._try_set(
                key, ips, port, timeout=timeout, local_address=local_address, socket_options=socket_options
            )
        except (httpcore.NetworkError, httpcore.TimeoutException):
            if not self._re_resolve:
                raise
            # All pinned IPs failed at connect: self-heal by re-resolving and
            # retrying the fresh set once. A re-validation failure (now-internal
            # resolution) propagates fail-closed rather than connecting.
            self._pinned_hosts[key] = await self._refresh_ips(key)
            return await self._try_set(
                key,
                self._pinned_hosts[key],
                port,
                timeout=timeout,
                local_address=local_address,
                socket_options=socket_options,
            )


class PinnedAsyncHTTPTransport(httpx.AsyncHTTPTransport):
    """An ``httpx.AsyncHTTPTransport`` pinned to validated addresses.

    Built with ``pinned_async_transport`` / ``pinned_async_client``. Pins the
    ``{hostname: validated_ip_set}`` mapping onto the underlying httpcore pool so
    the TCP connection goes to a validated IP while SNI/cert stay on the
    hostname. The full validated set is pinned (not the first address only) and
    the backend fails over / re-resolves across it.
    """

    def __init__(
        self,
        pinned_hosts: Mapping[str, str | Sequence[str]],
        verify: ssl.SSLContext | str | bool = True,
        http2: bool = False,
        trust_env: bool | None = None,
        limits: httpx.Limits | None = None,
        allow_networks: Sequence[str] | None = None,
        re_resolve: bool = True,
        resolve_ttl_seconds: float | None = None,
        loudness_guard: bool = False,
        connector_type: str = egress_metrics.DEFAULT_CONNECTOR_TYPE,
    ) -> None:
        # ``trust_env`` is ``None`` by default and resolves to the process-wide
        # SSRF_TRUST_PROXY setting. When true, honoring a proxy (HTTP_PROXY /
        # HTTPS_PROXY / ALL_PROXY) makes httpcore re-resolve the target
        # server-side and connect_tcp only ever sees the proxy host, which is not
        # in the pin map — so pinning is effectively bypassed. Safe by default; an
        # explicit opt-in (or the SSRF_TRUST_PROXY fleet setting) is for
        # proxy-terminated egress where the proxy IS the trusted boundary.
        resolved_trust = _default_trust_env() if trust_env is None else trust_env
        # ``limits`` (when supplied) flows into the underlying httpcore pool so a
        # connector that configures max_connections / max_keepalive keeps its
        # pooling budget even though it now owns the transport explicitly. When
        # the caller does not pass ``limits`` we forward httpx's default pool cap
        # (Limits(100/20)) — NOT an empty ``httpx.Limits()`` (which httpcore
        # substitutes with sys.maxsize, i.e. an unbounded pool) — so a pinned
        # transport keeps the same connection cap as a stock httpx client.
        super().__init__(
            verify=verify,
            http2=http2,
            trust_env=resolved_trust,
            limits=limits if limits is not None else httpx.Limits(max_connections=100, max_keepalive_connections=20),
        )
        _warn_if_proxied(resolved_trust)
        # HTTPCORE SEAM: the pin is installed by overriding httpcore's PRIVATE
        # `_pool._network_backend` attribute. httpcore (httpx 0.28.x / httpcore
        # 1.0.x — the known-good range documented in backend/pyproject.toml)
        # routes connect_tcp through this attribute when building each connection.
        # This contract is private and unsupported: if httpcore ever stops routing
        # that attribute into connection creation, the pin is silently DROPPED
        # (re-opening the DNS-rebinding window) with no error at all. The loudness
        # guard below makes that un-pin explicit.
        self._pin_backend = _PinnedAsyncNetworkBackend(
            pinned_hosts,
            allow_networks=allow_networks,
            re_resolve=re_resolve,
            resolve_ttl_seconds=resolve_ttl_seconds,
            connector_type=connector_type,
        )
        self._pool._network_backend = self._pin_backend
        self._loudness_guard = loudness_guard
        self._pin_proven = False

    def _enforce_pin_live(self) -> None:
        """Prove the pin is live, or raise :class:`PinDroppedError`.

        After a completed request the pinned backend's ``connect_tcp`` must have
        been consulted at least once. A successful request where it was NEVER
        consulted means httpcore stopped routing the private ``_network_backend``
        attribute into connection creation — the pin silently dropped. Throwing
        here turns that silent regression into a loud failure.
        """
        if not self._loudness_guard or self._pin_proven:
            return
        if self._pin_backend.connect_calls == 0:
            self._pin_proven = True  # only honk once
            _log.critical(
                "ssrf.pin_dropped",
                extra={"hosts": ",".join(sorted(self._pin_backend._pinned_hosts))},
            )
            raise PinDroppedError(
                "SSRF: the pinned transport's network backend was never consulted for the "
                "connection — httpcore likely stopped routing the private `_pool._network_backend` "
                "attribute, and the DNS-rebinding pin has been silently dropped. Constrain httpcore "
                "to the known-good range documented in backend/pyproject.toml."
            )
        self._pin_proven = True

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        # Only check the loudness guard after a COMPLETED request (not when the
        # request raised), so a genuine connect/DNS error surfaces as itself and
        # never gets masked by the guard. A raising request propagates directly
        # from ``super().handle_async_request``, which skips the guard for free.
        response = await super().handle_async_request(request)
        self._enforce_pin_live()
        return response


async def pinned_async_transport(
    url: str,
    *,
    allow_networks: Sequence[str] | None = None,
    verify: ssl.SSLContext | str | bool = True,
    http2: bool = False,
    trust_env: bool | None = None,
    limits: httpx.Limits | None = None,
    re_resolve: bool = True,
    resolve_ttl_seconds: float | None = None,
    loudness_guard: bool = False,
    connector_type: str = egress_metrics.DEFAULT_CONNECTOR_TYPE,
) -> httpx.AsyncHTTPTransport:
    """Build a pinned-IP async transport for ``url``.

    Resolves and validates ``url`` (fails closed), then returns an
    :class:`httpx.AsyncHTTPTransport` that connects to the validated address
    set while keeping the original hostname for TLS SNI and certificate
    verification. ``allow_networks`` layers a tenant-scoped CIDR allowlist on
    the global floor.

    ``trust_env`` is ``None`` by default and resolves to the
    ``SSRF_TRUST_PROXY`` setting (``False`` unless set): honoring a proxy would
    let it re-resolve the target server-side and silently defeat pinning. Opt in
    only when the proxy itself is trusted (a warning is logged if a proxy env var
    is present).

    ``re_resolve`` (default ``True``) enables the failover self-heal: the FULL
    validated IP set is pinned and, on a connect failure or after
    ``resolve_ttl_seconds`` (default :data:`_RESOLVE_TTL_DEFAULT_SECONDS`), the
    host is re-resolved and re-validated so a decommissioned/stale IP self-heals
    instead of a long-lived client hanging onto a dead address.

    ``loudness_guard`` (default ``False``) proves the pin is live on the first
    completed request: it raises :class:`PinDroppedError` if the pinned network
    backend was never consulted. Off by default because an intercepted transport
    (e.g. an HTTP mock at the pool layer) legitimately never consults it; enable
    on long-lived real-egress clients (model backends).

    ``connector_type`` (default :data:`egress_metrics.DEFAULT_CONNECTOR_TYPE`)
    labels the emitted ``modulo_egress_pinned_total`` /
    ``modulo_egress_rejected_total`` records so the egress layer is attributable
    to the connector (or model backend) that built it.
    """
    try:
        target = await resolve_pinned_ip(url, allow_networks=allow_networks)
    except ValueError as exc:
        egress_metrics.record_rejected(connector_type, _eh_label_host(url), _classify_reject_reason(exc, url))
        raise
    transport = PinnedAsyncHTTPTransport(
        {target.host: target.ips},
        verify=verify,
        http2=http2,
        trust_env=trust_env,
        limits=limits,
        allow_networks=allow_networks,
        re_resolve=re_resolve,
        resolve_ttl_seconds=resolve_ttl_seconds,
        loudness_guard=loudness_guard,
        connector_type=connector_type,
    )
    egress_metrics.record_pinned(connector_type, target.host)
    return transport


async def pinned_async_client(
    url: str,
    *,
    allow_networks: Sequence[str] | None = None,
    verify: ssl.SSLContext | str | bool = True,
    http2: bool = False,
    trust_env: bool | None = None,
    follow_redirects: bool = False,
    limits: httpx.Limits | None = None,
    loudness_guard: bool = False,
    connector_type: str = egress_metrics.DEFAULT_CONNECTOR_TYPE,
    **client_kwargs: Any,
) -> httpx.AsyncClient:
    """Build a pinned-IP ``httpx.AsyncClient`` for ``url``.

    The client's transport is pinned to the validated address while SNI/cert use
    the original hostname. Redirects default to not-followed; if
    ``follow_redirects`` is enabled the caller MUST re-validate each hop with
    :func:`resolve_pinned_ip` / :func:`validate_outbound_url_async` against the
    same policy (the pinned transport only protects the primary origin, and
    :class:`UnpinnedHostError` refuses any hop outside the pin map).

    ``trust_env`` is ``None`` by default and resolves to the ``SSRF_TRUST_PROXY``
    setting (``False`` unless set; a proxy defeats pinning). Opt in only when the
    proxy is trusted. ``loudness_guard`` defaults ``False`` (see
    :func:`pinned_async_transport`).

    ``**client_kwargs`` (``headers``, ``timeout``, ``auth``, ``base_url``, …) are
    forwarded to :class:`httpx.AsyncClient`, mirroring
    :func:`pinned_async_client_sync`; a caller-supplied ``transport`` key is
    rejected so a caller cannot silently bypass the pinned transport.
    """
    if "transport" in client_kwargs:
        raise ValueError(
            "pinned_async_client: 'transport' must not be passed via client_kwargs "
            "— it would bypass the pinned transport. Use the pinned transport built here."
        )
    transport = await pinned_async_transport(
        url,
        allow_networks=allow_networks,
        verify=verify,
        http2=http2,
        trust_env=trust_env,
        limits=limits,
        loudness_guard=loudness_guard,
        connector_type=connector_type,
    )
    resolved_trust = _default_trust_env() if trust_env is None else trust_env
    return httpx.AsyncClient(
        transport=transport,
        verify=verify,
        http2=http2,
        trust_env=resolved_trust,
        follow_redirects=follow_redirects,
        **client_kwargs,
    )


def normalize_url_host(url: str) -> str:
    """Return the normalised hostname of an ``http(s)`` URL.

    Lowercases and strips a trailing dot so the returned value is directly
    comparable against a configured allowlist. Raises ``ValueError`` for a
    non-http(s) scheme, embedded userinfo credentials, or a missing hostname.
    This is the host-string extraction used by the OIDC endpoint allowlist;
    full SSRF/IP validation is performed separately by
    :func:`resolve_pinned_ip` / :func:`pinned_async_client`.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("URL must use http:// or https:// scheme")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("URL must not contain userinfo credentials")
    host = (parsed.hostname or "").rstrip(".").strip("[]").lower()
    if not host:
        raise ValueError("URL must have a valid hostname")
    return host


def derive_oidc_allowed_hosts(discovery_url: str, issuer: str | None = None) -> frozenset[str]:
    """Compute the host allowlist for a remote OIDC discovery document.

    The trust anchor is the admin-configured ``discovery_url``. A derived
    endpoint (``jwks_uri`` / ``token_endpoint`` / ``userinfo_endpoint`` /
    ``authorization_endpoint``) that the document points at may only target the
    discovery host or the ``issuer`` host (per OIDC the issuer should itself
    match the discovery host). This is the host-match check that stops a
    compromised or malicious discovery document from redirecting traffic at an
    internal / cloud-metadata address or an arbitrary third party.
    """
    hosts = {normalize_url_host(discovery_url)}
    if issuer:
        with contextlib.suppress(ValueError):
            hosts.add(normalize_url_host(issuer))
    return frozenset(hosts)


def require_url_host_in_allowlist(url: str, allowed_hosts: Iterable[str]) -> str:
    """Return the normalised host of ``url`` if it is in ``allowed_hosts``.

    Raises ``ValueError`` when ``url`` is not a well-formed ``http(s)`` URL or
    its host is not in the allowlist. Enforces that a remote-supplied OIDC
    endpoint only targets the provider's own host(s) — the caller uses the
    returned host as proof the endpoint was allowlisted before requesting it.
    """
    host = normalize_url_host(url)
    allowed = {entry.rstrip(".").strip("[]").lower() for entry in allowed_hosts}
    if host not in allowed:
        raise ValueError(f"URL host '{host}' is not in the OIDC provider host allowlist (allowed: {sorted(allowed)})")
    return host


def pinned_async_transport_sync(
    url: str,
    *,
    allow_networks: Sequence[str] | None = None,
    verify: ssl.SSLContext | str | bool = True,
    http2: bool = False,
    trust_env: bool | None = None,
    limits: httpx.Limits | None = None,
    re_resolve: bool = True,
    resolve_ttl_seconds: float | None = None,
    loudness_guard: bool = False,
    connector_type: str = egress_metrics.DEFAULT_CONNECTOR_TYPE,
) -> httpx.AsyncHTTPTransport:
    """Synchronous :func:`pinned_async_transport` for sync ``_client()`` builders.

    Resolves + validates ``url`` synchronously and returns a
    :class:`httpx.AsyncHTTPTransport` pinned to the validated address set, so a
    connector that constructs its client without awaiting does not re-resolve
    the host at connect time (closing the DNS-rebinding window). ``trust_env`` is
    ``None`` by default and resolves to the ``SSRF_TRUST_PROXY`` setting
    (``False`` unless set; a proxy defeats pinning). ``loudness_guard`` defaults
    ``False`` (see :func:`pinned_async_transport`). ``connector_type`` labels the
    egress metric records (see :func:`pinned_async_transport`).
    """
    try:
        target = _resolve_pinned_ip_sync(url, allow_networks=allow_networks)
    except ValueError as exc:
        egress_metrics.record_rejected(connector_type, _eh_label_host(url), _classify_reject_reason(exc, url))
        raise
    transport = PinnedAsyncHTTPTransport(
        {target.host: target.ips},
        verify=verify,
        http2=http2,
        trust_env=trust_env,
        limits=limits,
        allow_networks=allow_networks,
        re_resolve=re_resolve,
        resolve_ttl_seconds=resolve_ttl_seconds,
        loudness_guard=loudness_guard,
        connector_type=connector_type,
    )
    egress_metrics.record_pinned(connector_type, target.host)
    return transport


def pinned_async_client_sync(
    url: str,
    *,
    allow_networks: Sequence[str] | None = None,
    verify: ssl.SSLContext | str | bool = True,
    http2: bool = False,
    trust_env: bool | None = None,
    follow_redirects: bool = False,
    limits: httpx.Limits | None = None,
    loudness_guard: bool = False,
    connector_type: str = egress_metrics.DEFAULT_CONNECTOR_TYPE,
    **client_kwargs: Any,
) -> httpx.AsyncClient:
    """Synchronous :func:`pinned_async_client` for sync ``_client()`` builders.

    Builds a pinned-IP ``httpx.AsyncClient`` whose transport is pinned to the
    validated address set for ``url`` while SNI/cert use the original hostname.
    Synchronous so a connector's ``def _client`` can return a ready-made
    pinned client. ``**client_kwargs`` (``base_url``, ``headers``, ``timeout``,
    ``auth``, ``limits``, …) are forwarded to :class:`httpx.AsyncClient`.
    ``trust_env`` is ``None`` by default and resolves to the ``SSRF_TRUST_PROXY``
    setting (``False`` unless set; a proxy defeats pinning). ``loudness_guard``
    defaults ``False`` (see :func:`pinned_async_transport`). A caller-supplied
    ``transport`` key is rejected so it cannot silently bypass the pinned
    transport.
    """
    if "transport" in client_kwargs:
        raise ValueError(
            "pinned_async_client_sync: 'transport' must not be passed via client_kwargs "
            "— it would bypass the pinned transport. Use the pinned transport built here."
        )
    transport = pinned_async_transport_sync(
        url,
        allow_networks=allow_networks,
        verify=verify,
        http2=http2,
        trust_env=trust_env,
        limits=limits,
        loudness_guard=loudness_guard,
        connector_type=connector_type,
    )
    resolved_trust = _default_trust_env() if trust_env is None else trust_env
    return httpx.AsyncClient(
        transport=transport,
        verify=verify,
        http2=http2,
        trust_env=resolved_trust,
        follow_redirects=follow_redirects,
        **client_kwargs,
    )
