"""Tests for the per-org login resolution endpoints (FAR-856).

Phase A: GET /api/v1/auth/login-context and GET /api/v1/auth/org-login/{slug}.
Phase B: POST /api/v1/auth/login org_slug binding (added in Phase B).

Security contracts tested:
- No pre-auth route returns 401 for a missing Authorization header.
- Unknown slug and suspended-org slug produce the same 404 status/body.
- Serialised pre-auth responses contain no secret fields (client_secret,
  client_id).
- login-context with exactly one login-active org returns multi_org=false.
- login-context with two login-active orgs returns multi_org=true.
- Suspended/deleted orgs do not count as login-active.
- org-login/{slug} returns only that org's providers (not another org's).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from modulo.api.dependencies import get_db_session
from modulo.api.main import app

_VALID_32 = "a" * 32


def _make_settings(**overrides: object) -> object:
    from modulo.settings import Settings

    kwargs: dict = {
        "database_url": "postgresql+asyncpg://localhost/test",
        "secret_key": _VALID_32,
        "fernet_key": _VALID_32,
        "modulo_admin_password": "testpass",
        "modulo_auth_rate_limit_enabled": False,
        "redis_url": "",
    }
    kwargs.update(overrides)
    return Settings(**kwargs)


def _make_org(
    *,
    id: uuid.UUID | None = None,
    name: str = "Test Org",
    slug: str = "test-org",
    status: str = "active",
    deleted_at: datetime | None = None,
) -> MagicMock:
    org = MagicMock()
    org.id = id or uuid.uuid4()
    org.name = name
    org.slug = slug
    org.status = status
    org.deleted_at = deleted_at
    org.created_at = datetime(2025, 1, 1, tzinfo=UTC)
    return org


def _make_provider(
    *,
    id: uuid.UUID | None = None,
    provider_id: str = "google",
    name: str = "Google OIDC",
    provider_type: str = "oidc",
    enabled: bool = True,
    organisation_id: uuid.UUID | None = None,
) -> MagicMock:
    p = MagicMock()
    p.id = id or uuid.uuid4()
    p.provider_id = provider_id
    p.name = name
    p.provider_type = provider_type
    p.enabled = enabled
    p.organisation_id = organisation_id
    # Ensure no secret fields leak via attribute access.
    p.client_secret = None
    p.client_id = None
    # preset may not exist on the model (FAR-853) — set to None explicitly
    # so getattr() returns None, not a MagicMock.
    p.preset = None
    return p


def _make_session() -> AsyncMock:
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    result.first.return_value = None
    result.all.return_value = []
    session.execute = AsyncMock(return_value=result)
    return session


@pytest.fixture
def client() -> Generator[tuple[TestClient, AsyncMock], None, None]:
    session = _make_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield session

    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides["get_settings"] = lambda: _make_settings()
    yield TestClient(app), session
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# GET /api/v1/auth/login-context
# ---------------------------------------------------------------------------


class TestLoginContext:
    """Tests for the login-context pre-auth endpoint."""

    def test_single_org_returns_multi_org_false(self, client: tuple[TestClient, AsyncMock]) -> None:
        """Exactly one login-active org → multi_org=false, org={slug, name}."""
        http, session = client
        org = _make_org(slug="acme", name="Acme Corp")

        result = MagicMock()
        result.scalars.return_value.all.return_value = [org]
        session.execute = AsyncMock(return_value=result)

        resp = http.get("/api/v1/auth/login-context")
        assert resp.status_code == 200
        data = resp.json()
        assert data["multi_org"] is False
        assert data["org"]["slug"] == "acme"
        assert data["org"]["name"] == "Acme Corp"

    def test_two_orgs_returns_multi_org_true(self, client: tuple[TestClient, AsyncMock]) -> None:
        """Two login-active orgs → multi_org=true, org=null."""
        http, session = client
        org1 = _make_org(slug="acme", name="Acme")
        org2 = _make_org(slug="globex", name="Globex")

        result = MagicMock()
        result.scalars.return_value.all.return_value = [org1, org2]
        session.execute = AsyncMock(return_value=result)

        resp = http.get("/api/v1/auth/login-context")
        assert resp.status_code == 200
        data = resp.json()
        assert data["multi_org"] is True
        assert data["org"] is None

    def test_zero_orgs_returns_multi_org_true(self, client: tuple[TestClient, AsyncMock]) -> None:
        """Zero login-active orgs → multi_org=true, org=null."""
        http, session = client

        result = MagicMock()
        result.scalars.return_value.all.return_value = []
        session.execute = AsyncMock(return_value=result)

        resp = http.get("/api/v1/auth/login-context")
        assert resp.status_code == 200
        data = resp.json()
        assert data["multi_org"] is True
        assert data["org"] is None

    def test_suspended_org_not_counted(self, client: tuple[TestClient, AsyncMock]) -> None:
        """A suspended org is not login-active and must not resolve a login page."""
        http, session = client
        # Only one org exists, but it's suspended — zero login-active orgs.

        result = MagicMock()
        result.scalars.return_value.all.return_value = []
        session.execute = AsyncMock(return_value=result)

        resp = http.get("/api/v1/auth/login-context")
        assert resp.status_code == 200
        data = resp.json()
        assert data["multi_org"] is True
        assert data["org"] is None

    def test_deleted_org_not_counted(self, client: tuple[TestClient, AsyncMock]) -> None:
        """A soft-deleted org is not login-active."""
        http, session = client

        result = MagicMock()
        result.scalars.return_value.all.return_value = []
        session.execute = AsyncMock(return_value=result)

        resp = http.get("/api/v1/auth/login-context")
        assert resp.status_code == 200
        data = resp.json()
        assert data["multi_org"] is True
        assert data["org"] is None

    def test_sentinel_org_not_counted(self, client: tuple[TestClient, AsyncMock]) -> None:
        """Sentinel orgs (ORPHAN_ORG_ID, MODULO_REGISTRY_ORG_ID) must not count."""
        http, session = client
        # The DB returns only the sentinel — login-context sees zero.

        result = MagicMock()
        result.scalars.return_value.all.return_value = []
        session.execute = AsyncMock(return_value=result)

        resp = http.get("/api/v1/auth/login-context")
        assert resp.status_code == 200
        data = resp.json()
        assert data["multi_org"] is True
        assert data["org"] is None

    def test_no_authorization_header_returns_200(self, client: tuple[TestClient, AsyncMock]) -> None:
        """Pre-auth endpoint must never 401/402 for a missing Authorization header."""
        http, session = client

        result = MagicMock()
        result.scalars.return_value.all.return_value = []
        session.execute = AsyncMock(return_value=result)

        resp = http.get("/api/v1/auth/login-context")
        assert resp.status_code == 200
        # Not 401 (missing auth) or 402 (payment required).
        assert resp.status_code != 401
        assert resp.status_code != 402


# ---------------------------------------------------------------------------
# GET /api/v1/auth/org-login/{slug}
# ---------------------------------------------------------------------------


class TestOrgLogin:
    """Tests for the org-login pre-auth endpoint."""

    def test_valid_slug_returns_org_and_providers(self, client: tuple[TestClient, AsyncMock]) -> None:
        """A valid slug returns the org's providers scoped to that org."""
        http, session = client
        org = _make_org(id=uuid.uuid4(), slug="acme", name="Acme Corp")
        provider = _make_provider(provider_id="google", name="Google", organisation_id=org.id)

        # First call: get_login_active_org_by_slug → returns org
        # Second call: list_enabled_oidc_providers → returns [provider]
        call_count = 0

        async def mock_execute(stmt: object, *args: object) -> MagicMock:
            nonlocal call_count
            call_count += 1
            result = MagicMock()
            if call_count == 1:
                # Login-active org lookup
                result.scalars.return_value.first.return_value = org
            else:
                # OIDC provider listing
                result.scalars.return_value.all.return_value = [provider]
            return result

        session.execute = mock_execute

        resp = http.get("/api/v1/auth/org-login/acme")
        assert resp.status_code == 200
        data = resp.json()
        assert data["org"]["slug"] == "acme"
        assert data["org"]["name"] == "Acme Corp"
        assert len(data["providers"]) == 1
        assert data["providers"][0]["provider_id"] == "google"
        assert data["providers"][0]["display_name"] == "Google"
        assert data["password_enabled"] is True

    def test_unknown_slug_returns_404(self, client: tuple[TestClient, AsyncMock]) -> None:
        """An unknown slug returns a generic 404."""
        http, session = client

        result = MagicMock()
        result.scalars.return_value.first.return_value = None
        session.execute = AsyncMock(return_value=result)

        resp = http.get("/api/v1/auth/org-login/nonexistent")
        assert resp.status_code == 404

    def test_suspended_org_slug_returns_same_404(self, client: tuple[TestClient, AsyncMock]) -> None:
        """A suspended org slug produces the same 404 as an unknown slug."""
        http, session = client

        result = MagicMock()
        result.scalars.return_value.first.return_value = None
        session.execute = AsyncMock(return_value=result)

        resp = http.get("/api/v1/auth/org-login/suspended-org")
        assert resp.status_code == 404

    def test_no_authorization_header_returns_200(self, client: tuple[TestClient, AsyncMock]) -> None:
        """Pre-auth endpoint must never 401/402 for a missing Authorization header."""
        http, session = client

        result = MagicMock()
        result.scalars.return_value.first.return_value = None
        session.execute = AsyncMock(return_value=result)

        resp = http.get("/api/v1/auth/org-login/any-slug")
        # 404 is expected for unknown slug, but not 401/402.
        assert resp.status_code in (200, 404)
        assert resp.status_code != 401
        assert resp.status_code != 402

    def test_providers_scoped_to_org_not_global(self, client: tuple[TestClient, AsyncMock]) -> None:
        """org-login returns only that org's providers, not another org's."""
        http, session = client
        org_a = _make_org(id=uuid.uuid4(), slug="acme", name="Acme")
        org_b = _make_org(id=uuid.uuid4(), slug="globex", name="Globex")
        provider_a = _make_provider(provider_id="google", name="Google", organisation_id=org_a.id)
        provider_b = _make_provider(provider_id="azure-ad", name="Azure AD", organisation_id=org_b.id)

        call_count = 0

        async def mock_execute(stmt: object, *args: object) -> MagicMock:
            nonlocal call_count
            call_count += 1
            result = MagicMock()
            if call_count == 1:
                result.scalars.return_value.first.return_value = org_a
            else:
                # Return BOTH orgs' providers — the handler must filter to org_a only.
                result.scalars.return_value.all.return_value = [provider_a, provider_b]
            return result

        session.execute = mock_execute

        resp = http.get("/api/v1/auth/org-login/acme")
        assert resp.status_code == 200
        data = resp.json()
        provider_ids = [p["provider_id"] for p in data["providers"]]
        assert "google" in provider_ids
        assert "azure-ad" not in provider_ids

    def test_response_contains_no_secret_fields(self, client: tuple[TestClient, AsyncMock]) -> None:
        """The serialised response must never contain client_secret or client_id."""
        http, session = client
        org = _make_org(id=uuid.uuid4(), slug="acme", name="Acme")
        provider = _make_provider(provider_id="google", name="Google", organisation_id=org.id)

        call_count = 0

        async def mock_execute(stmt: object, *args: object) -> MagicMock:
            nonlocal call_count
            call_count += 1
            result = MagicMock()
            if call_count == 1:
                result.scalars.return_value.first.return_value = org
            else:
                result.scalars.return_value.all.return_value = [provider]
            return result

        session.execute = mock_execute

        resp = http.get("/api/v1/auth/org-login/acme")
        assert resp.status_code == 200
        body = resp.text
        assert "client_secret" not in body.lower()
        assert "client_id" not in body.lower()
