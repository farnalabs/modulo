"""Contract tests for the OAuth discovery metadata (FAR-1476).

A stock MCP harness (Claude Code 2.1.290 is the observed one) cannot attach to
Modulo's remote MCP server unless the server answers RFC 9728
protected-resource metadata and, from its ``authorization_servers`` link, RFC
8414 authorization-server metadata — and answers them as *JSON*, rather than
letting the SPA's ``index.html`` static fallback swallow ``/.well-known/*``
with ``content-type: text/html``. These tests pin:

* both documents (plus the ``/mcp`` path-suffix variant) return ``200`` with
  ``application/json`` and the exact documented key set and values;
* ``registration_endpoint`` is ABSENT — there is deliberately no RFC 7591
  dynamic client registration, clients pass an explicit ``--client-id``;
* ``scopes_supported`` is sourced from ``modulo.auth.oauth.VALID_SCOPES``
  (never a second hand-written list) and is sorted for determinism;
* the endpoints are reachable with NO ``Authorization`` header (a harness
  reads them *before* it holds a credential);
* the MCP ``401`` carries ``WWW-Authenticate: Bearer resource_metadata="…"``,
  while a ``403``/policy denial does not;
* the documents are not shadowed by the SPA fallback (JSON, not ``text/html``).
"""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI, Response
from fastapi.testclient import TestClient

from modulo.api.main import _init_once_mount_spa, app
from modulo.api.routes.oauth_metadata import router as oauth_metadata_router
from modulo.auth.oauth import VALID_SCOPES
from modulo.core.runtime_config.store import get_runtime_config_store
from modulo.settings import Settings, get_settings

_PUBLIC = "https://modulo.example.com"

_AUTHORIZATION_SERVER_PATH = "/.well-known/oauth-authorization-server"
_PROTECTED_RESOURCE_PATH = "/.well-known/oauth-protected-resource"
_PROTECTED_RESOURCE_MCP_PATH = "/.well-known/oauth-protected-resource/mcp"

# ``sorted(VALID_SCOPES)`` — computed, never spelled out: the whole point of
# the contract is that the advertised list has ONE source of truth.
_EXPECTED_SCOPES = sorted(VALID_SCOPES)


def _settings_values() -> dict[str, Any]:
    return {
        "database_url": "postgresql+asyncpg://localhost/test",
        "secret_key": "a" * 32,
        "fernet_key": "a" * 32,
        "modulo_admin_password": "test",
        "redis_url": "",
    }


# NOTE: both overrides must take NO parameters. FastAPI analyses the signature
# of the override callable, so a ``**kwargs`` parameter is surfaced as a query
# field and every request answers ``422 query.overrides: Field required``.
def _make_settings() -> Settings:
    """A Settings instance with a pinned public URL."""
    return Settings(**_settings_values(), modulo_public_url=_PUBLIC)


def _make_settings_without_public_url() -> Settings:
    """A Settings instance with an EMPTY public URL."""
    return Settings(**_settings_values(), modulo_public_url="")


def _make_settings_localhost_default() -> Settings:
    """A Settings instance left at the ``http://localhost:8000`` placeholder default."""
    return Settings(**_settings_values(), modulo_public_url="http://localhost:8000")


def _content_type(resp: Any) -> str:
    """Media type without any charset parameter (``Any``: httpx vs starlette response types)."""
    return str(resp.headers["content-type"]).split(";")[0].strip()


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    """Main app, unauthenticated, with a pinned public URL.

    The runtime-config store is process-global and another test may leave a
    ``MODULO_PUBLIC_URL`` override behind, which would win over the pinned
    Settings value and make the assertions below non-deterministic — so the
    override is cleared for the duration and restored afterwards.
    """
    store = get_runtime_config_store()
    previous = store.get_override("MODULO_PUBLIC_URL")
    store.clear_override("MODULO_PUBLIC_URL")
    app.dependency_overrides[get_settings] = _make_settings
    yield TestClient(app)
    app.dependency_overrides.clear()
    if previous is None:
        store.clear_override("MODULO_PUBLIC_URL")
    else:
        store.set_override("MODULO_PUBLIC_URL", previous)


