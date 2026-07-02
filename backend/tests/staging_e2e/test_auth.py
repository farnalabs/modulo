"""Authentication tests across all tenant contexts."""
from __future__ import annotations

import httpx
import pytest

from .tenant_setup import TenantContext


@pytest.mark.asyncio
async def test_login_succeeds(tenant: TenantContext) -> None:
    async with httpx.AsyncClient(base_url=tenant.base_url, verify=False) as client:
        resp = await client.post(
            "/api/v1/auth/login",
            json={"email": tenant.user_email, "password": tenant.user_password},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert "access_token" in data or "token" in data


@pytest.mark.asyncio
async def test_login_wrong_password_fails(tenant: TenantContext) -> None:
    async with httpx.AsyncClient(base_url=tenant.base_url, verify=False) as client:
        resp = await client.post(
            "/api/v1/auth/login",
            json={"email": tenant.user_email, "password": "wrong-password-123"},
        )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_login_wrong_email_fails(tenant: TenantContext) -> None:
    async with httpx.AsyncClient(base_url=tenant.base_url, verify=False) as client:
        resp = await client.post(
            "/api/v1/auth/login",
            json={"email": "nonexistent@e2e.modulo", "password": "some-password"},
        )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_authenticated_endpoint_with_valid_token(tenant_client: httpx.AsyncClient) -> None:
    """Using the tenant_client fixture (already authenticated), call an endpoint."""
    resp = await tenant_client.get("/api/v1/me")
    assert resp.status_code == 200
    me = resp.json()
    assert "user" in me, f"/api/v1/me missing 'user' key: {list(me.keys())}"


@pytest.mark.asyncio
async def test_authenticated_endpoint_without_token(tenant: TenantContext) -> None:
    async with httpx.AsyncClient(base_url=tenant.base_url, verify=False) as client:
        resp = await client.get("/api/v1/me")
    assert resp.status_code == 401
