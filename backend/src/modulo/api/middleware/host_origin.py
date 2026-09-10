"""Loopback Host/Origin allowlist middleware (FAR-671 slice 3 / ADR 031).

Closes the DNS-rebinding class for the native single-port serving path: when
the API serves the SPA itself (``serve_spa`` ON, loopback bind), every
``Host`` header outside the loopback allowlist is a binding/rebinding
attempt, not a browser the operator controls — reject with 403. A present
``Origin`` header must also match the allowlist (cross-origin attacker
frames/pages never gain access).

LAN mode: ``MODULO_LAN_ORIGINS`` (comma-separated hostnames) widens the
allowlist with the explicitly configured LAN hostnames ONLY — an open allow
everything behaviour would re-open the rebinding class.

Only mounted when ``serve_spa`` is ON (``modulo.api.main._init_once_mount_spa``);
Docker/Fly are unaffected — nginx terminates the public Host there.

TODO(P3): the middleware is platform-independent; the launcher's Windows
bind seam is elsewhere.
"""

import logging
from collections.abc import Iterable
from urllib.parse import urlparse

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

_log = logging.getLogger(__name__)

LOOPBACK_HOSTS: frozenset[str] = frozenset({"localhost", "127.0.0.1", "::1", "[::1]"})
LAN_ORIGINS_ENV = "MODULO_LAN_ORIGINS"

__all__ = ["LAN_ORIGINS_ENV", "LOOPBACK_HOSTS", "HostOriginMiddleware", "split_lan_origins"]


def split_lan_origins(value: str | None) -> tuple[str, ...]:
    """Parse the comma-separated ``MODULO_LAN_ORIGINS`` value."""
    if not value:
        return ()
    return tuple(sorted({item.strip().lower() for item in value.split(",") if item.strip()}))


def _host_of_header(value: str) -> str:
    """``Host`` header → bare hostname (port stripped, IPv6 bracket form kept)."""
    host = value.strip()
    if not host:
        return ""
    if host.startswith("["):
        return host.split("]", 1)[0] + "]" if "]" in host else ""
    return host.partition(":")[0]


def _host_of_origin(origin: str) -> str:
    """``Origin`` header → bare hostname (``http://host[:port]``)."""
    parsed = urlparse(origin)
    return (parsed.hostname or "").lower()


class HostOriginMiddleware(BaseHTTPMiddleware):
    """403 every foreign Host/Origin; loopback + configured LAN only."""

    def __init__(self, app: object, lan_origins: Iterable[str] = ()) -> None:
        super().__init__(app)  # type: ignore[arg-type]
        self._lan_origins = frozenset(h.lower() for h in lan_origins)

    async def dispatch(self, request: Request, call_next: object) -> Response:
        assert callable(call_next)
        host = _host_of_header(request.headers.get("host", ""))
        if not self._host_allowed(host):
            _log.warning("middleware.host_origin_rejected kind=host host=%s", host or "<missing>")
            return JSONResponse({"detail": "Forbidden: host not allowed"}, status_code=403)
        origin = request.headers.get("origin")
        if origin and not self._origin_allowed(origin):
            _log.warning("middleware.host_origin_rejected kind=origin origin=%s", origin)
            return JSONResponse({"detail": "Forbidden: origin not allowed"}, status_code=403)
        return await call_next(request)  # type: ignore[no-any-return]

    def _effective_hosts(self) -> set[str]:
        return set(LOOPBACK_HOSTS) | self._lan_origins

    def _host_allowed(self, host: str) -> bool:
        if not host:
            return False
        candidates = {host.lower(), host.lower().rstrip("]")}
        return bool(candidates & self._effective_hosts())

    def _origin_allowed(self, origin: str) -> bool:
        origin_host = _host_of_origin(origin)
        if not origin_host:
            return False
        candidates = {origin_host, f"[{origin_host}]"}
        return bool(candidates & self._effective_hosts())
