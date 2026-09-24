"""Integration tests: the OIDC callback HTTP flow with a stubbed IdP (FAR-1080).

Drives ``GET /api/v1/auth/oidc/{provider}/callback`` end-to-end through the
real FastAPI app (ASGI transport + real Postgres sessions, RLS applied) after
stubbing the IdP's HTTP surface: the discovery document, the token endpoint and
the JWKS endpoint are served by a stub at the httpx boundary, while the ID
token itself is a real RS256 JWT signed against the stub's JWKS — so the REAL
``verify_id_token`` (signature + ``iss``/``aud``/``exp`` validation) runs.

This closes the gap the unit tests and FAR-1043 left open:
  - Unit tests mock ``oidc_process_callback`` / JIT CRUD — the HTTP callback
    route was only ever driven with a mocked token flow.
  - FAR-1043 (#797) proved the DB-level contract of ``jit_provision_user``
    against real Postgres, but never the HTTP path that calls it.

Proven here at the HTTP boundary: code exchange → ID-token verification →
FAR-855 join-gate enforcement → JIT provisioning (real Postgres, RLS,
invitation CAS) → the browser redirect with the FAR-837 frontend base
resolution (``MODULO_FRONTEND_URL``, falling back to ``MODULO_PUBLIC_URL`` —
never CORS ordering).

Honest scope — what a real IdP would exercise that these tests DO NOT:
  - a real IdP's token semantics (provider-specific claim shapes) — we control
    the signed token AND the JWKS, so real signature handling is exercised
    against a synthetic, well-formed IdP, not a foreign provider's quirks;
  - real TLS / an actual network transport to the IdP (httpx is stubbed at the
    client level, so the SSRF connect-pinning is neutralised for stub hosts);
  - the authorization redirect leg (``/oidc/{provider}/login``);
  - browser navigation after the callback redirect.
"""

import base64
import json
import uuid
from collections.abc import AsyncGenerator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import jwt as pyjwt
import pytest
import pytest_asyncio
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from modulo.api.dependencies import _get_engine, get_anonymous_plan_context, get_db_session, get_settings
from modulo.auth.secret_storage import encrypt_stored_secret

pytestmark = pytest.mark.integration

_STUB_HOST = "https://idp-stub.example.com"
_FRONTEND_BASE = "https://spa.example.com"
_PUBLIC_BASE = "https://api.example.com"
# Deliberately ordered so the FIRST CORS origin is the WRONG frontend base: if
# the FAR-837 bug (frontend URL picked from ``cors_origins.split(",")[0]``)
# returned, the redirect would go to the marketing site, not the SPA.
_CORS_WITH_WRONG_FIRST = f"https://marketing.example.com,{_PUBLIC_BASE}"
_SECRET_KEY = "a" * 32
# A valid Fernet key (matches the integration conftest's env default); the
# provider row's client_secret is encrypted at rest with this same key and
# decrypted by the real code path under test.
_FERNET_KEY = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="

_ALLOWLISTED_DOMAIN = "allowlisted.example.com"


# ---------------------------------------------------------------------------
# Stubbed IdP: real RS256 keys/JWKS, stubbed HTTP transport
# ---------------------------------------------------------------------------


def _gen_rsa_keypair() -> tuple[rsa.RSAPrivateKey, rsa.RSAPublicKey]:
    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
        backend=default_backend(),
    )
    return private_key, private_key.public_key()


_IDP_KEYPAIR: tuple[rsa.RSAPrivateKey, rsa.RSAPublicKey] = _gen_rsa_keypair()
_IDP_KID = "far1080-test-key-1"


def _pubkey_to_jwk(public_key: rsa.RSAPublicKey) -> dict[str, Any]:
    pub_numbers = public_key.public_numbers()

    def _int_to_b64url(num: int) -> str:
        byte_len = (num.bit_length() + 7) // 8
        return base64.urlsafe_b64encode(num.to_bytes(byte_len, byteorder="big")).rstrip(b"=").decode()

    return {
        "kty": "RSA",
        "n": _int_to_b64url(pub_numbers.n),
        "e": _int_to_b64url(pub_numbers.e),
        "alg": "RS256",
        "kid": _IDP_KID,
        "use": "sig",
    }


