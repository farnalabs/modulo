"""Cross-tenant data isolation integration tests.

Tests RLS enforcement and system admin cross-tenant operations
through the HTTP API layer using a real Postgres via Testcontainers.
"""

import os
import uuid
from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from modulo.auth.jwt import create_access_token

os.environ.setdefault("MODULO_AUTH_RATE_LIMIT_ENABLED", "false")
os.environ.setdefault("REDIS_URL", "")

pytestmark = pytest.mark.integration

_VALID_32 = "a" * 32


# ---------------------------------------------------------------------------
# DB seed helpers
# ---------------------------------------------------------------------------


async def _seed_org(db_engine: AsyncEngine, name: str) -> uuid.UUID:
    org_id = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO organisations (id, name, slug, settings_json) VALUES (:id, :name, :slug, '{}'::json)",
            ),
            {
                "id": str(org_id),
                "name": name,
                "slug": f"{name}-{org_id.hex[:8]}",
            },
        )
    return org_id


async def _seed_user(db_engine: AsyncEngine, org_id: uuid.UUID, email: str) -> uuid.UUID:
    account_id = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO accounts (id, email, display_name, "
                "auth_provider, active, password_hash) "
                "VALUES (:id, :email, :name, 'local', true, 'hash')",
            ),
            {
                "id": str(account_id),
                "email": email,
                "name": f"Admin {email}",
            },
        )
        await conn.execute(
            text(
                "INSERT INTO org_memberships (id, account_id, organisation_id, role) "
                "VALUES (:mid, :aid, :oid, 'admin')",
            ),
            {
                "mid": str(uuid.uuid4()),
                "aid": str(account_id),
                "oid": str(org_id),
            },
        )
    return account_id


async def _seed_pipeline(
    db_engine: AsyncEngine,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    name: str,
) -> uuid.UUID:
    pipeline_id = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO pipelines (id, organisation_id, name, description, account_id, "
                "max_concurrent_runs, lock_wait_timeout_seconds, node_timeout_seconds, "
                "run_context_defaults, graph_nodes_json, default_autonomy_level, visibility) "
                "VALUES (:id, :oid, :name, :desc, :uid, 5, 30, 300, "
                "'{}'::json, '[]'::json, 'manual_approval', 'org')",
            ),
            {
                "id": str(pipeline_id),
                "oid": str(org_id),
                "name": name,
                "desc": f"Pipeline for {name}",
                "uid": str(user_id),
            },
        )
    return pipeline_id


# ---------------------------------------------------------------------------
# HTTP client fixture — FastAPI app wired to the testcontainer database
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def integration_client(
    db_url: str,
    db_engine: AsyncEngine,
) -> AsyncClient:
    from modulo.api.dependencies import _get_engine, get_db_session
    from modulo.api.main import app
    from modulo.settings import Settings, get_settings

    settings = Settings(
        database_url=db_url,
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_csrf_enabled=False,
        modulo_auth_rate_limit_enabled=False,
        redis_url="",
        modulo_admin_password="",
    )

    async def override_session() -> AsyncGenerator[AsyncSession, None]:
        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with factory() as session:
            yield session

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[_get_engine] = lambda: db_engine
    app.dependency_overrides[get_db_session] = override_session

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", timeout=30.0) as client:
        yield client

    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# JWT token factory
# ---------------------------------------------------------------------------


def _token(
    org_id: uuid.UUID | None,
    user_id: uuid.UUID,
    role: str,
    is_system_admin: bool = False,
) -> str:
    return create_access_token(
        subject=f"user-{user_id.hex[:8]}",
        secret_key=_VALID_32,
        organisation_id=str(org_id) if org_id else "",
        account_id=str(user_id),
        org_role=role,
        is_system_admin=is_system_admin,
    )


# ---------------------------------------------------------------------------
# Fixtures: orgs, users, pipelines (module-scoped to reuse across tests)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(scope="module")
async def org_a(db_engine: AsyncEngine) -> uuid.UUID:
    return await _seed_org(db_engine, "CrossTenant-OrgA")


@pytest_asyncio.fixture(scope="module")
async def org_b(db_engine: AsyncEngine) -> uuid.UUID:
    return await _seed_org(db_engine, "CrossTenant-OrgB")


@pytest_asyncio.fixture(scope="module")
async def user_a(db_engine: AsyncEngine, org_a: uuid.UUID) -> uuid.UUID:
    return await _seed_user(db_engine, org_a, "admin-a@test.local")


@pytest_asyncio.fixture(scope="module")
async def user_b(db_engine: AsyncEngine, org_b: uuid.UUID) -> uuid.UUID:
    return await _seed_user(db_engine, org_b, "admin-b@test.local")


@pytest_asyncio.fixture(scope="module")
async def pipeline_a(
    db_engine: AsyncEngine,
    org_a: uuid.UUID,
    user_a: uuid.UUID,
) -> uuid.UUID:
    return await _seed_pipeline(db_engine, org_a, user_a, "CrossTenant-PipelineA")


@pytest_asyncio.fixture(scope="module")
async def pipeline_b(
    db_engine: AsyncEngine,
    org_b: uuid.UUID,
    user_b: uuid.UUID,
) -> uuid.UUID:
    return await _seed_pipeline(db_engine, org_b, user_b, "CrossTenant-PipelineB")


# ===================================================================
# Test 1: Org data isolation via RLS
# ===================================================================


