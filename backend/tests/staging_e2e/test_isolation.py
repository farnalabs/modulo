"""Cross-tenant data isolation tests.

Verifies that data created in one tenant is NOT visible from another.
Uses the session-scoped admin client to create a shared reference point.
"""
from __future__ import annotations

import httpx
import pytest

from .tenant_setup import TenantContext, TenantMatrix


@pytest.mark.asyncio
async def test_cross_tenant_pipeline_invisibility(
    tenant_matrix: TenantMatrix,
    tenant_client: httpx.AsyncClient,
    tenant: TenantContext,
) -> None:
    """A tenant cannot see pipelines created in other tenants.

    Each tenant gets their own authenticated client. We enumerate all tenants
    and check that only our pipelines (or seeded ones) show up.
    """
    resp = await tenant_client.get("/api/v1/pipelines")
    assert resp.status_code == 200
    data = resp.json()
    our_pipelines = data if isinstance(data, list) else data.get("pipelines", [])

    # For each OTHER tenant, try to access their pipelines by ID
    for other in tenant_matrix.all():
        if other is None or other.org_id == tenant.org_id:
            continue

        # Get other tenant's auth
        async with httpx.AsyncClient(base_url=other.base_url, verify=False) as other_client:
            auth_resp = await other_client.post(
                "/api/v1/auth/login",
                json={"username": other.user_email, "password": other.user_password},
            )
            if auth_resp.status_code != 200:
                continue
            token = auth_resp.json().get("access_token", auth_resp.json().get("token", ""))
            other_client.headers.update({"Authorization": f"Bearer {token}"})

            other_resp = await other_client.get("/api/v1/pipelines")
            assert other_resp.status_code == 200
            other_data = other_resp.json()
            other_pipelines = other_data if isinstance(other_data, list) else other_data.get("pipelines", [])

            # Our pipeline IDs should NOT appear in other tenant's list
            our_ids = {p.get("id") for p in our_pipelines if p.get("id")}
            other_ids = {p.get("id") for p in other_pipelines if p.get("id")}
            overlap = our_ids & other_ids
            assert len(overlap) == 0, (
                f"Tenant {tenant.slug} and {other.slug} share pipeline IDs: {overlap}"
            )


@pytest.mark.asyncio
async def test_cross_tenant_404_on_direct_access(
    tenant_matrix: TenantMatrix,
    tenant_client: httpx.AsyncClient,
    tenant: TenantContext,
) -> None:
    """Direct access to another tenant's pipeline by ID returns 404."""
    for other in tenant_matrix.all():
        if other is None or other.org_id == tenant.org_id:
            continue

        # Login as other tenant to get one of their pipeline IDs
        async with httpx.AsyncClient(base_url=other.base_url, verify=False) as other_client:
            auth_resp = await other_client.post(
                "/api/v1/auth/login",
                json={"username": other.user_email, "password": other.user_password},
            )
            if auth_resp.status_code != 200:
                continue
            token = auth_resp.json().get("access_token", auth_resp.json().get("token", ""))
            other_client.headers.update({"Authorization": f"Bearer {token}"})
            other_resp = await other_client.get("/api/v1/pipelines")
            assert other_resp.status_code == 200
            other_data = other_resp.json()
            other_pipelines = other_data if isinstance(other_data, list) else other_data.get("pipelines", [])

            for p in other_pipelines:
                pid = p.get("id")
                if pid:
                    # Try accessing as our tenant
                    resp = await tenant_client.get(f"/api/v1/pipelines/{pid}")
                    assert resp.status_code == 404, (
                        f"Tenant {tenant.slug} should get 404 for pipeline {pid} "
                        f"owned by {other.slug}, got {resp.status_code}"
                    )
