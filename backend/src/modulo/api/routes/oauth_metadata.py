"""OAuth discovery metadata for the remote MCP server (FAR-1476).

Stock MCP harnesses cannot attach to a remote MCP endpoint by hand: they
bootstrap OAuth 2.0 by fetching the RFC 9728 protected-resource metadata for
the resource they want, following its ``authorization_servers`` link to the
RFC 8414 authorization-server metadata, and only then starting the
authorization-code flow against the advertised endpoints (Claude Code 2.1.290
is the harness that observed this). Modulo served neither document — the
static SPA fallback answered ``/.well-known/*`` with ``index.html`` and
``content-type: text/html`` — so a stock config could never connect.

Design constraints:

* **Discovery only.** There is deliberately NO ``registration_endpoint``:
  RFC 7591 dynamic client registration is out of scope (FAR-1476), so a
  client is given an explicit ``--client-id`` instead. Advertising a
  registration endpoint we do not implement would send every harness down a
  404 path — a worse outcome than omitting it.
* **Pre-auth, org-less, unconditional.** These documents are served with no
  authentication and no org context, so they cannot consult the per-org
  ``mcp_server`` plan flag. That is correct: a harness must be able to read
  them *before* it holds any credential. The kill switch remains enforced
  where it always was — at ``/mcp`` (``McpAuthMiddleware`` →
  ``_call_next_if_feature_enabled``) and inside the OAuth protocol endpoints
  (``/mcp/oauth/{authorize,token,refresh}``, which check the flag against the
  resolved client's org). Discovery documents describe the server's
  existence; they grant nothing.
* **Registered before the SPA mount.** The single-port path mounts
  ``_SpaFallbackStaticFiles`` at ``/`` last, which answers any unmatched path
  with ``index.html``. These routes are included from ``main.py`` ahead of
  that mount (and ahead of the ``/mcp`` sub-app), so the fallback can never
  shadow them.
* **No OpenAPI surface.** The routes are declared ``include_in_schema=False``:
  they are protocol plumbing, never called by the typed frontend client, and
  keeping them out of ``openapi.json`` leaves the generated schema.ts
  contract untouched.

Standards: RFC 8414 (authorization-server metadata), RFC 9728 (protected
resource metadata), RFC 7636 (PKCE ``S256``), RFC 6749 §4.1 (authorization
code grant).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse

from modulo.auth.oauth import VALID_SCOPES
from modulo.core.runtime_config.key_bridge import get_public_url, public_url_is_configured
from modulo.settings import Settings, get_settings

router = APIRouter(tags=["oauth-metadata"])

#: RFC 8414 authorization-server metadata document.
AUTHORIZATION_SERVER_METADATA_PATH = "/.well-known/oauth-authorization-server"

#: RFC 9728 protected-resource metadata document (the bare path and the
#: ``/mcp`` path-suffix variant — RFC 9728 §3 permits both forms, and MCP
#: clients have been observed asking for either).
PROTECTED_RESOURCE_METADATA_PATH = "/.well-known/oauth-protected-resource"
PROTECTED_RESOURCE_MCP_PATH = "/.well-known/oauth-protected-resource/mcp"

#: Discovery documents describe a slow-moving configuration surface (the
#: public URL and the scope vocabulary). Five minutes keeps a reconnecting
#: harness from re-fetching on every session start while still picking up an
#: admin's ``MODULO_PUBLIC_URL`` override promptly. ``public`` is safe here:
#: the documents carry no per-user or per-org data.
_CACHE_CONTROL = "public, max-age=300"


def resolve_public_base_url(request: Request, settings: Settings | None = None) -> str:
    """The public origin every discovery URL is anchored to.

    ``MODULO_PUBLIC_URL`` (resolved through the runtime-config bridge so an
    admin ``PUT /api/v1/admin/runtime-config`` override wins over the
    boot-time value) is the authority when it is *configured* — that is what
    production and any split-origin deployment want.

    **Fallback:** when it is unconfigured (falsy, or left at the
    ``http://localhost:8000`` placeholder default — the same test the OAuth
    flow uses via :func:`public_url_is_configured`) the origin the request
    actually arrived on is used instead. The discovery routes below do NOT
    advertise that fallback: they fail visible with ``500`` (see
    :func:`_unconfigured_response`), matching the flow's refusal, so a stock
    harness is never pointed at a localhost issuer the flow would reject.
    The fallback remains for the ``WWW-Authenticate`` challenge, which is
    best-effort and anchored to the origin the client actually used.

    A trailing slash is stripped from either source so no advertised URL
    ever contains ``//``.
    """
    resolved = settings if settings is not None else get_settings()
    public_url = get_public_url(resolved)
    if not public_url_is_configured(resolved):
        return str(request.base_url).rstrip("/")
    return public_url.rstrip("/")


def _unconfigured_response() -> Response:
    """``500`` matching the OAuth flow's refusal when ``MODULO_PUBLIC_URL`` is unset.

    Discovery must not advertise an issuer the flow would refuse: a
    deployment that never configures ``MODULO_PUBLIC_URL`` gets the same
    visible failure here as it does from ``register_oauth_client`` and the
    authorize/token endpoints, instead of a document pointing at
    ``http://localhost:8000`` (review feedback on PR #1384).
    """
    return JSONResponse(
        {"error": "server_error", "detail": "MODULO_PUBLIC_URL must be configured"},
        status_code=500,
    )


def protected_resource_metadata_url(request: Request, settings: Settings | None = None) -> str:
    """Absolute RFC 9728 metadata URL for the ``WWW-Authenticate`` challenge.

    Used by the MCP ``401`` responses (``mcp_server``) so an unauthenticated
    client is told where the protected-resource metadata lives instead of
    being left to guess.
    """
    return f"{resolve_public_base_url(request, settings)}{PROTECTED_RESOURCE_METADATA_PATH}"


def _scopes() -> list[str]:
    """Advertised scopes — single source of truth is :data:`VALID_SCOPES`.

    Sorted so the document is byte-stable across processes (a ``frozenset``
    iterates in hash order, which differs between runs under hash
    randomisation).
    """
    return sorted(VALID_SCOPES)


def _json(payload: dict[str, object]) -> Response:
    return JSONResponse(payload, headers={"Cache-Control": _CACHE_CONTROL})


@router.get(AUTHORIZATION_SERVER_METADATA_PATH, include_in_schema=False)
def authorization_server_metadata(request: Request, settings: Settings = Depends(get_settings)) -> Response:
    """RFC 8414 authorization-server metadata for this Modulo instance.

    Unauthenticated by design (see the module docstring). No
    ``registration_endpoint``: there is no RFC 7591 dynamic client
    registration — clients pass an explicit ``--client-id``.
    """
    if not public_url_is_configured(settings):
        return _unconfigured_response()
    base = resolve_public_base_url(request, settings)
    return _json(
        {
            "issuer": base,
            "authorization_endpoint": f"{base}/mcp/oauth/authorize",
            "token_endpoint": f"{base}/mcp/oauth/token",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["client_secret_post", "client_secret_basic"],
            "scopes_supported": _scopes(),
        }
    )


def _protected_resource_payload(base: str) -> dict[str, object]:
    return {
        "resource": f"{base}/mcp/",
        "authorization_servers": [base],
        "bearer_methods_supported": ["header"],
        "scopes_supported": _scopes(),
    }


@router.get(PROTECTED_RESOURCE_METADATA_PATH, include_in_schema=False)
def protected_resource_metadata(request: Request, settings: Settings = Depends(get_settings)) -> Response:
    """RFC 9728 protected-resource metadata for ``/mcp/``."""
    if not public_url_is_configured(settings):
        return _unconfigured_response()
    return _json(_protected_resource_payload(resolve_public_base_url(request, settings)))


@router.get(PROTECTED_RESOURCE_MCP_PATH, include_in_schema=False)
def protected_resource_metadata_mcp(request: Request, settings: Settings = Depends(get_settings)) -> Response:
    """Path-suffix variant of the RFC 9728 document (``/.well-known/oauth-protected-resource/mcp``)."""
    if not public_url_is_configured(settings):
        return _unconfigured_response()
    return _json(_protected_resource_payload(resolve_public_base_url(request, settings)))
