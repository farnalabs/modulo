"""Pytest fixtures for staging E2E tests."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator

import httpx
import pytest
import pytest_asyncio

from .tenant_setup import STAGING_URL, TenantContext, TenantMatrix, destroy_tenants, setup_tenants

# Session-level lock for tenant setup/teardown
_tenant_lock = asyncio.Lock()


@pytest_asyncio.fixture(scope="session")
async def tenant_matrix() -> AsyncGenerator[TenantMatrix, None]:
    """Create all 4 tenants once per test session."""
    matrix = await setup_tenants(seed_fn=None)
    yield matrix
    await destroy_tenants(matrix)


@pytest.fixture(params=[
    pytest.param("community-new", id="community-new"),
    pytest.param("community-existing", id="community-existing"),
    pytest.param("team-new", id="team-new"),
    pytest.param("team-existing", id="team-existing"),
])
def tenant_key(request) -> str:
    return request.param


@pytest_asyncio.fixture
async def tenant(tenant_matrix: TenantMatrix, tenant_key: str) -> TenantContext:
    return tenant_matrix.by_key(tenant_key)


@pytest_asyncio.fixture(loop_scope="function")
async def tenant_client(tenant: TenantContext) -> AsyncGenerator[httpx.AsyncClient, None]:
    """Authenticated httpx client for a specific tenant."""
    async with httpx.AsyncClient(base_url=tenant.base_url, verify=False, timeout=60.0) as client:
        resp = await client.post(
            "/api/v1/auth/login",
            json={"email": tenant.user_email, "password": tenant.user_password},
        )
        assert resp.status_code == 200, f"Auth failed for {tenant.slug}: {resp.text}"
        token = resp.json().get("access_token", resp.json().get("token", ""))
        client.headers.update({"Authorization": f"Bearer {token}"})
        yield client


@pytest_asyncio.fixture(scope="session")
async def admin_client() -> AsyncGenerator[httpx.AsyncClient, None]:
    from .tenant_setup import get_admin_token
    token = await get_admin_token()
    async with httpx.AsyncClient(
        base_url=STAGING_URL, verify=False, timeout=60.0,
        headers={"Authorization": f"Bearer {token}"},
    ) as client:
        yield client