def _make_id_token(*, sub: str, email: str, issuer: str, audience: str) -> str:
    now = datetime.now(UTC)
    claims = {
        "sub": sub,
        "iss": issuer,
        "aud": audience,
        "email": email,
        "email_verified": True,
        "name": "Stub IdP User",
        "iat": now,
        "exp": now + timedelta(hours=1),
    }
    pem_key = _IDP_KEYPAIR[0].private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return str(pyjwt.encode(claims, pem_key, algorithm="RS256", headers={"kid": _IDP_KID}))


class _StubIdP:
    """Minimal stub IdP served AT the httpx client boundary.

    The flow's real clients (``pinned_async_client`` in ``sso.py`` and
    ``oidc_verify.py``) both construct ``httpx.AsyncClient`` — the proven
    repo pattern for stubbing the SSRF-pinned transport is patching that class
    and exposing a single stub client that answers by URL. Every request is
    recorded so tests can assert the code exchange actually posted the real
    authorization code, client credentials and redirect_uri.
    """

    def __init__(self, *, issuer: str, discovery_url: str, id_token: str) -> None:
        self.authority = issuer.rstrip("/")
        self.discovery_url = discovery_url
        self.id_token = id_token
        self.requests: list[tuple[str, str, dict[str, Any]]] = []

    def _doc(self) -> dict[str, Any]:
        return {
            "issuer": self.authority,
            "authorization_endpoint": f"{self.authority}/authorize",
            "token_endpoint": f"{self.authority}/token",
            "jwks_uri": f"{self.authority}/.well-known/jwks",
        }

    async def get(self, url: str, **kwargs: Any) -> httpx.Response:
        self.requests.append(("GET", url, kwargs))
        request = httpx.Request("GET", url)
        if url == self.discovery_url:
            return httpx.Response(200, request=request, json=self._doc())
        if url == f"{self.authority}/.well-known/jwks":
            return httpx.Response(200, request=request, json={"keys": [_pubkey_to_jwk(_IDP_KEYPAIR[1])]})
        raise AssertionError(f"Unexpected GET to stubbed IdP: {url}")

    async def post(self, url: str, **kwargs: Any) -> httpx.Response:
        self.requests.append(("POST", url, kwargs))
        if url == f"{self.authority}/token":
            # Don't echo the client secret back into the recorded kwargs.
            return httpx.Response(
                200,
                request=httpx.Request("POST", url),
                json={"id_token": self.id_token, "access_token": "stub-access-for-checks"},
            )
        raise AssertionError(f"Unexpected POST to stubbed IdP: {url}")

    def token_exchange_requests(self) -> list[tuple[str, str, dict[str, Any]]]:
        return [r for r in self.requests if r[0] == "POST" and r[1].endswith("/token")]


# ---------------------------------------------------------------------------
# DB seeding (superuser engine — bypasses RLS for setup), mirroring the
# helpers in test_sso_invite_consume.py.
# ---------------------------------------------------------------------------


async def _create_org(engine: AsyncEngine) -> uuid.UUID:
    org_id = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO organisations (id, name, slug, settings_json) VALUES (:id, :name, :slug, '{}'::json)"),
            {"id": str(org_id), "name": f"OIDC CB {org_id.hex[:8]}", "slug": f"oidccb-{org_id.hex[:8]}"},
        )
    return org_id


async def _create_inviter(engine: AsyncEngine, org_id: uuid.UUID) -> uuid.UUID:
    acc_id = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO accounts (id, email, display_name, password_hash, auth_provider, active) "
                "VALUES (:id, :email, :name, 'hash', 'local', true)"
            ),
            {"id": str(acc_id), "email": f"owner-{acc_id.hex[:8]}@example.com", "name": "Owner"},
        )
        await conn.execute(
            text(
                "INSERT INTO org_memberships (id, account_id, organisation_id, role) VALUES (:mid, :aid, :oid, 'admin')"
            ),
            {"mid": str(uuid.uuid4()), "aid": str(acc_id), "oid": str(org_id)},
        )
    return acc_id


