"""Tests for the admin org management API."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from modulo.api.dependencies import get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_tenant_user, get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal, TenantPrincipal
from modulo.db.models.organisation import Organisation

ORG_ID = uuid4()
USER_ID = uuid4()
ADMIN_PRINCIPAL = AuthenticatedPrincipal(
    username="admin@test",
    organisation_id=ORG_ID,
    account_id=USER_ID,
    org_role="admin",
)
VIEWER_PRINCIPAL = AuthenticatedPrincipal(
    username="viewer@test",
    organisation_id=ORG_ID,
    account_id=uuid4(),
    org_role="viewer",
)
SYSTEM_ADMIN_PRINCIPAL = AuthenticatedPrincipal(
    username="sysadmin@test",
    organisation_id=ORG_ID,
    account_id=uuid4(),
    org_role="admin",
    is_system_admin=True,
)


@pytest.fixture
def mock_session():
    """Create a mock DB session for testing."""
    from unittest.mock import AsyncMock, MagicMock

    session = AsyncMock()
    session.begin = MagicMock()
    session.begin.return_value.__aenter__.return_value = session
    session.begin.return_value.__aexit__.return_value = None
    session.in_transaction = MagicMock(return_value=True)
    session.execute.return_value = MagicMock()
    session.execute.return_value.scalar_one_or_none.return_value = None
    session.execute.return_value.scalar_one.return_value = 0
    session.execute.return_value.scalars.return_value.all.return_value = []
    session.add = MagicMock()
    session.flush.return_value = None
    return session


@pytest.fixture
def client_admin(mock_session):
    """Test client with admin auth + mock DB."""
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    app.dependency_overrides[get_db_session] = lambda: mock_session
    app.dependency_overrides[get_current_user] = lambda: ADMIN_PRINCIPAL
    transport = ASGITransport(app=app)
    client = AsyncClient(transport=transport, base_url="http://test")
    yield client
    app.dependency_overrides.clear()


@pytest.fixture
def client_viewer(mock_session):
    """Test client with viewer auth."""
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    app.dependency_overrides[get_db_session] = lambda: mock_session
    app.dependency_overrides[get_current_user] = lambda: VIEWER_PRINCIPAL
    transport = ASGITransport(app=app)
    client = AsyncClient(transport=transport, base_url="http://test")
    yield client
    app.dependency_overrides.clear()


@pytest.fixture
def client_system_admin(mock_session):
    """Test client with system admin auth."""
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    app.dependency_overrides[get_db_session] = lambda: mock_session
    app.dependency_overrides[get_current_user] = lambda: SYSTEM_ADMIN_PRINCIPAL
    transport = ASGITransport(app=app)
    client = AsyncClient(transport=transport, base_url="http://test")
    yield client
    app.dependency_overrides.clear()


@pytest.fixture
def client_tenant_member(mock_session):
    """Test client authenticated as an org member (not necessarily admin).

    Overrides ``get_current_tenant_user`` (the org-scoped dependency used by
    the org read surface) so any org member can exercise the kill-switch read.
    """
    tenant = TenantPrincipal(
        username="member@test",
        organisation_id=ORG_ID,
        account_id=uuid4(),
        org_role="viewer",
    )
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    app.dependency_overrides[get_db_session] = lambda: mock_session
    app.dependency_overrides[get_current_tenant_user] = lambda: tenant
    transport = ASGITransport(app=app)
    client = AsyncClient(transport=transport, base_url="http://test")
    yield client
    app.dependency_overrides.clear()


# ── POST /api/v1/admin/orgs ──────────────────────────────────────────────


@pytest.mark.anyio
async def test_create_org_success(client_system_admin, mock_session):
    """System admin can create an org with valid name and slug."""
    import modulo.api.routes.admin_orgs as admin_orgs

    original_get_slug = admin_orgs.get_organisation_by_slug
    admin_orgs.get_organisation_by_slug = AsyncMock(return_value=None)

    original_create = admin_orgs.create_organisation

    async def mock_create_org(session, *, name, slug, created_by, plan_id=None):
        return Organisation(
            id=uuid4(),
            name=name,
            slug=slug,
            status="active",
            created_at=datetime.now(UTC),
        )

    admin_orgs.create_organisation = mock_create_org

    try:
        resp = await client_system_admin.post(
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
    """Viewers get 403 when creating orgs."""
    resp = await client_viewer.post(
        "/api/v1/admin/orgs",
        json={"name": "Test Org", "slug": "test-org"},
    )
    assert resp.status_code == 403


@pytest.mark.anyio
async def test_create_org_admin_forbidden(client_admin):
    """Regular org admin (not system admin) gets 403 when creating orgs."""
    resp = await client_admin.post(
        "/api/v1/admin/orgs",
        json={"name": "Test Org", "slug": "test-org"},
    )
    assert resp.status_code == 403


@pytest.mark.anyio
async def test_create_org_invalid_slug(client_system_admin):
    """Invalid slug format returns 422."""
    resp = await client_system_admin.post(
        "/api/v1/admin/orgs",
        json={"name": "Test Org", "slug": "UPPERCASE-SLUG"},
    )
    assert resp.status_code == 422


@pytest.mark.anyio
async def test_create_org_slug_collision(client_system_admin):
    """Duplicate slug returns 409."""
    import modulo.api.routes.admin_orgs as admin_orgs

    existing = Organisation(
        id=uuid4(),
        name="Existing",
        slug="taken",
        status="active",
        created_at=datetime.now(UTC),
    )
    original = admin_orgs.get_organisation_by_slug
    admin_orgs.get_organisation_by_slug = AsyncMock(return_value=existing)

    try:
        resp = await client_system_admin.post(
            "/api/v1/admin/orgs",
            json={"name": "Test", "slug": "taken"},
        )
        assert resp.status_code == 409
    finally:
        admin_orgs.get_organisation_by_slug = original


@pytest.mark.anyio
async def test_create_org_duplicate_slug_orig(client_system_admin):
    """Duplicate slug returns 409 (alternate path)."""
    import modulo.api.routes.admin_orgs as admin_orgs

    existing_org = Organisation(
        id=uuid4(),
        name="Existing",
        slug="dup-slug",
        status="active",
        created_at=datetime.now(UTC),
    )
    original = admin_orgs.get_organisation_by_slug
    admin_orgs.get_organisation_by_slug = AsyncMock(return_value=existing_org)

    try:
        resp = await client_system_admin.post(
            "/api/v1/admin/orgs",
            json={"name": "Test Org", "slug": "dup-slug"},
        )
        assert resp.status_code == 409
    finally:
        admin_orgs.get_organisation_by_slug = original


# ── POST /api/v1/admin/orgs/{org_id}/users ───────────────────────────────


@pytest.mark.anyio
async def test_create_org_user_success(client_system_admin):
    """System admin can create a user in a specified org."""
    import modulo.api.routes.admin_orgs as admin_orgs
    from modulo.db.models.account import Account

    target_org_id = uuid4()

    target_org = Organisation(
        id=target_org_id,
        name="Target Org",
        slug="target",
        status="active",
        created_at=datetime.now(UTC),
    )

    original_get_org = admin_orgs.get_organisation
    admin_orgs.get_organisation = AsyncMock(return_value=target_org)

    original_get_account = admin_orgs.get_account_by_email
    admin_orgs.get_account_by_email = AsyncMock(return_value=None)

    original_create_account = admin_orgs.create_account

    async def mock_create_account(session, *, email, display_name, password_hash, auth_provider="local"):
        return Account(
            id=uuid4(),
            email=email,
            display_name=display_name,
            password_hash=password_hash,
            auth_provider=auth_provider,
            created_at=datetime.now(UTC),
        )

    admin_orgs.create_account = mock_create_account

    original_create_membership = admin_orgs.create_membership

    async def mock_create_membership(session, *, account_id, org_id, role="runner"):
        from modulo.db.models.org_membership import OrgMembership

        return OrgMembership(
            id=uuid4(),
            account_id=account_id,
            organisation_id=org_id,
            role=role,
        )

    admin_orgs.create_membership = mock_create_membership

    try:
        resp = await client_system_admin.post(
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
        admin_orgs.get_account_by_email = original_get_account
        admin_orgs.create_account = original_create_account
        admin_orgs.create_membership = original_create_membership


@pytest.mark.anyio
async def test_create_org_user_admin_forbidden(client_admin):
    """Regular org admin (not system admin) gets 403 when creating org users."""
    resp = await client_admin.post(
        f"/api/v1/admin/orgs/{uuid4()}/users",
        json={
            "email": "user@example.com",
            "display_name": "User",
            "password": "securepassword123",
            "org_role": "runner",
        },
    )
    assert resp.status_code == 403


@pytest.mark.anyio
async def test_create_org_user_org_not_found(client_system_admin):
    """Non-existent org returns 404."""
    import modulo.api.routes.admin_orgs as admin_orgs

    original = admin_orgs.get_organisation
    admin_orgs.get_organisation = AsyncMock(return_value=None)

    try:
        resp = await client_system_admin.post(
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
async def test_create_org_user_weak_password(client_system_admin):
    """Weak password returns 422."""
    resp = await client_system_admin.post(
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
async def test_create_org_user_invalid_role(client_system_admin):
    """Invalid role returns 422."""
    resp = await client_system_admin.post(
        f"/api/v1/admin/orgs/{uuid4()}/users",
        json={
            "email": "user@example.com",
            "display_name": "User",
            "password": "securepassword123",
            "org_role": "superadmin",
        },
    )
    assert resp.status_code == 422


# -- Org-scoped guardrails kill-switch read (non-admins) -------------------


@pytest.mark.anyio
async def test_org_member_reads_guardrails_kill_switch(client_tenant_member, mock_session):
    """Any org member (non-admin) can read the org's kill-switch state."""
    import modulo.api.routes.org_settings as org_settings

    original_get_org = org_settings.get_organisation

    async def fake_get_org(session, org_id):
        return Organisation(
            id=org_id,
            name="Test Org",
            slug="test-org",
            status="active",
            guardrails_kill_switch=True,
            guardrails_kill_switch_at=datetime.now(UTC),
        )

    org_settings.get_organisation = fake_get_org
    try:
        resp = await client_tenant_member.get("/api/v1/org/settings/guardrails/kill-switch")
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        data = resp.json()
        assert data["enabled"] is True
        assert data["enabled_at"] is not None
    finally:
        org_settings.get_organisation = original_get_org


