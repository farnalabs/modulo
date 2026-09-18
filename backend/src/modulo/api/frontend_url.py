"""Shared helper: resolve the browser-facing frontend base URL.

Used by SSO post-login redirects and MCP OAuth authorize links so that
both surfaces derive the same value from a single source of truth.

Resolution order (deployment-agnostic):

1. ``MODULO_FRONTEND_URL`` (env / ``settings.modulo_frontend_url``) — set
   only when the SPA and API are on *different* origins (split-origin
   deployment).
2. ``MODULO_PUBLIC_URL`` (env / ``settings.modulo_public_url``) — the
   correct default because the standard self-host topology has nginx
   serving the SPA and the API on the same origin.
3. ``http://localhost:5173`` when both are empty (local dev fallback).

Rationale: CORS origins are for CORS, not for routing the browser after
login.  Picking ``cors_origins.split(",")[0]`` is wrong on any deployment
where the marketing site appears first in the CORS list (e.g.
``modulo.run,app.modulo.run``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from modulo.settings import Settings

_DEFAULT_FALLBACK = "http://localhost:5173"


def resolve_frontend_url(settings: Settings) -> str:
    """Return the browser-facing frontend base URL, trailing slash stripped."""
    raw = settings.modulo_frontend_url or settings.modulo_public_url
    return (raw or _DEFAULT_FALLBACK).rstrip("/")