async def _create_oidc_provider(
    engine: AsyncEngine,
    *,
    org_id: uuid.UUID,
    provider_id: str,
    discovery_url: str,
    client_id: str,
    allowed_domains: list[str],
    auto_provision: bool = True,
    default_role: str = "viewer",
) -> uuid.UUID:
    domains_json = json.dumps(allowed_domains)
    async with engine.begin() as conn:
        result = await conn.execute(
            text(
                "INSERT INTO sso_providers "
                "(id, organisation_id, provider_type, name, provider_id, client_id, "
                "client_secret, discovery_url, enabled, auto_provision, "
                "allowed_domains, default_role, group_mappings, preset) "
                "VALUES (:id, :oid, 'oidc', 'Stub OIDC', :pid, :cid, "
                ":secret, :disc, true, :auto, CAST(:domains AS json), :role, '[]'::json, 'custom') "
                "RETURNING id"
            ),
            {
                "id": str(uuid.uuid4()),
                "oid": str(org_id),
                "pid": provider_id,
                "cid": client_id,
                # The client_secret column is fernet-encrypted at rest and the
                # provider resolution DECRYPTS it with the instance key — this
                # handcrafted row must carry the real encrypted shape.
                "secret": encrypt_stored_secret(f"stub-client-secret-{org_id.hex[:8]}", _FERNET_KEY),
                "disc": discovery_url,
                "auto": auto_provision,
                "domains": domains_json,
                "role": default_role,
            },
        )
    return uuid.UUID(str(result.scalar_one()))


async def _create_invitation(
    engine: AsyncEngine,
    *,
    org_id: uuid.UUID,
    invited_by: uuid.UUID,
    email: str,
    org_role: str,
) -> uuid.UUID:
    inv_id = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO invitations "
                "(id, organisation_id, email, display_name, org_role, token_hash, invited_by, expires_at) "
                "VALUES (:id, :oid, :email, :name, :role, :hash, :by, :exp)"
            ),
            {
                "id": str(inv_id),
                "oid": str(org_id),
                "email": email,
                "name": f"Invited {email}",
                "role": org_role,
                "hash": f"oidc-http-{inv_id.hex[:16]}",
                "by": str(invited_by),
                "exp": datetime.now(UTC) + timedelta(hours=24),
            },
        )
    return inv_id


async def _get_account_id(engine: AsyncEngine, email: str) -> uuid.UUID | None:
    async with engine.connect() as conn:
        row = (
            await conn.execute(text("SELECT id FROM accounts WHERE email = :email"), {"email": email})
        ).scalar_one_or_none()
    return uuid.UUID(str(row)) if row is not None else None


async def _get_membership_role(engine: AsyncEngine, email: str, org_id: uuid.UUID) -> str | None:
    async with engine.connect() as conn:
        row = (
            (
                await conn.execute(
                    text(
                        "SELECT m.role FROM org_memberships m "
                        "JOIN accounts a ON a.id = m.account_id "
                        "WHERE a.email = :email AND m.organisation_id = :oid AND m.deactivated_at IS NULL"
                    ),
                    {"email": email, "oid": str(org_id)},
                )
            )
            .mappings()
            .first()
        )
    return str(row["role"]) if row else None


async def _get_invitation_state(engine: AsyncEngine, inv_id: uuid.UUID) -> dict[str, Any] | None:
    async with engine.connect() as conn:
        row = (
            (
                await conn.execute(
                    text("SELECT consumed_at, revoked_at FROM invitations WHERE id = :id"),
                    {"id": str(inv_id)},
                )
            )
            .mappings()
            .first()
        )
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# App client: models tests/integration/conftest.py::integration_client but
# carries the FAR-837 redirect-base settings under test.
# ---------------------------------------------------------------------------


class _AllFeaturesPlan:
    """Plan-context stub reporting every feature enabled — team-tier equivalent."""

    def feature_enabled(self, name: str) -> bool:
        return True

    def list_enabled_features(self) -> list[Any]:
        return []

    def tier(self) -> str:
        return "team"

    def has_license_key(self) -> bool:
        return True


def _build_settings(frontend_url: str) -> Any:
    from modulo.settings import Settings

    return Settings(
        database_url="postgresql+asyncpg://localhost/modulo_unused",
        secret_key=_SECRET_KEY,
        fernet_key=_FERNET_KEY,
        modulo_csrf_enabled=False,
        modulo_auth_rate_limit_enabled=False,
        redis_url="",
        modulo_admin_password="",
        # FAR-837 surface under test: split-origin deployment, with the FIRST
        # CORS origin deliberately the WRONG origin for the redirect.
        modulo_public_url=_PUBLIC_BASE,
        modulo_frontend_url=frontend_url,
        cors_origins=_CORS_WITH_WRONG_FIRST,
    )