# ---------------------------------------------------------------------------
# RFC 8414 — authorization-server metadata
# ---------------------------------------------------------------------------


def test_authorization_server_metadata_shape(client: TestClient) -> None:
    resp = client.get(_AUTHORIZATION_SERVER_PATH)
    assert resp.status_code == 200
    assert _content_type(resp) == "application/json"

    body = resp.json()
    assert set(body) == {
        "issuer",
        "authorization_endpoint",
        "token_endpoint",
        "response_types_supported",
        "grant_types_supported",
        "code_challenge_methods_supported",
        "token_endpoint_auth_methods_supported",
        "scopes_supported",
    }
    # No RFC 7591 dynamic client registration: advertising an endpoint we do
    # not implement would send every harness down a 404 path.
    assert "registration_endpoint" not in body

    assert body["issuer"] == _PUBLIC
    assert body["authorization_endpoint"] == f"{_PUBLIC}/mcp/oauth/authorize"
    assert body["token_endpoint"] == f"{_PUBLIC}/mcp/oauth/token"
    assert body["response_types_supported"] == ["code"]
    assert body["grant_types_supported"] == ["authorization_code", "refresh_token"]
    assert body["code_challenge_methods_supported"] == ["S256"]
    assert body["token_endpoint_auth_methods_supported"] == [
        "client_secret_post",
        "client_secret_basic",
    ]
    assert body["scopes_supported"] == _EXPECTED_SCOPES


def test_authorization_server_metadata_cache_control(client: TestClient) -> None:
    resp = client.get(_AUTHORIZATION_SERVER_PATH)
    assert resp.headers["cache-control"] == "public, max-age=300"


# ---------------------------------------------------------------------------
# RFC 9728 — protected-resource metadata (both path forms)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", [_PROTECTED_RESOURCE_PATH, _PROTECTED_RESOURCE_MCP_PATH])
def test_protected_resource_metadata_shape(client: TestClient, path: str) -> None:
    resp = client.get(path)
    assert resp.status_code == 200
    assert _content_type(resp) == "application/json"

    body = resp.json()
    assert set(body) == {
        "resource",
        "authorization_servers",
        "bearer_methods_supported",
        "scopes_supported",
    }
    assert body["resource"] == f"{_PUBLIC}/mcp/"
    assert body["authorization_servers"] == [_PUBLIC]
    assert body["bearer_methods_supported"] == ["header"]
    assert body["scopes_supported"] == _EXPECTED_SCOPES
    assert resp.headers["cache-control"] == "public, max-age=300"


# ---------------------------------------------------------------------------
# Scope vocabulary: single source of truth
# ---------------------------------------------------------------------------


def test_scopes_supported_is_exactly_valid_scopes(client: TestClient) -> None:
    """Both documents advertise ``VALID_SCOPES`` — and nothing else.

    Asserted against the frozenset itself (not a literal) so widening
    ``VALID_SCOPES`` cannot silently leave the discovery documents behind.
    """
    for path in (_AUTHORIZATION_SERVER_PATH, _PROTECTED_RESOURCE_PATH, _PROTECTED_RESOURCE_MCP_PATH):
        scopes = client.get(path).json()["scopes_supported"]
        assert set(scopes) == set(VALID_SCOPES)
        # Sorted, so the document is byte-stable across processes (a
        # frozenset iterates in hash order).
        assert scopes == sorted(VALID_SCOPES)


# ---------------------------------------------------------------------------
# Pre-auth reachability (the lesson: a pre-auth route must be proven with an
# unauthenticated request — an authenticated test client proves nothing)
# ---------------------------------------------------------------------------


