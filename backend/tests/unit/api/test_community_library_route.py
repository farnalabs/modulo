"""Route-resolution tests for the hosted community library endpoints (FAR-363).

Regression coverage for a route-shadowing bug: ``library_router`` registers a
single-segment ``GET /api/v1/libraries/{primitive_id}`` (typed as a UUID). If
``library_router`` is mounted BEFORE ``community_library_router`` (prefix
``/api/v1/libraries/community``), FastAPI matches ``/api/v1/libraries/community``
against the UUID path param first and returns 422 (uuid_parsing) instead of
falling through to the community list route. These tests assert the community
routes actually resolve through the real app router.
"""

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from modulo.api.dependencies import get_db_session
from modulo.api.main import app
from modulo.api.routes import community_library as community_library_module
from modulo.auth.dependencies import get_current_tenant_user
from modulo.auth.jwt import TenantPrincipal
from modulo.db import settings_resolver

ORG_ID = uuid4()
ACCOUNT_ID = uuid4()
PRINCIPAL = TenantPrincipal(
    username="community-test@test",
    organisation_id=ORG_ID,
    account_id=ACCOUNT_ID,
    org_role="admin",
)


@pytest.fixture
def client(mock_session, monkeypatch):
    """Test client with tenant auth + mock DB, routed through the real app."""
    app.dependency_overrides[get_db_session] = lambda: mock_session
    app.dependency_overrides[get_current_tenant_user] = lambda: PRINCIPAL

    # The list endpoint is fail-open, but stub the service layer so the test
    # exercises pure route resolution without a real community manifest.
    async def _fake_list(*_a, **_k):
        return []

    async def _fake_manifest(*_a, **_k):
        return None

    monkeypatch.setattr(community_library_module, "list_community_entries", _fake_list)
    monkeypatch.setattr(community_library_module, "get_cached_manifest", _fake_manifest)

    async def _fake_entry(*_a, **_k):
        return {"id": "x", "type": "pipeline", "slug": "x", "content_sha256": None}

    monkeypatch.setattr(community_library_module, "get_community_entry", _fake_entry)

    # Avoid the authz enforcement DB lookup (and its mock-session coroutine
    # warnings) by stubbing the resolver; admin role is still allowed.
    async def _fake_enforce(*_a, **_k):
        return False

    monkeypatch.setattr(settings_resolver, "resolve_authz_enforce", _fake_enforce)
    transport = ASGITransport(app=app)
    client = AsyncClient(transport=transport, base_url="http://test")
    yield client
    app.dependency_overrides.clear()


@pytest.fixture
def mock_session():
    """Mock DB session whose ``begin()`` is a usable async context manager."""
    session = AsyncMock()
    session.begin = MagicMock()
    session.begin.return_value.__aenter__.return_value = session
    session.begin.return_value.__aexit__.return_value = None
    return session


async def test_community_list_route_resolves_not_shadowed(client):
    """GET /api/v1/libraries/community must reach the community handler.

    Before the router-ordering fix this returned 422 (uuid_parsing) because the
    library router's ``/{primitive_id}`` matched first.
    """
    resp = await client.get("/api/v1/libraries/community")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "items" in body
    assert "total" in body
    assert isinstance(body["items"], list)


async def test_community_detail_route_resolves(client):
    """GET /api/v1/libraries/community/<id> reaches the community detail handler.

    A 422 would indicate the library router's UUID param shadowed this route.
    """
    resp = await client.get(f"/api/v1/libraries/community/{uuid4()}")
    assert resp.status_code in (200, 404), resp.text
    assert resp.status_code != 422