async def _all_features_ctx() -> Any:
    return _AllFeaturesPlan()


async def _make_oidc_client(app: Any, app_engine: AsyncEngine, frontend_url: str) -> AsyncClient:
    """Build an ASGI client on the real app with production-shape overrides."""
    app.dependency_overrides[get_settings] = lambda: _build_settings(frontend_url)
    app.dependency_overrides[_get_engine] = lambda: app_engine

    factory = async_sessionmaker(app_engine, expire_on_commit=False)

    async def override_session() -> AsyncGenerator[AsyncSession, None]:
        async with factory() as session:
            yield session

    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[get_anonymous_plan_context] = _all_features_ctx

    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url=_PUBLIC_BASE, timeout=30.0)


@pytest_asyncio.fixture
async def oidc_app(app_engine: AsyncEngine):
    """Yield a client-builder, then clear the app's dependency overrides."""
    from modulo.api.main import app

    async def build(frontend_url: str = _FRONTEND_BASE) -> AsyncClient:
        return await _make_oidc_client(app, app_engine, frontend_url)

    try:
        yield build
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Small test-local helpers
# ---------------------------------------------------------------------------


def _callback_url(provider_id: str) -> str:
    """The redirect_uri the login route registers with the IdP."""
    return f"{_PUBLIC_BASE}/api/v1/auth/oidc/{provider_id}/callback"


def _fragment_params(url: str) -> dict[str, str]:
    fragment = url.split("#", 1)[1] if "#" in url else ""
    return dict(part.split("=", 1) for part in fragment.split("&") if "=" in part)


def _b64url_decode(segment: str) -> bytes:
    return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))


def _jwt_payload(token: str) -> dict[str, Any]:
    return json.loads(_b64url_decode(token.split(".")[1]))


def _signed_state(provider_id: str) -> str:
    from modulo.auth.sso import sign_state

    return sign_state(f"{provider_id}:{uuid.uuid4()}", _SECRET_KEY)


@dataclass
class _World:
    """Seed bundle: org + DB OIDC provider + matching stub IdP."""

    org_id: uuid.UUID
    provider_id: str
    client_id: str
    discovery_url: str
    issuer: str
    default_role: str = "viewer"
    stub: _StubIdP = field(init=False)

    def __post_init__(self) -> None:
        self.stub = _StubIdP(issuer=self.issuer, discovery_url=self.discovery_url, id_token="")


async def _seed_provider(db_engine: AsyncEngine, *, allowed_domains: list[str] | None) -> _World:
    """Create org + provider row + stub; returns the bundle for the test."""
    suffix = uuid.uuid4().hex[:10]
    org_id = await _create_org(db_engine)
    client_id = f"stub-client-{suffix}"
    discovery_url = f"{_STUB_HOST}/.well-known/openid-configuration-{suffix}"
    issuer = f"{_STUB_HOST}/issuer-{suffix}"
    provider_id = f"stubpid-{suffix}"
    await _create_oidc_provider(
        db_engine,
        org_id=org_id,
        provider_id=provider_id,
        discovery_url=discovery_url,
        client_id=client_id,
        allowed_domains=allowed_domains or [],
        default_role="viewer",
    )
    return _World(
        org_id=org_id,
        provider_id=provider_id,
        client_id=client_id,
        discovery_url=discovery_url,
        issuer=issuer,
    )