def test_discovery_needs_no_credentials(client: TestClient) -> None:
    """No Authorization header at all: a harness fetches these BEFORE it has a credential.

    If any of these answered 401 the whole OAuth bootstrap would be
    unreachable — exactly the failure mode the SSO pre-auth routes shipped
    with before they were given an explicit unauthenticated test.
    """
    for path in (_AUTHORIZATION_SERVER_PATH, _PROTECTED_RESOURCE_PATH, _PROTECTED_RESOURCE_MCP_PATH):
        resp = client.get(path)
        assert resp.status_code == 200
        assert resp.status_code != 401


@pytest.mark.parametrize("path", [_AUTHORIZATION_SERVER_PATH, _PROTECTED_RESOURCE_PATH, _PROTECTED_RESOURCE_MCP_PATH])
def test_discovery_fails_visible_when_public_url_is_unset(client: TestClient, path: str) -> None:
    """An EMPTY ``MODULO_PUBLIC_URL`` → ``500``, matching the OAuth flow.

    Discovery must not advertise an issuer the flow would refuse
    (``register_oauth_client`` answers ``500`` for an unconfigured public
    URL), so it fails the same visible way instead of pointing a stock
    harness at a localhost URL (review feedback on PR #1384).
    """
    app.dependency_overrides[get_settings] = _make_settings_without_public_url
    resp = client.get(path)
    assert resp.status_code == 500
    assert resp.json()["detail"] == "MODULO_PUBLIC_URL must be configured"


@pytest.mark.parametrize("path", [_AUTHORIZATION_SERVER_PATH, _PROTECTED_RESOURCE_PATH, _PROTECTED_RESOURCE_MCP_PATH])
def test_discovery_fails_visible_when_public_url_is_the_localhost_default(client: TestClient, path: str) -> None:
    """The ``http://localhost:8000`` Settings default counts as unconfigured → ``500``.

    The flow refuses that exact value, so discovery must too: a prod
    deployment that never sets ``MODULO_PUBLIC_URL`` must not advertise
    ``http://localhost:8000`` issuer/endpoint URLs.
    """
    app.dependency_overrides[get_settings] = _make_settings_localhost_default
    resp = client.get(path)
    assert resp.status_code == 500
    assert resp.json()["detail"] == "MODULO_PUBLIC_URL must be configured"


def test_challenge_falls_back_to_request_origin_when_public_url_unset(client: TestClient) -> None:
    """The ``WWW-Authenticate`` challenge stays best-effort when unconfigured.

    The discovery routes fail visible (above); the challenge still points the
    client at the metadata URL on the origin it actually used. ``client`` is
    taken only so the process-global ``MODULO_PUBLIC_URL`` override is cleared
    for the duration (see its fixture docstring).
    """
    from modulo.api.mcp_server import _resource_metadata_challenge

    with patch("modulo.api.routes.oauth_metadata.get_settings", _make_settings_without_public_url):
        request = MagicMock()
        request.base_url = "http://dev.example.com/"
        headers = _resource_metadata_challenge(request)

    assert headers == {
        "WWW-Authenticate": ('Bearer resource_metadata="http://dev.example.com/.well-known/oauth-protected-resource"')
    }


# ---------------------------------------------------------------------------
# Not shadowed by the SPA fallback
# ---------------------------------------------------------------------------


def test_discovery_is_not_shadowed_by_the_spa_fallback(tmp_path: Path) -> None:
    """The SPA ``/`` mount must not answer ``/.well-known/*`` with index.html.

    Built the same way ``main.py`` builds the real app: the discovery router
    is registered FIRST, then the flag-gated SPA fallback is mounted at ``/``.
    The control request proves the fallback really is mounted (it answers an
    arbitrary deep link with HTML), so the JSON response above it is evidence
    of routing order, not of a missing mount.
    """
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html><body>SPA-SHELL</body></html>", encoding="utf-8")

    test_app = FastAPI()
    test_app.include_router(oauth_metadata_router)
    # A pinned public URL: discovery now fails visible (500) when unconfigured,
    # so the routing-order proof needs a configured origin.
    test_app.dependency_overrides[get_settings] = _make_settings
    mounted = _init_once_mount_spa(test_app, {"MODULO_SERVE_SPA": "1", "MODULO_FRONTEND_DIST": str(dist)})
    assert mounted is True

    # base_url matches the loopback Host/Origin allowlist the SPA mount adds.
    with TestClient(test_app, base_url="http://127.0.0.1:18000") as spa_client:
        discovery = spa_client.get(_AUTHORIZATION_SERVER_PATH)
        deep_link = spa_client.get("/some/spa/deep/link")

    assert discovery.status_code == 200
    assert _content_type(discovery) == "application/json"
    assert "text/html" not in discovery.headers["content-type"]
    # Control: the fallback IS mounted and does answer other paths with HTML.
    assert deep_link.status_code == 200
    assert _content_type(deep_link) == "text/html"


