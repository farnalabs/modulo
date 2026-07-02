"""Create and manage tenant orgs for the 2x2 staging E2E matrix.

Four tenant contexts:
  - community-new:   fresh org, community tier
  - community-existing: org with pre-seeded data, community tier
  - team-new:        fresh org, team tier
  - team-existing:   org with pre-seeded data, team tier
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass

import httpx

STAGING_URL = "https://staging.modulo.run"
ADMIN_EMAIL = "admin@staging.modulo"
ADMIN_PASSWORD = "admin123"
SLUG_PREFIX = "e2e-"  # used by cleanup to identify test orgs


@dataclass
class TenantContext:
    slug: str
    org_id: str
    user_email: str
    user_password: str
    plan_id: str
    is_existing: bool  # has pre-seeded data
    base_url: str = STAGING_URL


@dataclass
class TenantMatrix:
    """Holds all four tenant contexts."""
    community_new: TenantContext | None = None
    community_existing: TenantContext | None = None
    team_new: TenantContext | None = None
    team_existing: TenantContext | None = None

    def all(self) -> list[TenantContext]:
        return [
            t for t in
            [self.community_new, self.community_existing, self.team_new, self.team_existing]
            if t is not None
        ]

    def by_key(self, key: str) -> TenantContext:
        mapping = {
            "community-new": self.community_new,
            "community-existing": self.community_existing,
            "team-new": self.team_new,
            "team-existing": self.team_existing,
        }
        ctx = mapping[key]
        if ctx is None:
            raise KeyError(f"Tenant {key} not available")
        return ctx


def _random_slug() -> str:
    return f"{SLUG_PREFIX}{secrets.token_hex(6)}"


async def get_admin_token(
    base_url: str = STAGING_URL,
    email: str = ADMIN_EMAIL,
    password: str = ADMIN_PASSWORD,
) -> str:
    async with httpx.AsyncClient(base_url=base_url, verify=False) as client:
        resp = await client.post(
            "/api/v1/auth/login",
            json={"email": email, "password": password},
        )
        if resp.status_code != 200:
            raise RuntimeError(f"Admin auth failed ({resp.status_code}): {resp.text}")
        data = resp.json()
        return data.get("access_token", data.get("token", ""))


async def create_tenant_org(
    client: httpx.AsyncClient,
    plan_id: str,
) -> TenantContext:
    """Create a single tenant org with an admin user. Returns the context."""
    slug = _random_slug()
    resp = await client.post(
        "/api/v1/admin/orgs",
        json={"name": f"E2E {plan_id.title()} {slug}", "slug": slug, "plan_id": plan_id},
    )
    if resp.status_code != 201:
        raise RuntimeError(f"Org creation failed ({resp.status_code}): {resp.text}")
    org_id = resp.json()["id"]

    user_email = f"admin-{slug}@e2e.modulo"
    user_password = secrets.token_urlsafe(16)
    user_resp = await client.post(
        f"/api/v1/admin/orgs/{org_id}/users",
        json={
            "email": user_email,
            "display_name": f"Admin {plan_id.title()} {slug}",
            "password": user_password,
            "org_role": "admin",
        },
    )
    if user_resp.status_code != 201:
        raise RuntimeError(f"User creation failed ({user_resp.status_code}): {user_resp.text}")

    return TenantContext(
        slug=slug,
        org_id=org_id,
        user_email=user_email,
        user_password=user_password,
        plan_id=plan_id,
        is_existing=False,
        base_url=str(client.base_url).rstrip("/"),
    )


async def setup_tenants(
    base_url: str = STAGING_URL,
    admin_email: str = ADMIN_EMAIL,
    admin_password: str = ADMIN_PASSWORD,
    seed_fn=None,
) -> TenantMatrix:
    """Create all 4 tenant orgs and seed data where needed."""
    token = await get_admin_token(base_url, admin_email, admin_password)
    headers = {"Authorization": f"Bearer {token}"}

    matrix = TenantMatrix()

    async with httpx.AsyncClient(base_url=base_url, verify=False, headers=headers) as client:
        matrix.community_new = await create_tenant_org(client, "community")
        matrix.team_new = await create_tenant_org(client, "team")

        matrix.community_existing = await create_tenant_org(client, "community")
        matrix.team_existing = await create_tenant_org(client, "team")

        matrix.community_existing.is_existing = True
        matrix.team_existing.is_existing = True

    # Seed existing orgs using their own auth tokens
    if seed_fn is not None:
        for ctx in [matrix.community_existing, matrix.team_existing]:
            if ctx:
                await seed_fn(
                    base_url=base_url,
                    user_email=ctx.user_email,
                    user_password=ctx.user_password,
                    org_id=ctx.org_id,
                )

    return matrix


async def destroy_tenants(
    matrix: TenantMatrix,
    base_url: str = STAGING_URL,
    admin_email: str = ADMIN_EMAIL,
    admin_password: str = ADMIN_PASSWORD,
) -> None:
    """Best-effort teardown: delete all tenant orgs."""
    token = await get_admin_token(base_url, admin_email, admin_password)
    headers = {"Authorization": f"Bearer {token}"}

    async with httpx.AsyncClient(base_url=base_url, verify=False, headers=headers):
        for ctx in matrix.all():
            if not ctx:
                continue
            try:
                # Login as the tenant admin to own the deletion request
                tenant_token = await get_admin_token(base_url, ctx.user_email, ctx.user_password)
                async with httpx.AsyncClient(
                    base_url=base_url, verify=False,
                    headers={"Authorization": f"Bearer {tenant_token}"},
                ) as tenant_client:
                    await tenant_client.post("/api/v1/admin/org/deletion-request")
            except Exception:
                pass  # best effort