@contextmanager
def _idp_stubbed(world: _World, id_token: str):
    """Patch httpx.AsyncClient + stub-SSRF DNS so the flow talks to the stub IdP.

    Every ``httpx.AsyncClient(...)`` the flow constructs resolves to the stub,
    covering both the SSRF pinned transport (``sso.py``) and the JWKS fetch
    (``oidc_verify.py``). ``validate_outbound_url_async`` is the one dependency
    that performs real DNS resolution and is neutralised for stub hosts only.
    """
    world.stub.id_token = id_token
    with ExitStack() as stack:

        class _StubClient:
            def __init__(self, *args: object, **kwargs: object) -> None:
                # Accept (and ignore) whatever real transport/http2 kwargs the
                # SSRF pinned transport passes through.
                self._args = args

            async def __aenter__(self) -> _StubIdP:
                return world.stub

            async def __aexit__(self, *exc: object) -> None:
                return None

        cls = stack.enter_context(patch("httpx.AsyncClient"))
        cls.side_effect = _StubClient
        stack.enter_context(
            patch(
                "modulo.auth.sso.validate_outbound_url_async",
                new=AsyncMock(return_value=None),
            )
        )
        yield world.stub


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_oidc_callback_success_provisions_and_redirects_to_frontend_url(
    db_engine: AsyncEngine,
    app_engine: AsyncEngine,
    oidc_app,
) -> None:
    """Happy path: real callback → exchange, verify, JIT provision, redirect."""
    world = await _seed_provider(db_engine, allowed_domains=[_ALLOWLISTED_DOMAIN])
    email = f"good-{world.org_id.hex[:8]}@{_ALLOWLISTED_DOMAIN}"
    id_token = _make_id_token(sub=world.client_id, email=email, issuer=world.issuer, audience=world.client_id)

    client = await oidc_app()
    try:
        with (
            _idp_stubbed(world, id_token),
        ):
            resp = await client.get(
                _callback_url(world.provider_id),
                params={
                    "code": "authorization-code-CB",
                    "state": _signed_state(world.provider_id),
                },
            )
    finally:
        await client.aclose()

    # 1. Success is a browser redirect (never a JSON payload / never a 500).
    assert resp.status_code == 307, resp.text
    location = resp.headers["location"]
    # 2. Redirect base = MODULO_FRONTEND_URL. With CORS ordered as
    #    "marketing,api", picking cors_origins[0] (the FAR-837 bug) would
    #    produce https://marketing.example.com — assert it did NOT.
    assert location.startswith(f"{_FRONTEND_BASE}/auth/callback#"), location
    assert location.startswith("https://marketing.example.com") is False, (
        "redirect base resolved from CORS-origin ordering — the FAR-837 bug returned"
    )

    params = _fragment_params(location)
    assert set(params) == {"access_token"}

    # The issued access token identifies the provisioned identity in the org.
    access_payload = _jwt_payload(params["access_token"])
    assert access_payload["sub"] == email
    assert access_payload["org_id"] == str(world.org_id)
    assert access_payload["org_role"] == "viewer"  # provider's default_role
    # The refresh JWT is a real rotation-family body (create_refresh_token);
    # FAR-1197 moves it out of the fragment and into the httpOnly cookie.
    refresh_payload = _jwt_payload(resp.cookies["modulo_refresh"])
    assert refresh_payload["purpose"] == "refresh"
    assert isinstance(refresh_payload.get("token_family"), str)
    assert refresh_payload["token_family"]

    # 3. The code exchange posted the real values to the (stubbed) token endpoint.
    exchanges = world.stub.token_exchange_requests()
    assert len(exchanges) == 1
    _verb, url, kwargs = exchanges[0]
    assert url == f"{world.issuer}/token"
    assert kwargs["data"]["grant_type"] == "authorization_code"
    assert kwargs["data"]["code"] == "authorization-code-CB"
    assert kwargs["data"]["client_id"] == world.client_id
    assert kwargs["data"]["redirect_uri"] == _callback_url(world.provider_id)

    # 4. The JIT provision landed in the real DB against the provider's org.
    account_id = await _get_account_id(db_engine, email)
    assert account_id is not None, "SSO identity must be provisioned into accounts"
    role = await _get_membership_role(db_engine, email, world.org_id)
    assert role == "viewer"


async def test_oidc_callback_redirect_base_falls_back_to_public_url_when_frontend_unset(
    db_engine: AsyncEngine,
    app_engine: AsyncEngine,
    oidc_app,
) -> None:
    """MODULO_FRONTEND_URL empty → redirects to MODULO_PUBLIC_URL (same-origin)."""
    world = await _seed_provider(db_engine, allowed_domains=[_ALLOWLISTED_DOMAIN])
    email = f"fallback-{world.org_id.hex[:8]}@{_ALLOWLISTED_DOMAIN}"
    id_token = _make_id_token(sub=world.client_id, email=email, issuer=world.issuer, audience=world.client_id)

    client = await oidc_app(frontend_url="")
    try:
        with _idp_stubbed(world, id_token):
            resp = await client.get(
                _callback_url(world.provider_id),
                params={
                    "code": "authorization-code-FB",
                    "state": _signed_state(world.provider_id),
                },
            )
    finally:
        await client.aclose()

    assert resp.status_code == 307, resp.text
    assert resp.headers["location"].startswith(f"{_PUBLIC_BASE}/auth/callback#"), resp.headers["location"]


