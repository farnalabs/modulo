"""FAR-1038: TLS enforcement for remote Docker endpoints.

Concrete-provider-free home for Docker endpoint classification and the TLS
gate.  Both Docker consumers import from here so they share ONE enforcement
point:

- :class:`~modulo.core.runtime_provider.docker.DockerRuntimeProvider`
  validates its endpoint at registration, and
- the Bundled Runner orphan reconciler validates the same endpoint before it
  opens its engine client.

It lives outside ``docker.py`` so that non-provider consumers can reuse the
gate without importing the concrete provider module (forbidden by the
``no-concrete-runtime-provider-imports`` architecture contract) and without
pulling ``aiodocker`` in transitively.
"""

from __future__ import annotations

import ipaddress
import logging
import os
from urllib.parse import ParseResult, urlparse

_log = logging.getLogger(__name__)

# The shipped compose overlay uses ``tcp://docker-socket-proxy:2375`` on a
# private bridge network — that is the only non-unix, non-loopback endpoint
# that is exempt from the TLS requirement.  Loopback TCP endpoints are also
# exempt because they do not traverse a network.
COMPOSE_INTERNAL_HOST = "docker-socket-proxy"
# The port the shipped compose overlay publishes the proxy on.  The hostname
# exemption is PINNED to this port: a look-alike host on any other port is a
# remote endpoint and still requires TLS.
COMPOSE_INTERNAL_PORT = 2375
# Environment variables that signal TLS is configured for a Docker endpoint.
TLS_ENV_VARS = ("DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH")
# Operator escape hatch: set to a non-empty, non-false value to allow a
# deliberately-insecure remote TCP endpoint without TLS.  Logged prominently
# at provider construction.
ALLOW_INSECURE_ENDPOINT_ENV = "MODULO_DOCKER_ALLOW_INSECURE_ENDPOINT"


def is_loopback_hostname(hostname: str) -> bool:
    """Return True if *hostname* refers to a loopback address or ``localhost``.

    Handles: ``localhost``, bare IPv4 loopback (``127.x.y.z``), bare IPv6
    ``::1``, and the bracketed IPv6 form (``[::1]``).
    """
    # Strip brackets from the bracketed IPv6 form ([::1]).
    stripped = hostname.strip("[]")
    if stripped.lower() in ("localhost", "::1"):
        return True
    try:
        return ipaddress.ip_address(stripped).is_loopback
    except ValueError:
        return False


def parsed_port(parsed: ParseResult) -> int | None:
    """Return *parsed*'s port, or ``None`` when absent / malformed.

    ``urlparse().port`` raises ``ValueError`` for a non-numeric or
    out-of-range port; such an endpoint is simply not the compose-internal
    proxy, so treat it as "no port" instead of crashing validation.
    """
    try:
        return parsed.port
    except ValueError:
        return None


def is_local_endpoint(endpoint: str | None) -> bool:
    """Return True if *endpoint* is a local (non-remote) Docker endpoint.

    Local endpoints:
    - ``None`` or empty (default local socket)
    - ``unix://`` sockets and bare paths (no scheme)
    - Loopback TCP: ``localhost``, ``127.x.y.z``, ``::1`` / ``[::1]``
    - The shipped compose-internal ``tcp://docker-socket-proxy:2375``
      (host AND port pinned — a look-alike host on any other port is remote)

    Everything else (any other ``tcp://`` host) is remote and MUST use TLS.
    """
    if not endpoint:
        return True
    parsed = urlparse(endpoint if "://" in endpoint else f"unix://{endpoint}")
    scheme = parsed.scheme.lower()
    if scheme in ("unix", ""):
        return True
    if scheme == "tcp" and parsed.hostname:
        if parsed.hostname.lower() == COMPOSE_INTERNAL_HOST and parsed_port(parsed) == COMPOSE_INTERNAL_PORT:
            return True
        if is_loopback_hostname(parsed.hostname):
            return True
    return False


def is_tls_configured() -> bool:
    """Return True if the process environment signals Docker TLS is configured."""
    for var in TLS_ENV_VARS:
        val = os.environ.get(var, "").strip()
        if val and val not in ("0", "false", "False"):
            return True
    return False


def is_insecure_endpoint_allowed() -> bool:
    """Return True if the operator explicitly opted in to insecure remote endpoints."""
    val = os.environ.get(ALLOW_INSECURE_ENDPOINT_ENV, "").strip()
    return bool(val and val not in ("0", "false", "False"))


def validate_docker_endpoint_tls(endpoint: str | None) -> None:
    """Validate that a remote Docker endpoint has TLS configured.

    Raises ``ValueError`` with an actionable message when a non-local TCP
    endpoint is used without TLS.  Local (unix / None / loopback) and
    compose-internal endpoints are always accepted.

    The ``MODULO_DOCKER_ALLOW_INSECURE_ENDPOINT`` escape hatch (set to any
    non-empty, non-false value) bypasses the check but logs a prominent
    warning at provider construction.
    """
    if is_local_endpoint(endpoint):
        return
    if is_tls_configured():
        return
    if is_insecure_endpoint_allowed():
        _log.warning(
            "INSECURE: Docker endpoint '%s' accepted without TLS "
            "(MODULO_DOCKER_ALLOW_INSECURE_ENDPOINT is set) — "
            "credentials will be transmitted in cleartext",
            endpoint,
        )
        return
    raise ValueError(
        f"Remote Docker endpoint '{endpoint}' requires TLS.  "
        "Set DOCKER_TLS_VERIFY=1 and DOCKER_CERT_PATH to a directory "
        "containing client certificates (cert.pem, key.pem, ca.pem), "
        "or use a local unix socket / the compose-internal proxy instead. "
        "See docs/security/bundled-runner-operator-guide.md §8 for details. "
        "To bypass this check for a private bridge endpoint, set "
        "MODULO_DOCKER_ALLOW_INSECURE_ENDPOINT=1 (see §8 for caveats)."
    )