class TestOrgDataIsolation:
    """Org A must not see Org B's data, and vice versa."""

    async def test_org_a_cannot_see_org_b_pipelines(
        self,
        integration_client: AsyncClient,
        org_a: uuid.UUID,
        user_a: uuid.UUID,
        pipeline_b: uuid.UUID,
    ) -> None:
        token = _token(org_a, user_a, "admin")
        resp = await integration_client.get(
            "/api/v1/pipelines",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        ids = {p["id"] for p in resp.json()["items"]}
        assert str(pipeline_b) not in ids, "OrgA should not see OrgB's pipeline"

    async def test_org_b_cannot_see_org_a_pipelines(
        self,
        integration_client: AsyncClient,
        org_b: uuid.UUID,
        user_b: uuid.UUID,
        pipeline_a: uuid.UUID,
    ) -> None:
        token = _token(org_b, user_b, "admin")
        resp = await integration_client.get(
            "/api/v1/pipelines",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        ids = {p["id"] for p in resp.json()["items"]}
        assert str(pipeline_a) not in ids, "OrgB should not see OrgA's pipeline"

    async def test_org_a_sees_own_pipeline(
        self,
        integration_client: AsyncClient,
        org_a: uuid.UUID,
        user_a: uuid.UUID,
        pipeline_a: uuid.UUID,
    ) -> None:
        token = _token(org_a, user_a, "admin")
        resp = await integration_client.get(
            "/api/v1/pipelines",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        ids = {p["id"] for p in resp.json()["items"]}
        assert str(pipeline_a) in ids, "OrgA should see its own pipeline"


# ===================================================================
# Test 2: System admin can access any org's data
# ===================================================================


class TestSystemAdminAccess:
    """System admin (no org_id claim) bypasses RLS and sees all orgs."""

    async def test_system_admin_sees_all_pipelines(
        self,
        integration_client: AsyncClient,
        pipeline_a: uuid.UUID,
        pipeline_b: uuid.UUID,
    ) -> None:
        sys_admin_id = uuid.uuid4()
        token = _token(
            org_id=None,
            user_id=sys_admin_id,
            role="system_admin",
            is_system_admin=True,
        )
        resp = await integration_client.get(
            "/api/v1/pipelines",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        ids = {p["id"] for p in resp.json()["items"]}
        assert str(pipeline_a) in ids, "System admin should see pipeline A"
        assert str(pipeline_b) in ids, "System admin should see pipeline B"


# ===================================================================
# Test 3: Org admin cannot access other org's admin endpoints
# ===================================================================


class TestOrgAdminCrossOrgForbidden:
    """Org-scoped user gets 403 on admin routes for a different org."""

    async def test_org_admin_gets_403_on_admin_create_user_in_other_org(
        self,
        integration_client: AsyncClient,
        org_a: uuid.UUID,
        org_b: uuid.UUID,
        user_a: uuid.UUID,
    ) -> None:
        token = _token(org_a, user_a, "admin")
        resp = await integration_client.post(
            f"/api/v1/admin/orgs/{org_b}/users",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "email": "cross-org@test.local",
                "display_name": "Cross Org",
                "password": "testpassword123",
                "org_role": "runner",
            },
        )
        assert resp.status_code == 403, f"Expected 403, got {resp.status_code}: {resp.text}"


# ===================================================================
# Test 4: System admin uses explicit org_id parameter
# ===================================================================


# ===================================================================
# Test 5: Cross-org single-resource fetch returns 404
# ===================================================================


class TestCrossOrgSingleResourceFetch:
    """Getting a resource by ID from another org must return 404 (not 403)."""

    async def test_get_other_org_pipeline_by_id_returns_404(
        self,
        integration_client: AsyncClient,
        org_a: uuid.UUID,
        user_a: uuid.UUID,
        pipeline_b: uuid.UUID,
    ) -> None:
        token = _token(org_a, user_a, "admin")
        resp = await integration_client.get(
            f"/api/v1/pipelines/{pipeline_b}",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 404, (
            f"Expected 404 for cross-org pipeline fetch, got {resp.status_code}: {resp.text}"
        )

    async def test_get_own_org_pipeline_by_id_succeeds(
        self,
        integration_client: AsyncClient,
        org_a: uuid.UUID,
        user_a: uuid.UUID,
        pipeline_a: uuid.UUID,
    ) -> None:
        token = _token(org_a, user_a, "admin")
        resp = await integration_client.get(
            f"/api/v1/pipelines/{pipeline_a}",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, f"Expected 200 for own org pipeline fetch, got {resp.status_code}: {resp.text}"
        data = resp.json()
        assert data["id"] == str(pipeline_a)


# ===================================================================
# Test 6: System admin uses explicit org_id parameter
# ===================================================================


class TestSystemAdminExplicitOrgParam:
    """System admin's JWT org_id is ignored; the path org_id is used."""

    async def test_system_admin_can_create_user_in_any_org(
        self,
        integration_client: AsyncClient,
        org_a: uuid.UUID,
    ) -> None:
        sys_admin_id = uuid.uuid4()
        token = _token(
            org_id=None,
            user_id=sys_admin_id,
            role="system_admin",
            is_system_admin=True,
        )
        resp = await integration_client.post(
            f"/api/v1/admin/orgs/{org_a}/users",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "email": "sysadmin-created@test.local",
                "display_name": "Created By SysAdmin",
                "password": "securepassword123",
                "org_role": "operator",
            },
        )
        assert resp.status_code == 201, f"Expected 201, got {resp.status_code}: {resp.text}"
        data = resp.json()
        assert data["email"] == "sysadmin-created@test.local"
        assert data["org_role"] == "operator"