async def test_oidc_callback_join_gate_denial_is_clean_401_and_leaves_no_orphan_account(
    db_engine: AsyncEngine,
    app_engine: AsyncEngine,
    oidc_app,
) -> None:
    """FAR-855 denial over HTTP: clean user-facing 401, never a 500, no rows."""
    world = await _seed_provider(db_engine, allowed_domains=[_ALLOWLISTED_DOMAIN])
    denied_email = f"stranger-{world.org_id.hex[:8]}@notallowlisted.example.com"
    id_token = _make_id_token(sub=world.client_id, email=denied_email, issuer=world.issuer, audience=world.client_id)

    client = await oidc_app()
    try:
        with _idp_stubbed(world, id_token):
            resp = await client.get(
                _callback_url(world.provider_id),
                params={
                    "code": "authorization-code-DENIED",
                    "state": _signed_state(world.provider_id),
                },
            )
    finally:
        await client.aclose()

    # Clean user-facing failure — the join-gate message, NOT a 500 / crash page.
    assert resp.status_code == 401, resp.text
    from modulo.auth.sso import Constants

    assert resp.json()["detail"] == Constants.MSG_SSO_JOIN_DENIED

    # Fail-closed assertion against the REAL DB: no orphan Account row, no membership.
    assert await _get_account_id(db_engine, denied_email) is None, (
        "a join-gate-denied identity must leave no orphan Account row"
    )


async def test_oidc_callback_consumes_pending_invitation_for_non_allowlisted_domain(
    db_engine: AsyncEngine,
    app_engine: AsyncEngine,
    oidc_app,
) -> None:
    """Invitation path reachable through the FULL HTTP flow, not only via jit_provision_user.

    Provider is domain-allowlist-restricted, yet a pending invitation for a
    NON-allowlisted domain (role 'operator') is consumed by this HTTP surface
    and grants the invitation's role.
    """
    world = await _seed_provider(db_engine, allowed_domains=[_ALLOWLISTED_DOMAIN])
    admin_id = await _create_inviter(db_engine, world.org_id)
    invited_email = f"invited-http-{world.org_id.hex[:8]}@unlisted.example.com"
    inv_db_id = await _create_invitation(
        db_engine,
        org_id=world.org_id,
        invited_by=admin_id,
        email=invited_email,
        org_role="operator",
    )

    id_token = _make_id_token(sub=world.client_id, email=invited_email, issuer=world.issuer, audience=world.client_id)

    client = await oidc_app()
    try:
        with _idp_stubbed(world, id_token):
            resp = await client.get(
                _callback_url(world.provider_id),
                params={
                    "code": "authorization-code-INV",
                    "state": _signed_state(world.provider_id),
                },
            )
    finally:
        await client.aclose()

    assert resp.status_code == 307, resp.text  # the invite converted at sign-in

    # The invitation grants the role it specifies — via THIS HTTP path.
    role = await _get_membership_role(db_engine, invited_email, world.org_id)
    assert role == "operator", f"invitation role must win through the HTTP flow, got {role}"

    state_row = await _get_invitation_state(db_engine, inv_db_id)
    assert state_row is not None
    assert state_row["consumed_at"] is not None, "the HTTP callback must CAS-consume the invitation"
    assert state_row["revoked_at"] is None

    access_payload = _jwt_payload(_fragment_params(resp.headers["location"])["access_token"])
    assert access_payload["org_role"] == "operator"


async def test_oidc_callback_is_reachable_without_authorization_header(oidc_app) -> None:
    """A browser redirect target must not 401 "Not authenticated" when unauthenticated (FAR-847).

    Exercises exactly what a browser does: a bare GET with NO Authorization
    header. Missing code/state produces the route's own 400 — which proves the
    request got past the auth/feature dependencies to the handler at all.
    """
    client = await oidc_app()
    try:
        resp = await client.get("/api/v1/auth/oidc/any-provider/callback")  # no code, no state, no auth
    finally:
        await client.aclose()

    assert resp.status_code == 400, f"expected the route's own 400, got {resp.status_code}: {resp.text}"
    assert "code" in resp.json()["detail"]  # the route's own validation message
    assert "authenticated" not in resp.json()["detail"].lower(), (
        "a pre-auth browser redirect target surfaced an HTTP authentication error"
    )