# ---------------------------------------------------------------------------
# WWW-Authenticate on the MCP 401
# ---------------------------------------------------------------------------


def test_mcp_401_carries_resource_metadata_challenge() -> None:
    """The unauthenticated MCP ``401`` tells the client where to find the metadata.

    ``_extract_bearer_token`` builds this response; a stock harness reads the
    challenge instead of having to be told the well-known URL by hand.
    """
    from modulo.api.mcp_server import build_mcp_asgi_app

    with (
        patch("modulo.api.routes.oauth_metadata.get_settings", _make_settings),
        TestClient(build_mcp_asgi_app()) as mcp_client,
    ):
        resp = mcp_client.get("/mcp")

    assert resp.status_code == 401
    # The JSON body is unchanged by this feature.
    assert resp.json() == {"error": "unauthorized", "detail": "Bearer token required"}

    challenge = resp.headers["WWW-Authenticate"]
    assert challenge == f'Bearer resource_metadata="{_PUBLIC}/.well-known/oauth-protected-resource"'


def test_mcp_401_challenge_absent_on_policy_denials() -> None:
    """A ``403`` is an authenticated principal being denied — never a challenge."""
    from modulo.api.mcp_server import _with_resource_metadata

    forbidden = Response(
        '{"error":"forbidden","detail":"No org role claim on token"}',
        status_code=403,
        media_type="application/json",
    )
    out = _with_resource_metadata(forbidden, MagicMock())
    assert out.status_code == 403
    assert "WWW-Authenticate" not in out.headers


def test_mcp_401_challenge_added_by_the_boundary_helper() -> None:
    """The request-less token-family helper's 401 gets the header at the middleware boundary."""
    from modulo.api.mcp_server import _with_resource_metadata

    unauthorized = Response(
        '{"error":"unauthorized","detail":"Token family revoked"}',
        status_code=401,
        media_type="application/json",
    )
    with patch("modulo.api.routes.oauth_metadata.get_settings", _make_settings):
        out = _with_resource_metadata(unauthorized, MagicMock())
    assert out.status_code == 401
    assert out.headers["WWW-Authenticate"] == (
        f'Bearer resource_metadata="{_PUBLIC}/.well-known/oauth-protected-resource"'
    )


def test_mcp_401_challenge_leaves_5xx_alone() -> None:
    """A 503 outage reply is not an auth challenge — it must stay untouched."""
    from modulo.api.mcp_server import _with_resource_metadata

    unavailable = Response(
        '{"error":"service_unavailable","detail":"Database temporarily unavailable."}',
        status_code=503,
        media_type="application/json",
    )
    out = _with_resource_metadata(unavailable, MagicMock())
    assert out.status_code == 503
    assert "WWW-Authenticate" not in out.headers


def test_missing_bearer_response_built_by_extract_bearer_token_has_the_header() -> None:
    """The response object itself (not just what the middleware returns) carries the challenge."""
    from modulo.api.mcp_server import _extract_bearer_token

    request = MagicMock()
    request.headers = {"Authorization": "Basic abc"}
    with patch("modulo.api.routes.oauth_metadata.get_settings", _make_settings):
        token, err = _extract_bearer_token(request)

    assert token is None
    assert err is not None
    assert err.status_code == 401
    assert err.headers["WWW-Authenticate"] == (
        f'Bearer resource_metadata="{_PUBLIC}/.well-known/oauth-protected-resource"'
    )
