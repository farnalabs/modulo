"""Tests for the admin org management API."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient, ASGITransport

from modulo.api.main import app
from modulo.api.dependencies import get_db_session
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.db.models.organisation import Organisation


ORG_ID = uuid4()
USER_ID = uuid4()
ADMIN_PRINCIPAL = AuthenticatedPrincipal(
    username="admin@test",
    organisation_id=ORG_ID,
    user_id=USER_ID,
    org_role="admin",
)
VIEWER_PRINCIPAL = AuthenticatedPrincipal(
    username="viewer@test",
    organisation_id=ORG_ID,
    user_id=uuid4(),
    org_role="viewer",
)


@pytest.fixture
def mock_session():
    """Create a mock DB session for testing."""
    from unittest.mock import AsyncMock, MagicMock

    session = AsyncMock()
    session.begin.return_value.__aenter__.return_value = session
    session.execute.return_value.scalar_one_or_none.return_value = None
    session.execute.return_value.scalar_one.return_value = 0
    session.execute.return_value.scalars.return_value.all.return_value = []
    session.flush.return_value = None
    return session


@pytest.fixture
def client_admin(mock_session):
    """Test client with admin auth + mock DB."""
    app.dependency_overrides[get_db_session] = lambda: mock_session
    app.dependency_overrides[get_current_user] = lambda: ADMIN_PRINCIPAL
    transport = ASGITransport(app=app)
    client = AsyncClient(transport=transport, base_url="http://test")
    yield client
    app.dependency_overrides.clear()


@pytest.fixture
def client_viewer(mock_session):
    """Test client with viewer auth."""
    app.dependency_overrides[get_db_session] = lambda: mock_session
    app.dependency_overrides[get_current_user] = lambda: VIEWER_PRINCIPAL
    transport = ASGITransport(app=app)
    client = AsyncClient(transport=transport, base_url="http://test")
    yield client
    app.dependency_overrides.clear()


# ── POST /api/v1/admin/orgs ──────────────────────────────────────────────


@pytest.mark.anyio
async def test_create_org_success(client_admin, mock_session):
    """Admin can create an org with valid name and slug."""
    import modulo.api.routes.admin_orgs as admin_orgs

    original_get_slug = admin_orgs.get_organisation_by_slug
    admin_orgs.get_organisation_by_slug = AsyncMock(return_value=None)

    original_create = admin_orgs.create_organisation

    async def mock_create_org(session, *, name, slug, created_by):
        org = Organisation(
            id=uuid4(),
            name=name,
            slug=slug,
            status="active",
            created_at=datetime.now(timezone.utc),
        )
        return org

    admin_orgs.create_organisation = mock_create_org

    try:
        resp = await client_admin.post(
            "/api/v1/admin/orgs",
            json={"name": "Test Org", "slug": "test-org"},
        )
        assert resp.status_code == 201, f"Expected 201, got {resp.status_code}: {resp.text}"
        data = resp.json()
        assert data["name"] == "Test Org"
        assert data["slug"] == "test-org"
        assert data["status"] == "active"
        assert UUID(data["id"])
    finally:
        admin_orgs.get_organisation_by_slug = original_get_slug
        admin_orgs.create_organisation = original_create


@pytest.mark.anyio
async def test_create_org_viewer_forbidden(client_viewer):
    """Non-admin users get 403 when creating orgs."""
    resp = await client_viewer.post(
        "/api/v1/admin/orgs",
        json={"name": "Test Org", "slug": "test-org"},
    )
    assert resp.status_code == 403


@pytest.mark.anyio
async def test_create_org_invalid_slug(client_admin):
    """Invalid slug format returns 422."""
    resp = await client_admin.post(
        "/api/v1/admin/orgs",
        json={"name": "Test Org", "slug": "UPPERCASE-SLUG"},
    )
    assert resp.status_code in (422,)


@pytest.mark.anyio
async def test_create_org_slug_collision(client_admin):
    """Duplicate slug returns 409."""
    import modulo.api.routes.admin_orgs as admin_orgs

    existing = Organisation(
        id=uuid4(), name="Existing", slug="taken", status="active",
        created_at=datetime.now(timezone.utc),
    )
    original = admin_orgs.get_organisation_by_slug
    admin_orgs.get_organisation_by_slug = AsyncMock(return_value=existing)

    try:
        resp = await client_admin.post(
            "/api/v1/admin/orgs",
            json={"name": "Test", "slug": "taken"},
        )
        assert resp.status_code == 409
    finally:
        admin_orgs.get_organisation_by_slug = original


@pytest.mark.anyio
async def test_create_org_duplicate_slug_orig(client_admin):
    """Duplicate slug returns 409 (alternate path)."""
    import modulo.api.routes.admin_orgs as admin_orgs

    existing_org = Organisation(
        id=uuid4(), name="Existing", slug="dup-slug", status="active",
        created_at=datetime.now(timezone.utc),
    )
    original = admin_orgs.get_organisation_by_slug
    admin_orgs.get_organisation_by_slug = AsyncMock(return_value=existing_org)

    try:
        resp = await client_admin.post(
            "/api/v1/admin/orgs",
            json={"name": "Test Org", "slug": "dup-slug"},
        )
        assert resp.status_code == 409
    finally:
        admin_orgs.get_organisation_by_slug = original


# ── POST /api/v1/admin/orgs/{org_id}/users ───────────────────────────────


@pytest.mark.anyio
async def test_create_org_user_success(client_admin):
    """Create a user in a specified org."""
    import modulo.api.routes.admin_orgs as admin_orgs
    from modulo.db.models.user import User

    target_org_id = uuid4()

    target_org = Organisation(
        id=target_org_id, name="Target Org", slug="target", status="active",
        created_at=datetime.now(timezone.utc),
    )

    original_get_org = admin_orgs.get_organisation
    admin_orgs.get_organisation = AsyncMock(return_value=target_org)

    original_get_user = admin_orgs.get_user_by_email
    admin_orgs.get_user_by_email = AsyncMock(return_value=None)

    original_create_user = admin_orgs.create_user

    async def mock_create_user(session, *, org_id, email, display_name, password_hash, org_role, auth_provider="local"):
        user = User(
            id=uuid4(),
            organisation_id=org_id,
            email=email,
            display_name=display_name,
            org_role=org_role,
            auth_provider=auth_provider,
            created_at=datetime.now(timezone.utc),
        )
        return user

    admin_orgs.create_user = mock_create_user

    try:
        resp = await client_admin.post(
            f"/api/v1/admin/orgs/{target_org_id}/users",
            json={
                "email": "newuser@example.com",
                "display_name": "New User",
                "password": "securepassword123",
                "org_role": "runner",
            },
        )
        assert resp.status_code == 201, f"Expected 201, got {resp.status_code}: {resp.text}"
        data = resp.json()
        assert data["email"] == "newuser@example.com"
        assert data["org_role"] == "runner"
        assert UUID(data["id"])
    finally:
        admin_orgs.get_organisation = original_get_org
        admin_orgs.get_user_by_email = original_get_user
        admin_orgs.create_user = original_create_user


@pytest.mark.anyio
async def test_create_org_user_org_not_found(client_admin):
    """Non-existent org returns 404."""
    import modulo.api.routes.admin_orgs as admin_orgs

    original = admin_orgs.get_organisation
    admin_orgs.get_organisation = AsyncMock(return_value=None)

    try:
        resp = await client_admin.post(
            f"/api/v1/admin/orgs/{uuid4()}/users",
            json={
                "email": "user@example.com",
                "display_name": "User",
                "password": "securepassword123",
                "org_role": "runner",
            },
        )
        assert resp.status_code == 404
    finally:
        admin_orgs.get_organisation = original


@pytest.mark.anyio
async def test_create_org_user_weak_password(client_admin):
    """Weak password returns 422."""
    resp = await client_admin.post(
        f"/api/v1/admin/orgs/{uuid4()}/users",
        json={
            "email": "user@example.com",
            "display_name": "User",
            "password": "short",
            "org_role": "runner",
        },
    )
    assert resp.status_code == 422


@pytest.mark.anyio
async def test_create_org_user_invalid_role(client_admin):
    """Invalid role returns 422."""
    resp = await client_admin.post(
        f"/api/v1/admin/orgs/{uuid4()}/users",
        json={
            "email": "user@example.com",
            "display_name": "User",
            "password": "securepassword123",
            "org_role": "superadmin",
        },
    )
    assert resp.status_code == 422