@pytest.mark.anyio
async def test_org_member_reads_kill_switch_off(client_tenant_member, mock_session):
    """A non-admin org member sees the kill-switch OFF state too."""
    import modulo.api.routes.org_settings as org_settings

    original_get_org = org_settings.get_organisation

    async def fake_get_org(session, org_id):
        return Organisation(
            id=org_id,
            name="Test Org",
            slug="test-org",
            status="active",
            guardrails_kill_switch=False,
            guardrails_kill_switch_at=None,
        )

    org_settings.get_organisation = fake_get_org
    try:
        resp = await client_tenant_member.get("/api/v1/org/settings/guardrails/kill-switch")
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        data = resp.json()
        assert data["enabled"] is False
        assert data["enabled_at"] is None
    finally:
        org_settings.get_organisation = original_get_org


@pytest.mark.anyio
async def test_org_member_read_is_org_scoped(client_tenant_member, mock_session):
    """The read uses the caller's OWN organisation id, never an arbitrary one.

    The org-scoped route reads via the authenticated tenant's organisation_id
    (mirroring the org_settings precedent), so a caller cannot target another
    org's kill-switch state.
    """
    import modulo.api.routes.org_settings as org_settings

    captured: list = []
    original_set_rls = org_settings.set_rls_org

    async def fake_set_rls(session, org_id):
        captured.append(org_id)
        await original_set_rls(session, org_id)

    original_get_org = org_settings.get_organisation

    async def fake_get_org(session, org_id):
        captured.append(org_id)
        return Organisation(
            id=org_id,
            name="Test Org",
            slug="test-org",
            status="active",
            guardrails_kill_switch=True,
            guardrails_kill_switch_at=datetime.now(UTC),
        )

    org_settings.set_rls_org = fake_set_rls
    org_settings.get_organisation = fake_get_org
    try:
        resp = await client_tenant_member.get("/api/v1/org/settings/guardrails/kill-switch")
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        # Every DB read is scoped to the authenticated caller's organisation.
        assert captured, f"Not org-scoped: {captured}"
        assert all(oid == ORG_ID for oid in captured), f"Not org-scoped: {captured}"
    finally:
        org_settings.set_rls_org = original_set_rls
        org_settings.get_organisation = original_get_org
